"""Parsing what the other bots print when you ask them about themselves.

Dyno, Carl-bot and Sapphire have no public configuration API. What they do have
is a set of commands that print their current settings into chat -- ``?welcome``,
``!autoresponse list``, ``/tag list`` and so on. ``?import capture`` records a
channel for a few minutes while a staff member runs those commands, and this
module reads what they posted.

It is genuinely limited and says so. A bot that changes its output format breaks
the parse, and some settings are simply never printed. Everything recognised
lands in the draft; everything else lands in ``unparsed`` where a person can see
it, and ``?import paste`` exists for the rest.

Input is plain text and embed dictionaries. No Discord import, no database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Variable spellings used by the bots Deezee replaces, mapped onto Deezee's.
#: Anything not in here is left alone and reported, because a variable silently
#: rewritten into the wrong thing is worse than one left visible.
VARIABLE_MAP: dict[str, str] = {
    # Dyno
    "{user}": "{user}",
    "{user.name}": "{user}",
    "{user.mention}": "{mention}",
    "{user.id}": "{id}",
    "{server}": "{server}",
    "{server.name}": "{server}",
    "{server.count}": "{count}",
    "{members}": "{count}",
    # Carl-bot
    "{member}": "{user}",
    "{member.mention}": "{mention}",
    "{member.name}": "{user}",
    "{guild}": "{server}",
    "{guild.name}": "{server}",
    "{membercount}": "{count}",
    # Sapphire
    "{{user}}": "{user}",
    "{{server}}": "{server}",
    "{{membercount}}": "{count}",
    "%user%": "{user}",
    "%server%": "{server}",
    "%membercount%": "{count}",
}

#: Names that identify the bot a captured message came from.
KNOWN_BOTS = ("dyno", "carl-bot", "carlbot", "sapphire", "lawliet", "mee6",
              "double counter")

_ANY_VARIABLE = re.compile(r"(\{\{?[a-z_.]+\}?\}|%[a-z_]+%)", re.IGNORECASE)

_WELCOME_HINTS = ("welcome", "join message", "greeting")
_GOODBYE_HINTS = ("goodbye", "farewell", "leave message")
_AR_HINTS = ("autoresponse", "auto-response", "autoresponder", "auto response")
_TAG_HINTS = ("tag list", "tags", "custom command")
_WORDS_HINTS = ("banned word", "blacklisted word", "word filter", "bad word")
_LOG_HINTS = ("log channel", "logging", "modlog", "mod log")


@dataclass(slots=True)
class Capture:
    """Everything one capture window produced."""

    welcome: str = ""
    goodbye: str = ""
    autoresponses: list[tuple[str, str]] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    banned_words: list[str] = field(default_factory=list)
    log_channels: dict[str, int] = field(default_factory=dict)
    untranslated: list[str] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.welcome, self.goodbye, self.autoresponses, self.tags,
                self.banned_words, self.log_channels,
            )
        )

    def summary(self) -> list[str]:
        """One line per thing found, for the review screen."""
        lines: list[str] = []
        if self.welcome:
            lines.append("Welcome message")
        if self.goodbye:
            lines.append("Goodbye message")
        if self.autoresponses:
            lines.append(f"{len(self.autoresponses)} autoresponse(s)")
        if self.tags:
            lines.append(f"{len(self.tags)} tag name(s)")
        if self.banned_words:
            lines.append(f"{len(self.banned_words)} banned word(s)")
        if self.log_channels:
            lines.append(f"{len(self.log_channels)} log channel(s)")
        return lines


def translate_variables(text: str) -> tuple[str, list[str]]:
    """Rewrite another bot's variables into Deezee's.

    Returns:
        ``(translated, untranslated)``. Anything with no equivalent is left
        exactly as written and named in the second list, so the person reviewing
        the import can decide what it should have been.
    """
    untranslated: list[str] = []

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        mapped = VARIABLE_MAP.get(token) or VARIABLE_MAP.get(token.lower())
        if mapped is None:
            untranslated.append(token)
            return token
        return mapped

    return _ANY_VARIABLE.sub(replace, text), sorted(set(untranslated))


def _mentioned_channel(text: str) -> int | None:
    match = re.search(r"<#(\d{15,20})>", text)
    return int(match.group(1)) if match else None


def _looks_like(text: str, hints: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(hint in lowered for hint in hints)


def _embed_text(embed: dict) -> str:
    """Flatten an embed into one searchable string."""
    parts = [
        str(embed.get("title") or ""),
        str(embed.get("description") or ""),
        str((embed.get("author") or {}).get("name") or ""),
        str((embed.get("footer") or {}).get("text") or ""),
    ]
    for raw_field in embed.get("fields") or []:
        parts.append(str(raw_field.get("name") or ""))
        parts.append(str(raw_field.get("value") or ""))
    return "\n".join(part for part in parts if part)


def parse_messages(
    messages: list[tuple[str, str, list[dict]]],
) -> Capture:
    """Read a capture window.

    Args:
        messages: ``(author_name, content, embeds)`` triples in the order they
            were posted.

    Returns:
        A :class:`Capture`. Nothing here writes anywhere; the cog decides what to
        stage and ``?import review`` decides what goes live.
    """
    result = Capture()

    for author, content, embeds in messages:
        blob = "\n".join([content, *(_embed_text(e) for e in embeds)]).strip()
        if not blob:
            continue

        author_lower = author.lower()
        from_a_bot = any(name in author_lower for name in KNOWN_BOTS)

        if _looks_like(blob, _WELCOME_HINTS) and not result.welcome:
            body = _extract_message_body(blob)
            if body:
                translated, untranslated = translate_variables(body)
                result.welcome = translated
                result.untranslated.extend(untranslated)
                continue

        if _looks_like(blob, _GOODBYE_HINTS) and not result.goodbye:
            body = _extract_message_body(blob)
            if body:
                translated, untranslated = translate_variables(body)
                result.goodbye = translated
                result.untranslated.extend(untranslated)
                continue

        if _looks_like(blob, _AR_HINTS):
            found = _extract_pairs(blob)
            if found:
                result.autoresponses.extend(found)
                continue

        if _looks_like(blob, _WORDS_HINTS):
            words = _extract_word_list(blob)
            if words:
                result.banned_words.extend(words)
                continue

        if _looks_like(blob, _TAG_HINTS):
            names = _extract_names(blob)
            if names:
                result.tags.extend(names)
                continue

        if _looks_like(blob, _LOG_HINTS):
            channel_id = _mentioned_channel(blob)
            if channel_id:
                result.log_channels[_log_kind(blob)] = channel_id
                continue

        if from_a_bot:
            # It came from a bot Deezee is replacing and was not recognised.
            # Worth showing rather than discarding.
            result.unparsed.append(f"{author}: {blob[:160]}")

    result.untranslated = sorted(set(result.untranslated))
    result.autoresponses = list(dict.fromkeys(result.autoresponses))
    result.tags = list(dict.fromkeys(result.tags))
    result.banned_words = list(dict.fromkeys(result.banned_words))
    return result


def _log_kind(text: str) -> str:
    lowered = text.lower()
    for kind in ("message", "member", "role", "channel", "voice", "server",
                 "invite", "moderation", "automod"):
        if kind in lowered:
            return kind
    return "moderation"


def _extract_message_body(blob: str) -> str:
    """Pull the actual template out of a settings dump.

    Bots print the message either in a code block or after a label. Both are
    tried, longest sensible match first.
    """
    fenced = re.search(r"```(?:\w+\n)?(.+?)```", blob, re.DOTALL)
    if fenced:
        return fenced.group(1).strip()[:1800]

    labelled = re.search(
        r"(?:message|text|content)\s*[:\-]\s*(.+)", blob, re.IGNORECASE
    )
    if labelled:
        return labelled.group(1).strip()[:1800]

    # A single-line response that is nothing but the message.
    lines = [line.strip() for line in blob.splitlines() if line.strip()]
    if len(lines) == 1 and len(lines[0]) > 10:
        return lines[0][:1800]
    return ""


def _extract_pairs(blob: str) -> list[tuple[str, str]]:
    """Trigger/response pairs from a list output.

    Recognises the three separators the source bots use between a trigger and
    its response: an arrow, a colon and a pipe.
    """
    pairs: list[tuple[str, str]] = []
    for line in blob.splitlines():
        cleaned = line.strip().lstrip("-•*0123456789. ").strip()
        if not cleaned or len(cleaned) < 3:
            continue
        for separator in ("→", "->", " => ", " : ", " | "):
            if separator in cleaned:
                trigger, _, response = cleaned.partition(separator)
                trigger = trigger.strip().strip("`\"'")
                response = response.strip().strip("`\"'")
                if trigger and response:
                    translated, _ = translate_variables(response)
                    pairs.append((trigger[:100], translated[:500]))
                break
    return pairs


def _extract_word_list(blob: str) -> list[str]:
    """A comma or newline separated word list, minus the surrounding prose."""
    fenced = re.search(r"```(?:\w+\n)?(.+?)```", blob, re.DOTALL)
    body = fenced.group(1) if fenced else blob

    candidates: list[str] = []
    for chunk in re.split(r"[,\n]", body):
        word = chunk.strip().strip("`\"'*-• ")
        # Words only, and nothing long enough to be a sentence of explanation.
        if 2 <= len(word) <= 40 and " " not in word and not word.endswith(":"):
            candidates.append(word.lower())
    return candidates[:500]


def _extract_names(blob: str) -> list[str]:
    """Bare names from a list, such as a tag listing."""
    fenced = re.search(r"```(?:\w+\n)?(.+?)```", blob, re.DOTALL)
    body = fenced.group(1) if fenced else blob

    names: list[str] = []
    for chunk in re.split(r"[,\n]", body):
        name = chunk.strip().strip("`\"'*-• ")
        if re.fullmatch(r"[A-Za-z0-9_\-]{1,32}", name or ""):
            names.append(name.lower())
    return names[:500]
