"""Google Safe Browsing v5 client.

v5 ``urls:search`` speaks protobuf and only protobuf -- adding ``alt=json``
returns ``400 Unsupported Output Format`` regardless of the Accept header. The
response message is three fields deep, so it is decoded here by hand rather than
adding the ``protobuf`` package, which would cost boot time on every Wispbyte
restart for a forty-line job.

The query parameter is ``urls``, and it repeats. ``url`` and ``uri`` are both
rejected with ``Field 'uri' could not be found in request message``, which is
worth stating plainly because it is the shape most examples online use.

v4 is deliberately not used: it is deprecated and shuts down on 31 March 2027.

Three tiers, in order, so the API only ever sees domains nothing else could
answer for:

1. ``data/trusted_domains.txt``, shipped with the bot.
2. Per-guild additions from ``?scanlinks trust``.
3. The API, for whatever is left. Verdicts are cached for the ``cacheDuration``
   the API returns.

Without a key, tiers 1 and 2 still work and unknown domains fall back to the
bundled phishing blocklist.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import aiohttp

log = logging.getLogger(__name__)

_URLS_ENDPOINT = "https://safebrowsing.googleapis.com/v5/urls:search"

#: ThreatType enum values from the v5 protobuf definition.
THREAT_TYPES: dict[int, str] = {
    1: "malware",
    2: "social_engineering",
    3: "unwanted_software",
    4: "potentially_harmful_application",
}

#: Used when the API answers but names no threat.
CLEAN = "clean"

#: Fallback cache lifetime when the response omits one.
DEFAULT_CACHE_SECONDS = 300

#: Give up on a lookup after this long. A slow API must not hold up a message.
REQUEST_TIMEOUT = 5.0

#: URLs per request. ``urls`` is a repeated parameter, so one message's links go
#: in one round trip. 50 is the documented ceiling.
BATCH_SIZE = 50


@dataclass(slots=True)
class Verdict:
    """The outcome of scanning one URL."""

    url: str
    domain: str
    verdict: str
    source: str  # bundled | guild | cache | api | blocklist | unchecked
    cache_seconds: int = DEFAULT_CACHE_SECONDS

    @property
    def is_threat(self) -> bool:
        return self.verdict != CLEAN

    @property
    def label(self) -> str:
        return self.verdict.replace("_", " ").title()


# --- Protobuf decoding -----------------------------------------------------
# Only what the SearchUrisResponse actually contains:
#   field 1: repeated FullHash/Threat (length-delimited submessages)
#   field 2: Duration cacheDuration   (length-delimited submessage)
# Inside a threat submessage:
#   field 1: string expression / uri
#   field 2: repeated ThreatType (varint, possibly packed)


def _read_varint(data: bytes, index: int) -> tuple[int, int]:
    """Read one base-128 varint. Returns ``(value, next_index)``."""
    result = 0
    shift = 0
    while index < len(data):
        byte = data[index]
        index += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, index
        shift += 7
        if shift > 63:
            raise ValueError("varint is too long to be valid")
    raise ValueError("truncated varint")


def _walk(data: bytes):
    """Yield ``(field_number, wire_type, payload)`` for one protobuf message.

    ``payload`` is an int for varints and bytes for length-delimited fields.
    Fixed-width fields are skipped: this message contains none.
    """
    index = 0
    while index < len(data):
        key, index = _read_varint(data, index)
        field_number, wire_type = key >> 3, key & 0x07

        if wire_type == 0:
            value, index = _read_varint(data, index)
            yield field_number, wire_type, value
        elif wire_type == 2:
            length, index = _read_varint(data, index)
            yield field_number, wire_type, data[index : index + length]
            index += length
        elif wire_type == 5:
            index += 4
        elif wire_type == 1:
            index += 8
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")


def parse_response(payload: bytes) -> tuple[dict[str, list[str]], int]:
    """Decode a v5 ``SearchUrisResponse``.

    Returns:
        ``(threats, cache_seconds)`` where ``threats`` maps each flagged
        expression to the threat type names attached to it. An empty mapping
        means every URL in the request was clean.
    """
    threats: dict[str, list[str]] = {}
    cache_seconds = DEFAULT_CACHE_SECONDS

    for field_number, wire_type, value in _walk(payload):
        if field_number == 1 and wire_type == 2:
            expression = ""
            types: list[str] = []
            for sub_field, sub_wire, sub_value in _walk(value):
                if sub_field == 1 and sub_wire == 2:
                    expression = sub_value.decode("utf-8", errors="replace")
                elif sub_field == 2:
                    if sub_wire == 0:
                        types.append(THREAT_TYPES.get(sub_value, f"type_{sub_value}"))
                    elif sub_wire == 2:
                        # Packed repeated enum: a run of varints in one field.
                        offset = 0
                        while offset < len(sub_value):
                            enum_value, offset = _read_varint(sub_value, offset)
                            types.append(
                                THREAT_TYPES.get(enum_value, f"type_{enum_value}")
                            )
            if expression:
                threats[expression] = types

        elif field_number == 2 and wire_type == 2:
            # google.protobuf.Duration: field 1 is seconds.
            for sub_field, sub_wire, sub_value in _walk(value):
                if sub_field == 1 and sub_wire == 0:
                    cache_seconds = max(int(sub_value), 60)

    return threats, cache_seconds


# --- Domain lists ----------------------------------------------------------


def load_domain_file(path: Path) -> frozenset[str]:
    """Read a newline-separated domain list, ignoring blanks and ``#`` comments."""
    if not path.is_file():
        log.warning("Domain list %s is missing; treating it as empty", path)
        return frozenset()

    domains: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.split("#", 1)[0].strip().lower()
        if line:
            domains.add(line.removeprefix("www."))
    return frozenset(domains)


class SafeBrowsing:
    """Three-tier link scanner with an on-disk verdict cache.

    A single instance is shared by the whole bot. It owns one ``aiohttp``
    session, opened lazily so a bot that never scans a link never pays for one.
    """

    __slots__ = ("_key", "_db", "_trusted", "_phishing", "_session", "_lock", "_disabled")

    def __init__(self, api_key: str | None, db: object, data_dir: Path) -> None:
        self._key = api_key
        self._db = db
        self._trusted = load_domain_file(data_dir / "trusted_domains.txt")
        self._phishing = load_domain_file(data_dir / "phishing_blocklist.txt")
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        self._disabled = False

        log.info(
            "Link scanner: %d bundled trusted domain(s), %d blocklisted, API %s",
            len(self._trusted),
            len(self._phishing),
            "enabled" if api_key else "disabled (no key)",
        )

    @property
    def has_api(self) -> bool:
        """Whether tier 3 is available."""
        return bool(self._key) and not self._disabled

    @property
    def bundled_count(self) -> int:
        return len(self._trusted)

    async def close(self) -> None:
        """Close the HTTP session, if one was ever opened."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
                )
            return self._session

    # --- Tiers -------------------------------------------------------------

    def is_bundled_trusted(self, domain: str) -> bool:
        """Tier 1: on the shipped trusted list."""
        from .filters import domain_matches

        return domain_matches(domain, self._trusted) is not None

    def is_blocklisted(self, domain: str) -> bool:
        """Offline fallback: on the bundled phishing blocklist."""
        from .filters import domain_matches

        return domain_matches(domain, self._phishing) is not None

    async def is_guild_trusted(self, guild_id: int, domain: str) -> bool:
        """Tier 2: added by this guild with ``?scanlinks trust``."""
        from .filters import domain_matches

        rows = await self._db.fetchall(
            "SELECT domain FROM trusted_domains WHERE guild_id = ?", (guild_id,)
        )
        return domain_matches(domain, [row["domain"] for row in rows]) is not None

    async def _cached(self, url: str) -> str | None:
        """Look up a cached verdict, ignoring expired rows."""
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        row = await self._db.fetchone(
            "SELECT verdict FROM scan_cache WHERE url_hash = ? AND expires_at > ?",
            (digest, int(time.time())),
        )
        return row["verdict"] if row else None

    async def _cache(self, url: str, verdict: str, seconds: int) -> None:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        now = int(time.time())
        await self._db.execute(
            "INSERT INTO scan_cache (url_hash, verdict, expires_at, created_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(url_hash) DO UPDATE SET "
            "verdict = excluded.verdict, expires_at = excluded.expires_at",
            (digest, verdict, now + seconds, now),
        )

    async def prune_cache(self) -> int:
        """Delete expired verdicts. Called by the scheduler."""
        cursor = await self._db.execute(
            "DELETE FROM scan_cache WHERE expires_at < ?", (int(time.time()),)
        )
        return cursor.rowcount

    # --- Scanning ----------------------------------------------------------

    async def scan(self, guild_id: int, urls: list[str]) -> list[Verdict]:
        """Scan URLs, consulting each tier in turn.

        Args:
            guild_id: Whose trusted list applies.
            urls: Raw URLs pulled out of a message.

        Returns:
            One verdict per URL that was not trusted. Trusted URLs are omitted
            entirely -- the caller only cares about what is left.
        """
        from .filters import extract_domain

        unknown: list[tuple[str, str]] = []
        results: list[Verdict] = []

        for url in urls:
            domain = extract_domain(url)
            if not domain or "." not in domain:
                continue
            if self.is_bundled_trusted(domain):
                continue
            if await self.is_guild_trusted(guild_id, domain):
                continue

            cached = await self._cached(url)
            if cached is not None:
                if cached != CLEAN:
                    results.append(Verdict(url, domain, cached, "cache"))
                continue

            if self.is_blocklisted(domain):
                results.append(Verdict(url, domain, "social_engineering", "blocklist"))
                await self._cache(url, "social_engineering", DEFAULT_CACHE_SECONDS)
                continue

            unknown.append((url, domain))

        if unknown and self.has_api:
            results.extend(await self._query_api(unknown))

        return results

    @staticmethod
    def _strip_scheme(url: str) -> str:
        """Reduce a URL to the form Safe Browsing echoes back in a threat."""
        lowered = url.strip().lower()
        for prefix in ("https://", "http://"):
            if lowered.startswith(prefix):
                lowered = lowered[len(prefix) :]
                break
        return lowered.removeprefix("www.")

    async def _query_api(self, unknown: list[tuple[str, str]]) -> list[Verdict]:
        """Ask Safe Browsing about the URLs nothing else could answer for.

        ``urls`` is a repeated query parameter, so a whole message goes in one
        request rather than one round trip per link. Batched at 50, which is the
        documented ceiling.

        Failures are non-fatal and produce no verdicts. A scanner outage must not
        turn into every link being deleted, and equally must not be reported as
        every link being clean -- the offline blocklist has already run, and
        silence is the honest outcome for the rest.
        """
        results: list[Verdict] = []
        session = await self._get_session()

        for start in range(0, len(unknown), BATCH_SIZE):
            batch = unknown[start : start + BATCH_SIZE]
            query = f"?key={quote(self._key)}" + "".join(
                f"&urls={quote(url, safe='')}" for url, _ in batch
            )

            try:
                async with session.get(_URLS_ENDPOINT + query) as response:
                    if response.status in (401, 403):
                        # A rejected key fails identically on every future call.
                        # Stop trying until the process restarts.
                        log.error(
                            "Safe Browsing rejected the API key (%d); "
                            "tier 3 scanning is now disabled",
                            response.status,
                        )
                        self._disabled = True
                        return results
                    if response.status == 429:
                        log.warning("Safe Browsing quota exceeded; skipping lookup")
                        return results
                    if response.status != 200:
                        body = (await response.read())[:200]
                        log.warning(
                            "Safe Browsing returned %d: %r", response.status, body
                        )
                        continue
                    payload = await response.read()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.warning("Safe Browsing request failed: %s", exc)
                continue

            try:
                threats, cache_seconds = parse_response(payload)
            except ValueError as exc:
                log.warning("Could not decode Safe Browsing response: %s", exc)
                continue

            # Threats come back as canonicalised expressions with the scheme
            # stripped, and may be broader than what was submitted -- a whole
            # domain rather than the exact path. Match both ways.
            for url, domain in batch:
                stripped = self._strip_scheme(url)
                verdict = CLEAN
                for expression, types in threats.items():
                    if stripped.startswith(expression) or expression.startswith(stripped):
                        verdict = types[0] if types else "social_engineering"
                        break

                await self._cache(url, verdict, cache_seconds)
                if verdict != CLEAN:
                    results.append(Verdict(url, domain, verdict, "api", cache_seconds))

        return results
