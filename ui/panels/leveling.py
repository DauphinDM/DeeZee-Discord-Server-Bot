"""The ``?levelconfig`` panel.

Every leveling setting in one embed: rates, cooldown, multiplier, where
level-ups are announced and what they say, whether level roles stack, and voice
XP. Nothing here needs a browser.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

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

#: Where a level-up is announced.
ANNOUNCE_MODES: dict[str, str] = {
    "current": "In the channel they were talking in",
    "channel": "Always in one chosen channel",
    "dm": "Direct message to the member",
    "off": "Do not announce level-ups at all",
}


class RatesModal(discord.ui.Modal, title="XP rates"):
    """The four numbers that decide how fast XP accrues."""

    minimum = discord.ui.TextInput(label="Minimum XP per message", max_length=5)
    maximum = discord.ui.TextInput(label="Maximum XP per message", max_length=5)
    cooldown = discord.ui.TextInput(
        label="Seconds between awards, per member", max_length=5
    )
    multiplier = discord.ui.TextInput(
        label="Multiplier (1 = normal, 2 = double XP)", max_length=3
    )
    voice_rate = discord.ui.TextInput(
        label="Voice XP per minute", max_length=4, required=False
    )

    def __init__(self, panel: LevelingPanel) -> None:
        super().__init__(timeout=300.0)
        self.panel = panel
        config = panel.config
        self.minimum.default = str(config["xp_min"])
        self.maximum.default = str(config["xp_max"])
        self.cooldown.default = str(config["xp_cooldown"])
        self.multiplier.default = str(config["xp_multiplier"])
        self.voice_rate.default = str(config["voice_xp_rate"])

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = {
            "xp_min": self.minimum.value.strip(),
            "xp_max": self.maximum.value.strip(),
            "xp_cooldown": self.cooldown.value.strip(),
            "xp_multiplier": self.multiplier.value.strip(),
            "voice_xp_rate": self.voice_rate.value.strip() or "0",
        }
        values: dict[str, int] = {}
        for key, text in raw.items():
            if not text.isdigit():
                await interaction.response.send_message(
                    f"`{text}` is not a whole number.", ephemeral=True
                )
                return
            values[key] = int(text)

        if values["xp_min"] > values["xp_max"]:
            await interaction.response.send_message(
                "The minimum cannot be above the maximum.", ephemeral=True
            )
            return
        if values["xp_multiplier"] < 1:
            await interaction.response.send_message(
                "The multiplier must be at least 1. Use the toggle to turn XP off.",
                ephemeral=True,
            )
            return
        # A cooldown of zero would write a row per message. That is the CPU and
        # storage budget gone, so it is refused rather than clamped silently.
        if values["xp_cooldown"] < 5:
            await interaction.response.send_message(
                "The cooldown must be at least 5 seconds. Below that, every "
                "message writes to the database.",
                ephemeral=True,
            )
            return

        await self.panel.cog.bot.guild_config.set_many(interaction.guild.id, values)
        await self.panel.refresh(interaction)


class MessageModal(discord.ui.Modal, title="Level-up message"):
    """What the announcement says."""

    body = discord.ui.TextInput(
        label="Message",
        style=discord.TextStyle.paragraph,
        max_length=500,
        placeholder="{mention} reached level **{level}**.",
    )

    def __init__(self, panel: LevelingPanel) -> None:
        super().__init__(timeout=300.0)
        self.panel = panel
        self.body.default = panel.config["level_up_message"]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.panel.cog.bot.guild_config.set(
            interaction.guild.id, "level_up_message", self.body.value.strip()
        )
        await self.panel.refresh(interaction)


class AnnounceModeSelect(discord.ui.Select):
    """Where level-ups go."""

    def __init__(self, panel: LevelingPanel) -> None:
        super().__init__(
            placeholder="Where level-ups are announced",
            row=1,
            options=[
                discord.SelectOption(
                    label=name.title(),
                    value=name,
                    description=help_text,
                    default=name == panel.config["level_up_mode"],
                )
                for name, help_text in ANNOUNCE_MODES.items()
            ],
        )
        self.panel = panel

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.panel.cog.bot.guild_config.set(
            interaction.guild.id, "level_up_mode", self.values[0]
        )
        await self.panel.refresh(interaction)


class AnnounceChannelSelect(discord.ui.ChannelSelect):
    """The fixed announcement channel, used by the ``channel`` mode."""

    def __init__(self, panel: LevelingPanel) -> None:
        super().__init__(
            placeholder="Announcement channel (used by 'channel' mode)",
            channel_types=[discord.ChannelType.text],
            min_values=0,
            max_values=1,
            row=0,
        )
        self.panel = panel

    async def callback(self, interaction: discord.Interaction) -> None:
        channel_id = self.values[0].id if self.values else None
        await self.panel.cog.bot.guild_config.set(
            interaction.guild.id, "level_up_channel_id", channel_id
        )
        await self.panel.refresh(interaction)


class LevelingPanel(BaseView):
    """Root leveling panel."""

    def __init__(self, cog: Any, author: discord.abc.User, config: dict[str, Any],
                 level_roles: list[dict[str, Any]]) -> None:
        super().__init__(author, timeout=PANEL_TIMEOUT)
        self.cog = cog
        self.config = config
        self.level_roles = level_roles
        self._build()

    def _build(self) -> None:
        self.clear_items()
        self.add_item(AnnounceChannelSelect(self))
        self.add_item(AnnounceModeSelect(self))
        self.add_item(self.toggle_xp)
        self.add_item(self.toggle_voice)
        self.add_item(self.toggle_stack)
        self.add_item(self.rates)
        # Named ``announcement`` rather than ``message``: BaseView already owns a
        # ``message`` attribute (the message carrying the view), and a button of
        # that name overwrites it with None the moment the view is constructed.
        self.add_item(self.announcement)
        self.add_item(self.close)

        enabled = bool(self.config["xp_enabled"])
        self.toggle_xp.label = "XP: on" if enabled else "XP: off"
        self.toggle_xp.style = (
            discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary
        )

        voice = bool(self.config["voice_xp_enabled"])
        self.toggle_voice.label = "Voice XP: on" if voice else "Voice XP: off"
        self.toggle_voice.style = (
            discord.ButtonStyle.success if voice else discord.ButtonStyle.secondary
        )

        stack = bool(self.config["level_role_stack"])
        self.toggle_stack.label = "Roles: stack" if stack else "Roles: replace"
        self.toggle_stack.style = discord.ButtonStyle.primary

    def embed(self, guild: discord.Guild) -> discord.Embed:
        config = self.config
        enabled = bool(config["xp_enabled"])

        embed = discord.Embed(
            title="Leveling",
            description=(
                f"{EMOJI_ON if enabled else EMOJI_OFF} XP is "
                f"**{'on' if enabled else 'off'}** in this server."
            ),
            colour=COLOUR_SUCCESS if enabled else COLOUR_DEFAULT,
        )
        embed.add_field(
            name="Rates",
            value=(
                f"**{config['xp_min']}–{config['xp_max']}** XP per message\n"
                f"×**{config['xp_multiplier']}** multiplier\n"
                f"one award per **{config['xp_cooldown']}s** per member"
            ),
            inline=True,
        )

        channel_id = config["level_up_channel_id"]
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        embed.add_field(
            name="Announcements",
            value=(
                f"**{config['level_up_mode']}** — "
                f"{ANNOUNCE_MODES[config['level_up_mode']]}\n"
                f"Channel: {channel.mention if channel else 'not set'}"
            ),
            inline=True,
        )

        embed.add_field(
            name="Voice XP",
            value=(
                f"{EMOJI_ON if config['voice_xp_enabled'] else EMOJI_OFF} "
                f"**{config['voice_xp_rate']}** XP per minute\n"
                "AFK and self-deafened members earn nothing."
            ),
            inline=True,
        )

        embed.add_field(
            name="Message",
            value=f"```{config['level_up_message'][:300]}```"
            "Variables: `{user}` `{mention}` `{level}` `{xp}` `{server}`",
            inline=False,
        )

        if self.level_roles:
            lines = []
            for row in sorted(self.level_roles, key=lambda r: r["level"]):
                role = guild.get_role(row["role_id"])
                name = role.mention if role else "*deleted role*"
                lines.append(f"Level **{row['level']}** → {name}")
            embed.add_field(
                name=f"Level roles ({len(self.level_roles)}) — "
                f"{'stacking' if config['level_role_stack'] else 'replacing'}",
                value="\n".join(lines[:10])[:1024],
                inline=False,
            )
        else:
            embed.add_field(
                name="Level roles",
                value="None. Add one with `?levelrole add <level> <role>`.",
                inline=False,
            )

        embed.set_footer(text=f"{guild.name} • expires in {PANEL_TIMEOUT:.0f}s")
        return embed

    async def refresh(self, interaction: discord.Interaction) -> None:
        self.config = await self.cog.bot.guild_config.get(interaction.guild.id)
        self.level_roles = await self.cog.load_level_roles(interaction.guild.id)
        self._build()
        await interaction.response.edit_message(
            embed=self.embed(interaction.guild), view=self
        )

    @discord.ui.button(label="XP: off", style=discord.ButtonStyle.secondary, row=2)
    async def toggle_xp(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        """Turn message XP on or off."""
        await self.cog.bot.guild_config.set(
            interaction.guild.id, "xp_enabled", 0 if self.config["xp_enabled"] else 1
        )
        await self.refresh(interaction)

    @discord.ui.button(label="Voice XP: off", style=discord.ButtonStyle.secondary, row=2)
    async def toggle_voice(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        """Turn voice XP on or off."""
        await self.cog.bot.guild_config.set(
            interaction.guild.id,
            "voice_xp_enabled",
            0 if self.config["voice_xp_enabled"] else 1,
        )
        await self.refresh(interaction)

    @discord.ui.button(label="Roles: stack", style=discord.ButtonStyle.primary, row=2)
    async def toggle_stack(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        """Switch between keeping every level role and replacing the previous one."""
        await self.cog.bot.guild_config.set(
            interaction.guild.id,
            "level_role_stack",
            0 if self.config["level_role_stack"] else 1,
        )
        await self.refresh(interaction)

    @discord.ui.button(label="Rates", style=discord.ButtonStyle.primary, row=3)
    async def rates(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        """Open the numeric editor."""
        await interaction.response.send_modal(RatesModal(self))

    @discord.ui.button(label="Message", style=discord.ButtonStyle.primary, row=3)
    async def announcement(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        """Edit the level-up announcement text."""
        await interaction.response.send_modal(MessageModal(self))

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary, row=3)
    async def close(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        """Close the panel."""
        self.disable_all()
        await interaction.response.edit_message(view=self)
        self.stop()


async def open_panel(ctx: commands.Context, cog: Any) -> None:
    """Send the leveling panel for this guild."""
    config = await cog.bot.guild_config.get(ctx.guild.id)
    roles = await cog.load_level_roles(ctx.guild.id)
    view = LevelingPanel(cog, ctx.author, config, roles)
    view.message = await ctx.send(embed=view.embed(ctx.guild), view=view)
