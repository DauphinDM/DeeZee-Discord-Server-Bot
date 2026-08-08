"""Authority tiers, hierarchy enforcement and command checks.

Every moderation command routes through this module. No command does its own
permission check -- that is the whole point of having one, because a check that
exists in nineteen places is a check that is wrong in one of them.

Two rules hold unconditionally:

* A root owner can never be targeted by any command, by anyone, including the
  Discord server owner and other bot owners.
* Neither owner list is editable from inside Discord. They come from ``.env``
  and nothing here writes to them.
"""

from __future__ import annotations

import logging
import time
from enum import IntEnum
from typing import TYPE_CHECKING, Any, Callable, TypeVar

import discord
from discord.ext import commands

from .errors import Blacklisted, HierarchyError, OwnerOnly, RootOwnerProtected

if TYPE_CHECKING:
    from .bot import DeezeeBot

log = logging.getLogger(__name__)

T = TypeVar("T")

#: Discord permissions that make someone a moderator even without a mod role.
#: Anyone who can already ban by hand is not gated from doing it through the bot.
_MOD_PERMISSIONS = (
    "kick_members",
    "ban_members",
    "manage_messages",
    "moderate_members",
)


class Tier(IntEnum):
    """Authority level, highest wins.

    Ordered so that comparisons read naturally: ``tier >= Tier.ADMIN``.
    """

    MEMBER = 1
    MOD = 2
    ADMIN = 3
    OWNER = 4
    ROOT = 5

    @property
    def label(self) -> str:
        return {
            Tier.MEMBER: "Member",
            Tier.MOD: "Moderator",
            Tier.ADMIN: "Administrator",
            Tier.OWNER: "Bot Owner",
            Tier.ROOT: "Root Owner",
        }[self]


class PermissionStore:
    """Cached access to ``permission_roles`` and ``protected_roles``.

    Read on every moderation command, so it is cached per guild and invalidated
    on write. The sets are small -- a handful of role IDs -- and reloading one
    costs a single query.
    """

    __slots__ = ("_db", "_admin", "_mod", "_protected", "_blacklist")

    def __init__(self, db: Any) -> None:
        self._db = db
        self._admin: dict[int, frozenset[int]] = {}
        self._mod: dict[int, frozenset[int]] = {}
        self._protected: dict[int, frozenset[int]] = {}
        self._blacklist: dict[int, frozenset[tuple[int, str]]] = {}

    # --- Reads -------------------------------------------------------------

    async def admin_roles(self, guild_id: int) -> frozenset[int]:
        """Role IDs that grant the admin tier."""
        if guild_id not in self._admin:
            await self._load_permission_roles(guild_id)
        return self._admin[guild_id]

    async def mod_roles(self, guild_id: int) -> frozenset[int]:
        """Role IDs that grant the mod tier."""
        if guild_id not in self._mod:
            await self._load_permission_roles(guild_id)
        return self._mod[guild_id]

    async def protected_roles(self, guild_id: int) -> frozenset[int]:
        """Role IDs whose holders cannot be moderated."""
        cached = self._protected.get(guild_id)
        if cached is not None:
            return cached
        rows = await self._db.fetchall(
            "SELECT role_id FROM protected_roles WHERE guild_id = ?", (guild_id,)
        )
        result = frozenset(row["role_id"] for row in rows)
        self._protected[guild_id] = result
        return result

    async def blacklist(self, guild_id: int) -> frozenset[tuple[int, str]]:
        """``(entity_id, entity_type)`` pairs ignored in this guild.

        Includes bot-wide entries, which are stored under ``guild_id = 0``.
        """
        cached = self._blacklist.get(guild_id)
        if cached is not None:
            return cached
        rows = await self._db.fetchall(
            "SELECT entity_id, entity_type FROM blacklist WHERE guild_id IN (0, ?)",
            (guild_id,),
        )
        result = frozenset((row["entity_id"], row["entity_type"]) for row in rows)
        self._blacklist[guild_id] = result
        return result

    async def _load_permission_roles(self, guild_id: int) -> None:
        rows = await self._db.fetchall(
            "SELECT role_id, tier FROM permission_roles WHERE guild_id = ?", (guild_id,)
        )
        self._admin[guild_id] = frozenset(r["role_id"] for r in rows if r["tier"] == "admin")
        self._mod[guild_id] = frozenset(r["role_id"] for r in rows if r["tier"] == "mod")

    # --- Writes ------------------------------------------------------------

    async def add_permission_role(
        self, guild_id: int, role_id: int, tier: str, added_by: int
    ) -> bool:
        """Grant a tier to a role. Returns False if it already had it."""
        if tier not in {"admin", "mod"}:
            raise ValueError(f"tier must be 'admin' or 'mod', not {tier!r}")
        cursor = await self._db.execute(
            "INSERT OR IGNORE INTO permission_roles "
            "(guild_id, role_id, tier, added_by, created_at) VALUES (?, ?, ?, ?, ?)",
            (guild_id, role_id, tier, added_by, int(time.time())),
        )
        self.invalidate(guild_id)
        return cursor.rowcount > 0

    async def remove_permission_role(self, guild_id: int, role_id: int, tier: str) -> bool:
        """Revoke a tier from a role. Returns False if it did not have it."""
        cursor = await self._db.execute(
            "DELETE FROM permission_roles WHERE guild_id = ? AND role_id = ? AND tier = ?",
            (guild_id, role_id, tier),
        )
        self.invalidate(guild_id)
        return cursor.rowcount > 0

    async def add_protected_role(self, guild_id: int, role_id: int, added_by: int) -> bool:
        """Shield a role's holders from moderation. False if already protected."""
        cursor = await self._db.execute(
            "INSERT OR IGNORE INTO protected_roles (guild_id, role_id, added_by, created_at) "
            "VALUES (?, ?, ?, ?)",
            (guild_id, role_id, added_by, int(time.time())),
        )
        self.invalidate(guild_id)
        return cursor.rowcount > 0

    async def remove_protected_role(self, guild_id: int, role_id: int) -> bool:
        """Remove a role's protection. False if it was not protected."""
        cursor = await self._db.execute(
            "DELETE FROM protected_roles WHERE guild_id = ? AND role_id = ?",
            (guild_id, role_id),
        )
        self.invalidate(guild_id)
        return cursor.rowcount > 0

    def invalidate(self, guild_id: int) -> None:
        """Drop one guild's cached sets."""
        self._admin.pop(guild_id, None)
        self._mod.pop(guild_id, None)
        self._protected.pop(guild_id, None)
        self._blacklist.pop(guild_id, None)

    def clear(self) -> None:
        self._admin.clear()
        self._mod.clear()
        self._protected.clear()
        self._blacklist.clear()


# --- Tier resolution -------------------------------------------------------


async def tier_of(bot: DeezeeBot, user: discord.abc.User, guild: discord.Guild | None) -> Tier:
    """Resolve someone's authority tier.

    The two owner tiers come from ``.env`` and are checked before anything
    Discord-side, so they hold in every guild and cannot be affected by roles.
    """
    if bot.config.is_root_owner(user.id):
        return Tier.ROOT
    if bot.config.is_owner(user.id):
        return Tier.OWNER

    if guild is None or not isinstance(user, discord.Member):
        return Tier.MEMBER

    if user.id == guild.owner_id or user.guild_permissions.administrator:
        return Tier.ADMIN

    role_ids = {role.id for role in user.roles}

    if role_ids & await bot.permissions.admin_roles(guild.id):
        return Tier.ADMIN
    if role_ids & await bot.permissions.mod_roles(guild.id):
        return Tier.MOD
    if any(getattr(user.guild_permissions, name) for name in _MOD_PERMISSIONS):
        return Tier.MOD

    return Tier.MEMBER


async def has_tier(bot: DeezeeBot, user: discord.abc.User, guild: discord.Guild | None,
                   required: Tier) -> bool:
    """True if the user meets or exceeds ``required``."""
    return await tier_of(bot, user, guild) >= required


# --- Hierarchy enforcement -------------------------------------------------


async def _record_root_refusal(
    bot: DeezeeBot,
    guild: discord.Guild,
    invoker: discord.abc.User,
    target: discord.abc.User,
    action: str,
) -> None:
    """Persist and announce a refused action against a root owner.

    Written straight to the database rather than through the logging cog, so the
    record exists even if that cog failed to load. The dispatched event is what
    the logging cog later turns into a mod-log embed.
    """
    try:
        await bot.db.execute(
            "INSERT INTO permission_audit "
            "(guild_id, invoker_id, invoker_tag, target_id, target_tag, action, refusal, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                guild.id,
                invoker.id,
                str(invoker),
                target.id,
                str(target),
                action,
                "root_owner_protected",
                int(time.time()),
            ),
        )
    except Exception:
        # Never let bookkeeping turn a refusal into a crash. The refusal itself
        # still stands, and the log line below still records it.
        log.exception("Could not write permission_audit row")

    log.warning(
        "REFUSED: %s (%s) attempted %r against root owner %s (%s) in guild %s",
        invoker,
        invoker.id,
        action,
        target,
        target.id,
        guild.id,
    )
    bot.dispatch("root_protection_triggered", guild, invoker, target, action)


async def enforce_hierarchy(
    ctx: commands.Context,
    target: discord.Member | discord.User,
    *,
    action: str,
    bot_permission: str | None = None,
) -> None:
    """Run the full guard before acting against someone.

    Checks, in order:

    1. Target is a root owner -- refuse, whoever asked. Recorded and announced.
    2. Target is the bot itself -- refuse. Not an authority question.
    3. Invoker is a root owner -- allow, skip everything below.
    4. Target is the guild owner -- refuse.
    5. Target holds a protected role -- refuse.
    6. Target's top role is at or above the invoker's -- refuse.
    7. Target's top role is at or above the bot's -- refuse, saying so.
    8. The bot lacks the Discord permission -- refuse, naming it.

    Steps 4 to 7 are skipped for a target who is not in the guild, because a
    ``User`` has no roles to compare. Step 1 still applies: a root owner cannot
    be banned by ID either.

    Args:
        ctx: The invoking context.
        target: Who the action is against.
        action: Command name, for the audit record.
        bot_permission: ``discord.Permissions`` attribute the bot needs, if any.

    Raises:
        RootOwnerProtected: The target is a root owner.
        HierarchyError: Any other guard failed. The message says which.
    """
    bot: DeezeeBot = ctx.bot
    guild = ctx.guild
    invoker = ctx.author

    # 1. Root owners are untouchable. This is checked first and unconditionally:
    #    before the invoker's own tier, before role positions, before anything.
    if bot.config.is_root_owner(target.id):
        if guild is not None:
            await _record_root_refusal(bot, guild, invoker, target, action)
        raise RootOwnerProtected(target)

    if guild is None:
        return

    # 2. The bot is never a valid target. This sits above the root bypass on
    #    purpose: it is not a question of authority but of possibility. Discord
    #    refuses a bot's action against itself no matter who asked, so allowing
    #    it through would only produce a confusing 403 later.
    if bot.user is not None and target.id == bot.user.id:
        raise HierarchyError("I cannot use that command on myself.")

    # 3. Root owners bypass every remaining check.
    if bot.config.is_root_owner(invoker.id):
        return

    # A target who has left, or was never here, has no roles to weigh. Ban and
    # unban by ID legitimately reach this path.
    if not isinstance(target, discord.Member):
        await _check_bot_permission(ctx, bot_permission)
        return

    # 4. The server owner outranks everyone, including the bot.
    if target.id == guild.owner_id:
        raise HierarchyError("I cannot act against the server owner.")

    # 5. Protected roles shield their holders even from someone who outranks them.
    protected = await bot.permissions.protected_roles(guild.id)
    shielded = [role for role in target.roles if role.id in protected]
    if shielded:
        raise HierarchyError(
            f"{target.mention} holds a protected role "
            f"({', '.join(role.name for role in shielded)}) and cannot be moderated."
        )

    # 6. Role position, invoker against target. Owners and admins are exempt:
    #    a bot owner acting from outside the role tree would otherwise be
    #    blocked by it. The guild owner is exempt for the same reason.
    invoker_tier = await tier_of(bot, invoker, guild)
    if invoker_tier < Tier.OWNER and invoker.id != guild.owner_id:
        if isinstance(invoker, discord.Member) and target.top_role >= invoker.top_role:
            raise HierarchyError(
                f"{target.mention}'s highest role ({target.top_role.mention}) is not "
                f"below yours ({invoker.top_role.mention}), so you cannot moderate them."
            )

    # 7. Role position, bot against target. No tier exempts this one -- Discord
    #    enforces it server-side and would return 50013 regardless.
    if target.top_role >= guild.me.top_role:
        raise HierarchyError(
            f"{target.mention}'s highest role ({target.top_role.mention}) is at or above "
            f"mine ({guild.me.top_role.mention}). Move my role higher in "
            "Server Settings -> Roles."
        )

    # 8. Finally, whether the bot can actually perform the action.
    await _check_bot_permission(ctx, bot_permission)


async def _check_bot_permission(ctx: commands.Context, permission: str | None) -> None:
    """Raise if the bot lacks a named guild permission."""
    if permission is None or ctx.guild is None:
        return
    if getattr(ctx.guild.me.guild_permissions, permission, False):
        return
    # The global error handler already renders this one readably, naming the
    # permission and where to grant it.
    raise commands.BotMissingPermissions([permission])


def can_manage_role(guild: discord.Guild, role: discord.Role) -> tuple[bool, str]:
    """Whether the bot can assign or remove a role, and why not if it cannot.

    Returns:
        ``(True, "")`` or ``(False, reason)``. Callers show the reason verbatim.
    """
    if role.managed:
        return False, (
            f"{role.mention} is managed by Discord or an integration "
            "(a bot role, a booster role, or a subscription role). "
            "Nobody can assign it manually."
        )
    if role.is_default():
        return False, "@everyone cannot be assigned or removed."
    if role >= guild.me.top_role:
        return False, (
            f"{role.mention} is at or above my highest role ({guild.me.top_role.mention}). "
            "Move my role higher in Server Settings -> Roles."
        )
    if not guild.me.guild_permissions.manage_roles:
        return False, "I do not have the Manage Roles permission."
    return True, ""


# --- Command checks --------------------------------------------------------
# Built on commands.check so one decorator covers both halves of a hybrid
# command. Every command in this bot is hybrid, so app_commands.check is never
# needed separately.


def require_tier(tier: Tier) -> Callable[[T], T]:
    """Restrict a command to a tier and above."""

    async def predicate(ctx: commands.Context) -> bool:
        if await has_tier(ctx.bot, ctx.author, ctx.guild, tier):
            return True
        raise commands.CheckFailure(
            f"That command needs the **{tier.label}** tier or higher."
        )

    return commands.check(predicate)


def root_only() -> Callable[[T], T]:
    """Restrict a command to root owners.

    Used by anything that could alter the bot's own authority -- there is
    nothing above this tier to appeal to.
    """

    async def predicate(ctx: commands.Context) -> bool:
        if ctx.bot.config.is_root_owner(ctx.author.id):
            return True
        raise OwnerOnly(root_only=True)

    return commands.check(predicate)


def owner_only() -> Callable[[T], T]:
    """Restrict a command to root or secondary owners."""

    async def predicate(ctx: commands.Context) -> bool:
        if ctx.bot.config.is_owner(ctx.author.id):
            return True
        raise OwnerOnly()

    return commands.check(predicate)


def admin_only() -> Callable[[T], T]:
    """Restrict a command to the admin tier and above."""
    return require_tier(Tier.ADMIN)


def mod_only() -> Callable[[T], T]:
    """Restrict a command to the mod tier and above."""
    return require_tier(Tier.MOD)


def guild_only() -> Callable[[T], T]:
    """Reject the command in DMs."""

    async def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    return commands.check(predicate)


# --- Global checks ---------------------------------------------------------


async def _global_blacklist_check(ctx: commands.Context) -> bool:
    """Refuse commands from blacklisted users, roles and channels.

    Registered as a bot-wide check, so it runs before every command without a
    decorator anywhere. Owners are exempt, and a root owner can never be
    blacklisted in the first place -- the write is refused at the command layer.
    """
    bot: DeezeeBot = ctx.bot

    if bot.config.is_owner(ctx.author.id):
        return True
    if ctx.guild is None:
        return True

    entries = await bot.permissions.blacklist(ctx.guild.id)
    if not entries:
        return True

    if (ctx.author.id, "user") in entries:
        raise Blacklisted()
    if (ctx.channel.id, "channel") in entries:
        raise Blacklisted()
    if isinstance(ctx.author, discord.Member):
        for role in ctx.author.roles:
            if (role.id, "role") in entries:
                raise Blacklisted()

    return True


def install(bot: DeezeeBot) -> None:
    """Attach the permission store and the global blacklist check.

    Called from ``setup_hook`` before any cog loads, so the check is in place by
    the time a cog's ``cog_load`` runs. The store itself is built in
    ``DeezeeBot.__init__`` so that its type is visible on the class.
    """
    bot.add_check(_global_blacklist_check)
    log.debug("Permission layer installed")
