"""The migration panels.

Two screens live here.

:class:`ImportReview` is the only place in the bot where staged data becomes live
configuration, and it is deliberately per-section: accepting the leveling import
and rejecting the automod import has to be one press each, not one press for
both.

:class:`ReactionRoleMapper` handles the part of a reaction-role adoption that no
bot can do automatically. The emoji on the message are readable; which role each
one grants lives in the other bot's database and is not. So the mapping is set
once, by hand, with a role picker per emoji.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import discord

from core.constants import (
    COLOUR_DEFAULT,
    COLOUR_SUCCESS,
    COLOUR_WARNING,
    EMOJI_OFF,
    EMOJI_ON,
    MAX_SELECT_OPTIONS,
    PANEL_TIMEOUT,
)

from ..views import BaseView

if TYPE_CHECKING:
    from discord.ext import commands

log = logging.getLogger(__name__)


class SectionSelect(discord.ui.Select):
    """Picks which staged section the buttons act on."""

    def __init__(self, panel: ImportReview) -> None:
        options = [
            discord.SelectOption(
                label=row["section"],
                value=row["section"],
                description=f"from {row['source']}"
                + (" — already applied" if row["applied_at"] else ""),
                emoji=EMOJI_ON if row["applied_at"] else EMOJI_OFF,
                default=row["section"] == panel.selected,
            )
            for row in panel.drafts[:MAX_SELECT_OPTIONS]
        ]
        super().__init__(
            placeholder="Choose a section to review",
            options=options or [discord.SelectOption(label="Nothing staged", value="-")],
            disabled=not options,
            row=0,
        )
        self.panel = panel

    async def callback(self, interaction: discord.Interaction) -> None:
        self.panel.selected = self.values[0]
        await self.panel.refresh(interaction)


class ImportReview(BaseView):
    """Diff a staged section against live configuration, then apply or discard."""

    def __init__(self, cog: Any, author: discord.abc.User,
                 drafts: list[dict[str, Any]], selected: str | None = None) -> None:
        super().__init__(author, timeout=PANEL_TIMEOUT)
        self.cog = cog
        self.drafts = drafts
        self.selected = selected or (drafts[0]["section"] if drafts else "")
        self.note = ""
        self._build()

    def _build(self) -> None:
        self.clear_items()
        self.add_item(SectionSelect(self))
        self.add_item(self.apply)
        self.add_item(self.discard)
        self.add_item(self.close)

        current = self.current()
        self.apply.disabled = current is None
        self.discard.disabled = current is None
        if current is not None and current["applied_at"]:
            self.apply.label = "Apply again"

    def current(self) -> dict[str, Any] | None:
        for row in self.drafts:
            if row["section"] == self.selected:
                return row
        return None

    async def embed(self, guild: discord.Guild) -> discord.Embed:
        if not self.drafts:
            return discord.Embed(
                title="Nothing staged",
                description=(
                    "No import has been run yet. Start with `?import status` for the "
                    "checklist, or `?import levels`, `?import modlog`, "
                    "`?import capture`."
                ),
                colour=COLOUR_DEFAULT,
            )

        row = self.current()
        if row is None:
            return discord.Embed(description="Pick a section.", colour=COLOUR_DEFAULT)

        lines = await self.cog.describe_draft(guild, row)

        embed = discord.Embed(
            title=f"Review: {row['section']}",
            description="\n".join(lines)[:4000],
            colour=COLOUR_SUCCESS if row["applied_at"] else COLOUR_WARNING,
        )
        embed.add_field(name="Source", value=row["source"], inline=True)
        embed.add_field(
            name="Staged",
            value=f"<t:{row['created_at']}:R>",
            inline=True,
        )
        embed.add_field(
            name="Status",
            value="Applied" if row["applied_at"] else "**Not applied**",
            inline=True,
        )
        if self.note:
            embed.add_field(name="Result", value=self.note, inline=False)
        embed.set_footer(
            text="Nothing here is live until you press Apply. Every apply is "
            "reversible with ?import undo for 7 days."
        )
        return embed

    async def refresh(self, interaction: discord.Interaction) -> None:
        self.drafts = await self.cog.load_drafts(interaction.guild.id)
        self._build()
        await interaction.response.edit_message(
            embed=await self.embed(interaction.guild), view=self
        )

    @discord.ui.button(label="Apply this section", style=discord.ButtonStyle.success,
                       row=1)
    async def apply(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Copy the staged section into live configuration."""
        row = self.current()
        if row is None:
            return
        await interaction.response.defer()
        try:
            self.note = await self.cog.apply_section(
                interaction.guild, row, interaction.user
            )
        except Exception as exc:
            log.exception("Import apply failed for %s", row["section"])
            self.note = f"Failed: {exc}"

        self.drafts = await self.cog.load_drafts(interaction.guild.id)
        self._build()
        await interaction.edit_original_response(
            embed=await self.embed(interaction.guild), view=self
        )

    @discord.ui.button(label="Discard", style=discord.ButtonStyle.danger, row=1)
    async def discard(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        """Throw the staged section away. Live configuration is untouched."""
        row = self.current()
        if row is None:
            return
        await self.cog.discard_section(interaction.guild.id, row["section"])
        self.note = f"Discarded the staged **{row['section']}** data."
        self.drafts = await self.cog.load_drafts(interaction.guild.id)
        self.selected = self.drafts[0]["section"] if self.drafts else ""
        self._build()
        await interaction.response.edit_message(
            embed=await self.embed(interaction.guild), view=self
        )

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary, row=1)
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Close the review panel. Nothing is applied."""
        self.disable_all()
        await interaction.response.edit_message(view=self)
        self.stop()


class EmojiRoleSelect(discord.ui.RoleSelect):
    """One role picker, bound to one emoji on an adopted message."""

    def __init__(self, mapper: ReactionRoleMapper, emoji: str, row: int) -> None:
        super().__init__(
            placeholder=f"{emoji}  →  which role?",
            min_values=0,
            max_values=1,
            row=row,
        )
        self.mapper = mapper
        self.emoji_key = emoji

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.values:
            self.mapper.mapping[self.emoji_key] = self.values[0].id
        else:
            self.mapper.mapping.pop(self.emoji_key, None)
        await self.mapper.refresh(interaction)


class ReactionRoleMapper(BaseView):
    """Map each emoji on an existing message to a role.

    The one part of a reaction-role adoption that cannot be automated: the emoji
    are on the message, but which role each grants is in the other bot's
    database. A picker per emoji is the honest way to do it.

    Four emoji at a time, because a view holds five rows and the last is needed
    for the buttons.
    """

    PER_PAGE = 4

    def __init__(self, cog: Any, author: discord.abc.User, message: discord.Message,
                 emojis: list[str]) -> None:
        super().__init__(author, timeout=PANEL_TIMEOUT)
        self.cog = cog
        self.message = message
        self.emojis = emojis
        self.mapping: dict[str, int] = {}
        self.page = 0
        self.rebuild_as_buttons = False
        self._build()

    def _build(self) -> None:
        self.clear_items()
        start = self.page * self.PER_PAGE
        for index, emoji in enumerate(self.emojis[start : start + self.PER_PAGE]):
            self.add_item(EmojiRoleSelect(self, emoji, row=index))

        self.add_item(self.previous)
        self.add_item(self.next)
        self.add_item(self.style)
        self.add_item(self.save)
        self.add_item(self.close)

        pages = max(1, (len(self.emojis) + self.PER_PAGE - 1) // self.PER_PAGE)
        self.previous.disabled = self.page == 0
        self.next.disabled = self.page >= pages - 1
        self.style.label = (
            "Rebuild as buttons" if self.rebuild_as_buttons else "Keep reactions"
        )
        self.save.disabled = not self.mapping

    def embed(self, guild: discord.Guild) -> discord.Embed:
        lines = []
        for emoji in self.emojis:
            role_id = self.mapping.get(emoji)
            role = guild.get_role(role_id) if role_id else None
            lines.append(f"{emoji} → {role.mention if role else '*not set*'}")

        embed = discord.Embed(
            title="Adopt a reaction-role message",
            description=(
                f"[The message]({self.message.jump_url}) carries "
                f"**{len(self.emojis)}** emoji. Which role each one grants lives in "
                "the other bot's database, not on the message, so it has to be set "
                "here.\n\n" + "\n".join(lines)
            ),
            colour=COLOUR_SUCCESS if self.mapping else COLOUR_DEFAULT,
        )
        embed.add_field(
            name="When saved",
            value=(
                "Deezee will **rebuild the menu as buttons** in a new message."
                if self.rebuild_as_buttons
                else "Deezee will **take over the existing message** and keep using "
                "reactions. The other bot is not removed and may also respond until "
                "you turn it off yourself."
            ),
            inline=False,
        )
        pages = max(1, (len(self.emojis) + self.PER_PAGE - 1) // self.PER_PAGE)
        embed.set_footer(text=f"Page {self.page + 1}/{pages} • {len(self.mapping)} mapped")
        return embed

    async def refresh(self, interaction: discord.Interaction) -> None:
        self._build()
        await interaction.response.edit_message(
            embed=self.embed(interaction.guild), view=self
        )

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, row=4)
    async def previous(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        """Previous four emoji."""
        self.page = max(0, self.page - 1)
        await self.refresh(interaction)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, row=4)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Next four emoji."""
        self.page += 1
        await self.refresh(interaction)

    @discord.ui.button(label="Keep reactions", style=discord.ButtonStyle.primary, row=4)
    async def style(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Switch between adopting the message and rebuilding it as buttons."""
        self.rebuild_as_buttons = not self.rebuild_as_buttons
        await self.refresh(interaction)

    @discord.ui.button(label="Save menu", style=discord.ButtonStyle.success, row=4)
    async def save(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Create the menu from the mapping."""
        await interaction.response.defer()
        try:
            summary = await self.cog.adopt_menu(
                interaction.guild, self.message, self.mapping,
                rebuild=self.rebuild_as_buttons, author=interaction.user,
            )
        except Exception as exc:
            log.exception("Reaction-role adoption failed")
            await interaction.followup.send(f"That failed: {exc}", ephemeral=True)
            return

        self.disable_all()
        self.stop()
        await interaction.edit_original_response(
            embed=discord.Embed(
                title="Menu adopted", description=summary, colour=COLOUR_SUCCESS
            ),
            view=self,
        )

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary, row=4)
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Close without saving."""
        self.disable_all()
        await interaction.response.edit_message(view=self)
        self.stop()


async def open_review(
    ctx: commands.Context, cog: Any, section: str | None = None
) -> None:
    """Send the review panel."""
    drafts = await cog.load_drafts(ctx.guild.id)
    view = ImportReview(cog, ctx.author, drafts, section)
    view.message = await ctx.send(embed=await view.embed(ctx.guild), view=view)
