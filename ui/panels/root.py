"""The ``?config`` root panel.

The entry point to every setting in the bot. One button per category; each opens
the panel that category's cog already owns, so there is exactly one
implementation of each settings screen and this file only has to know how to
reach them.

If a cog failed to load, its button says so rather than silently disappearing --
a missing button is indistinguishable from a feature that does not exist.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

import discord

from core.constants import (
    COLOUR_DEFAULT,
    COLOUR_SUCCESS,
    EMOJI_OFF,
    EMOJI_ON,
    PANEL_TIMEOUT,
)

from ..views import BaseView

if TYPE_CHECKING:
    from discord.ext import commands

log = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class Category:
    """One button on the root panel.

    Attributes:
        label: Button text.
        cog: Cog that owns the panel, by class name.
        opener: Name of the module-level ``open_*`` function in that cog's panel
            module, or the name of a command to point at when there is no panel.
        summary: One line for the embed.
        indicator: Config key whose truthiness shows as on/off, if any.
    """

    label: str
    cog: str
    summary: str
    indicator: str | None = None
    hint: str = ""


CATEGORIES: tuple[Category, ...] = (
    Category("Moderation", "Moderation",
             "Mute role, DM-on-punish, protected roles",
             "mute_role_id", "?modroles • ?protectedroles • ?config"),
    Category("Automod", "Automod",
             "13 filters: words, links, spam, caps, invites", None, "?automod"),
    Category("Anti-raid", "AntiRaid",
             "Raid detection, verification gate, alt heuristics",
             "raid_mode", "?antiraid • ?verification • ?altconfig"),
    Category("Roles", "Roles",
             "Reaction-role menus, autoroles, sticky roles, self-roles",
             None, "?reactionrole • ?autorole • ?selfroles"),
    Category("Logging", "Logging",
             "A channel per event group, plus per-event toggles",
             "log_messages_channel_id", "?logging"),
    Category("Leveling", "Leveling",
             "XP rates, announcements, level roles, voice XP",
             "xp_enabled", "?levelconfig"),
    Category("Economy", "Economy",
             "Currency, daily and work rewards, shop, gambling",
             "economy_enabled", "?ecoadmin • ?shopconfig"),
    Category("Welcome", "ServerConfig",
             "Join and leave messages", "welcome_channel_id",
             "?welcome • ?goodbye"),
    Category("Starboard", "Utility",
             "Star emoji, threshold, blacklist", "starboard_channel_id",
             "?starboard"),
    Category("Tags", "Tags",
             "Tags, custom commands, autoresponses", None,
             "?tag • ?customcommand • ?autoresponse"),
    Category("Import", "Importer",
             "Migrate off Dyno, Carl-bot, Sapphire, MEE6, Lawliet",
             None, "?import"),
)


class CategoryButton(discord.ui.Button):
    """Opens one category's panel, or explains how to reach it."""

    def __init__(self, panel: ConfigRoot, category: Category, row: int) -> None:
        cog = panel.bot.get_cog(category.cog)
        available = cog is not None
        opener: Callable[..., Any] | None = (
            getattr(cog, "open_config_panel", None) if cog else None
        )

        super().__init__(
            label=category.label,
            style=discord.ButtonStyle.primary if available else discord.ButtonStyle.secondary,
            disabled=not available,
            row=row,
        )
        self.panel = panel
        self.category = category
        self.opener = opener

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.opener is None:
            # Every category has commands even when it has no dedicated panel.
            # Naming them is more useful than a disabled button.
            await interaction.response.send_message(
                embed=discord.Embed(
                    title=self.category.label,
                    description=(
                        f"{self.category.summary}\n\n"
                        f"This category is configured with its own commands:\n"
                        f"**{self.category.hint}**"
                    ),
                    colour=COLOUR_DEFAULT,
                ),
                ephemeral=True,
            )
            return

        await self.opener(interaction)


class ConfigRoot(BaseView):
    """Category buttons, with a live on/off indicator per category."""

    def __init__(self, bot: Any, author: discord.abc.User, config: dict[str, Any]) -> None:
        super().__init__(author, timeout=PANEL_TIMEOUT)
        self.bot = bot
        self.config = config

        # clear_items() first: the @discord.ui.button decorator has already added
        # `close` during View.__init__, and adding it again produces two buttons
        # with the same custom_id -- which Discord rejects with a 400 at send
        # time, not at construction time. Every other panel clears for the same
        # reason.
        self.clear_items()
        for index, category in enumerate(CATEGORIES):
            self.add_item(CategoryButton(self, category, row=index // 4))
        self.add_item(self.close)

    def embed(self, guild: discord.Guild) -> discord.Embed:
        lines = []
        for category in CATEGORIES:
            loaded = self.bot.get_cog(category.cog) is not None
            if category.indicator is None:
                state = "•"
            elif self.config.get(category.indicator):
                state = EMOJI_ON
            else:
                state = EMOJI_OFF
            note = "" if loaded else "  *(not loaded)*"
            lines.append(f"{state} **{category.label}** — {category.summary}{note}")

        embed = discord.Embed(
            title=f"Configuration — {guild.name}",
            description=(
                "Everything this bot does is configured from here. "
                "There is no web dashboard, on purpose.\n\n" + "\n".join(lines)
            ),
            colour=COLOUR_SUCCESS,
        )
        embed.set_footer(
            text=f"Prefix: {self.config.get('prefix', '?')} • "
            f"this panel is yours alone and expires in {PANEL_TIMEOUT:.0f}s"
        )
        return embed

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, row=4)
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Close the root panel."""
        self.disable_all()
        await interaction.response.edit_message(view=self)
        self.stop()


async def open_root(ctx: commands.Context) -> None:
    """Send the root config panel."""
    config = await ctx.bot.guild_config.get(ctx.guild.id)
    view = ConfigRoot(ctx.bot, ctx.author, config)
    view.message = await ctx.send(embed=view.embed(ctx.guild), view=view)
