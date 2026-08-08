"""The restricted variable engine for tags, custom commands and autoresponses.

Carl-bot's TagScript is a small programming language. This is not: it is a
substituter over a fixed table of names, plus two random pickers. That is a
deliberate trade, recorded as decision 4 in ``docs/DESIGN.md`` -- a scripting
language inside a moderation bot is arbitrary execution inside your server, run
by whoever can create a tag.

What that buys, concretely:

* No loops, so no tag can spin the CPU.
* No conditionals, so a tag cannot behave differently for staff.
* No nesting, so ``{random:{random:a|b}|c}`` is text, not recursion.
* Every substitution is bounded, so output length is knowable in advance.

The engine has no Discord imports. The caller resolves the member, guild and
channel into plain strings and hands over a mapping.
"""

from __future__ import annotations

import random
import re

#: Longest output a rendered template may produce. Discord's own message limit
#: is 2000; the extra room lets a caller trim with an ellipsis rather than
#: failing.
MAX_OUTPUT = 4000

#: Substitutions applied per render. Bounded so a template full of pickers
#: cannot turn into a long loop.
MAX_SUBSTITUTIONS = 200

#: ``{name}`` or ``{name.attribute}``. No nesting: the pattern cannot match a
#: brace inside a brace, which is what makes recursion impossible.
#:
#: Digits are allowed after the first character, because ``{arg1}`` is a real
#: variable name -- a letters-only pattern silently leaves it as literal text.
_SIMPLE = re.compile(
    r"\{([a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)?)\}", re.IGNORECASE
)

#: ``{random:a|b|c}`` and ``{choose:a|b|c}``. Same reason: no braces inside.
_PICKER = re.compile(r"\{(random|choose):([^{}]*)\}", re.IGNORECASE)

#: Every name the engine understands, with what it means. Shown by ``?variables``,
#: so this table is the documentation as well as the implementation.
VARIABLES: dict[str, str] = {
    "user": "The member's display name",
    "user.name": "Their username",
    "user.tag": "Their full tag",
    "user.id": "Their user ID",
    "user.mention": "A ping",
    "user.avatar": "URL of their avatar",
    "server": "Server name",
    "server.id": "Server ID",
    "server.count": "Member count",
    "server.icon": "URL of the server icon",
    "channel": "Channel name",
    "channel.id": "Channel ID",
    "channel.mention": "A channel link",
    "count": "Member count -- short form of {server.count}",
    "args": "Everything typed after the tag name",
    "arg1": "The first word after the tag name",
    "arg2": "The second word",
    "arg3": "The third word",
}

#: The two pickers, documented alongside the names above.
PICKERS: dict[str, str] = {
    "{random:a|b|c}": "Picks one at random, every time it is used",
    "{choose:a|b|c}": "The same thing -- both spellings work",
}


def build_context(
    *,
    user_name: str = "",
    user_username: str = "",
    user_tag: str = "",
    user_id: int = 0,
    user_mention: str = "",
    user_avatar: str = "",
    server_name: str = "",
    server_id: int = 0,
    server_count: int = 0,
    server_icon: str = "",
    channel_name: str = "",
    channel_id: int = 0,
    channel_mention: str = "",
    args: str = "",
) -> dict[str, str]:
    """Turn resolved Discord objects into the flat mapping :func:`render` wants.

    Everything arrives as a plain value so this module stays free of Discord
    imports and can be tested without a gateway connection.
    """
    words = args.split()
    return {
        "user": user_name,
        "user.name": user_username,
        "user.tag": user_tag,
        "user.id": str(user_id),
        "user.mention": user_mention,
        "user.avatar": user_avatar,
        "server": server_name,
        "server.id": str(server_id),
        "server.count": str(server_count),
        "server.icon": server_icon,
        "channel": channel_name,
        "channel.id": str(channel_id),
        "channel.mention": channel_mention,
        "count": str(server_count),
        "args": args,
        "arg1": words[0] if len(words) > 0 else "",
        "arg2": words[1] if len(words) > 1 else "",
        "arg3": words[2] if len(words) > 2 else "",
    }


def render(template: str, context: dict[str, str]) -> str:
    """Substitute every known variable and picker in ``template``.

    An unknown name is left exactly as written. That is deliberate: silently
    deleting ``{levelup}`` would make a typo look like the tag was empty, and
    leaving it visible is how the author finds out.

    Args:
        template: The stored text.
        context: A mapping from :func:`build_context`.

    Returns:
        The rendered text, truncated at :data:`MAX_OUTPUT`.
    """
    used = 0

    def pick(match: re.Match[str]) -> str:
        nonlocal used
        used += 1
        if used > MAX_SUBSTITUTIONS:
            return match.group(0)
        choices = [part.strip() for part in match.group(2).split("|") if part.strip()]
        return random.choice(choices) if choices else ""

    def name(match: re.Match[str]) -> str:
        nonlocal used
        used += 1
        if used > MAX_SUBSTITUTIONS:
            return match.group(0)
        key = match.group(1).lower()
        return context.get(key, match.group(0))

    # Pickers first: a picker may contain text that looks like a variable, and
    # resolving the variable afterwards is the order an author expects.
    text = _PICKER.sub(pick, template)
    text = _SIMPLE.sub(name, text)

    return text[:MAX_OUTPUT]


def find_unknown(template: str) -> list[str]:
    """Names in a template that the engine does not recognise.

    Used at creation time so an author is told about a typo before the tag is
    saved rather than the first time somebody runs it.
    """
    return sorted(
        {
            match.group(1)
            for match in _SIMPLE.finditer(template)
            if match.group(1).lower() not in VARIABLES
        }
    )


def preview(template: str) -> str:
    """Render a template with obviously fake values, for a config preview."""
    return render(
        template,
        build_context(
            user_name="Example",
            user_username="example",
            user_tag="example#0001",
            user_id=123456789012345678,
            user_mention="@Example",
            server_name="Your Server",
            server_id=987654321098765432,
            server_count=1234,
            channel_name="general",
            channel_id=111111111111111111,
            channel_mention="#general",
            args="some text here",
        ),
    )
