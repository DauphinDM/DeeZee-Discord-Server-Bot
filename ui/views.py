"""Base view behaviour and the confirmation pattern.

Every interactive component in this bot inherits from :class:`BaseView`, which
enforces two rules that are easy to get wrong once per view and painful to fix
later:

* Only the person who ran the command can press the buttons.
* When the view times out, its buttons visibly disable rather than sitting there
  looking live and doing nothing.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands

from core.constants import (
    COLOUR_ERROR,
    COLOUR_MUTED,
    COLOUR_WARNING,
    CONFIRM_TIMEOUT,
    CONFIRM_TIMEOUT_DEV,
    EMOJI_ERROR,
    EMOJI_WARNING,
    PANEL_TIMEOUT,
)

if TYPE_CHECKING:
    from core.bot import DeezeeBot

log = logging.getLogger(__name__)


class BaseView(discord.ui.View):
    """A view locked to one user, which cleans up after itself.

    Attributes:
        author: Who is allowed to interact. ``None`` means anyone may.
        message: The message carrying this view, set by the helpers below so
            :meth:`on_timeout` can edit it.
    """

    def __init__(
        self,
        author: discord.abc.User | None = None,
        *,
        timeout: float | None = PANEL_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)
        self.author = author
        self.message: discord.Message | discord.InteractionMessage | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Reject anyone who did not run the command.

        The refusal is ephemeral: a bystander pressing a button should not
        produce a public message in the channel.
        """
        if self.author is None or interaction.user.id == self.author.id:
            return True

        await interaction.response.send_message(
            embed=discord.Embed(
                description=(
                    f"{EMOJI_ERROR}  These controls belong to {self.author.mention}. "
                    "Run the command yourself to get your own."
                ),
                colour=COLOUR_ERROR,
            ),
            ephemeral=True,
        )
        return False

    def disable_all(self) -> None:
        """Grey out every child component in place."""
        for child in self.children:
            if isinstance(child, (discord.ui.Button, discord.ui.Select)):
                child.disabled = True

    async def on_timeout(self) -> None:
        """Disable the controls so nothing is left looking interactive."""
        self.disable_all()
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            # The message was deleted, or the bot lost access to the channel.
            # Neither is worth reporting; the view is dead either way.
            log.debug("Could not disable timed-out view")

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[Any],
    ) -> None:
        """Report a component failure without leaving the interaction hanging.

        Discord shows "This interaction failed" if nothing responds, which tells
        the user nothing. The traceback goes to the log instead.
        """
        log.exception(
            "Error in %s component of %s",
            type(item).__name__,
            type(self).__name__,
            exc_info=error,
        )
        embed = discord.Embed(
            description=f"{EMOJI_ERROR}  That control failed. It has been logged.",
            colour=COLOUR_ERROR,
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            pass

    async def stop_and_disable(self, interaction: discord.Interaction | None = None) -> None:
        """Finish the view now, disabling its controls."""
        self.disable_all()
        self.stop()
        try:
            if interaction is not None:
                await interaction.response.edit_message(view=self)
            elif self.message is not None:
                await self.message.edit(view=self)
        except discord.HTTPException:
            pass


class ConfirmView(BaseView):
    """Confirm / Cancel, invoker-locked, timeout counts as cancel.

    Used by every destructive command: ban, softban, massban, kick, large purge,
    nuke, lockdown, mass-role, clearwarns, config reset and import apply.

    Attributes:
        value: ``True`` confirmed, ``False`` cancelled, ``None`` timed out.
    """

    def __init__(
        self,
        author: discord.abc.User,
        *,
        timeout: float = CONFIRM_TIMEOUT,
        confirm_label: str = "Confirm",
        cancel_label: str = "Cancel",
        confirm_style: discord.ButtonStyle = discord.ButtonStyle.danger,
    ) -> None:
        super().__init__(author, timeout=timeout)
        self.value: bool | None = None

        # Built in code rather than with decorators so the labels and the style
        # can differ per call site -- "Ban 40 members" reads better than
        # "Confirm", and a non-destructive confirm should not be red.
        confirm = discord.ui.Button(label=confirm_label, style=confirm_style)
        confirm.callback = self._confirm
        self.add_item(confirm)

        cancel = discord.ui.Button(label=cancel_label, style=discord.ButtonStyle.secondary)
        cancel.callback = self._cancel
        self.add_item(cancel)

    async def _confirm(self, interaction: discord.Interaction) -> None:
        self.value = True
        self.disable_all()
        await interaction.response.edit_message(view=self)
        self.stop()

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self.value = False
        self.disable_all()
        embed = discord.Embed(description="Cancelled.", colour=COLOUR_MUTED)
        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()


def confirm_timeout_for(bot: DeezeeBot, guild: discord.Guild | None) -> float:
    """How long a confirmation should wait.

    Shorter in the development guild, where you are testing rather than
    deliberating over whether to ban someone.
    """
    return CONFIRM_TIMEOUT_DEV if bot.is_dev_guild(guild) else CONFIRM_TIMEOUT


async def confirm(
    ctx: commands.Context,
    *,
    title: str,
    description: str,
    confirm_label: str = "Confirm",
    fields: dict[str, str] | None = None,
    danger: bool = True,
) -> bool:
    """Ask for confirmation and return what the user chose.

    The embed must state exactly what will happen and to how many people before
    anyone presses anything -- that is the whole value of the pattern.

    Args:
        ctx: The invoking context. Only its author may answer.
        title: Embed title, e.g. "Ban 40 members?".
        description: What is about to happen, in plain language.
        confirm_label: Text on the confirm button. Say the action, not "Yes".
        fields: Extra detail rows, e.g. reason, duration, affected count.
        danger: Red confirm button and warning colour. Off for reversible things.

    Returns:
        True if confirmed. False if cancelled *or* timed out -- a timeout is
        never treated as consent.
    """
    timeout = confirm_timeout_for(ctx.bot, ctx.guild)

    embed = discord.Embed(
        title=f"{EMOJI_WARNING}  {title}" if danger else title,
        description=description,
        colour=COLOUR_WARNING if danger else COLOUR_MUTED,
    )
    for name, value in (fields or {}).items():
        embed.add_field(name=name, value=value, inline=True)
    embed.set_footer(text=f"This expires in {timeout:.0f} seconds and counts as cancelled.")

    view = ConfirmView(
        ctx.author,
        timeout=timeout,
        confirm_label=confirm_label,
        confirm_style=discord.ButtonStyle.danger if danger else discord.ButtonStyle.success,
    )
    view.message = await ctx.send(embed=embed, view=view)

    await view.wait()

    if view.value is None:
        # Timed out. Say so, rather than leaving a dead embed on screen.
        embed.colour = COLOUR_MUTED
        embed.set_footer(text="Timed out -- nothing was done.")
        try:
            await view.message.edit(embed=embed, view=view)
        except discord.HTTPException:
            pass
        return False

    return view.value


async def send_view(
    ctx: commands.Context,
    view: BaseView,
    *,
    embed: discord.Embed | None = None,
    content: str | None = None,
    ephemeral: bool = False,
) -> discord.Message:
    """Send a view and record its message on it.

    Always use this rather than ``ctx.send(view=...)``: without
    ``view.message`` set, the view cannot disable its own buttons on timeout.
    """
    message = await ctx.send(content=content, embed=embed, view=view, ephemeral=ephemeral)
    view.message = message
    return message


async def respond_view(
    interaction: discord.Interaction,
    view: BaseView,
    *,
    embed: discord.Embed | None = None,
    content: str | None = None,
    ephemeral: bool = True,
) -> None:
    """Same as :func:`send_view`, for an interaction rather than a context."""
    if interaction.response.is_done():
        message = await interaction.followup.send(
            content=content, embed=embed, view=view, ephemeral=ephemeral, wait=True
        )
    else:
        await interaction.response.send_message(
            content=content, embed=embed, view=view, ephemeral=ephemeral
        )
        message = await interaction.original_response()
    view.message = message
