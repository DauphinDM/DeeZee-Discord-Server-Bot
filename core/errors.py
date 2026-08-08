"""Exception types and the global error handler.

Every command failure -- prefix or slash -- lands here. The user sees a short
embed that says what went wrong and what to do about it; the log gets the
traceback; the owner gets a DM for anything unexpected. A raw traceback never
reaches a Discord channel.
"""

from __future__ import annotations

import logging
import traceback
import uuid
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from .constants import COLOUR_ERROR, COLOUR_WARNING, EMOJI_ERROR, EMOJI_WARNING

if TYPE_CHECKING:
    from .bot import DeezeeBot

log = logging.getLogger(__name__)

#: Truncation point for the traceback included in the owner DM. Discord's
#: message limit is 2000 characters and the wrapper costs some of them.
_DM_TRACEBACK_LIMIT = 1700


# --- Exception types -------------------------------------------------------


class DeezeeError(commands.CommandError, app_commands.AppCommandError):
    """Base for every error this bot raises deliberately.

    Inherits from both command-error hierarchies so one ``raise`` works from a
    prefix command and a slash command alike, and both handlers can catch it
    with a single ``except``.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class HierarchyError(DeezeeError):
    """The action is blocked by role position or by a protection rule."""


class RootOwnerProtected(HierarchyError):
    """Someone tried to act against a root owner.

    Always refused, whoever asked -- including the guild owner and other bot
    owners. Attempts are logged to the mod-log naming the invoker.
    """

    def __init__(self, target: discord.abc.User | discord.Member) -> None:
        super().__init__(
            f"{target} is a root owner of this bot and cannot be targeted by any command."
        )
        self.target = target


class NotConfigured(DeezeeError):
    """A command needs a setting that has not been set yet."""

    def __init__(self, setting: str, config_path: str) -> None:
        super().__init__(
            f"This command needs **{setting}** to be configured first. "
            f"Set it with `?config` -> {config_path}."
        )
        self.setting = setting
        self.config_path = config_path


class OwnerOnly(DeezeeError):
    """The command is restricted to bot owners."""

    def __init__(self, root_only: bool = False) -> None:
        tier = "a root owner" if root_only else "a bot owner"
        super().__init__(f"That command is restricted to {tier}.")
        self.root_only = root_only


class Blacklisted(DeezeeError):
    """The invoker, their role or the channel is on the ignore list.

    Handled silently: telling someone exactly which rule ignores them just
    invites them to work around it.
    """

    def __init__(self, reason: str | None = None) -> None:
        super().__init__(reason or "You are not permitted to use this bot here.")


class ServiceUnavailable(DeezeeError):
    """An external service the command depends on is unreachable or unconfigured."""

    def __init__(self, service: str, detail: str | None = None) -> None:
        message = f"{service} is unavailable right now."
        if detail:
            message = f"{message} {detail}"
        super().__init__(message)
        self.service = service


# --- Embed construction ----------------------------------------------------


def _embed(description: str, *, warning: bool = False) -> discord.Embed:
    """Build the standard error embed."""
    emoji = EMOJI_WARNING if warning else EMOJI_ERROR
    colour = COLOUR_WARNING if warning else COLOUR_ERROR
    return discord.Embed(description=f"{emoji}  {description}", colour=colour)


def _format_permissions(names: list[str]) -> str:
    """Turn discord.py's snake_case permission names into readable text."""
    pretty = [name.replace("_", " ").title() for name in names]
    if len(pretty) == 1:
        return f"**{pretty[0]}**"
    return ", ".join(f"**{p}**" for p in pretty[:-1]) + f" and **{pretty[-1]}**"


def describe(error: Exception) -> tuple[str, bool] | None:
    """Map a known exception to user-facing text.

    Returns:
        ``(description, is_warning)`` for anything expected, or ``None`` if the
        error is unrecognised and should be treated as a bug.
    """
    # Our own errors already carry a written message.
    if isinstance(error, RootOwnerProtected):
        return error.message, False
    if isinstance(error, DeezeeError):
        return error.message, True

    if isinstance(error, commands.MissingPermissions):
        return (
            f"You need {_format_permissions(error.missing_permissions)} to use that.",
            True,
        )
    if isinstance(error, (commands.BotMissingPermissions, app_commands.BotMissingPermissions)):
        return (
            f"I need {_format_permissions(list(error.missing_permissions))} "
            "to do that. Grant it in Server Settings -> Roles.",
            True,
        )
    if isinstance(error, app_commands.MissingPermissions):
        return (
            f"You need {_format_permissions(list(error.missing_permissions))} to use that.",
            True,
        )
    if isinstance(error, commands.MemberNotFound):
        return (
            f"Could not find a member matching `{error.argument}`. "
            "Try a mention, an exact name, or the user ID.",
            True,
        )
    if isinstance(error, commands.UserNotFound):
        return (
            f"Could not find a user matching `{error.argument}`. "
            "For someone not in this server, use their ID.",
            True,
        )
    if isinstance(error, commands.RoleNotFound):
        return f"Could not find a role matching `{error.argument}`.", True
    if isinstance(error, commands.ChannelNotFound):
        return f"Could not find a channel matching `{error.argument}`.", True
    if isinstance(error, commands.BadBoolArgument):
        return (
            f"`{error.argument}` is not a yes/no value. "
            "Use `on`/`off`, `true`/`false` or `yes`/`no`.",
            True,
        )
    if isinstance(error, commands.MissingRequiredArgument):
        return f"Missing the `{error.param.name}` argument.", True
    if isinstance(error, commands.BadArgument):
        return str(error) or "One of those arguments was not valid.", True
    if isinstance(error, (commands.CommandOnCooldown, app_commands.CommandOnCooldown)):
        return (
            f"Slow down -- try again in **{error.retry_after:.1f}s**.",
            True,
        )
    if isinstance(error, commands.MaxConcurrencyReached):
        return "That command is already running. Wait for it to finish.", True
    if isinstance(error, commands.NoPrivateMessage):
        return "That command only works inside a server.", True
    if isinstance(error, commands.NotOwner):
        return "That command is restricted to bot owners.", True
    if isinstance(error, commands.DisabledCommand):
        return "That command is disabled.", True

    # Raw API failures. 50013 is by far the most common and has a specific fix.
    if isinstance(error, discord.Forbidden):
        if error.code == 50013:
            return (
                "Discord refused that: my role is too low, or I am missing a "
                "permission. Move my role above the roles I manage.",
                True,
            )
        return f"Discord refused that action ({error.code}).", True
    if isinstance(error, discord.NotFound):
        return "That no longer exists -- it was probably deleted already.", True
    if isinstance(error, discord.HTTPException) and error.status == 429:
        return "I am being rate limited. Try again in a moment.", True

    return None


# --- Delivery --------------------------------------------------------------


async def _reply(ctx: commands.Context, embed: discord.Embed) -> None:
    """Send an error embed for a prefix command, swallowing delivery failures.

    If the bot cannot post the error, there is nowhere left to report that.
    """
    try:
        await ctx.send(embed=embed, delete_after=20 if ctx.guild else None)
    except discord.HTTPException:
        log.debug("Could not deliver error embed in channel %s", ctx.channel.id)


async def _respond(interaction: discord.Interaction, embed: discord.Embed) -> None:
    """Send an error embed for an interaction, ephemeral, response or followup."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.HTTPException:
        log.debug("Could not deliver error embed for interaction %s", interaction.id)


async def _report_unexpected(
    bot: DeezeeBot,
    error: Exception,
    *,
    where: str,
    user: discord.abc.User,
    guild: discord.Guild | None,
) -> str:
    """Log an unexpected error and DM the root owners. Returns the error ID."""
    error_id = uuid.uuid4().hex[:8]
    guild_label = f"{guild.name} ({guild.id})" if guild else "DM"

    log.error(
        "[%s] Unhandled error in %s | user=%s (%s) | guild=%s",
        error_id,
        where,
        user,
        user.id,
        guild_label,
        exc_info=error,
    )

    formatted = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    if len(formatted) > _DM_TRACEBACK_LIMIT:
        # Keep the tail: the innermost frame and the exception line matter more
        # than the outer call chain.
        formatted = "...\n" + formatted[-_DM_TRACEBACK_LIMIT:]

    embed = discord.Embed(
        title=f"Unhandled error `{error_id}`",
        description=f"```py\n{formatted}\n```",
        colour=COLOUR_ERROR,
    )
    embed.add_field(name="Where", value=where, inline=False)
    embed.add_field(name="User", value=f"{user} (`{user.id}`)", inline=True)
    embed.add_field(name="Guild", value=guild_label, inline=True)

    for owner_id in bot.config.root_owner_ids:
        try:
            owner = bot.get_user(owner_id) or await bot.fetch_user(owner_id)
            await owner.send(embed=embed)
        except (discord.HTTPException, discord.NotFound):
            # An owner with DMs closed is not an emergency; the log still has it.
            log.debug("Could not DM error report to owner %s", owner_id)

    return error_id


def install(bot: DeezeeBot) -> None:
    """Attach the global handlers to a bot instance.

    Called once from ``setup_hook``. Cogs that define ``cog_command_error``
    still take precedence for their own commands; anything they re-raise or do
    not define arrives here.
    """

    @bot.event
    async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
        # Unwrap: discord.py wraps anything raised inside a command body.
        error = getattr(error, "original", error)

        if isinstance(error, commands.CommandNotFound):
            # An unknown prefix is almost always someone typing normally.
            return
        if isinstance(error, Blacklisted):
            return

        # describe() runs before the CheckFailure fallback: MissingPermissions
        # and NotOwner are both CheckFailure subclasses, and they have far more
        # useful messages than the generic one.
        described = describe(error)
        if described is not None:
            description, warning = described
            await _reply(ctx, _embed(description, warning=warning))
            return

        if isinstance(error, commands.CheckFailure):
            # A check that failed without explaining itself. Say the minimum.
            await _reply(ctx, _embed("You cannot use that command here.", warning=True))
            return

        error_id = await _report_unexpected(
            bot,
            error,
            where=f"?{ctx.command.qualified_name if ctx.command else 'unknown'}",
            user=ctx.author,
            guild=ctx.guild,
        )
        await _reply(
            ctx,
            _embed(
                "Something went wrong on my end. It has been logged and the "
                f"owner notified.\nError ID: `{error_id}`"
            ),
        )

    async def on_app_command_error(
        interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        error = getattr(error, "original", error)

        if isinstance(error, Blacklisted):
            # Interactions must be acknowledged or Discord shows "failed", so
            # unlike the prefix path this cannot stay fully silent.
            await _respond(interaction, _embed("You cannot use that here.", warning=True))
            return

        described = describe(error)
        if described is not None:
            description, warning = described
            await _respond(interaction, _embed(description, warning=warning))
            return

        if isinstance(error, app_commands.CheckFailure):
            await _respond(interaction, _embed("You cannot use that here.", warning=True))
            return

        command = interaction.command.qualified_name if interaction.command else "unknown"
        error_id = await _report_unexpected(
            bot,
            error,
            where=f"/{command}",
            user=interaction.user,
            guild=interaction.guild,
        )
        await _respond(
            interaction,
            _embed(
                "Something went wrong on my end. It has been logged and the "
                f"owner notified.\nError ID: `{error_id}`"
            ),
        )

    bot.tree.on_error = on_app_command_error
