"""Embed paginator.

Anything that can produce more than one embed's worth of output goes through
here: warning lists, case histories, the leaderboard, tag lists, the help
command, audit results.

Splitting happens up front, in memory, from data the caller already has. Pages
are never regenerated on each press, so paging is free.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import discord
from discord.ext import commands

from core.constants import (
    COLOUR_DEFAULT,
    EMOJI_FIRST,
    EMOJI_LAST,
    EMOJI_NEXT,
    EMOJI_PREVIOUS,
    EMOJI_STOP,
    MAX_EMBED_DESCRIPTION,
    MAX_EMBED_FIELDS,
    PAGINATOR_TIMEOUT,
)

from .views import BaseView

log = logging.getLogger(__name__)


class PageJumpModal(discord.ui.Modal, title="Jump to page"):
    """Ask for a page number.

    Worth a modal rather than more buttons: a 40-page case history is unusable
    with next/previous alone, and five buttons is already the width of a row.
    """

    page = discord.ui.TextInput(
        label="Page number",
        placeholder="e.g. 7",
        min_length=1,
        max_length=5,
        required=True,
    )

    def __init__(self, paginator: Paginator) -> None:
        super().__init__(timeout=60.0)
        self.paginator = paginator
        self.page.placeholder = f"1 to {len(paginator.pages)}"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.page.value.strip()
        if not raw.isdigit():
            await interaction.response.send_message(
                f"`{raw}` is not a page number.", ephemeral=True
            )
            return

        index = int(raw) - 1
        if not 0 <= index < len(self.paginator.pages):
            await interaction.response.send_message(
                f"There are only {len(self.paginator.pages)} pages.", ephemeral=True
            )
            return

        await self.paginator.show_page(interaction, index)


class Paginator(BaseView):
    """Button-driven embed paginator, locked to the invoker.

    A single-page paginator sends a plain embed with no controls, so short
    output never carries five dead buttons.
    """

    def __init__(
        self,
        pages: Sequence[discord.Embed],
        author: discord.abc.User | None = None,
        *,
        timeout: float = PAGINATOR_TIMEOUT,
        show_page_numbers: bool = True,
    ) -> None:
        super().__init__(author, timeout=timeout)

        if not pages:
            raise ValueError("A paginator needs at least one page.")

        self.pages = list(pages)
        self.index = 0

        if show_page_numbers and len(self.pages) > 1:
            self._stamp_page_numbers()

        # The jump button is pointless below four pages -- next/previous already
        # reaches everything -- so it is only added when it earns its space.
        if len(self.pages) < 4:
            self.remove_item(self.jump)

        self._sync_buttons()

    def _stamp_page_numbers(self) -> None:
        """Append the page counter to each footer, keeping existing text."""
        total = len(self.pages)
        for number, embed in enumerate(self.pages, start=1):
            counter = f"Page {number}/{total}"
            existing = embed.footer.text
            embed.set_footer(
                text=f"{existing} • {counter}" if existing else counter,
                icon_url=embed.footer.icon_url,
            )

    def _sync_buttons(self) -> None:
        """Disable controls that would do nothing from the current page."""
        at_start = self.index == 0
        at_end = self.index == len(self.pages) - 1

        self.first.disabled = at_start
        self.previous.disabled = at_start
        self.next.disabled = at_end
        self.last.disabled = at_end

    async def show_page(self, interaction: discord.Interaction, index: int) -> None:
        """Move to a page and update the message."""
        self.index = max(0, min(index, len(self.pages) - 1))
        self._sync_buttons()

        if interaction.response.is_done():
            await interaction.edit_original_response(
                embed=self.pages[self.index], view=self
            )
        else:
            await interaction.response.edit_message(
                embed=self.pages[self.index], view=self
            )

    # --- Controls ----------------------------------------------------------
    # Order here is the order they appear in the row.

    @discord.ui.button(emoji=EMOJI_FIRST, style=discord.ButtonStyle.secondary)
    async def first(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Jump to the first page."""
        await self.show_page(interaction, 0)

    @discord.ui.button(emoji=EMOJI_PREVIOUS, style=discord.ButtonStyle.primary)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Go back one page."""
        await self.show_page(interaction, self.index - 1)

    @discord.ui.button(emoji=EMOJI_STOP, style=discord.ButtonStyle.danger)
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Close the paginator, leaving the current page visible."""
        self.disable_all()
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(emoji=EMOJI_NEXT, style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Go forward one page."""
        await self.show_page(interaction, self.index + 1)

    @discord.ui.button(emoji=EMOJI_LAST, style=discord.ButtonStyle.secondary)
    async def last(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Jump to the last page."""
        await self.show_page(interaction, len(self.pages) - 1)

    @discord.ui.button(label="Jump", style=discord.ButtonStyle.secondary, row=1)
    async def jump(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Open a modal asking which page to go to."""
        await interaction.response.send_modal(PageJumpModal(self))

    # --- Sending -----------------------------------------------------------

    async def start(
        self, ctx: commands.Context, *, ephemeral: bool = False
    ) -> discord.Message:
        """Send the first page.

        A single-page result is sent without controls, since there is nowhere to
        page to.
        """
        if len(self.pages) == 1:
            self.stop()
            return await ctx.send(embed=self.pages[0], ephemeral=ephemeral)

        message = await ctx.send(embed=self.pages[0], view=self, ephemeral=ephemeral)
        self.message = message
        return message

    async def start_interaction(
        self, interaction: discord.Interaction, *, ephemeral: bool = True
    ) -> None:
        """Send the first page in response to an interaction."""
        if len(self.pages) == 1:
            self.stop()
            if interaction.response.is_done():
                await interaction.followup.send(embed=self.pages[0], ephemeral=ephemeral)
            else:
                await interaction.response.send_message(
                    embed=self.pages[0], ephemeral=ephemeral
                )
            return

        if interaction.response.is_done():
            self.message = await interaction.followup.send(
                embed=self.pages[0], view=self, ephemeral=ephemeral, wait=True
            )
        else:
            await interaction.response.send_message(
                embed=self.pages[0], view=self, ephemeral=ephemeral
            )
            self.message = await interaction.original_response()


# --- Page builders ---------------------------------------------------------
# Callers hand over a flat list; these do the splitting. Every builder respects
# Discord's own limits, so a long reason or a 300-entry leaderboard cannot
# produce a 400 from the API.


def paginate_lines(
    lines: Sequence[str],
    *,
    title: str | None = None,
    per_page: int = 10,
    colour: int = COLOUR_DEFAULT,
    description: str | None = None,
    empty_message: str = "Nothing to show.",
) -> list[discord.Embed]:
    """Split a list of lines into embeds.

    Args:
        lines: Already-formatted lines, one per entry.
        title: Repeated on every page.
        per_page: Lines per page, further reduced if they would overflow.
        colour: Embed colour.
        description: Text placed above the lines on every page.
        empty_message: Used when ``lines`` is empty, so callers never have to
            special-case an empty result.

    Returns:
        At least one embed, always.
    """
    if not lines:
        return [discord.Embed(title=title, description=empty_message, colour=colour)]

    header = f"{description}\n\n" if description else ""
    budget = MAX_EMBED_DESCRIPTION - len(header)

    pages: list[discord.Embed] = []
    current: list[str] = []
    length = 0

    for line in lines:
        # A single line longer than a whole embed is truncated rather than
        # silently dropped; losing output without saying so is worse.
        if len(line) > budget:
            line = line[: budget - 3] + "..."

        if current and (len(current) >= per_page or length + len(line) + 1 > budget):
            pages.append(
                discord.Embed(
                    title=title, description=header + "\n".join(current), colour=colour
                )
            )
            current, length = [], 0

        current.append(line)
        length += len(line) + 1

    if current:
        pages.append(
            discord.Embed(title=title, description=header + "\n".join(current), colour=colour)
        )

    return pages


def paginate_fields(
    fields: Sequence[tuple[str, str, bool]],
    *,
    title: str | None = None,
    per_page: int = 6,
    colour: int = COLOUR_DEFAULT,
    description: str | None = None,
    empty_message: str = "Nothing to show.",
) -> list[discord.Embed]:
    """Split ``(name, value, inline)`` triples into embeds.

    Used where each entry needs its own heading -- a case history, a config
    summary, a tag list with usage counts.
    """
    if not fields:
        return [discord.Embed(title=title, description=empty_message, colour=colour)]

    per_page = min(per_page, MAX_EMBED_FIELDS)
    pages: list[discord.Embed] = []

    for start in range(0, len(fields), per_page):
        embed = discord.Embed(title=title, description=description, colour=colour)
        for name, value, inline in fields[start : start + per_page]:
            embed.add_field(name=name, value=value, inline=inline)
        pages.append(embed)

    return pages


def paginate_text(
    text: str,
    *,
    title: str | None = None,
    colour: int = COLOUR_DEFAULT,
    code_block: str | None = None,
) -> list[discord.Embed]:
    """Split a long block of text into embeds, breaking on line boundaries.

    Args:
        text: The whole body.
        title: Repeated on every page.
        colour: Embed colour.
        code_block: Language for a fenced block, e.g. ``"py"``. ``None`` for
            plain text.

    Returns:
        At least one embed, always.
    """
    wrapper = len(f"```{code_block}\n\n```") if code_block is not None else 0
    budget = MAX_EMBED_DESCRIPTION - wrapper

    chunks: list[str] = []
    current: list[str] = []
    length = 0

    for line in text.splitlines() or [""]:
        # Break an over-long single line by character count; nothing else can be
        # done with a 5000-character line that contains no newline.
        while len(line) > budget:
            chunks.append(line[:budget])
            line = line[budget:]

        if current and length + len(line) + 1 > budget:
            chunks.append("\n".join(current))
            current, length = [], 0

        current.append(line)
        length += len(line) + 1

    if current:
        chunks.append("\n".join(current))

    return [
        discord.Embed(
            title=title,
            description=f"```{code_block}\n{chunk}\n```" if code_block is not None else chunk,
            colour=colour,
        )
        for chunk in chunks or [""]
    ]
