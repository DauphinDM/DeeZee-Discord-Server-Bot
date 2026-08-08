"""Server event logging, snipe buffers and audit-log search.

Covers the 6 commands in the command sheet's "Logging" section.

Everything here is **off until a channel is chosen**. A bot that starts writing
to a channel nobody picked is a bot that fills a channel nobody asked it to.

Configuration has two levels. A *group* -- messages, members, roles, channels,
voice, server, invites, moderation, automod -- gets a destination channel, and
that switches the whole group on. Individual events inside an enabled group can
then be switched off one at a time, stored as exceptions in
``log_disabled_events``. Groups are the unit of configuration because nine
channels is already more than most servers want and per-event channels would be
thirty.

The snipe buffers are deliberately **in memory and never written to disk**: a
permanent, searchable record of every deleted message is a different and much
more invasive product than a five-message undo.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from core import permissions
from core.constants import (
    COLOUR_DEFAULT,
    COLOUR_ERROR,
    COLOUR_INFO,
    COLOUR_MUTED,
    COLOUR_SUCCESS,
    COLOUR_WARNING,
    EMOJI_SUCCESS,
    SNIPE_BUFFER_SIZE,
    TS_LONG_DATE_TIME,
    TS_RELATIVE,
)
from core.errors import DeezeeError
from ui.paginator import Paginator, paginate_lines
from ui.panels.logging import open_panel

log = logging.getLogger(__name__)

#: Message content is truncated to this before it goes in an embed field.
CONTENT_LIMIT = 900


@dataclass(slots=True, frozen=True)
class Group:
    """One configurable group of events."""

    label: str
    summary: str
    events: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Snipe:
    """A deleted or edited message held in memory."""

    author_id: int
    author_tag: str
    content: str
    before: str
    when: int
    attachments: int
    jump_url: str


class Logging(commands.Cog):
    """Event logging, snipe, and audit-log search."""

    #: Every group and the events inside it. The keys are what
    #: ``log_disabled_events`` stores, so renaming one would orphan its rows --
    #: which is why they are spelled out here rather than derived from anything.
    GROUPS: dict[str, Group] = {
        "messages": Group(
            "Messages",
            "Deletions and edits. Bot messages are never logged -- they are noise",
            {
                "message_delete": "Message deleted",
                "message_edit": "Message edited",
                "message_bulk_delete": "Messages purged in bulk",
            },
        ),
        "members": Group(
            "Members",
            "Joins, leaves, nicknames and avatars",
            {
                "member_join": "Member joined",
                "member_leave": "Member left",
                "member_nickname": "Nickname changed",
                "member_avatar": "Avatar or username changed",
            },
        ),
        "roles": Group(
            "Roles",
            "Role creation, deletion, edits, and who gained or lost one",
            {
                "role_create": "Role created",
                "role_delete": "Role deleted",
                "role_update": "Role edited",
                "member_roles": "Member gained or lost a role",
            },
        ),
        "channels": Group(
            "Channels",
            "Channel and thread lifecycle",
            {
                "channel_create": "Channel created",
                "channel_delete": "Channel deleted",
                "channel_update": "Channel edited",
                "thread_create": "Thread created",
                "thread_delete": "Thread deleted",
            },
        ),
        "voice": Group(
            "Voice",
            "Who joined, left or moved between voice channels",
            {
                "voice_join": "Joined a voice channel",
                "voice_leave": "Left a voice channel",
                "voice_move": "Moved between voice channels",
            },
        ),
        "server": Group(
            "Server",
            "Server settings, emoji and stickers",
            {
                "guild_update": "Server settings changed",
                "emoji_update": "Emoji added or removed",
                "sticker_update": "Stickers added or removed",
            },
        ),
        "invites": Group(
            "Invites",
            "Invite creation and deletion",
            {
                "invite_create": "Invite created",
                "invite_delete": "Invite deleted",
            },
        ),
        "moderation": Group(
            "Moderation",
            "Bans, unbans, timeouts, and refused actions against a root owner",
            {
                "member_ban": "Member banned",
                "member_unban": "Member unbanned",
                "member_timeout": "Member timed out or released",
                "root_protection": "Action against a root owner refused",
            },
        ),
        "automod": Group(
            "Automod",
            "Every automod trip, with the filter and what it did",
            {"automod_action": "Automod acted on a message"},
        ),
    }

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        #: Deleted messages, newest first, per channel. Bounded by
        #: SNIPE_BUFFER_SIZE per channel and never persisted.
        self._deleted: dict[int, deque[Snipe]] = defaultdict(
            lambda: deque(maxlen=SNIPE_BUFFER_SIZE)
        )
        #: Same, for edits.
        self._edited: dict[int, deque[Snipe]] = defaultdict(
            lambda: deque(maxlen=SNIPE_BUFFER_SIZE)
        )
        #: Cached ignore sets, keyed by guild. Read on every logged event.
        self._ignores: dict[int, set[tuple[int, str]]] = {}
        #: Cached per-event exceptions, keyed by guild.
        self._disabled: dict[int, set[str]] = {}

    async def open_config_panel(self, interaction: discord.Interaction) -> None:
        """Open this cog's panel from the ``?config`` root panel."""
        from ui.panels.logging import LoggingPanel

        config, disabled = await self.load_state(interaction.guild.id)
        view = LoggingPanel(self, interaction.user, self.GROUPS, config, disabled)
        await interaction.response.send_message(
            embed=view.embed(interaction.guild), view=view, ephemeral=True
        )
        view.message = await interaction.original_response()

    # =======================================================================
    # State
    # =======================================================================

    async def load_state(self, guild_id: int) -> tuple[dict[str, Any], set[str]]:
        """Return the guild's config dict and its disabled-event set."""
        config = await self.bot.guild_config.get(guild_id)
        return config, await self.disabled_events(guild_id)

    async def disabled_events(self, guild_id: int) -> set[str]:
        """Events switched off inside an otherwise-enabled group."""
        cached = self._disabled.get(guild_id)
        if cached is not None:
            return cached
        rows = await self.bot.db.fetchall(
            "SELECT event FROM log_disabled_events WHERE guild_id = ?", (guild_id,)
        )
        result = {row["event"] for row in rows}
        self._disabled[guild_id] = result
        return result

    async def ignores(self, guild_id: int) -> set[tuple[int, str]]:
        """``(entity_id, entity_type)`` pairs excluded from logging."""
        cached = self._ignores.get(guild_id)
        if cached is not None:
            return cached
        rows = await self.bot.db.fetchall(
            "SELECT entity_id, entity_type FROM log_ignores WHERE guild_id = ?",
            (guild_id,),
        )
        result = {(row["entity_id"], row["entity_type"]) for row in rows}
        self._ignores[guild_id] = result
        return result

    def invalidate(self, guild_id: int) -> None:
        self._ignores.pop(guild_id, None)
        self._disabled.pop(guild_id, None)

    async def set_group_channel(
        self, guild_id: int, group: str, channel_id: int | None
    ) -> None:
        """Point a group at a channel, or switch the group off with ``None``."""
        if group not in self.GROUPS:
            raise KeyError(group)
        await self.bot.guild_config.set(guild_id, f"log_{group}_channel_id", channel_id)

    async def toggle_event(self, guild_id: int, event: str) -> bool:
        """Flip one event's exception row. Returns True if it is now enabled."""
        disabled = await self.disabled_events(guild_id)
        if event in disabled:
            await self.bot.db.execute(
                "DELETE FROM log_disabled_events WHERE guild_id = ? AND event = ?",
                (guild_id, event),
            )
            disabled.discard(event)
            return True
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO log_disabled_events (guild_id, event) VALUES (?, ?)",
            (guild_id, event),
        )
        disabled.add(event)
        return False

    async def enable_all_events(self, guild_id: int, group: str) -> None:
        """Clear every exception inside one group."""
        events = list(self.GROUPS[group].events)
        if not events:
            return
        placeholders = ", ".join("?" * len(events))
        await self.bot.db.execute(
            f"DELETE FROM log_disabled_events WHERE guild_id = ? "
            f"AND event IN ({placeholders})",
            (guild_id, *events),
        )
        self._disabled.pop(guild_id, None)

    # =======================================================================
    # Emitting
    # =======================================================================

    def _group_of(self, event: str) -> str | None:
        for name, group in self.GROUPS.items():
            if event in group.events:
                return name
        return None

    async def _is_ignored(self, guild_id: int, *entities: Any) -> bool:
        """Whether any of the given channels, members or roles is on the ignore list."""
        entries = await self.ignores(guild_id)
        if not entries:
            return False
        for entity in entities:
            if entity is None:
                continue
            if isinstance(entity, discord.Member):
                if (entity.id, "user") in entries:
                    return True
                if any((role.id, "role") in entries for role in entity.roles):
                    return True
            elif isinstance(entity, discord.Role):
                if (entity.id, "role") in entries:
                    return True
            elif isinstance(entity, int):
                if (entity, "user") in entries or (entity, "channel") in entries:
                    return True
            else:
                kind = "channel" if hasattr(entity, "guild") else "user"
                if (entity.id, kind) in entries:
                    return True
        return False

    async def emit(
        self,
        guild: discord.Guild | None,
        event: str,
        embed: discord.Embed,
        *,
        ignore: tuple[Any, ...] = (),
    ) -> None:
        """Post one log entry, if its group has a channel and it is not ignored.

        Every failure is swallowed on purpose. Logging is observation: it must
        never raise into the event that produced it, and a missing permission on
        a log channel must not take down message handling.
        """
        if guild is None:
            return
        group = self._group_of(event)
        if group is None:
            return

        channel_id = await self.bot.guild_config.channel_id(
            guild.id, f"log_{group}_channel_id"
        )
        if channel_id is None:
            return
        if event in await self.disabled_events(guild.id):
            return
        if ignore and await self._is_ignored(guild.id, *ignore):
            return
        if (channel_id, "channel") in await self.ignores(guild.id):
            return

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            log.warning(
                "Cannot write to the %s log channel in guild %s: missing permissions",
                group, guild.id,
            )
        except discord.HTTPException as exc:
            log.debug("Log post failed: %s", exc)

    @staticmethod
    def _base(title: str, colour: int, *, user: discord.abc.User | None = None) -> discord.Embed:
        """Start a log embed with the pieces every entry carries."""
        embed = discord.Embed(
            title=title, colour=colour, timestamp=discord.utils.utcnow()
        )
        if user is not None:
            embed.set_author(name=str(user), icon_url=user.display_avatar.url)
            embed.set_footer(text=f"User ID: {user.id}")
        return embed

    @staticmethod
    def _shorten(text: str) -> str:
        """Fit message content into an embed field without losing the fact of it."""
        if not text:
            return "*(no text)*"
        if len(text) <= CONTENT_LIMIT:
            return text
        return text[:CONTENT_LIMIT] + f"… *(+{len(text) - CONTENT_LIMIT} characters)*"

    async def _audit_actor(
        self, guild: discord.Guild, action: discord.AuditLogAction, target_id: int | None
    ) -> discord.abc.User | None:
        """Find who did something, from the audit log.

        Discord does not name the actor in the gateway event for a deletion, a
        ban or a role edit -- the audit log is the only source. Missing the
        permission is normal and returns ``None`` rather than raising.
        """
        if not guild.me.guild_permissions.view_audit_log:
            return None
        try:
            async for entry in guild.audit_logs(limit=5, action=action):
                if entry.created_at.timestamp() < time.time() - 15:
                    break  # Too old to be about this event.
                if target_id is None or getattr(entry.target, "id", None) == target_id:
                    return entry.user
        except (discord.Forbidden, discord.HTTPException):
            return None
        return None

    # =======================================================================
    # Message events
    # =======================================================================

    @commands.Cog.listener("on_message_delete")
    async def log_message_delete(self, message: discord.Message) -> None:
        """Record and log a deleted message that was still in the cache."""
        if message.guild is None or message.author.bot:
            return

        self._deleted[message.channel.id].appendleft(
            Snipe(
                author_id=message.author.id,
                author_tag=str(message.author),
                content=message.content,
                before="",
                when=int(time.time()),
                attachments=len(message.attachments),
                jump_url=message.jump_url,
            )
        )

        embed = self._base("Message deleted", COLOUR_ERROR, user=message.author)
        embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        embed.add_field(
            name="Sent", value=f"<t:{int(message.created_at.timestamp())}:{TS_RELATIVE}>",
            inline=True,
        )
        embed.add_field(
            name="Content", value=self._shorten(message.content), inline=False
        )
        if message.attachments:
            embed.add_field(
                name="Attachments",
                value="\n".join(a.filename for a in message.attachments[:5]),
                inline=False,
            )

        actor = await self._audit_actor(
            message.guild, discord.AuditLogAction.message_delete, message.author.id
        )
        if actor is not None and actor.id != message.author.id:
            embed.add_field(name="Deleted by", value=f"{actor} (`{actor.id}`)", inline=False)

        await self.emit(
            message.guild, "message_delete", embed,
            ignore=(message.channel, message.author),
        )

    @commands.Cog.listener("on_raw_message_delete")
    async def log_raw_message_delete(
        self, payload: discord.RawMessageDeleteEvent
    ) -> None:
        """Note a deletion the cache had already evicted.

        Only the IDs survive, so this says what it can rather than pretending to
        know the content. Without it, anything older than the 500-message cache
        would vanish from the log silently.
        """
        if payload.guild_id is None or payload.cached_message is not None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        channel = guild.get_channel(payload.channel_id)

        embed = self._base("Message deleted (not cached)", COLOUR_MUTED)
        embed.description = (
            "This message was older than my message cache, so its content and "
            "author are not available."
        )
        embed.add_field(
            name="Channel",
            value=channel.mention if channel else f"`{payload.channel_id}`",
            inline=True,
        )
        embed.add_field(name="Message ID", value=f"`{payload.message_id}`", inline=True)
        await self.emit(guild, "message_delete", embed, ignore=(channel,))

    @commands.Cog.listener("on_message_edit")
    async def log_message_edit(
        self, before: discord.Message, after: discord.Message
    ) -> None:
        """Record and log an edit."""
        if after.guild is None or after.author.bot:
            return
        # An embed resolving on a link fires an edit with identical content.
        if before.content == after.content:
            return

        self._edited[after.channel.id].appendleft(
            Snipe(
                author_id=after.author.id,
                author_tag=str(after.author),
                content=after.content,
                before=before.content,
                when=int(time.time()),
                attachments=len(after.attachments),
                jump_url=after.jump_url,
            )
        )

        embed = self._base("Message edited", COLOUR_WARNING, user=after.author)
        embed.add_field(name="Channel", value=after.channel.mention, inline=True)
        embed.add_field(name="Jump", value=f"[Go to message]({after.jump_url})", inline=True)
        embed.add_field(name="Before", value=self._shorten(before.content), inline=False)
        embed.add_field(name="After", value=self._shorten(after.content), inline=False)

        await self.emit(
            after.guild, "message_edit", embed, ignore=(after.channel, after.author)
        )

    @commands.Cog.listener("on_bulk_message_delete")
    async def log_bulk_delete(self, messages: list[discord.Message]) -> None:
        """Log a purge as one entry rather than a hundred."""
        if not messages or messages[0].guild is None:
            return
        channel = messages[0].channel
        authors: dict[str, int] = {}
        for message in messages:
            authors[str(message.author)] = authors.get(str(message.author), 0) + 1

        embed = self._base("Messages purged", COLOUR_ERROR)
        embed.add_field(name="Channel", value=channel.mention, inline=True)
        embed.add_field(name="Count", value=str(len(messages)), inline=True)
        embed.add_field(
            name="Authors",
            value="\n".join(
                f"{name} — {count}"
                for name, count in sorted(authors.items(), key=lambda x: -x[1])[:10]
            ),
            inline=False,
        )
        await self.emit(
            messages[0].guild, "message_bulk_delete", embed, ignore=(channel,)
        )

    # =======================================================================
    # Member events
    # =======================================================================

    @commands.Cog.listener("on_member_join")
    async def log_member_join(self, member: discord.Member) -> None:
        created = int(member.created_at.timestamp())
        embed = self._base("Member joined", COLOUR_SUCCESS, user=member)
        embed.add_field(name="Member", value=f"{member.mention}", inline=True)
        embed.add_field(
            name="Account created",
            value=f"<t:{created}:{TS_LONG_DATE_TIME}>\n(<t:{created}:{TS_RELATIVE}>)",
            inline=True,
        )
        embed.add_field(
            name="Member count", value=str(member.guild.member_count or "?"), inline=True
        )
        await self.emit(member.guild, "member_join", embed, ignore=(member,))

    @commands.Cog.listener("on_member_remove")
    async def log_member_leave(self, member: discord.Member) -> None:
        joined = int(member.joined_at.timestamp()) if member.joined_at else None
        embed = self._base("Member left", COLOUR_MUTED, user=member)
        embed.add_field(name="Member", value=f"{member} (`{member.id}`)", inline=True)
        if joined:
            embed.add_field(
                name="Joined", value=f"<t:{joined}:{TS_RELATIVE}>", inline=True
            )
        roles = [r.mention for r in member.roles if not r.is_default()]
        if roles:
            embed.add_field(
                name=f"Roles held ({len(roles)})",
                value=", ".join(roles)[:1024],
                inline=False,
            )
        await self.emit(member.guild, "member_leave", embed, ignore=(member,))

    @commands.Cog.listener("on_member_update")
    async def log_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        """Split one gateway event into the three things people care about."""
        if before.nick != after.nick:
            embed = self._base("Nickname changed", COLOUR_INFO, user=after)
            embed.add_field(name="Before", value=before.nick or "*none*", inline=True)
            embed.add_field(name="After", value=after.nick or "*none*", inline=True)
            await self.emit(after.guild, "member_nickname", embed, ignore=(after,))

        if before.roles != after.roles:
            gained = [r for r in after.roles if r not in before.roles]
            lost = [r for r in before.roles if r not in after.roles]
            embed = self._base("Roles changed", COLOUR_INFO, user=after)
            if gained:
                embed.add_field(
                    name="Gained", value=", ".join(r.mention for r in gained)[:1024],
                    inline=False,
                )
            if lost:
                embed.add_field(
                    name="Lost", value=", ".join(r.mention for r in lost)[:1024],
                    inline=False,
                )
            actor = await self._audit_actor(
                after.guild, discord.AuditLogAction.member_role_update, after.id
            )
            if actor is not None:
                embed.add_field(name="Changed by", value=str(actor), inline=False)
            await self.emit(after.guild, "member_roles", embed, ignore=(after,))

        if before.timed_out_until != after.timed_out_until:
            until = after.timed_out_until
            embed = self._base(
                "Timed out" if until else "Timeout lifted",
                COLOUR_WARNING if until else COLOUR_SUCCESS,
                user=after,
            )
            if until:
                embed.add_field(
                    name="Until",
                    value=f"<t:{int(until.timestamp())}:{TS_LONG_DATE_TIME}> "
                    f"(<t:{int(until.timestamp())}:{TS_RELATIVE}>)",
                    inline=False,
                )
            await self.emit(after.guild, "member_timeout", embed, ignore=(after,))

    @commands.Cog.listener("on_user_update")
    async def log_user_update(self, before: discord.User, after: discord.User) -> None:
        """Log username and avatar changes in every guild the user shares with us."""
        changed_name = before.name != after.name
        changed_avatar = before.display_avatar.key != after.display_avatar.key
        if not changed_name and not changed_avatar:
            return

        embed = self._base("Profile changed", COLOUR_INFO, user=after)
        if changed_name:
            embed.add_field(name="Username", value=f"{before.name} → {after.name}",
                            inline=False)
        if changed_avatar:
            embed.add_field(name="Avatar", value="Changed", inline=False)
            embed.set_thumbnail(url=after.display_avatar.url)

        for guild in self.bot.guilds:
            if guild.get_member(after.id) is not None:
                await self.emit(guild, "member_avatar", embed, ignore=(after.id,))

    @commands.Cog.listener("on_member_ban")
    async def log_member_ban(
        self, guild: discord.Guild, user: discord.abc.User
    ) -> None:
        embed = self._base("Member banned", COLOUR_ERROR, user=user)
        actor = await self._audit_actor(guild, discord.AuditLogAction.ban, user.id)
        if actor is not None:
            embed.add_field(name="Banned by", value=f"{actor} (`{actor.id}`)", inline=False)
        await self.emit(guild, "member_ban", embed, ignore=(user.id,))

    @commands.Cog.listener("on_member_unban")
    async def log_member_unban(
        self, guild: discord.Guild, user: discord.abc.User
    ) -> None:
        embed = self._base("Member unbanned", COLOUR_SUCCESS, user=user)
        actor = await self._audit_actor(guild, discord.AuditLogAction.unban, user.id)
        if actor is not None:
            embed.add_field(name="Unbanned by", value=str(actor), inline=False)
        await self.emit(guild, "member_unban", embed, ignore=(user.id,))

    @commands.Cog.listener("on_root_protection_triggered")
    async def log_root_protection(
        self,
        guild: discord.Guild,
        invoker: discord.abc.User,
        target: discord.abc.User,
        action: str,
    ) -> None:
        """Announce a refused action against a root owner.

        Deliberately not ignorable and not silenceable by the ignore list: the
        whole point of the protection is that attempts against it are visible.
        """
        embed = self._base("Action against a root owner refused", COLOUR_ERROR)
        embed.description = (
            f"**{invoker}** (`{invoker.id}`) tried to run `{action}` against "
            f"**{target}** (`{target.id}`).\n\nThe action was refused."
        )
        await self.emit(guild, "root_protection", embed)

    # =======================================================================
    # Role, channel, voice, server, invite events
    # =======================================================================

    @commands.Cog.listener("on_guild_role_create")
    async def log_role_create(self, role: discord.Role) -> None:
        embed = self._base("Role created", COLOUR_SUCCESS)
        embed.description = f"{role.mention} `{role.id}`"
        embed.add_field(name="Colour", value=str(role.colour), inline=True)
        embed.add_field(name="Hoisted", value="Yes" if role.hoist else "No", inline=True)
        await self.emit(role.guild, "role_create", embed, ignore=(role,))

    @commands.Cog.listener("on_guild_role_delete")
    async def log_role_delete(self, role: discord.Role) -> None:
        embed = self._base("Role deleted", COLOUR_ERROR)
        embed.description = f"**{role.name}** `{role.id}`"
        actor = await self._audit_actor(
            role.guild, discord.AuditLogAction.role_delete, role.id
        )
        if actor is not None:
            embed.add_field(name="Deleted by", value=str(actor), inline=False)
        await self.emit(role.guild, "role_delete", embed, ignore=(role,))

    @commands.Cog.listener("on_guild_role_update")
    async def log_role_update(
        self, before: discord.Role, after: discord.Role
    ) -> None:
        changes = []
        if before.name != after.name:
            changes.append(f"Name: `{before.name}` → `{after.name}`")
        if before.colour != after.colour:
            changes.append(f"Colour: `{before.colour}` → `{after.colour}`")
        if before.hoist != after.hoist:
            changes.append(f"Hoisted: {before.hoist} → {after.hoist}")
        if before.mentionable != after.mentionable:
            changes.append(f"Mentionable: {before.mentionable} → {after.mentionable}")
        if before.permissions != after.permissions:
            gained = [
                name.replace("_", " ")
                for name, value in after.permissions
                if value and not getattr(before.permissions, name)
            ]
            lost = [
                name.replace("_", " ")
                for name, value in before.permissions
                if value and not getattr(after.permissions, name)
            ]
            if gained:
                changes.append("Permissions gained: " + ", ".join(gained))
            if lost:
                changes.append("Permissions lost: " + ", ".join(lost))
        if not changes:
            return

        embed = self._base("Role edited", COLOUR_INFO)
        embed.description = f"{after.mention} `{after.id}`\n\n" + "\n".join(changes)
        await self.emit(after.guild, "role_update", embed, ignore=(after,))

    @commands.Cog.listener("on_guild_channel_create")
    async def log_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        embed = self._base("Channel created", COLOUR_SUCCESS)
        embed.description = f"{channel.mention} `{channel.id}`"
        embed.add_field(name="Type", value=str(channel.type), inline=True)
        await self.emit(channel.guild, "channel_create", embed, ignore=(channel,))

    @commands.Cog.listener("on_guild_channel_delete")
    async def log_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        embed = self._base("Channel deleted", COLOUR_ERROR)
        embed.description = f"**#{channel.name}** `{channel.id}`"
        actor = await self._audit_actor(
            channel.guild, discord.AuditLogAction.channel_delete, channel.id
        )
        if actor is not None:
            embed.add_field(name="Deleted by", value=str(actor), inline=False)
        await self.emit(channel.guild, "channel_delete", embed, ignore=(channel,))

    @commands.Cog.listener("on_guild_channel_update")
    async def log_channel_update(
        self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel
    ) -> None:
        changes = []
        if before.name != after.name:
            changes.append(f"Name: `{before.name}` → `{after.name}`")
        if getattr(before, "topic", None) != getattr(after, "topic", None):
            changes.append("Topic changed")
        if getattr(before, "slowmode_delay", None) != getattr(after, "slowmode_delay", None):
            changes.append(
                f"Slowmode: {getattr(before, 'slowmode_delay', 0)}s → "
                f"{getattr(after, 'slowmode_delay', 0)}s"
            )
        if getattr(before, "nsfw", None) != getattr(after, "nsfw", None):
            changes.append(f"NSFW: {getattr(before, 'nsfw', False)} → "
                           f"{getattr(after, 'nsfw', False)}")
        if before.overwrites != after.overwrites:
            changes.append("Permission overwrites changed")
        if not changes:
            return

        embed = self._base("Channel edited", COLOUR_INFO)
        embed.description = f"{after.mention} `{after.id}`\n\n" + "\n".join(changes)
        await self.emit(after.guild, "channel_update", embed, ignore=(after,))

    @commands.Cog.listener("on_thread_create")
    async def log_thread_create(self, thread: discord.Thread) -> None:
        embed = self._base("Thread created", COLOUR_SUCCESS)
        embed.description = f"{thread.mention} in {thread.parent.mention if thread.parent else '?'}"
        if thread.owner_id:
            embed.add_field(name="Created by", value=f"<@{thread.owner_id}>", inline=True)
        await self.emit(thread.guild, "thread_create", embed, ignore=(thread.parent,))

    @commands.Cog.listener("on_thread_delete")
    async def log_thread_delete(self, thread: discord.Thread) -> None:
        embed = self._base("Thread deleted", COLOUR_ERROR)
        embed.description = f"**{thread.name}** `{thread.id}`"
        await self.emit(thread.guild, "thread_delete", embed, ignore=(thread.parent,))

    @commands.Cog.listener("on_voice_state_update")
    async def log_voice(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if before.channel == after.channel:
            return  # A mute or deafen, not a movement.

        if before.channel is None and after.channel is not None:
            embed = self._base("Joined voice", COLOUR_SUCCESS, user=member)
            embed.add_field(name="Channel", value=after.channel.mention, inline=True)
            await self.emit(member.guild, "voice_join", embed,
                            ignore=(member, after.channel))
        elif after.channel is None and before.channel is not None:
            embed = self._base("Left voice", COLOUR_MUTED, user=member)
            embed.add_field(name="Channel", value=before.channel.mention, inline=True)
            await self.emit(member.guild, "voice_leave", embed,
                            ignore=(member, before.channel))
        else:
            embed = self._base("Moved voice channel", COLOUR_INFO, user=member)
            embed.add_field(name="From", value=before.channel.mention, inline=True)
            embed.add_field(name="To", value=after.channel.mention, inline=True)
            await self.emit(member.guild, "voice_move", embed, ignore=(member,))

    @commands.Cog.listener("on_guild_update")
    async def log_guild_update(
        self, before: discord.Guild, after: discord.Guild
    ) -> None:
        changes = []
        if before.name != after.name:
            changes.append(f"Name: `{before.name}` → `{after.name}`")
        if before.owner_id != after.owner_id:
            changes.append(f"Owner: <@{before.owner_id}> → <@{after.owner_id}>")
        if before.icon != after.icon:
            changes.append("Icon changed")
        if before.verification_level != after.verification_level:
            changes.append(
                f"Verification: {before.verification_level} → {after.verification_level}"
            )
        if before.premium_tier != after.premium_tier:
            changes.append(f"Boost tier: {before.premium_tier} → {after.premium_tier}")
        if not changes:
            return

        embed = self._base("Server settings changed", COLOUR_INFO)
        embed.description = "\n".join(changes)
        await self.emit(after, "guild_update", embed)

    @commands.Cog.listener("on_guild_emojis_update")
    async def log_emoji_update(
        self, guild: discord.Guild, before: list[Any], after: list[Any]
    ) -> None:
        added = [e for e in after if e not in before]
        removed = [e for e in before if e not in after]
        if not added and not removed:
            return
        embed = self._base("Emoji changed", COLOUR_INFO)
        if added:
            embed.add_field(
                name="Added", value=" ".join(str(e) for e in added)[:1024], inline=False
            )
        if removed:
            embed.add_field(
                name="Removed", value=", ".join(e.name for e in removed)[:1024],
                inline=False,
            )
        await self.emit(guild, "emoji_update", embed)

    @commands.Cog.listener("on_guild_stickers_update")
    async def log_sticker_update(
        self, guild: discord.Guild, before: list[Any], after: list[Any]
    ) -> None:
        added = [s for s in after if s not in before]
        removed = [s for s in before if s not in after]
        if not added and not removed:
            return
        embed = self._base("Stickers changed", COLOUR_INFO)
        if added:
            embed.add_field(
                name="Added", value=", ".join(s.name for s in added)[:1024], inline=False
            )
        if removed:
            embed.add_field(
                name="Removed", value=", ".join(s.name for s in removed)[:1024],
                inline=False,
            )
        await self.emit(guild, "sticker_update", embed)

    @commands.Cog.listener("on_invite_create")
    async def log_invite_create(self, invite: discord.Invite) -> None:
        embed = self._base("Invite created", COLOUR_INFO, user=invite.inviter)
        embed.add_field(name="Code", value=f"`{invite.code}`", inline=True)
        if invite.channel is not None:
            embed.add_field(name="Channel", value=f"<#{invite.channel.id}>", inline=True)
        embed.add_field(
            name="Max uses", value=str(invite.max_uses or "unlimited"), inline=True
        )
        embed.add_field(
            name="Expires",
            value=f"<t:{int(invite.expires_at.timestamp())}:{TS_RELATIVE}>"
            if invite.expires_at
            else "never",
            inline=True,
        )
        await self.emit(invite.guild, "invite_create", embed)

    @commands.Cog.listener("on_invite_delete")
    async def log_invite_delete(self, invite: discord.Invite) -> None:
        embed = self._base("Invite deleted", COLOUR_MUTED)
        embed.description = f"Code `{invite.code}`"
        await self.emit(invite.guild, "invite_delete", embed)

    @commands.Cog.listener("on_automod_action")
    async def log_automod(
        self, message: discord.Message, trip: Any, action: str
    ) -> None:
        """Render an automod trip.

        The automod cog dispatches this rather than writing the log itself, so
        it never has to know where any log channel is.
        """
        embed = self._base("Automod triggered", COLOUR_WARNING, user=message.author)
        embed.add_field(name="Filter", value=getattr(trip, "name", "?"), inline=True)
        embed.add_field(name="Action", value=action, inline=True)
        embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        embed.add_field(
            name="Why", value=str(getattr(trip, "reason", "?"))[:1024], inline=False
        )
        embed.add_field(
            name="Message", value=self._shorten(message.content), inline=False
        )
        await self.emit(message.guild, "automod_action", embed)

    # =======================================================================
    # Commands
    # =======================================================================

    @commands.hybrid_group(name="logging", aliases=["logs", "log"], fallback="panel")
    @permissions.admin_only()
    @permissions.guild_only()
    async def logging_group(self, ctx: commands.Context) -> None:
        """Open the logging panel: a channel per event group, plus toggles."""
        await open_panel(ctx, self)

    @logging_group.command(name="ignore", aliases=["logignore"])
    @app_commands.describe(
        action="add, remove or list",
        target="Channel, role or member -- mention, ID or name",
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def logging_ignore(
        self, ctx: commands.Context, action: str = "list", *, target: Optional[str] = None
    ) -> None:
        """Exclude channels, roles or members from logging.

        ``target`` is a plain string rather than three separate options because
        application commands cannot express "a channel or a role or a member" in
        one parameter -- a mention, an ID or a name all work.
        """
        choice = action.lower()

        if choice == "list":
            entries = await self.ignores(ctx.guild.id)
            lines = []
            for entity_id, kind in sorted(entries, key=lambda e: e[1]):
                mention = {
                    "channel": f"<#{entity_id}>",
                    "role": f"<@&{entity_id}>",
                    "user": f"<@{entity_id}>",
                }[kind]
                lines.append(f"{mention} — {kind} `{entity_id}`")
            pages = paginate_lines(
                lines,
                title="Ignored by logging",
                per_page=15,
                description="`?logging ignore add #channel` to add one.",
                empty_message="Nothing is excluded from logging.",
            )
            await Paginator(pages, ctx.author).start(ctx)
            return

        if target is None:
            raise DeezeeError("Name the channel, role or member.")

        resolved, kind = await self._resolve_entity(ctx, target)

        if choice == "add":
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO log_ignores "
                "(guild_id, entity_id, entity_type, added_by, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (ctx.guild.id, resolved.id, kind, ctx.author.id, int(time.time())),
            )
            self.invalidate(ctx.guild.id)
            await ctx.send(
                embed=discord.Embed(
                    description=f"{EMOJI_SUCCESS}  {resolved.mention} is no longer logged.",
                    colour=COLOUR_SUCCESS,
                )
            )
            return

        if choice in {"remove", "delete", "unignore"}:
            cursor = await self.bot.db.execute(
                "DELETE FROM log_ignores WHERE guild_id = ? AND entity_id = ? "
                "AND entity_type = ?",
                (ctx.guild.id, resolved.id, kind),
            )
            self.invalidate(ctx.guild.id)
            if not cursor.rowcount:
                raise DeezeeError(f"{resolved.mention} was not on the ignore list.")
            await ctx.send(
                embed=discord.Embed(
                    description=f"{EMOJI_SUCCESS}  {resolved.mention} is logged again.",
                    colour=COLOUR_SUCCESS,
                )
            )
            return

        raise DeezeeError("Use `add`, `remove` or `list`.")

    @staticmethod
    async def _resolve_entity(ctx: commands.Context, query: str) -> tuple[Any, str]:
        """Turn a mention, ID or name into a channel, role or member.

        Tried in that order because an ID is ambiguous between the three and a
        channel is the most common thing to exclude from logging.

        Returns:
            ``(entity, kind)`` where kind is ``channel``, ``role`` or ``user``.
        """
        for converter, kind in (
            (commands.TextChannelConverter(), "channel"),
            (commands.RoleConverter(), "role"),
            (commands.MemberConverter(), "user"),
        ):
            try:
                return await converter.convert(ctx, query), kind
            except commands.BadArgument:
                continue
        raise DeezeeError(
            f"`{query}` is not a channel, role or member I can find here."
        )

    @commands.hybrid_command(name="modlog", with_app_command=False)
    @app_commands.describe(channel="Channel for numbered cases. Blank shows the current one")
    @permissions.admin_only()
    @permissions.guild_only()
    async def modlog_cmd(
        self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None
    ) -> None:
        """Set the channel that receives numbered moderation cases."""
        if channel is None:
            current = await self.bot.guild_config.channel_id(
                ctx.guild.id, "mod_log_channel_id"
            )
            target = ctx.guild.get_channel(current) if current else None
            embed = discord.Embed(
                title="Mod-log channel",
                description=(
                    f"Cases are posted to {target.mention}."
                    if target
                    else "**Not set.** Cases are still recorded and `?case` still "
                    "finds them -- they are simply not posted anywhere.\n\n"
                    "Set one with `?modlog #channel`."
                ),
                colour=COLOUR_SUCCESS if target else COLOUR_DEFAULT,
            )
            await ctx.send(embed=embed)
            return

        perms = channel.permissions_for(ctx.guild.me)
        if not perms.send_messages or not perms.embed_links:
            raise DeezeeError(
                f"I need Send Messages and Embed Links in {channel.mention} "
                "before it can be the mod-log."
            )

        await self.bot.guild_config.set(ctx.guild.id, "mod_log_channel_id", channel.id)
        await ctx.send(
            embed=discord.Embed(
                description=f"{EMOJI_SUCCESS}  Moderation cases now post to "
                f"{channel.mention}.",
                colour=COLOUR_SUCCESS,
            )
        )

    @commands.hybrid_command(name="snipe")
    @app_commands.describe(
        channel="Which channel. Defaults to here", index="1 is the most recent"
    )
    @permissions.mod_only()
    @permissions.guild_only()
    async def snipe(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
        index: int = 1,
    ) -> None:
        """Show a recently deleted message.

        The buffer holds a handful of messages per channel, in memory only. It is
        emptied by a restart and nothing is ever written to disk.
        """
        await self._send_snipe(ctx, self._deleted, channel, index, "deleted")

    @commands.hybrid_command(name="editsnipe", aliases=["esnipe"])
    @app_commands.describe(
        channel="Which channel. Defaults to here", index="1 is the most recent"
    )
    @permissions.mod_only()
    @permissions.guild_only()
    async def editsnipe(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
        index: int = 1,
    ) -> None:
        """Show the before and after of a recently edited message."""
        await self._send_snipe(ctx, self._edited, channel, index, "edited")

    async def _send_snipe(
        self,
        ctx: commands.Context,
        store: dict[int, deque[Snipe]],
        channel: discord.TextChannel | None,
        index: int,
        kind: str,
    ) -> None:
        """Shared body for both snipe commands."""
        target = channel or ctx.channel
        buffer = store.get(target.id)
        if not buffer:
            raise DeezeeError(
                f"Nothing {kind} in {target.mention} since I last restarted. "
                "The buffer is in memory only."
            )
        if not 1 <= index <= len(buffer):
            raise DeezeeError(
                f"I have {len(buffer)} {kind} message(s) for {target.mention}. "
                f"Pick 1 to {len(buffer)}."
            )

        entry = buffer[index - 1]
        if await self._is_ignored(ctx.guild.id, entry.author_id, target):
            raise DeezeeError("That message is excluded from logging.")

        embed = discord.Embed(
            title=f"{kind.title()} message {index} of {len(buffer)}",
            colour=COLOUR_ERROR if kind == "deleted" else COLOUR_WARNING,
        )
        embed.add_field(
            name="Author", value=f"<@{entry.author_id}> ({entry.author_tag})", inline=True
        )
        embed.add_field(name="Channel", value=target.mention, inline=True)
        embed.add_field(
            name="When", value=f"<t:{entry.when}:{TS_RELATIVE}>", inline=True
        )
        if entry.before:
            embed.add_field(name="Before", value=self._shorten(entry.before), inline=False)
            embed.add_field(name="After", value=self._shorten(entry.content), inline=False)
        else:
            embed.add_field(
                name="Content", value=self._shorten(entry.content), inline=False
            )
        if entry.attachments:
            embed.add_field(
                name="Attachments",
                value=f"{entry.attachments} attachment(s), not stored",
                inline=False,
            )
        embed.set_footer(text="In-memory buffer -- cleared on restart, never saved")
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="auditlog", aliases=["audit"])
    @app_commands.describe(
        action="Filter by action, e.g. ban, kick, member_role_update",
        user="Only entries by this moderator",
        limit="How many entries. Max 100",
    )
    @permissions.mod_only()
    @permissions.guild_only()
    async def auditlog(
        self,
        ctx: commands.Context,
        action: Optional[str] = None,
        user: Optional[discord.Member] = None,
        limit: int = 10,
    ) -> None:
        """Show recent Discord audit-log entries, filterable by action and actor."""
        if not ctx.guild.me.guild_permissions.view_audit_log:
            raise commands.BotMissingPermissions(["view_audit_log"])

        limit = max(1, min(limit, 100))

        # MISSING, not None. ``audit_logs`` treats None as "a value was given"
        # and immediately does ``user.id`` / ``action.value`` on it; only MISSING
        # means "no filter".
        chosen: Any = discord.utils.MISSING
        if action is not None:
            key = action.lower().replace(" ", "_").replace("-", "_")
            chosen = getattr(discord.AuditLogAction, key, None)
            if not isinstance(chosen, discord.AuditLogAction):
                names = [a.name for a in discord.AuditLogAction]
                matches = [name for name in names if key in name][:12]
                raise DeezeeError(
                    f"`{action}` is not an audit action."
                    + (f" Did you mean: {', '.join(f'`{m}`' for m in matches)}?"
                       if matches else "")
                )

        if ctx.interaction is not None and not ctx.interaction.response.is_done():
            await ctx.defer()

        lines: list[str] = []
        try:
            async for entry in ctx.guild.audit_logs(
                limit=limit,
                action=chosen,
                user=user if user is not None else discord.utils.MISSING,
            ):
                when = int(entry.created_at.timestamp())
                target = getattr(entry.target, "name", None) or getattr(
                    entry.target, "id", "?"
                )
                reason = f" — {entry.reason}" if entry.reason else ""
                lines.append(
                    f"**{entry.action.name.replace('_', ' ').title()}** "
                    f"by {entry.user} on `{target}`\n"
                    f"<t:{when}:{TS_RELATIVE}>{reason}"
                )
        except discord.Forbidden:
            raise commands.BotMissingPermissions(["view_audit_log"]) from None

        pages = paginate_lines(
            lines,
            title="Audit log",
            per_page=6,
            colour=COLOUR_INFO,
            description=(
                f"Filters: action="
                f"{chosen.name if chosen is not discord.utils.MISSING else 'any'}, "
                f"user={user or 'any'}"
            ),
            empty_message="No matching audit-log entries.",
        )
        await Paginator(pages, ctx.author).start(ctx)


async def setup(bot: commands.Bot) -> None:
    """discord.py extension entry point."""
    await bot.add_cog(Logging(bot))
