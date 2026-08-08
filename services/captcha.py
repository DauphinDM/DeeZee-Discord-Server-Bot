"""Locally generated captcha images.

No external service. Nothing leaves the process, so there is no API key, no
privacy question about sending member data anywhere, and no dependency on a
third party staying up.

This is the only place besides the rank card that uses Pillow, and Pillow is the
single largest memory risk against the 512 MiB budget. Three rules apply here
and are load-bearing rather than stylistic:

* One render at a time, behind a semaphore. Twenty people verifying at once
  queue; they do not allocate twenty canvases.
* Fixed small canvas. Nothing user-controlled reaches a size argument.
* The font is loaded once at import, not per render.
"""

from __future__ import annotations

import asyncio
import io
import logging
import random
import string

from PIL import Image, ImageDraw, ImageFilter, ImageFont

log = logging.getLogger(__name__)

#: Fixed output size. Large enough to read on a phone, small enough that the
#: buffer is a few tens of kilobytes.
WIDTH = 320
HEIGHT = 110

#: Characters used in challenges. 0/O, 1/I/l and 5/S are excluded: a captcha
#: that is ambiguous to a human is a support ticket, not a security measure.
ALPHABET = "ABCDEFGHJKMNPQRTUVWXYZ2346789"

#: Default challenge length.
LENGTH = 6

#: Only one render at a time. This is what bounds peak memory when a raid means
#: fifty people hit the verify button in the same second.
_RENDER_LOCK = asyncio.Semaphore(1)

#: Loaded once, at import. ``ImageFont.truetype`` is not cheap and calling it per
#: render would be the most expensive line in the module.
try:
    _FONT = ImageFont.load_default(size=54)
    _NOISE_FONT = ImageFont.load_default(size=22)
except TypeError:  # pragma: no cover - Pillow older than 10.1
    _FONT = ImageFont.load_default()
    _NOISE_FONT = _FONT
    log.warning(
        "Pillow is too old for scalable default fonts; captcha text will be small"
    )


def new_challenge(length: int = LENGTH) -> str:
    """Generate a random challenge string."""
    return "".join(random.choice(ALPHABET) for _ in range(length))


def normalise_answer(answer: str) -> str:
    """Fold an answer for comparison.

    Case is ignored and spaces are stripped. The ambiguous characters are not in
    the alphabet at all, so no substitution is needed here.
    """
    return "".join(answer.split()).upper()


def _render(text: str) -> bytes:
    """Draw the captcha. Runs in a worker thread, never on the event loop.

    Every image is opened in a ``with`` block so the buffers are released as soon
    as the PNG bytes exist, rather than waiting for the garbage collector.
    """
    background = (random.randint(230, 250),) * 3

    with Image.new("RGB", (WIDTH, HEIGHT), background) as image:
        draw = ImageDraw.Draw(image)

        # Noise first, so it sits behind the characters and cannot be separated
        # by colour alone.
        for _ in range(14):
            x1, y1 = random.randint(0, WIDTH), random.randint(0, HEIGHT)
            x2, y2 = random.randint(0, WIDTH), random.randint(0, HEIGHT)
            shade = random.randint(150, 205)
            draw.line((x1, y1, x2, y2), fill=(shade, shade, shade), width=1)

        for _ in range(6):
            x = random.randint(0, WIDTH - 30)
            y = random.randint(0, HEIGHT - 20)
            draw.text(
                (x, y),
                random.choice(ALPHABET),
                font=_NOISE_FONT,
                fill=(random.randint(170, 210),) * 3,
            )

        # Characters, each on its own slightly rotated tile so the baseline is
        # not a straight line for a solver to lock onto.
        step = WIDTH // (len(text) + 1)
        for index, character in enumerate(text):
            colour = (
                random.randint(0, 90),
                random.randint(0, 90),
                random.randint(0, 90),
            )
            with Image.new("RGBA", (step + 20, HEIGHT), (0, 0, 0, 0)) as tile:
                tile_draw = ImageDraw.Draw(tile)
                tile_draw.text((6, 18), character, font=_FONT, fill=colour + (255,))
                rotated = tile.rotate(
                    random.uniform(-26, 26), resample=Image.BICUBIC, expand=False
                )
                try:
                    image.paste(rotated, (index * step + 8, 0), rotated)
                finally:
                    rotated.close()

        blurred = image.filter(ImageFilter.GaussianBlur(0.6))
        try:
            buffer = io.BytesIO()
            blurred.save(buffer, format="PNG", optimize=True)
            return buffer.getvalue()
        finally:
            blurred.close()


async def generate(length: int = LENGTH) -> tuple[str, bytes]:
    """Produce a challenge and its PNG.

    Rendering happens in a worker thread: a composite takes long enough that
    doing it inline would stall the gateway heartbeat, and a stalled heartbeat
    is a disconnect.

    Returns:
        ``(answer, png_bytes)``. The answer is uppercase and should be stored,
        not held in a view -- a restart must not invalidate it.
    """
    text = new_challenge(length)
    async with _RENDER_LOCK:
        image = await asyncio.to_thread(_render, text)
    return text, image


def text_challenge() -> tuple[str, str]:
    """Produce a text-only challenge for guilds that cannot use images.

    Simple arithmetic rather than a distorted string: without an image there is
    nothing to distort, and a plain "type these letters" prompt is trivially
    scriptable.

    Returns:
        ``(answer, question)``.
    """
    left = random.randint(2, 19)
    right = random.randint(2, 19)
    if random.random() < 0.5:
        return str(left + right), f"What is **{left} + {right}**?"
    if left < right:
        left, right = right, left
    return str(left - right), f"What is **{left} - {right}**?"


def random_word(length: int = 5) -> str:
    """Random lowercase string, for challenges that ask for a typed word."""
    return "".join(random.choice(string.ascii_lowercase) for _ in range(length))
