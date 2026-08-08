"""Automod matching.

No Discord imports. Everything here takes plain strings and numbers, which is
what makes the tricky parts -- evasion-resistant word matching, zalgo detection,
sliding-window rate limits -- testable without a gateway connection.

The evasion handling is the point of this module. ``b a d w o r d``,
``b4dw0rd``, ``b​adword`` and ``ｂａｄｗｏｒｄ`` all have to reduce to the
same thing before matching, or the filter is decorative.
"""

from __future__ import annotations

import re
import time
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Iterable

# --- Normalisation ---------------------------------------------------------

#: Invisible characters used to break up a word without changing how it looks.
_INVISIBLE = re.compile(
    "["
    "​-‏"  # zero-width space through right-to-left mark
    "‪-‮"  # directional overrides
    "⁠-⁤"  # word joiner and invisible operators
    "﻿"  # byte-order mark
    "­"  # soft hyphen
    "]"
)

#: Digit substitutions. Safe to apply everywhere: a digit is not a word
#: boundary, so replacing it cannot change where one word ends and another
#: begins.
_LEET_DIGITS = str.maketrans(
    {"4": "a", "8": "b", "3": "e", "6": "g", "9": "g", "1": "i", "0": "o",
     "5": "s", "7": "t", "2": "z"}
)

#: Punctuation substitutions. Applied only in the squashed pass. Folding "!"
#: into "i" everywhere would turn "badword!" into "badwordi" and destroy the
#: word boundary that whole-word matching depends on.
_LEET_PUNCT = str.maketrans(
    {"@": "a", "^": "a", "(": "c", "<": "c", "{": "c", "€": "e",
     "!": "i", "|": "i", "¡": "i", "ø": "o", "$": "s", "+": "t"}
)

#: Everything that is not a letter or a digit, for the "squashed" comparison.
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

#: Three or more of the same character in a row.
_REPEATS = re.compile(r"(.)\1{2,}")

#: Any run of the same character, for the squashed comparison.
_ALL_REPEATS = re.compile(r"(.)\1+")


def strip_accents(text: str) -> str:
    """Remove combining marks, and fold full-width and styled letters.

    ``NFKD`` turns ``ｂ`` into ``b`` and ``é`` into ``e`` plus a combining
    accent; dropping the combining characters leaves plain ASCII where one
    exists. This is also what makes zalgo text comparable.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalise(text: str) -> str:
    """Reduce a message to a comparable form.

    Lowercases, removes invisible characters, folds accents and full-width
    letters, substitutes digit leetspeak, and collapses runs of three or more
    identical characters down to two.

    Punctuation is left alone. Spacing and punctuation are what whole-word
    matching relies on, so only :func:`squash` folds them.

    Returns:
        The normalised text, with its word boundaries intact.
    """
    text = _INVISIBLE.sub("", text)
    text = strip_accents(text).lower()
    text = text.translate(_LEET_DIGITS)
    return _REPEATS.sub(r"\1\1", text)


def squash(text: str) -> str:
    """Normalise aggressively for a boundary-free comparison.

    Applies punctuation leetspeak, removes everything that is not a letter or
    digit, and collapses every repeated character to one. That turns
    ``b.a.d.w.o.r.d``, ``b@dword`` and ``baaaadword`` into the same string.

    Used as a second pass rather than a replacement: collapsing this hard also
    erases word boundaries, so on its own it matches across them -- "class ic"
    would contain "assi".
    """
    text = normalise(text).translate(_LEET_PUNCT)
    return _ALL_REPEATS.sub(r"\1", _NON_ALNUM.sub("", text))


# --- Word filter -----------------------------------------------------------


def find_banned_word(
    content: str, words: Iterable[str], *, whole_word: bool = False
) -> str | None:
    """Return the first banned word present in ``content``, if any.

    Args:
        content: The raw message.
        words: Banned words, matched case-insensitively.
        whole_word: Require word boundaries. Off by default, which catches
            ``badwordy`` but also matches inside longer words -- the classic
            "Scunthorpe" trade-off, made explicit rather than assumed.

    Returns:
        The banned word that matched, for reporting, or ``None``.
    """
    normalised = normalise(content)
    squashed = squash(content)

    for word in words:
        needle = normalise(word)
        if not needle:
            continue

        if whole_word:
            if re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", normalised):
                return word
            continue

        if needle in normalised:
            return word

        # Second pass with spacing and repeats removed catches deliberate
        # separation and letter-stretching. Only applied to needles long enough
        # that a chance substring match is unlikely -- collapsing repeats turns
        # "good" into "god", which is exactly the kind of accident a short
        # needle would produce.
        squashed_needle = squash(word)
        if len(squashed_needle) >= 4 and squashed_needle in squashed:
            return word

    return None


# --- Simple content measures -----------------------------------------------


def caps_ratio(content: str) -> float:
    """Proportion of alphabetic characters that are uppercase, 0.0 to 1.0.

    Non-letters are excluded, so ``"HI!!!!!!"`` scores 1.0 rather than 0.25.
    """
    letters = [ch for ch in content if ch.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for ch in letters if ch.isupper()) / len(letters)


#: Unicode emoji ranges plus Discord's custom ``<:name:id>`` form.
_EMOJI = re.compile(
    "<a?:\\w+:\\d+>|["
    "\U0001f300-\U0001faff"
    "\U00002600-\U000027bf"
    "\U0001f1e6-\U0001f1ff"
    "\U00002190-\U000021ff"
    "\U00002b00-\U00002bff"
    "]"
)


def count_emoji(content: str) -> int:
    """Count Unicode and custom emoji in a message."""
    return len(_EMOJI.findall(content))


def zalgo_ratio(content: str) -> float:
    """Proportion of characters that are combining marks, 0.0 to 1.0.

    Ordinary accented text sits well under 0.3; zalgo stacks several marks per
    base character and runs far higher.
    """
    if not content:
        return 0.0
    combining = sum(1 for ch in content if unicodedata.combining(ch))
    return combining / len(content)


def newline_count(content: str) -> int:
    """Number of line breaks."""
    return content.count("\n")


# --- Links and invites -----------------------------------------------------

_URL = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)

#: Bare domains with no scheme: "example.com/path". Requires a known-shaped TLD
#: so ordinary sentences ending in a full stop are not treated as links.
_BARE_DOMAIN = re.compile(
    r"(?<![\w@./])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})(?:/\S*)?",
    re.IGNORECASE,
)

_INVITE = re.compile(
    r"(?:discord(?:app)?\.com/invite|discord\.gg|discord\.me|dsc\.gg|invite\.gg)"
    r"/([a-z0-9-]{2,32})",
    re.IGNORECASE,
)


def extract_urls(content: str, *, include_bare: bool = True) -> list[str]:
    """Find URLs in a message.

    Args:
        content: The raw message.
        include_bare: Also match domains written without ``http://``. Needed
            because "totally-not-a-scam.xyz/free" is a link to a human and to
            Discord's own embed handler.
    """
    found = _URL.findall(content)
    if include_bare:
        stripped = _URL.sub(" ", content)
        found.extend(match.group(0) for match in _BARE_DOMAIN.finditer(stripped))
    return found


def extract_domain(url: str) -> str:
    """Reduce a URL to its registrable-looking domain, lowercased.

    Strips the scheme, any credentials, the port, the path, and a leading
    ``www.``. Not a public-suffix parser -- that would need a bundled PSL and
    the trusted-domain check does not need one.
    """
    url = url.strip().lower()
    url = re.sub(r"^[a-z][a-z0-9+.-]*://", "", url)
    url = url.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    url = url.rsplit("@", 1)[-1]  # strip user:pass@
    url = url.split(":", 1)[0]  # strip port
    return url[4:] if url.startswith("www.") else url


def extract_domains(content: str) -> list[str]:
    """Every distinct domain mentioned in a message, in order of appearance."""
    seen: dict[str, None] = {}
    for url in extract_urls(content):
        domain = extract_domain(url)
        if domain and "." in domain:
            seen.setdefault(domain, None)
    return list(seen)


def domain_matches(domain: str, listed: Iterable[str]) -> str | None:
    """Match a domain against a list, treating entries as suffixes.

    ``github.com`` on the list matches ``gist.github.com`` but not
    ``notgithub.com`` -- the boundary check on the character before the suffix
    is what stops the second one.

    Returns:
        The list entry that matched, or ``None``.
    """
    domain = domain.lower().lstrip(".")
    for entry in listed:
        entry = entry.lower().lstrip(".").removeprefix("www.")
        if not entry:
            continue
        if domain == entry or domain.endswith("." + entry):
            return entry
    return None


def find_invites(content: str) -> list[str]:
    """Invite codes mentioned in a message."""
    return _INVITE.findall(content)


# --- Rate limiting ---------------------------------------------------------


@dataclass(slots=True)
class _Window:
    """Timestamps inside a sliding window, plus recent content hashes."""

    stamps: deque[float] = field(default_factory=deque)
    recent: deque[tuple[float, int]] = field(default_factory=deque)


class RateTracker:
    """Sliding-window counters for spam, duplicates, attachments and stickers.

    Held in memory and never persisted: these windows are seconds long, so a
    restart losing them costs nothing, while writing a row per message would be
    the busiest query in the bot.

    Memory is bounded by pruning on every access and by an idle sweep, so a raid
    that touches ten thousand user IDs does not leave ten thousand deques behind.
    """

    __slots__ = ("_windows", "_last_sweep")

    #: Seconds between full sweeps of idle entries.
    SWEEP_INTERVAL = 300.0

    #: An entry untouched for this long is dropped.
    IDLE_TIMEOUT = 600.0

    def __init__(self) -> None:
        self._windows: dict[tuple[int, int, str], _Window] = defaultdict(_Window)
        self._last_sweep = time.monotonic()

    def hit(
        self, guild_id: int, user_id: int, kind: str, *, window: float, amount: int = 1
    ) -> int:
        """Record activity and return the count inside the window.

        Args:
            guild_id: Guild the activity happened in.
            user_id: Who did it.
            kind: Counter name -- ``messages``, ``attachments``, ``stickers``.
            window: Window length in seconds.
            amount: How much to add. Attachments come several per message.

        Returns:
            The number of events inside the window, including this one.
        """
        now = time.monotonic()
        self._maybe_sweep(now)

        entry = self._windows[(guild_id, user_id, kind)]
        cutoff = now - window
        while entry.stamps and entry.stamps[0] < cutoff:
            entry.stamps.popleft()

        entry.stamps.extend([now] * max(1, amount))
        return len(entry.stamps)

    def repeats(
        self, guild_id: int, user_id: int, content: str, *, window: float
    ) -> int:
        """Record a message and return how many times it repeated in the window.

        Compares a hash of the normalised text, so ``hello`` and ``H E L L O``
        count as the same message. Storing hashes rather than the text keeps the
        memory cost flat regardless of message length.
        """
        now = time.monotonic()
        self._maybe_sweep(now)

        entry = self._windows[(guild_id, user_id, "duplicates")]
        cutoff = now - window
        while entry.recent and entry.recent[0][0] < cutoff:
            entry.recent.popleft()

        digest = hash(squash(content))
        count = sum(1 for _, other in entry.recent if other == digest) + 1
        entry.recent.append((now, digest))
        return count

    def reset(self, guild_id: int, user_id: int) -> None:
        """Clear every counter for one user, after they are punished."""
        for key in [k for k in self._windows if k[0] == guild_id and k[1] == user_id]:
            del self._windows[key]

    def _maybe_sweep(self, now: float) -> None:
        """Drop entries nobody has touched recently."""
        if now - self._last_sweep < self.SWEEP_INTERVAL:
            return
        self._last_sweep = now

        cutoff = now - self.IDLE_TIMEOUT
        for key in [
            k
            for k, entry in self._windows.items()
            if (not entry.stamps or entry.stamps[-1] < cutoff)
            and (not entry.recent or entry.recent[-1][0] < cutoff)
        ]:
            del self._windows[key]

    def __len__(self) -> int:
        return len(self._windows)


# --- Regex safety ----------------------------------------------------------

#: A quantifier applied to a group that itself contains a quantifier. This is
#: the shape behind catastrophic backtracking -- (a+)+ against a long
#: non-matching string can take exponential time and pin a core.
_NESTED_QUANTIFIER = re.compile(r"\([^)]*[+*}][^)]*\)\s*[+*{]")


def is_regex_safe(pattern: str, *, max_length: int = 200) -> tuple[bool, str]:
    """Whether a user-supplied pattern is safe to compile and run.

    Rejecting at configuration time is the whole point: a pattern accepted here
    runs against every message in the server, and a bad one is not a slow
    command but an unresponsive bot.

    Returns:
        ``(True, "")`` or ``(False, reason)``.
    """
    if len(pattern) > max_length:
        return False, f"Pattern is longer than {max_length} characters."
    if _NESTED_QUANTIFIER.search(pattern):
        return False, (
            "Pattern contains a quantifier applied to a group that is itself "
            "repeated, such as `(a+)+`. That shape can take exponential time on "
            "a message that nearly matches."
        )
    try:
        re.compile(pattern)
    except re.error as exc:
        return False, f"Not a valid regular expression: {exc}"
    return True, ""
