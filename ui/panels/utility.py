"""Interactive help and the embed builder.

Both replace something a dashboard would normally do. ``HelpView`` is the command
browser; ``EmbedBuilder`` is the rich-embed composer, with a live preview that is
the message itself rather than a mock-up of it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands

from core.constants import (
    COLOUR_DEFAULT,
    COLOUR_ERROR,
    MAX_EMBED_FIELDS,
    MAX_SELECT_OPTIONS,
    PANEL_TIMEOUT,
)

from ..views import BaseView

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

#: Short blurb per cog, so the category list means something before you open it.
CATEGORY_HELP: dict[str, str] = {
    "Moderation": "Bans, kicks, mutes, warnings, purge, channel locks",
    "Automod": "Word, link, spam, caps and invite filters",
    "AntiRaid": "Raid detection, verification, alt heuristics, quarantine",
    "Roles": "Role commands, reaction-role menus, autoroles, self-roles",
    "Logging": "Event logs, snipe, audit-log search",
    "Leveling": "XP, levels, rank cards, leaderboards",
    "Economy": "Currency, daily, work, shop, gambling",
    "Utility": "Server and user info, polls, reminders, starboard, help",
    "Fun": "Dice, 8ball, text transforms, pictures, reputation",
    "Giveaways": "Giveaways and scheduled messages",
    "Tags": "Tags, custom commands, autoresponses",
    "ServerConfig": "Prefix, welcome, permissions, modules, backups",
    "Importer": "Migrating off Dyno, Carl-bot, Sapphire, MEE6 and Lawliet",
}


class HelpSearchModal(discord.ui.Modal, title="Search commands"):
    """Free-text search across names, aliases and descriptions."""

    query = discord.ui.TextInput(
        label="Search for", placeholder="e.g. ban, xp, reaction role", max_length=60
    )

    def __init__(self, view: HelpView) -> None:
        super().__init__(timeout=120.0)
        self.view_ref = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.view_ref.show_search(interaction, self.query.value.strip())


class CategorySelect(discord.ui.Select):
    """Picks which category to list."""

    def __init__(self, view: HelpView) -> None:
        options = [
            discord.SelectOption(
                label="Overview", value="__home__", description="Every category at once"
            )
        ]
        for name in list(view.categories)[: MAX_SELECT_OPTIONS - 1]:
            options.append(
                discord.SelectOption(
                    label=name,
                    value=name,
                    description=CATEGORY_HELP.get(name, "Commands in this category")[:100],
                )
            )
        super().__init__(placeholder="Pick a category", options=options, row=0)
        self.view_ref = view

    async def callback(self, interaction: discord.Interaction) -> None:
        self.view_ref.selected = self.values[0]
        await self.view_ref.refresh(interaction)


class HelpView(BaseView):
    """Category browser with search.

    Only shows commands the invoker can actually run, resolved once when the
    view is built. A help page listing commands that answer "you may not use
    that" is worse than no help page.
    """

    def __init__(
        self,
        author: discord.abc.User,
        categories: dict[str, list[commands.Command]],
        prefix: str,
    ) -> None:
        super().__init__(author, timeout=PANEL_TIMEOUT)
        self.categories = categories
        self.prefix = prefix
        self.selected = "__home__"
        self.add_item(CategorySelect(self))

    def embed(self) -> discord.Embed:
        if self.selected == "__home__":
            return self._home_embed()
        return self._category_embed(self.selected)

    def _home_embed(self) -> discord.Embed:
        total = sum(len(v) for v in self.categories.values())
        embed = discord.Embed(
            title="Deezee — command help",
            description=(
                f"**{total}** command(s) you can run, in "
                f"**{len(self.categories)}** categories.\n"
                f"Prefix: `{self.prefix}` — mentioning me always works too.\n\n"
                f"`{self.prefix}help <command>` for detail on one.\n"
                "Pick a category below, or press Search."
            ),
            colour=COLOUR_DEFAULT,
        )
        for name, cmds in list(self.categories.items())[:MAX_EMBED_FIELDS]:
            embed.add_field(
                name=f"{name} ({len(cmds)})",
                value=CATEGORY_HELP.get(name, "​"),
                inline=True,
            )
        return embed

    def _category_embed(self, name: str) -> discord.Embed:
        cmds = self.categories.get(name, [])
        embed = discord.Embed(
            title=f"{name} — {len(cmds)} command(s)",
            description=CATEGORY_HELP.get(name, ""),
            colour=COLOUR_DEFAULT,
        )
        lines = []
        for command in cmds:
            summary = (command.short_doc or "").strip() or "No description."
            lines.append(f"`{self.prefix}{command.qualified_name}` — {summary}")

        # Split across fields rather than one description, so a long category
        # cannot blow the 4096-character description limit.
        chunk: list[str] = []
        length = 0
        part = 1
        for line in lines:
            if length + len(line) + 1 > 1000:
                embed.add_field(
                    name="​" if part > 1 else "Commands",
                    value="\n".join(chunk),
                    inline=False,
                )
                chunk, length, part = [], 0, part + 1
                if len(embed.fields) >= MAX_EMBED_FIELDS - 1:
                    break
            chunk.append(line)
            length += len(line) + 1
        if chunk and len(embed.fields) < MAX_EMBED_FIELDS:
            embed.add_field(
                name="​" if part > 1 else "Commands",
                value="\n".join(chunk),
                inline=False,
            )
        return embed

    async def refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def show_search(self, interaction: discord.Interaction, query: str) -> None:
        """Render search results as an ephemeral list."""
        needle = query.lower()
        hits: list[commands.Command] = []
        for cmds in self.categories.values():
            for command in cmds:
                haystack = " ".join(
                    [command.qualified_name, *command.aliases, command.short_doc or ""]
                ).lower()
                if needle in haystack:
                    hits.append(command)

        if not hits:
            await interaction.response.send_message(
                embed=discord.Embed(
                    description=f"Nothing matches `{query}`.", colour=COLOUR_ERROR
                ),
                ephemeral=True,
            )
            return

        lines = [
            f"`{self.prefix}{c.qualified_name}` — {(c.short_doc or '').strip()}"
            for c in hits[:20]
        ]
        embed = discord.Embed(
            title=f"{len(hits)} match(es) for “{query}”",
            description="\n".join(lines),
            colour=COLOUR_DEFAULT,
        )
        if len(hits) > 20:
            embed.set_footer(text=f"Showing 20 of {len(hits)}. Narrow the search.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Search", style=discord.ButtonStyle.primary, row=1)
    async def search(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        """Open the search box."""
        await interaction.response.send_modal(HelpSearchModal(self))

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary, row=1)
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Close the help browser."""
        self.disable_all()
        await interaction.response.edit_message(view=self)
        self.stop()


# ---------------------------------------------------------------------------
# Embed builder
# ---------------------------------------------------------------------------


class EmbedTextModal(discord.ui.Modal, title="Embed text"):
    """Title, description, colour, footer."""

    heading = discord.ui.TextInput(label="Title", max_length=256, required=False)
    body = discord.ui.TextInput(
        label="Description", style=discord.TextStyle.paragraph,
        max_length=4000, required=False,
    )
    colour = discord.ui.TextInput(
        label="Colour (hex, name, or random)", max_length=20, required=False
    )
    footer = discord.ui.TextInput(label="Footer", max_length=2048, required=False)
    image = discord.ui.TextInput(
        label="Image URL (https only)", max_length=500, required=False
    )

    def __init__(self, builder: EmbedBuilder) -> None:
        super().__init__(timeout=600.0)
        self.builder = builder
        embed = builder.embed
        self.heading.default = embed.title or ""
        self.body.default = embed.description or ""
        self.colour.default = str(embed.colour) if embed.colour else ""
        self.footer.default = embed.footer.text or ""
        self.image.default = embed.image.url or ""

    async def on_submit(self, interaction: discord.Interaction) -> None:
        embed = self.builder.embed
        embed.title = self.heading.value.strip() or None
        embed.description = self.body.value.strip() or None

        raw_colour = self.colour.value.strip()
        if raw_colour:
            parsed = self.builder.parse_colour(raw_colour)
            if parsed is None:
                await interaction.response.send_message(
                    f"`{raw_colour}` is not a colour.", ephemeral=True
                )
                return
            embed.colour = parsed

        footer = self.footer.value.strip()
        embed.set_footer(text=footer or None)

        url = self.image.value.strip()
        if url and not url.lower().startswith("https://"):
            # An http:// image would be silently dropped by Discord, and a
            # non-URL string is a 400 on send.
            await interaction.response.send_message(
                "The image URL must start with `https://`.", ephemeral=True
            )
            return
        embed.set_image(url=url or None)

        await self.builder.refresh(interaction)


class EmbedFieldModal(discord.ui.Modal, title="Add a field"):
    """One name/value/inline triple."""

    name = discord.ui.TextInput(label="Field name", max_length=256)
    value = discord.ui.TextInput(
        label="Field value", style=discord.TextStyle.paragraph, max_length=1024
    )
    inline = discord.ui.TextInput(
        label="Inline? yes or no", max_length=3, default="yes", required=False
    )

    def __init__(self, builder: EmbedBuilder) -> None:
        super().__init__(timeout=600.0)
        self.builder = builder

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if len(self.builder.embed.fields) >= MAX_EMBED_FIELDS:
            await interaction.response.send_message(
                f"An embed holds at most {MAX_EMBED_FIELDS} fields.", ephemeral=True
            )
            return
        self.builder.embed.add_field(
            name=self.name.value,
            value=self.value.value,
            inline=self.inline.value.strip().lower() not in {"no", "n", "false", "0"},
        )
        await self.builder.refresh(interaction)


class EmbedBuilder(BaseView):
    """Guided rich-embed composer with a live preview.

    The preview is the real embed, edited in place. There is no separate mock-up
    to drift out of sync with what will actually be posted.
    """

    def __init__(
        self,
        cog: Any,
        author: discord.abc.User,
        channel: discord.TextChannel,
        embed: discord.Embed | None = None,
        target: discord.Message | None = None,
    ) -> None:
        super().__init__(author, timeout=PANEL_TIMEOUT)
        self.cog = cog
        self.channel = channel
        self.embed = embed or discord.Embed(
            description="Press **Text** to start writing.", colour=COLOUR_DEFAULT
        )
        #: Set when editing an existing bot message rather than posting a new one.
        self.target = target
        self.posted: discord.Message | None = None
        if target is not None:
            self.post.label = "Save changes"

    @staticmethod
    def parse_colour(text: str) -> discord.Colour | None:
        """Delegate to the roles cog's parser so both accept the same words."""
        from cogs.roles import parse_colour

        return parse_colour(text)

    def preview(self) -> discord.Embed:
        return self.embed

    def status(self) -> str:
        fields = len(self.embed.fields)
        where = self.channel.mention
        if self.target is not None:
            return f"Editing [an existing message]({self.target.jump_url}) • {fields} field(s)"
        return f"Will post to {where} • {fields} field(s)"

    async def refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            content=self.status(), embed=self.embed, view=self
        )

    @discord.ui.button(label="Text", style=discord.ButtonStyle.primary, row=0)
    async def text(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Title, description, colour, footer and image."""
        await interaction.response.send_modal(EmbedTextModal(self))

    @discord.ui.button(label="Add field", style=discord.ButtonStyle.primary, row=0)
    async def add_field(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        """Append one field."""
        await interaction.response.send_modal(EmbedFieldModal(self))

    @discord.ui.button(label="Clear fields", style=discord.ButtonStyle.secondary, row=0)
    async def clear_fields(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        """Remove every field, keeping the rest."""
        self.embed.clear_fields()
        await self.refresh(interaction)

    @discord.ui.button(label="Post", style=discord.ButtonStyle.success, row=1)
    async def post(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Send the embed, or save it over the message being edited."""
        if not (self.embed.title or self.embed.description or self.embed.fields):
            await interaction.response.send_message(
                "The embed is empty. Add a title, a description or a field first.",
                ephemeral=True,
            )
            return

        try:
            if self.target is not None:
                await self.target.edit(embed=self.embed)
                self.posted = self.target
            else:
                self.posted = await self.channel.send(embed=self.embed)
        except discord.Forbidden:
            await interaction.response.send_message(
                f"I cannot post in {self.channel.mention}.", ephemeral=True
            )
            return
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                f"Discord refused that embed: {exc}", ephemeral=True
            )
            return

        self.disable_all()
        await interaction.response.edit_message(
            content=f"Posted: {self.posted.jump_url}", embed=self.embed, view=self
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        """Abandon the draft. Nothing is posted."""
        self.disable_all()
        await interaction.response.edit_message(
            content="Cancelled -- nothing was posted.", embed=None, view=self
        )
        self.stop()
