"""Join and leave message panels.

One class serves both. Welcome and goodbye differ only in which columns they
write and whether a DM copy is offered, and two near-identical panels would drift
apart the first time one of them was fixed.
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

#: Variables the message text understands. Deliberately a fixed list rather than
#: a scripting language -- see DESIGN.md decision 4.
VARIABLES = (
    ("{user}", "Their name"),
    ("{mention}", "A ping"),
    ("{tag}", "name#0000 style tag"),
    ("{id}", "Their user ID"),
    ("{server}", "Server name"),
    ("{count}", "Member count after the change"),
)


class MessageModal(discord.ui.Modal):
    """The message body, the DM copy and the delete-after timer."""

    def __init__(self, panel: GreetingPanel) -> None:
        super().__init__(title=f"{panel.label} message", timeout=600.0)
        self.panel = panel

        self.body = discord.ui.TextInput(
            label="Message",
            style=discord.TextStyle.paragraph,
            max_length=1800,
            default=panel.config[panel.key("message")],
            required=True,
        )
        self.add_item(self.body)

        self.delete_after = discord.ui.TextInput(
            label="Delete after (seconds, 0 to keep)",
            max_length=5,
            default=str(panel.config[panel.key("delete_after")]),
            required=False,
        )
        self.add_item(self.delete_after)

        self.dm: discord.ui.TextInput | None = None
        if panel.kind == "welcome":
            self.dm = discord.ui.TextInput(
                label="DM copy (blank to send none)",
                style=discord.TextStyle.paragraph,
                max_length=1800,
                default=panel.config["welcome_dm_message"],
                required=False,
            )
            self.add_item(self.dm)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.delete_after.value.strip() or "0"
        if not raw.isdigit():
            await interaction.response.send_message(
                f"`{raw}` is not a number of seconds.", ephemeral=True
            )
            return

        values: dict[str, Any] = {
            self.panel.key("message"): self.body.value.strip(),
            self.panel.key("delete_after"): min(int(raw), 3600),
        }
        if self.dm is not None:
            text = self.dm.value.strip()
            values["welcome_dm_message"] = text
            values["welcome_dm"] = int(bool(text))

        await self.panel.cog.bot.guild_config.set_many(interaction.guild.id, values)
        await self.panel.refresh(interaction)


class GreetingChannelSelect(discord.ui.ChannelSelect):
    """Where the greeting is posted. Clearing it switches the feature off."""

    def __init__(self, panel: GreetingPanel) -> None:
        super().__init__(
            placeholder=f"Channel for {panel.label.lower()} messages",
            channel_types=[discord.ChannelType.text],
            min_values=0,
            max_values=1,
            row=0,
        )
        self.panel = panel

    async def callback(self, interaction: discord.Interaction) -> None:
        channel_id = self.values[0].id if self.values else None
        await self.panel.cog.bot.guild_config.set(
            interaction.guild.id, self.panel.key("channel_id"), channel_id
        )
        await self.panel.refresh(interaction)


class GreetingPanel(BaseView):
    """Panel for either the join message or the leave message."""

    def __init__(
        self, cog: Any, author: discord.abc.User, config: dict[str, Any], kind: str
    ) -> None:
        super().__init__(author, timeout=PANEL_TIMEOUT)
        self.cog = cog
        self.config = config
        self.kind = kind
        self.label = "Welcome" if kind == "welcome" else "Goodbye"
        self._build()

    def key(self, suffix: str) -> str:
        """Config column for this panel's kind."""
        return f"{self.kind}_{suffix}"

    def _build(self) -> None:
        self.clear_items()
        self.add_item(GreetingChannelSelect(self))
        self.add_item(self.edit)
        self.add_item(self.toggle_embed)
        self.add_item(self.test)
        self.add_item(self.close)

        as_embed = bool(self.config[self.key("embed")])
        self.toggle_embed.label = "Style: embed" if as_embed else "Style: plain text"

        enabled = bool(self.config[self.key("channel_id")])
        self.test.disabled = not enabled

    def embed(self, guild: discord.Guild) -> discord.Embed:
        channel_id = self.config[self.key("channel_id")]
        channel = guild.get_channel(int(channel_id)) if channel_id else None

        embed = discord.Embed(
            title=f"{self.label} message",
            description=(
                f"{EMOJI_ON if channel else EMOJI_OFF} "
                + (
                    f"Posting to {channel.mention}."
                    if channel
                    else "**Off.** Pick a channel below to switch it on."
                )
            ),
            colour=COLOUR_SUCCESS if channel else COLOUR_DEFAULT,
        )
        embed.add_field(
            name="Message",
            value=f"```{self.config[self.key('message')][:900]}```",
            inline=False,
        )
        embed.add_field(
            name="Style",
            value="Embed" if self.config[self.key("embed")] else "Plain text",
            inline=True,
        )
        delete_after = self.config[self.key("delete_after")]
        embed.add_field(
            name="Auto-delete",
            value=f"After {delete_after}s" if delete_after else "Never",
            inline=True,
        )
        if self.kind == "welcome":
            embed.add_field(
                name="DM copy",
                value="Sent" if self.config["welcome_dm"] else "None",
                inline=True,
            )

        embed.add_field(
            name="Variables",
            value="  ".join(f"`{name}`" for name, _ in VARIABLES),
            inline=False,
        )
        embed.set_footer(text=f"{guild.name} • expires in {PANEL_TIMEOUT:.0f}s")
        return embed

    async def refresh(self, interaction: discord.Interaction) -> None:
        self.config = await self.cog.bot.guild_config.get(interaction.guild.id)
        self._build()
        await interaction.response.edit_message(
            embed=self.embed(interaction.guild), view=self
        )

    @discord.ui.button(label="Edit message", style=discord.ButtonStyle.primary, row=1)
    async def edit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Open the text editor."""
        await interaction.response.send_modal(MessageModal(self))

    @discord.ui.button(label="Style: plain text", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_embed(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        """Switch between a plain message and an embed."""
        await self.cog.bot.guild_config.set(
            interaction.guild.id,
            self.key("embed"),
            0 if self.config[self.key("embed")] else 1,
        )
        await self.refresh(interaction)

    @discord.ui.button(label="Test", style=discord.ButtonStyle.success, row=1)
    async def test(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Post a preview using the presser as the fake joiner."""
        await interaction.response.defer(ephemeral=True)
        try:
            await self.cog.send_greeting(interaction.user, self.kind, test=True)
        except Exception as exc:
            await interaction.followup.send(f"The test failed: {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            "Posted a test greeting using you as the joiner.", ephemeral=True
        )

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary, row=1)
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Close the panel."""
        self.disable_all()
        await interaction.response.edit_message(view=self)
        self.stop()


async def open_greeting(ctx: commands.Context, cog: Any, kind: str) -> None:
    """Send the welcome or goodbye panel."""
    config = await cog.bot.guild_config.get(ctx.guild.id)
    view = GreetingPanel(cog, ctx.author, config, kind)
    view.message = await ctx.send(embed=view.embed(ctx.guild), view=view)
