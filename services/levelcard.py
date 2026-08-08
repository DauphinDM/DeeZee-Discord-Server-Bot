"""The rank card, drawn locally with Pillow.

Same three rules as ``services/captcha.py``, and for the same reason -- Pillow is
the single biggest risk against the 512 MiB budget:

* One render at a time, behind a semaphore. Ten simultaneous ``?rank`` calls
  queue; they do not allocate ten canvases.
* Fixed canvas. Nothing user-controlled reaches a size argument.
* Fonts loaded once at import, never per render.

Avatars arrive as raw bytes from the caller, already size-capped at the Discord
CDN. This module never makes a network request and never imports discord.py, so
the whole thing is testable without a gateway connection.
"""

from __future__ import annotations

import asyncio
import io
import logging

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

#: Fixed output size. Wide enough to read the numbers on a phone.
WIDTH = 934
HEIGHT = 282

#: Avatars above this are rejected before Pillow touches them. A 2 MiB PNG is
#: already implausible for a 128-pixel avatar and decoding one is how a decoder
#: bomb gets in.
MAX_AVATAR_BYTES = 2 * 1024 * 1024

#: Only one render at a time. This is what bounds peak memory when the whole
#: server runs ?rank at once.
_RENDER_LOCK = asyncio.Semaphore(1)

_BACKGROUND = (35, 39, 42)
_PANEL = (44, 47, 51)
_TEXT = (255, 255, 255)
_MUTED = (163, 166, 170)
_BAR_EMPTY = (72, 75, 78)
_ACCENT = (88, 101, 242)

try:
    _FONT_LARGE = ImageFont.load_default(size=44)
    _FONT_MEDIUM = ImageFont.load_default(size=30)
    _FONT_SMALL = ImageFont.load_default(size=24)
except TypeError:  # pragma: no cover - Pillow older than 10.1
    _FONT_LARGE = _FONT_MEDIUM = _FONT_SMALL = ImageFont.load_default()
    log.warning("Pillow is too old for scalable default fonts; rank cards will be small")


def format_number(value: int) -> str:
    """Render a count compactly: 1234 -> 1.2k, 1234567 -> 1.2m."""
    if value < 1000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1000:.1f}k".replace(".0k", "k")
    return f"{value / 1_000_000:.1f}m".replace(".0m", "m")


def _circle_avatar(data: bytes, size: int) -> Image.Image:
    """Decode an avatar and mask it to a circle.

    Raises:
        ValueError: If the payload is too large or is not a readable image.
    """
    if len(data) > MAX_AVATAR_BYTES:
        raise ValueError("Avatar is larger than the accepted maximum.")

    with Image.open(io.BytesIO(data)) as source:
        avatar = source.convert("RGBA").resize((size, size), Image.LANCZOS)

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    avatar.putalpha(mask)
    mask.close()
    return avatar


def _rounded(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int],
             radius: int, fill: tuple[int, int, int]) -> None:
    """Draw a rounded rectangle, tolerating older Pillow builds."""
    try:
        draw.rounded_rectangle(box, radius=radius, fill=fill)
    except AttributeError:  # pragma: no cover - Pillow older than 8.2
        draw.rectangle(box, fill=fill)


def _render(
    *,
    name: str,
    level: int,
    rank: int,
    total_members: int,
    xp_into_level: int,
    xp_for_level: int,
    total_xp: int,
    messages: int,
    avatar: bytes | None,
) -> bytes:
    """Draw the card. Runs in a worker thread, never on the event loop."""
    with Image.new("RGB", (WIDTH, HEIGHT), _BACKGROUND) as card:
        draw = ImageDraw.Draw(card)
        _rounded(draw, (16, 16, WIDTH - 16, HEIGHT - 16), 24, _PANEL)

        if avatar is not None:
            try:
                circle = _circle_avatar(avatar, 180)
            except (ValueError, OSError) as exc:
                log.debug("Avatar could not be drawn: %s", exc)
            else:
                try:
                    card.paste(circle, (48, 51), circle)
                finally:
                    circle.close()

        left = 260

        draw.text((left, 60), name[:24], font=_FONT_LARGE, fill=_TEXT)
        draw.text(
            (left, 118),
            f"Rank #{rank} of {total_members}   •   Level {level}",
            font=_FONT_SMALL,
            fill=_MUTED,
        )

        # Progress bar. Guarded against a zero denominator, which a custom curve
        # or a hand-set XP value could otherwise produce.
        bar_left, bar_right = left, WIDTH - 60
        bar_top, bar_bottom = 176, 210
        _rounded(draw, (bar_left, bar_top, bar_right, bar_bottom), 17, _BAR_EMPTY)

        fraction = 0.0 if xp_for_level <= 0 else min(1.0, xp_into_level / xp_for_level)
        filled = int((bar_right - bar_left) * fraction)
        if filled > 4:
            _rounded(
                draw, (bar_left, bar_top, bar_left + filled, bar_bottom), 17, _ACCENT
            )

        draw.text(
            (bar_left, 222),
            f"{format_number(xp_into_level)} / {format_number(xp_for_level)} XP "
            f"to level {level + 1}",
            font=_FONT_SMALL,
            fill=_MUTED,
        )
        draw.text(
            (bar_right - 260, 222),
            f"{format_number(total_xp)} total   •   {format_number(messages)} msgs",
            font=_FONT_SMALL,
            fill=_MUTED,
        )

        buffer = io.BytesIO()
        card.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()


async def render_card(
    *,
    name: str,
    level: int,
    rank: int,
    total_members: int,
    xp_into_level: int,
    xp_for_level: int,
    total_xp: int,
    messages: int,
    avatar: bytes | None = None,
) -> bytes:
    """Produce a rank card PNG.

    Rendering happens in a worker thread: a composite takes long enough that
    doing it inline would stall the gateway heartbeat, and a stalled heartbeat is
    a disconnect.

    Returns:
        PNG bytes. Callers wrap this in a ``discord.File``.
    """
    async with _RENDER_LOCK:
        return await asyncio.to_thread(
            _render,
            name=name,
            level=level,
            rank=rank,
            total_members=total_members,
            xp_into_level=xp_into_level,
            xp_for_level=xp_for_level,
            total_xp=total_xp,
            messages=messages,
            avatar=avatar,
        )
