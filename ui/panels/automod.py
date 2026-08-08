"""The ``?automod`` control panel.

Replaces the web dashboard for automod: every filter's state, punishment and
thresholds are visible in one embed and changeable with buttons, a select menu
and modals. A moderator never opens a browser.
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

#: Punishments a filter can apply, in ascending severity.
ACTIONS = ("delete", "warn", "mute", "kick", "ban")


class ThresholdModal(discord.ui.Modal):
    """Numeric threshold editor for one filter.

    Built dynamically because each filter has different knobs -- caps takes a
    percentage and a minimum length, spam takes a count and a window.
    """

    def __init__(self, panel: AutomodPanel, name: str, spec: Any) -> None:
        super().__init__(title=f"{spec.label} thresholds", timeout=300.0)
        self.panel = panel
        self.name = name
        self.spec = spec
        self.inputs: list[discord.ui.TextInput] = []

        row = panel.filters.get(name, {})
        for index, (attr, label, placeholder) in enumerate(spec.params, start=1):
            field = discord.ui.TextInput(
                label=label,
                placeholder=placeholder,
                default=str(row.get(attr, 0)),
                required=True,
                max_length=6,
            )
            self.inputs.append(field)
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        values: dict[str, int] = {}
        for field, (attr, label, _) in zip(self.inputs, self.spec.params):
            raw = field.value.strip()
            if not raw.isdigit():
                await interaction.response.send_message(
                    f"`{raw}` is not a number. {label} must be a whole number.",
                    ephemeral=True,
                )
                return
            values[attr] = int(raw)

        await self.panel.cog.set_filter_params(interaction.guild.id, self.name, values)
        await self.panel.refresh(interaction)


class FilterSelect(discord.ui.Select):
    """Picks which filter the buttons below act on."""

    def __init__(self, panel: AutomodPanel) -> None:
        options = [
            discord.SelectOption(
                label=spec.label,
                value=name,
                description=spec.summary[:100],
                emoji=EMOJI_ON if panel.filters.get(name, {}).get("enabled") else EMOJI_OFF,
                default=name == panel.selected,
            )
            for name, spec in panel.specs.items()
        ]
        super().__init__(
            placeholder="Choose a filter to configure",
            options=options,
            row=0,
        )
        self.panel = panel

    async def callback(self, interaction: discord.Interaction) -> None:
        self.panel.selected = self.values[0]
        await self.panel.refresh(interaction)


class ActionSelect(discord.ui.Select):
    """Sets the punishment for the selected filter."""

    def __init__(self, panel: AutomodPanel) -> None:
        current = panel.filters.get(panel.selected, {}).get("action", "delete")
        super().__init__(
            placeholder="Punishment when this filter trips",
            options=[
                discord.SelectOption(
                    label=action.title(),
                    value=action,
                    description=_ACTION_HELP[action],
                    default=action == current,
                )
                for action in ACTIONS
            ],
            row=1,
        )
        self.panel = panel

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.panel.cog.set_filter_action(
            interaction.guild.id, self.panel.selected, self.values[0]
        )
        await self.panel.refresh(interaction)


_ACTION_HELP = {
    "delete": "Remove the message only",
    "warn": "Delete and issue a warning (feeds the ladder)",
    "mute": "Delete and time the member out",
    "kick": "Delete and remove the member",
    "ban": "Delete and ban the member",
}


class AutomodPanel(BaseView):
    """The root automod panel.

    Holds its own copy of the filter rows so a re-render costs no queries; the
    cog refreshes them after every write.
    """

    def __init__(
        self,
        cog: Any,
        author: discord.abc.User,
        specs: dict[str, Any],
        filters: dict[str, dict[str, Any]],
        *,
        selected: str | None = None,
    ) -> None:
        super().__init__(author, timeout=PANEL_TIMEOUT)
        self.cog = cog
        self.specs = specs
        self.filters = filters
        self.selected = selected or next(iter(specs))
        self._build()

    def _build(self) -> None:
        """Rebuild every component for the current selection."""
        self.clear_items()
        self.add_item(FilterSelect(self))
        self.add_item(ActionSelect(self))
        self.add_item(self.toggle)
        self.add_item(self.thresholds)
        self.add_item(self.close)

        row = self.filters.get(self.selected, {})
        enabled = bool(row.get("enabled"))
        self.toggle.label = "Disable" if enabled else "Enable"
        self.toggle.style = (
            discord.ButtonStyle.danger if enabled else discord.ButtonStyle.success
        )

        # A filter with no numeric knobs has nothing for the modal to edit.
        self.thresholds.disabled = not self.specs[self.selected].params

    def embed(self, guild: discord.Guild) -> discord.Embed:
        """Render current state: every filter's status, then the selection."""
        spec = self.specs[self.selected]
        row = self.filters.get(self.selected, {})

        lines = []
        for name, other in self.specs.items():
            state = EMOJI_ON if self.filters.get(name, {}).get("enabled") else EMOJI_OFF
            marker = "**" if name == self.selected else ""
            lines.append(f"{state} {marker}{other.label}{marker}")

        embed = discord.Embed(
            title="Automod",
            description="\n".join(lines),
            colour=COLOUR_SUCCESS
            if any(f.get("enabled") for f in self.filters.values())
            else COLOUR_DEFAULT,
        )

        detail = [f"**{spec.label}** — {spec.summary}"]
        detail.append(
            f"Status: {'enabled' if row.get('enabled') else 'disabled'} • "
            f"Punishment: {row.get('action', 'delete')}"
        )
        for attr, label, _ in spec.params:
            detail.append(f"{label}: **{row.get(attr, 0)}**")
        if spec.modes:
            detail.append(f"Mode: **{row.get('mode') or spec.modes[0]}**")

        embed.add_field(name="Selected", value="\n".join(detail), inline=False)
        embed.set_footer(
            text=f"{guild.name} • this panel is yours alone and expires in "
            f"{PANEL_TIMEOUT:.0f}s"
        )
        return embed

    async def refresh(self, interaction: discord.Interaction) -> None:
        """Reload from the database and redraw in place."""
        self.filters = await self.cog.load_filters(interaction.guild.id)
        self._build()
        await interaction.response.edit_message(
            embed=self.embed(interaction.guild), view=self
        )

    @discord.ui.button(label="Enable", style=discord.ButtonStyle.success, row=2)
    async def toggle(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        """Turn the selected filter on or off."""
        enabled = bool(self.filters.get(self.selected, {}).get("enabled"))
        await self.cog.set_filter_enabled(
            interaction.guild.id, self.selected, not enabled
        )
        await self.refresh(interaction)

    @discord.ui.button(label="Thresholds", style=discord.ButtonStyle.primary, row=2)
    async def thresholds(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        """Open the numeric editor for the selected filter."""
        await interaction.response.send_modal(
            ThresholdModal(self, self.selected, self.specs[self.selected])
        )

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary, row=2)
    async def close(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        """Close the panel, leaving the current state visible."""
        self.disable_all()
        await interaction.response.edit_message(view=self)
        self.stop()


async def open_panel(ctx: commands.Context, cog: Any) -> None:
    """Send the automod panel for this guild."""
    filters = await cog.load_filters(ctx.guild.id)
    view = AutomodPanel(cog, ctx.author, cog.specs, filters)
    view.message = await ctx.send(embed=view.embed(ctx.guild), view=view)
