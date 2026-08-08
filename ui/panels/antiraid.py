"""Panels for ``?antiraid``, ``?verification`` and ``?altconfig``.

Three panels rather than one: raid response, the verification gate and the alt
heuristics are configured by different people at different times, and merging
them would produce a panel nobody can read.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import discord

from core.constants import (
    COLOUR_DEFAULT,
    COLOUR_ERROR,
    COLOUR_INFO,
    COLOUR_SUCCESS,
    EMOJI_OFF,
    EMOJI_ON,
    PANEL_TIMEOUT,
)
from services.timeparse import format_duration, parse_duration

from ..views import BaseView

if TYPE_CHECKING:
    from discord.ext import commands

log = logging.getLogger(__name__)


def _seconds_field(value: Any) -> str:
    """Render a stored second-count for display."""
    return format_duration(int(value or 0)) if value else "off"


class NumberModal(discord.ui.Modal):
    """Edits a set of numeric or duration settings on ``guild_config``.

    One modal class covers every panel here because the shape is always the
    same: a handful of named settings, each a number or a duration.
    """

    def __init__(
        self,
        panel: _ConfigPanel,
        title: str,
        fields: list[tuple[str, str, str, bool]],
    ) -> None:
        super().__init__(title=title[:45], timeout=300.0)
        self.panel = panel
        #: ``(setting, label, placeholder, is_duration)``
        self.fields = fields
        self.inputs: list[discord.ui.TextInput] = []

        for setting, label, placeholder, is_duration in fields:
            current = panel.config.get(setting)
            default = (
                format_duration(int(current or 0), brief=True)
                if is_duration and current
                else str(current if current is not None else "")
            )
            field = discord.ui.TextInput(
                label=label[:45],
                placeholder=placeholder,
                default=default,
                required=False,
                max_length=16,
            )
            self.inputs.append(field)
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        updates: dict[str, Any] = {}

        for field, (setting, label, _, is_duration) in zip(self.inputs, self.fields):
            raw = field.value.strip()
            if not raw:
                continue

            if is_duration:
                if raw.lower() in {"off", "none", "0"}:
                    updates[setting] = 0
                    continue
                delta = parse_duration(raw)
                if delta is None:
                    await interaction.response.send_message(
                        f"`{raw}` is not a duration. {label} takes values like "
                        "`30s`, `10m` or `7d`.",
                        ephemeral=True,
                    )
                    return
                updates[setting] = int(delta.total_seconds())
                continue

            if not raw.isdigit():
                await interaction.response.send_message(
                    f"`{raw}` is not a number. {label} must be a whole number.",
                    ephemeral=True,
                )
                return
            updates[setting] = int(raw)

        if updates:
            await self.panel.cog.bot.guild_config.set_many(interaction.guild.id, updates)
        await self.panel.refresh(interaction)


class ChoiceSelect(discord.ui.Select):
    """Sets one text setting from a fixed list of values."""

    def __init__(
        self,
        panel: _ConfigPanel,
        setting: str,
        placeholder: str,
        choices: list[tuple[str, str]],
        row: int,
    ) -> None:
        current = panel.config.get(setting)
        super().__init__(
            placeholder=placeholder,
            row=row,
            options=[
                discord.SelectOption(
                    label=label, value=value, description=description[:100],
                    default=value == current,
                )
                for value, label, description in _expand(choices)
            ],
        )
        self.panel = panel
        self.setting = setting

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.panel.cog.bot.guild_config.set(
            interaction.guild.id, self.setting, self.values[0]
        )
        await self.panel.refresh(interaction)


def _expand(choices: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """Allow choices to be given as ``(value, description)`` pairs."""
    return [(value, value.title(), description) for value, description in choices]


class RolePicker(discord.ui.RoleSelect):
    """Assigns a role to a setting."""

    def __init__(self, panel: _ConfigPanel, setting: str, placeholder: str, row: int) -> None:
        super().__init__(placeholder=placeholder, row=row, min_values=1, max_values=1)
        self.panel = panel
        self.setting = setting

    async def callback(self, interaction: discord.Interaction) -> None:
        role = self.values[0]
        await self.panel.cog.bot.guild_config.set(
            interaction.guild.id, self.setting, role.id
        )
        await self.panel.refresh(interaction)


class ChannelPicker(discord.ui.ChannelSelect):
    """Assigns a text channel to a setting."""

    def __init__(self, panel: _ConfigPanel, setting: str, placeholder: str, row: int) -> None:
        super().__init__(
            placeholder=placeholder,
            row=row,
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
        )
        self.panel = panel
        self.setting = setting

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.panel.cog.bot.guild_config.set(
            interaction.guild.id, self.setting, self.values[0].id
        )
        await self.panel.refresh(interaction)


class _ConfigPanel(BaseView):
    """Shared behaviour for the three panels below."""

    title = "Panel"

    def __init__(self, cog: Any, author: discord.abc.User, config: dict[str, Any]) -> None:
        super().__init__(author, timeout=PANEL_TIMEOUT)
        self.cog = cog
        self.config = config
        self.build()

    def build(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def embed(self, guild: discord.Guild) -> discord.Embed:  # pragma: no cover
        raise NotImplementedError

    async def refresh(self, interaction: discord.Interaction) -> None:
        """Reload settings and redraw the panel in place."""
        self.cog.bot.guild_config.invalidate(interaction.guild.id)
        self.config = await self.cog.bot.guild_config.get(interaction.guild.id)
        self.clear_items()
        self.build()

        if interaction.response.is_done():
            await interaction.edit_original_response(
                embed=self.embed(interaction.guild), view=self
            )
        else:
            await interaction.response.edit_message(
                embed=self.embed(interaction.guild), view=self
            )

    def _role_line(self, guild: discord.Guild, setting: str) -> str:
        value = self.config.get(setting)
        if not value:
            return "not set"
        role = guild.get_role(int(value))
        return role.mention if role else f"missing role `{value}`"

    def _channel_line(self, guild: discord.Guild, setting: str) -> str:
        value = self.config.get(setting)
        if not value:
            return "not set"
        channel = guild.get_channel(int(value))
        return channel.mention if channel else f"missing channel `{value}`"

    def _add_close(self, row: int) -> None:
        button = discord.ui.Button(
            label="Close", style=discord.ButtonStyle.secondary, row=row
        )

        async def close(interaction: discord.Interaction) -> None:
            self.disable_all()
            await interaction.response.edit_message(view=self)
            self.stop()

        button.callback = close
        self.add_item(button)


class AntiRaidPanel(_ConfigPanel):
    """Join-rate thresholds, automatic response and the alert channel."""

    def build(self) -> None:
        self.add_item(
            ChoiceSelect(
                self, "raid_action", "What happens to joins during a raid",
                [
                    ("quarantine", "Strip roles, hold for review"),
                    ("kick", "Remove them; they can rejoin"),
                    ("ban", "Remove them permanently"),
                    ("none", "Log only, take no action"),
                ],
                row=0,
            )
        )
        self.add_item(ChannelPicker(self, "raid_alert_channel_id", "Alert channel", 1))

        toggle = discord.ui.Button(
            label="Raid mode off" if self.config.get("raid_mode") else "Raid mode on",
            style=discord.ButtonStyle.success
            if self.config.get("raid_mode")
            else discord.ButtonStyle.danger,
            row=2,
        )

        async def flip(interaction: discord.Interaction) -> None:
            enabled = not bool(self.config.get("raid_mode"))
            await self.cog.set_raid_mode(
                interaction.guild, enabled, reason=f"Toggled by {interaction.user}"
            )
            await self.refresh(interaction)

        toggle.callback = flip
        self.add_item(toggle)

        thresholds = discord.ui.Button(
            label="Thresholds", style=discord.ButtonStyle.primary, row=2
        )

        async def edit(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(
                NumberModal(
                    self,
                    "Raid thresholds",
                    [
                        ("raid_join_threshold", "Joins to trigger", "10", False),
                        ("raid_join_window", "Within", "60s", True),
                        ("raid_auto_off", "Turn off after quiet", "15m", True),
                    ],
                )
            )

        thresholds.callback = edit
        self.add_item(thresholds)
        self._add_close(2)

    def embed(self, guild: discord.Guild) -> discord.Embed:
        active = bool(self.config.get("raid_mode"))
        embed = discord.Embed(
            title="Anti-raid",
            description=(
                f"{EMOJI_ON if active else EMOJI_OFF} Raid mode is "
                f"**{'ACTIVE' if active else 'off'}**"
            ),
            colour=COLOUR_ERROR if active else COLOUR_DEFAULT,
        )
        threshold = self.config.get("raid_join_threshold") or 0
        embed.add_field(
            name="Trigger",
            value=(
                f"**{threshold}** joins within "
                f"{_seconds_field(self.config.get('raid_join_window'))}"
                if threshold else "automatic detection off"
            ),
            inline=False,
        )
        embed.add_field(
            name="Response", value=f"**{self.config.get('raid_action')}**", inline=True
        )
        embed.add_field(
            name="Auto-off after",
            value=_seconds_field(self.config.get("raid_auto_off")),
            inline=True,
        )
        embed.add_field(
            name="Alerts to",
            value=self._channel_line(guild, "raid_alert_channel_id"),
            inline=True,
        )
        embed.set_footer(text="Falls back to the mod-log if no alert channel is set.")
        return embed


class VerificationPanel(_ConfigPanel):
    """Gate mode, roles, captcha type and timeout."""

    def build(self) -> None:
        self.add_item(
            ChoiceSelect(
                self, "verification_mode", "Verification mode",
                [
                    ("off", "No gate"),
                    ("click", "Button in a channel; members opt in"),
                    ("join", "Applied on join with a holding role"),
                ],
                row=0,
            )
        )
        self.add_item(
            ChoiceSelect(
                self, "captcha_type", "Challenge type",
                [
                    ("image", "Distorted image, generated locally"),
                    ("text", "Arithmetic question, no image"),
                    ("button", "Press the button, no challenge"),
                ],
                row=1,
            )
        )
        self.add_item(RolePicker(self, "verified_role_id", "Verified role", 2))
        self.add_item(RolePicker(self, "unverified_role_id", "Unverified holding role", 3))

        post = discord.ui.Button(
            label="Post the verify button", style=discord.ButtonStyle.success, row=4
        )

        async def post_button(interaction: discord.Interaction) -> None:
            from cogs.antiraid import VerifyStartView

            channel = interaction.channel
            embed = discord.Embed(
                title="Verification required",
                description="Press the button below to gain access to this server.",
                colour=COLOUR_INFO,
            )
            try:
                await channel.send(embed=embed, view=VerifyStartView(self.cog))
            except discord.Forbidden:
                await interaction.response.send_message(
                    "I cannot post in this channel.", ephemeral=True
                )
                return
            await self.cog.bot.guild_config.set(
                interaction.guild.id, "verification_channel_id", channel.id
            )
            await self.refresh(interaction)

        post.callback = post_button
        self.add_item(post)

        limits = discord.ui.Button(label="Timeout", style=discord.ButtonStyle.primary, row=4)

        async def edit(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(
                NumberModal(
                    self,
                    "Verification limits",
                    [
                        ("verification_timeout", "Challenge valid for", "10m", True),
                        ("verification_attempts", "Attempts allowed", "3", False),
                    ],
                )
            )

        limits.callback = edit
        self.add_item(limits)
        self._add_close(4)

    def embed(self, guild: discord.Guild) -> discord.Embed:
        mode = self.config.get("verification_mode")
        embed = discord.Embed(
            title="Verification",
            description=f"Mode: **{mode}**",
            colour=COLOUR_SUCCESS if mode != "off" else COLOUR_DEFAULT,
        )
        embed.add_field(
            name="Challenge", value=f"**{self.config.get('captcha_type')}**", inline=True
        )
        embed.add_field(
            name="Valid for",
            value=_seconds_field(self.config.get("verification_timeout")),
            inline=True,
        )
        embed.add_field(
            name="Attempts",
            value=str(self.config.get("verification_attempts") or 3),
            inline=True,
        )
        embed.add_field(
            name="Verified role", value=self._role_line(guild, "verified_role_id"),
            inline=True,
        )
        embed.add_field(
            name="Unverified role", value=self._role_line(guild, "unverified_role_id"),
            inline=True,
        )
        embed.add_field(
            name="Button posted in",
            value=self._channel_line(guild, "verification_channel_id"),
            inline=True,
        )
        embed.set_footer(
            text="Captchas are generated locally. Nothing is sent to any external service."
        )
        return embed


class AltConfigPanel(_ConfigPanel):
    """Alt-risk thresholds, automatic action and the account-age gate."""

    def build(self) -> None:
        self.add_item(
            ChoiceSelect(
                self, "alt_action", "Action at the action threshold",
                [
                    ("none", "Flag for review only"),
                    ("quarantine", "Strip roles, hold for review"),
                    ("kick", "Remove them; they can rejoin"),
                    ("ban", "Remove them permanently"),
                ],
                row=0,
            )
        )
        self.add_item(
            ChoiceSelect(
                self, "min_age_action", "Action for accounts under the age limit",
                [
                    ("kick", "Remove them; they can rejoin"),
                    ("ban", "Remove them permanently"),
                    ("quarantine", "Strip roles, hold for review"),
                ],
                row=1,
            )
        )
        self.add_item(RolePicker(self, "quarantine_role_id", "Quarantine role", 2))

        thresholds = discord.ui.Button(
            label="Thresholds", style=discord.ButtonStyle.primary, row=3
        )

        async def edit(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(
                NumberModal(
                    self,
                    "Alt risk thresholds",
                    [
                        ("alt_flag_threshold", "Flag at score", "55", False),
                        ("alt_action_threshold", "Act at score", "80", False),
                        ("min_account_age", "Minimum account age", "7d", True),
                    ],
                )
            )

        thresholds.callback = edit
        self.add_item(thresholds)
        self._add_close(3)

    def embed(self, guild: discord.Guild) -> discord.Embed:
        embed = discord.Embed(
            title="Alt-account heuristics",
            description=(
                "Scores are heuristic. Discord never gives bots IP addresses, so "
                "VPNs and shared devices cannot be detected -- these are the "
                "signals a bot can actually see."
            ),
            colour=COLOUR_DEFAULT,
        )
        embed.add_field(
            name="Flag at", value=f"**{self.config.get('alt_flag_threshold')}**/100",
            inline=True,
        )
        embed.add_field(
            name="Act at", value=f"**{self.config.get('alt_action_threshold')}**/100",
            inline=True,
        )
        embed.add_field(
            name="Action", value=f"**{self.config.get('alt_action')}**", inline=True
        )
        embed.add_field(
            name="Minimum account age",
            value=_seconds_field(self.config.get("min_account_age")),
            inline=True,
        )
        embed.add_field(
            name="Too-young action",
            value=f"**{self.config.get('min_age_action')}**", inline=True,
        )
        embed.add_field(
            name="Quarantine role",
            value=self._role_line(guild, "quarantine_role_id"), inline=True,
        )
        return embed


async def _send(ctx: commands.Context, panel: _ConfigPanel) -> None:
    panel.message = await ctx.send(embed=panel.embed(ctx.guild), view=panel)


async def open_panel(ctx: commands.Context, cog: Any) -> None:
    """Send the anti-raid panel."""
    config = await cog.bot.guild_config.get(ctx.guild.id)
    await _send(ctx, AntiRaidPanel(cog, ctx.author, config))


async def open_verification(ctx: commands.Context, cog: Any) -> None:
    """Send the verification panel."""
    config = await cog.bot.guild_config.get(ctx.guild.id)
    await _send(ctx, VerificationPanel(cog, ctx.author, config))


async def open_altconfig(ctx: commands.Context, cog: Any) -> None:
    """Send the alt-heuristics panel."""
    config = await cog.bot.guild_config.get(ctx.guild.id)
    await _send(ctx, AltConfigPanel(cog, ctx.author, config))
