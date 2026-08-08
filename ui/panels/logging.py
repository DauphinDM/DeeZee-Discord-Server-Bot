"""The ``?logging`` control panel.

One embed shows every event group, where it posts and which of its events are
switched off; a channel picker and toggle buttons change all of it. This is the
whole logging dashboard, inside Discord.

Groups are the unit of configuration because nine channels is already more than
most servers want, and per-event channels would be ninety.
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


class GroupSelect(discord.ui.Select):
    """Picks which event group the controls below act on."""

    def __init__(self, panel: LoggingPanel) -> None:
        options = []
        for name, group in panel.groups.items():
            channel_id = panel.config.get(f"log_{name}_channel_id")
            options.append(
                discord.SelectOption(
                    label=group.label,
                    value=name,
                    description=group.summary[:100],
                    emoji=EMOJI_ON if channel_id else EMOJI_OFF,
                    default=name == panel.selected,
                )
            )
        super().__init__(placeholder="Choose an event group", options=options, row=0)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction) -> None:
        self.panel.selected = self.values[0]
        await self.panel.refresh(interaction)


class DestinationSelect(discord.ui.ChannelSelect):
    """Sets the destination channel for the selected group."""

    def __init__(self, panel: LoggingPanel) -> None:
        super().__init__(
            placeholder="Where this group posts",
            channel_types=[discord.ChannelType.text],
            min_values=0,
            max_values=1,
            row=1,
        )
        self.panel = panel

    async def callback(self, interaction: discord.Interaction) -> None:
        # An empty selection is how a group gets switched off again, so the
        # zero-value case is meaningful rather than an error.
        channel_id = self.values[0].id if self.values else None
        await self.panel.cog.set_group_channel(
            interaction.guild.id, self.panel.selected, channel_id
        )
        await self.panel.refresh(interaction)


class EventSelect(discord.ui.Select):
    """Turns individual events inside the selected group on or off."""

    def __init__(self, panel: LoggingPanel) -> None:
        group = panel.groups[panel.selected]
        disabled = panel.disabled
        options = [
            discord.SelectOption(
                label=label,
                value=event,
                description="Off -- select to turn on"
                if event in disabled
                else "On -- select to turn off",
                emoji=EMOJI_OFF if event in disabled else EMOJI_ON,
            )
            for event, label in group.events.items()
        ]
        super().__init__(
            placeholder="Toggle an individual event",
            options=options,
            row=2,
            disabled=not panel.config.get(f"log_{panel.selected}_channel_id"),
        )
        self.panel = panel

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.panel.cog.toggle_event(interaction.guild.id, self.values[0])
        await self.panel.refresh(interaction)


class LoggingPanel(BaseView):
    """Root logging panel: group, destination, per-event toggles."""

    def __init__(
        self,
        cog: Any,
        author: discord.abc.User,
        groups: dict[str, Any],
        config: dict[str, Any],
        disabled: set[str],
    ) -> None:
        super().__init__(author, timeout=PANEL_TIMEOUT)
        self.cog = cog
        self.groups = groups
        self.config = config
        self.disabled = disabled
        self.selected = next(iter(groups))
        self._build()

    def _build(self) -> None:
        self.clear_items()
        self.add_item(GroupSelect(self))
        self.add_item(DestinationSelect(self))
        self.add_item(EventSelect(self))
        self.add_item(self.enable_all)
        self.add_item(self.close)

    def embed(self, guild: discord.Guild) -> discord.Embed:
        """Show every group's destination, then the selected group in detail."""
        lines = []
        for name, group in self.groups.items():
            channel_id = self.config.get(f"log_{name}_channel_id")
            channel = guild.get_channel(int(channel_id)) if channel_id else None
            state = EMOJI_ON if channel else EMOJI_OFF
            where = channel.mention if channel else "not set"
            marker = "**" if name == self.selected else ""
            lines.append(f"{state} {marker}{group.label}{marker} — {where}")

        active = sum(
            1 for name in self.groups if self.config.get(f"log_{name}_channel_id")
        )
        embed = discord.Embed(
            title="Logging",
            description="\n".join(lines),
            colour=COLOUR_SUCCESS if active else COLOUR_DEFAULT,
        )

        group = self.groups[self.selected]
        detail = [f"**{group.label}** — {group.summary}", ""]
        for event, label in group.events.items():
            mark = EMOJI_OFF if event in self.disabled else EMOJI_ON
            detail.append(f"{mark} {label}")
        embed.add_field(name="Events in this group", value="\n".join(detail), inline=False)

        embed.set_footer(
            text=f"{guild.name} • {active}/{len(self.groups)} groups active • "
            f"expires in {PANEL_TIMEOUT:.0f}s"
        )
        return embed

    async def refresh(self, interaction: discord.Interaction) -> None:
        """Reload from the database and redraw in place."""
        self.config, self.disabled = await self.cog.load_state(interaction.guild.id)
        self._build()
        await interaction.response.edit_message(
            embed=self.embed(interaction.guild), view=self
        )

    @discord.ui.button(label="Turn every event on", style=discord.ButtonStyle.primary, row=3)
    async def enable_all(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        """Clear every per-event exception for the selected group."""
        await self.cog.enable_all_events(interaction.guild.id, self.selected)
        await self.refresh(interaction)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary, row=3)
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Close the panel."""
        self.disable_all()
        await interaction.response.edit_message(view=self)
        self.stop()


async def open_panel(ctx: commands.Context, cog: Any) -> None:
    """Send the logging panel for this guild."""
    config, disabled = await cog.load_state(ctx.guild.id)
    view = LoggingPanel(cog, ctx.author, cog.GROUPS, config, disabled)
    view.message = await ctx.send(embed=view.embed(ctx.guild), view=view)
