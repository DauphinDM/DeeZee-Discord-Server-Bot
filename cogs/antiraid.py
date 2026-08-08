"""Anti-raid, verification and alt-account heuristics.

Covers 14 buildable commands from the command sheet's 15-row Anti-raid section.
The fifteenth row is the VPN/proxy/Tor block, which is recorded as NOT BUILT:
Discord never exposes a member's IP address to a bot, and the only way to obtain
one is to redirect joiners to a web page you host. The no-dashboard constraint
rules that out, so ``?altcheck`` implements the strongest signals a bot can
actually see instead of pretending to something it cannot do.
"""

from __future__ import annotations

import io
import json
import logging
import time
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from core import modlog, permissions
from core.constants import (
    COLOUR_DEFAULT,
    COLOUR_ERROR,
    COLOUR_INFO,
    COLOUR_SUCCESS,
    COLOUR_WARNING,
    EMOJI_SUCCESS,
    TS_RELATIVE,
)
from core.errors import DeezeeError, NotConfigured
from services import captcha as captcha_service
from services import riskscore
from services.timeparse import format_duration, parse_duration
from ui.paginator import Paginator, paginate_lines
from ui.panels.antiraid import open_altconfig, open_panel, open_verification
from ui.views import BaseView, confirm

log = logging.getLogger(__name__)

#: Joins older than this are dropped from ``join_log`` by the pruning task.
JOIN_LOG_RETENTION_DAYS = 30

#: How many existing members ``?altcheck`` compares a username against. Bounded
#: because the comparison is O(n) per name and the member cache can be large.
SIMILARITY_SAMPLE = 400


class CaptchaModal(discord.ui.Modal, title="Verification"):
    """Takes the member's answer to a challenge."""

    answer = discord.ui.TextInput(
        label="Answer",
        placeholder="Type what you see in the image",
        required=True,
        max_length=32,
    )

    def __init__(self, cog: AntiRaid, question: str | None = None) -> None:
        super().__init__(timeout=300.0)
        self.cog = cog
        if question:
            self.answer.label = "Answer"
            self.answer.placeholder = "Type your answer"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.check_answer(interaction, self.answer.value)


class CaptchaView(BaseView):
    """Short-lived view holding the button that opens the answer modal.

    The answer itself lives in the ``verification`` table, not here, so a
    restart mid-verification does not silently invalidate the challenge.
    """

    def __init__(self, cog: AntiRaid, member: discord.abc.User) -> None:
        super().__init__(member, timeout=600.0)
        self.cog = cog

    @discord.ui.button(label="Enter answer", style=discord.ButtonStyle.success)
    async def enter(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(CaptchaModal(self.cog))


class VerifyStartView(discord.ui.View):
    """The persistent button that starts verification.

    Registered on every boot with a fixed ``custom_id``, so the message posted
    weeks ago keeps working. Never times out.
    """

    def __init__(self, cog: AntiRaid) -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Verify",
        style=discord.ButtonStyle.success,
        custom_id="verify:start",
        emoji="\N{WHITE HEAVY CHECK MARK}",
    )
    async def start(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self.cog.begin_verification(interaction)


class AntiRaid(commands.Cog):
    """Raid detection, the verification gate, alt heuristics and quarantine."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        #: Snapshot of invite use counts per guild, refreshed on join.
        self._invites: dict[int, dict[str, tuple[int, int | None]]] = {}

    async def open_config_panel(self, interaction: discord.Interaction) -> None:
        """Open this cog's panel from the ``?config`` root panel."""
        from ui.panels.antiraid import AntiRaidPanel

        config = await self.bot.guild_config.get(interaction.guild.id)
        view = AntiRaidPanel(self, interaction.user, config)
        await interaction.response.send_message(
            embed=view.embed(interaction.guild), view=view, ephemeral=True
        )
        view.message = await interaction.original_response()

    async def cog_load(self) -> None:
        self.bot.scheduler.register("raid_auto_off", self._scheduled_raid_off)
        self.bot.scheduler.register("prune_join_log", self._prune_join_log)

    async def cog_unload(self) -> None:
        for action in ("raid_auto_off", "prune_join_log"):
            self.bot.scheduler.unregister(action)

    def register_persistent_views(self, bot: Any) -> None:
        """Re-attach the verification button to messages posted before a restart."""
        bot.add_view(VerifyStartView(self))

    @commands.Cog.listener("on_ready")
    async def prime(self) -> None:
        """Seed the pruning task, then snapshot invite counts.

        Seeding comes first deliberately. The invite snapshot makes an API call
        per guild, and anything that depends on it finishing would be at the
        mercy of how long Discord takes to answer.
        """
        if not self.bot.guilds:
            return

        pending = await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM scheduled_actions "
            "WHERE action_type = 'prune_join_log' AND status = 'pending'"
        )
        if not pending:
            await self.bot.scheduler.schedule(
                self.bot.guilds[0].id, "prune_join_log", int(time.time()) + 600, {}
            )

        for guild in self.bot.guilds:
            await self._snapshot_invites(guild)

    # =======================================================================
    # Invite tracking
    # =======================================================================

    async def _snapshot_invites(self, guild: discord.Guild) -> None:
        """Record current invite use counts.

        Discord does not say which invite a member used. The only way to know is
        to hold the previous counts and find the one that went up, which means
        the snapshot has to be taken before the join, not after.
        """
        if not guild.me.guild_permissions.manage_guild:
            return
        try:
            invites = await guild.invites()
        except (discord.Forbidden, discord.HTTPException):
            return

        snapshot = {
            invite.code: (invite.uses or 0, invite.inviter.id if invite.inviter else None)
            for invite in invites
        }
        self._invites[guild.id] = snapshot

        await self.bot.guild_config.get(guild.id)
        await self.bot.db.executemany(
            "INSERT INTO invite_uses (guild_id, code, uses, inviter_id) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(guild_id, code) DO UPDATE SET "
            "uses = excluded.uses, inviter_id = excluded.inviter_id",
            [(guild.id, code, uses, inviter) for code, (uses, inviter) in snapshot.items()],
        )

    async def _identify_invite(
        self, guild: discord.Guild
    ) -> tuple[str | None, int | None]:
        """Work out which invite was just used by diffing the snapshot."""
        previous = self._invites.get(guild.id, {})
        if not guild.me.guild_permissions.manage_guild:
            return None, None

        try:
            invites = await guild.invites()
        except (discord.Forbidden, discord.HTTPException):
            return None, None

        used_code: str | None = None
        used_by: int | None = None
        snapshot: dict[str, tuple[int, int | None]] = {}

        for invite in invites:
            uses = invite.uses or 0
            inviter_id = invite.inviter.id if invite.inviter else None
            snapshot[invite.code] = (uses, inviter_id)

            before = previous.get(invite.code, (0, None))[0]
            if uses > before and used_code is None:
                used_code, used_by = invite.code, inviter_id

        self._invites[guild.id] = snapshot
        return used_code, used_by

    # =======================================================================
    # Join handling
    # =======================================================================

    @commands.Cog.listener("on_member_join")
    async def handle_join(self, member: discord.Member) -> None:
        """Score, log, and respond to a join.

        Order matters: the join is recorded before anything acts on it, so a
        member kicked by the age gate still shows up in ``?joins``.
        """
        guild = member.guild
        config = await self.bot.guild_config.get(guild.id)
        now = int(time.time())

        code, inviter_id = await self._identify_invite(guild)
        created = int(member.created_at.timestamp())

        burst = await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM join_log WHERE guild_id = ? AND joined_at >= ?",
            (guild.id, now - int(config["raid_join_window"] or 60)),
        )

        result = await self._score_member(member, burst=burst, invite_known=code is not None)
        flagged = result.score >= int(config["alt_flag_threshold"] or 55)

        await self.bot.db.execute(
            "INSERT INTO join_log (guild_id, user_id, user_tag, joined_at, "
            "account_created, invite_code, inviter_id, risk_score, risk_reasons, flagged) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild.id, member.id, str(member), now, created, code, inviter_id,
             result.score, json.dumps(result.reasons), int(flagged)),
        )

        # 1. Raid detection, which can change what happens to this member.
        if not config["raid_mode"] and int(config["raid_join_threshold"] or 0) > 0:
            if burst + 1 >= int(config["raid_join_threshold"]):
                await self.set_raid_mode(
                    guild, True,
                    reason=f"{burst + 1} joins within "
                           f"{format_duration(config['raid_join_window'])}",
                    automatic=True,
                )
                config = await self.bot.guild_config.get(guild.id)

        if config["raid_mode"]:
            if await self._apply_action(
                member, config["raid_action"], "Raid mode is active"
            ):
                return

        # 2. Account age gate.
        min_age = int(config["min_account_age"] or 0)
        if min_age and (now - created) < min_age:
            reason = (
                f"Account is younger than the {format_duration(min_age)} minimum"
            )
            if await self._apply_action(member, config["min_age_action"], reason):
                return

        # 3. Automatic response to a high risk score.
        if result.score >= int(config["alt_action_threshold"] or 80):
            if await self._apply_action(
                member, config["alt_action"], f"Alt risk score {result.score}/100"
            ):
                return

        # 4. Alert staff about anything flagged but not acted on.
        if flagged:
            await self._alert(
                guild,
                discord.Embed(
                    title="Flagged join",
                    description=f"{member.mention} `{member}` (`{member.id}`)\n"
                    f"Risk **{result.score}/100** ({result.level})",
                    colour=COLOUR_WARNING,
                ).add_field(
                    name="Signals", value="\n".join(result.reasons)[:1024], inline=False
                ),
            )

        # 5. Verification gate.
        if config["verification_mode"] == "join":
            await self._apply_unverified(member)

    @commands.Cog.listener("on_member_remove")
    async def handle_leave(self, member: discord.Member) -> None:
        """Record that a logged join later left."""
        await self.bot.db.execute(
            "UPDATE join_log SET left_at = ? WHERE guild_id = ? AND user_id = ? "
            "AND left_at IS NULL",
            (int(time.time()), member.guild.id, member.id),
        )

    async def _score_member(
        self, member: discord.Member, *, burst: int = 0, invite_known: bool = True
    ) -> riskscore.RiskResult:
        """Gather the signals available for a member and score them."""
        age = int(time.time()) - int(member.created_at.timestamp())

        # Compare against a bounded sample of cached members: the comparison is
        # linear per name, and the whole point is spotting a near-duplicate, not
        # producing an exhaustive ranking.
        names = [
            other.name
            for other in list(member.guild.members)[:SIMILARITY_SAMPLE]
            if other.id != member.id
        ]
        similarity, closest = riskscore.name_similarity(member.name, names)

        flags = member.public_flags
        has_flags = bool(getattr(flags, "spammer", False))

        return riskscore.score_account(
            account_age_seconds=age,
            has_default_avatar=member.avatar is None,
            similarity=similarity,
            similar_to=closest,
            join_burst=burst,
            invite_known=invite_known,
            has_flags=has_flags,
            name_is_generic=riskscore.looks_auto_generated(member.name),
        )

    async def _apply_action(
        self, member: discord.Member, action: str, reason: str
    ) -> bool:
        """Carry out an automatic response. Returns True if the member is gone."""
        if action in ("none", "", None):
            return False
        if self.bot.config.is_root_owner(member.id):
            return False
        if member.top_role >= member.guild.me.top_role:
            log.info("Cannot action %s: role is above mine", member)
            return False

        try:
            if action == "kick":
                await member.kick(reason=reason)
            elif action == "ban":
                await member.guild.ban(member, reason=reason, delete_message_seconds=0)
            elif action == "quarantine":
                await self.quarantine_member(member, member.guild.me, reason)
                return False
            else:
                return False
        except discord.HTTPException as exc:
            log.warning("Automatic %s failed in guild %s: %s", action, member.guild.id, exc)
            return False

        await modlog.create_case(
            self.bot, member.guild, action=action, target=member,
            moderator=member.guild.me, reason=reason,
        )
        return True

    async def _alert(self, guild: discord.Guild, embed: discord.Embed) -> None:
        """Post to the raid alert channel, falling back to the mod-log."""
        channel_id = await self.bot.guild_config.channel_id(
            guild.id, "raid_alert_channel_id"
        ) or await self.bot.guild_config.channel_id(guild.id, "mod_log_channel_id")
        if channel_id is None:
            return
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass

    # =======================================================================
    # Raid mode
    # =======================================================================

    async def set_raid_mode(
        self, guild: discord.Guild, enabled: bool, *, reason: str, automatic: bool = False
    ) -> None:
        """Turn raid mode on or off and announce it."""
        await self.bot.guild_config.set(guild.id, "raid_mode", int(enabled))

        config = await self.bot.guild_config.get(guild.id)
        if enabled:
            quiet = int(config["raid_auto_off"] or 0)
            if quiet:
                await self.bot.scheduler.schedule(
                    guild.id, "raid_auto_off", int(time.time()) + quiet, {}
                )
        else:
            await self.bot.scheduler.cancel_matching(guild.id, "raid_auto_off")

        embed = discord.Embed(
            title="Raid mode " + ("ENABLED" if enabled else "disabled"),
            description=reason,
            colour=COLOUR_ERROR if enabled else COLOUR_SUCCESS,
        )
        if enabled:
            embed.add_field(
                name="New joins",
                value=f"will be **{config['raid_action']}**",
                inline=True,
            )
            if config["raid_auto_off"]:
                embed.add_field(
                    name="Turns off after",
                    value=format_duration(config["raid_auto_off"]) + " of quiet",
                    inline=True,
                )
        embed.set_footer(text="Triggered automatically" if automatic else "Set by hand")
        await self._alert(guild, embed)

        await modlog.create_case(
            self.bot, guild,
            action="lockdown", target=guild.me, moderator=guild.me,
            reason=f"Raid mode {'on' if enabled else 'off'}: {reason}",
            post=False,
        )

    async def _scheduled_raid_off(self, guild_id: int, payload: dict[str, Any]) -> None:
        """Turn raid mode off after a quiet period, if it stayed quiet."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        config = await self.bot.guild_config.get(guild_id)
        if not config["raid_mode"]:
            return

        window = int(config["raid_join_window"] or 60)
        recent = await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM join_log WHERE guild_id = ? AND joined_at >= ?",
            (guild_id, int(time.time()) - window),
        )
        if recent >= int(config["raid_join_threshold"] or 10):
            # Still busy. Wait another full period rather than ending early.
            await self.bot.scheduler.schedule(
                guild_id, "raid_auto_off",
                int(time.time()) + int(config["raid_auto_off"] or 900), {},
            )
            return

        await self.set_raid_mode(
            guild, False, reason="No further join activity", automatic=True
        )

    async def _prune_join_log(self, guild_id: int, payload: dict[str, Any]) -> None:
        """Drop old join rows, then queue the next sweep."""
        cutoff = int(time.time()) - JOIN_LOG_RETENTION_DAYS * 86400
        cursor = await self.bot.db.execute(
            "DELETE FROM join_log WHERE joined_at < ?", (cutoff,)
        )
        if cursor.rowcount:
            log.info("Pruned %d old join_log row(s)", cursor.rowcount)
        await self.bot.scheduler.schedule(
            guild_id, "prune_join_log", int(time.time()) + 86400, {}
        )

    # =======================================================================
    # Quarantine
    # =======================================================================

    async def quarantine_member(
        self, member: discord.Member, moderator: discord.abc.User, reason: str
    ) -> discord.Role:
        """Strip a member's roles and apply the quarantine role.

        The removed roles are written down first. Stripping is easy; giving back
        exactly what someone had is only possible if it was recorded.
        """
        guild = member.guild
        role_id = await self.bot.guild_config.value(guild.id, "quarantine_role_id")
        role = guild.get_role(int(role_id)) if role_id else None
        if role is None:
            raise NotConfigured("a quarantine role", "Anti-raid")

        usable, why = permissions.can_manage_role(guild, role)
        if not usable:
            raise DeezeeError(f"I cannot use the quarantine role: {why}")

        removable = [
            r for r in member.roles
            if not r.is_default() and not r.managed and r < guild.me.top_role
        ]

        await self.bot.db.execute(
            "INSERT INTO quarantine (guild_id, user_id, roles, quarantined_by, reason, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(guild_id, user_id) "
            "DO UPDATE SET roles = excluded.roles, reason = excluded.reason",
            (guild.id, member.id, json.dumps([r.id for r in removable]),
             moderator.id, reason, int(time.time())),
        )

        try:
            if removable:
                await member.remove_roles(*removable, reason=reason)
            await member.add_roles(role, reason=reason)
        except discord.Forbidden:
            raise commands.BotMissingPermissions(["manage_roles"]) from None

        return role

    # =======================================================================
    # Verification
    # =======================================================================

    async def _apply_unverified(self, member: discord.Member) -> None:
        """Put a new member behind the gate."""
        role_id = await self.bot.guild_config.value(
            member.guild.id, "unverified_role_id"
        )
        if not role_id:
            return
        role = member.guild.get_role(int(role_id))
        if role is None:
            return
        usable, _ = permissions.can_manage_role(member.guild, role)
        if not usable:
            return
        try:
            await member.add_roles(role, reason="Awaiting verification")
        except discord.HTTPException:
            pass

    async def begin_verification(self, interaction: discord.Interaction) -> None:
        """Issue a challenge to whoever pressed the verify button."""
        guild = interaction.guild
        if guild is None:
            return

        config = await self.bot.guild_config.get(guild.id)
        if config["verification_mode"] == "off":
            await interaction.response.send_message(
                "Verification is not enabled in this server.", ephemeral=True
            )
            return

        member = interaction.user
        verified_id = config["verified_role_id"]
        if verified_id and isinstance(member, discord.Member):
            if any(r.id == int(verified_id) for r in member.roles):
                await interaction.response.send_message(
                    "You are already verified.", ephemeral=True
                )
                return

        # A button-only gate has no challenge to solve; pressing it is the test.
        if config["captcha_type"] == "button":
            await self._grant_verified(interaction, member)
            return

        expires = int(time.time()) + int(config["verification_timeout"] or 600)

        if config["captcha_type"] == "text":
            answer, question = captcha_service.text_challenge()
            await self._store_challenge(guild.id, member.id, answer, expires)
            view = CaptchaView(self, member)
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="Verification",
                    description=f"{question}\n\nPress the button and type your answer.",
                    colour=COLOUR_INFO,
                ),
                view=view,
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        answer, png = await captcha_service.generate()
        await self._store_challenge(guild.id, member.id, answer, expires)

        embed = discord.Embed(
            title="Verification",
            description="Type the characters shown below. Case does not matter.",
            colour=COLOUR_INFO,
        )
        embed.set_image(url="attachment://captcha.png")
        embed.set_footer(
            text=f"Expires in {format_duration(config['verification_timeout'])}"
        )
        await interaction.followup.send(
            embed=embed,
            file=discord.File(io.BytesIO(png), filename="captcha.png"),
            view=CaptchaView(self, member),
            ephemeral=True,
        )

    async def _store_challenge(
        self, guild_id: int, user_id: int, answer: str, expires: int
    ) -> None:
        """Persist a challenge so a restart does not invalidate it."""
        await self.bot.db.execute(
            "INSERT INTO verification (guild_id, user_id, answer, attempts, issued_at, "
            "expires_at) VALUES (?, ?, ?, 0, ?, ?) ON CONFLICT(guild_id, user_id) "
            "DO UPDATE SET answer = excluded.answer, attempts = 0, "
            "issued_at = excluded.issued_at, expires_at = excluded.expires_at, "
            "verified_at = NULL",
            (guild_id, user_id, answer, int(time.time()), expires),
        )

    async def check_answer(self, interaction: discord.Interaction, given: str) -> None:
        """Compare a submitted answer against the stored challenge."""
        guild = interaction.guild
        row = await self.bot.db.fetchone(
            "SELECT answer, attempts, expires_at FROM verification "
            "WHERE guild_id = ? AND user_id = ?",
            (guild.id, interaction.user.id),
        )
        if row is None or row["answer"] is None:
            await interaction.response.send_message(
                "You have no challenge waiting. Press Verify to get one.", ephemeral=True
            )
            return

        if row["expires_at"] < int(time.time()):
            await interaction.response.send_message(
                "That challenge expired. Press Verify to get a new one.", ephemeral=True
            )
            return

        config = await self.bot.guild_config.get(guild.id)
        limit = int(config["verification_attempts"] or 3)

        if captcha_service.normalise_answer(given) == captcha_service.normalise_answer(
            row["answer"]
        ):
            await self._grant_verified(interaction, interaction.user)
            return

        attempts = row["attempts"] + 1
        await self.bot.db.execute(
            "UPDATE verification SET attempts = ? WHERE guild_id = ? AND user_id = ?",
            (attempts, guild.id, interaction.user.id),
        )

        if attempts >= limit:
            await self.bot.db.execute(
                "UPDATE verification SET answer = NULL WHERE guild_id = ? AND user_id = ?",
                (guild.id, interaction.user.id),
            )
            await interaction.response.send_message(
                f"That was wrong {attempts} times, so this challenge is closed. "
                "Press Verify to start again.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"That is not right. {limit - attempts} attempt(s) left.", ephemeral=True
        )

    async def _grant_verified(
        self, interaction: discord.Interaction, member: discord.abc.User
    ) -> None:
        """Give the verified role and remove the holding role."""
        guild = interaction.guild
        config = await self.bot.guild_config.get(guild.id)

        if not isinstance(member, discord.Member):
            member = await self.bot.get_or_fetch_member(guild, member.id)
        if member is None:
            return

        added, removed = await self.apply_verified_roles(member, config)

        await self.bot.db.execute(
            "INSERT INTO verification (guild_id, user_id, answer, issued_at, expires_at, "
            "verified_at) VALUES (?, ?, NULL, ?, ?, ?) ON CONFLICT(guild_id, user_id) "
            "DO UPDATE SET answer = NULL, verified_at = excluded.verified_at",
            (guild.id, member.id, int(time.time()), int(time.time()), int(time.time())),
        )

        detail = ""
        if not added and config["verified_role_id"]:
            detail = "\nI could not give you the verified role -- tell a moderator."

        message = f"{EMOJI_SUCCESS}  Verified. Welcome to {guild.name}.{detail}"
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def apply_verified_roles(
        self, member: discord.Member, config: dict[str, Any]
    ) -> tuple[bool, bool]:
        """Add the verified role and strip the unverified one.

        Returns:
            ``(added, removed)``, so the caller can say what actually happened
            rather than claiming success it did not achieve.
        """
        guild = member.guild
        added = removed = False

        verified_id = config.get("verified_role_id")
        if verified_id:
            role = guild.get_role(int(verified_id))
            if role is not None and permissions.can_manage_role(guild, role)[0]:
                try:
                    await member.add_roles(role, reason="Verified")
                    added = True
                except discord.HTTPException:
                    pass

        unverified_id = config.get("unverified_role_id")
        if unverified_id:
            role = guild.get_role(int(unverified_id))
            if role is not None and role in member.roles:
                try:
                    await member.remove_roles(role, reason="Verified")
                    removed = True
                except discord.HTTPException:
                    pass

        return added, removed

    # =======================================================================
    # Commands
    # =======================================================================

    @commands.hybrid_command(name="antiraid", aliases=["raid"])
    @permissions.admin_only()
    @permissions.guild_only()
    async def antiraid(self, ctx: commands.Context) -> None:
        """Open the anti-raid panel."""
        await open_panel(ctx, self)

    @commands.hybrid_command(name="raidmode")
    @app_commands.describe(state="on or off", reason="Why")
    @permissions.admin_only()
    @permissions.guild_only()
    async def raidmode(
        self, ctx: commands.Context, state: str, *, reason: str = "Set by a moderator"
    ) -> None:
        """Force raid mode on or off."""
        state = state.lower()
        if state not in {"on", "off"}:
            raise DeezeeError("Use `?raidmode on` or `?raidmode off`.")

        config = await self.bot.guild_config.get(ctx.guild.id)
        enabled = state == "on"
        if bool(config["raid_mode"]) == enabled:
            raise DeezeeError(f"Raid mode is already {state}.")

        await self.set_raid_mode(ctx.guild, enabled, reason=f"{ctx.author}: {reason}")
        await ctx.send(embed=self._ok(
            f"Raid mode **{state}**." + (
                f" New joins will be **{config['raid_action']}**." if enabled else ""
            )
        ))

    @commands.hybrid_command(name="panic", aliases=["emergency"])
    @app_commands.describe(minutes="Quarantine members who joined in the last N minutes")
    @permissions.admin_only()
    @permissions.guild_only()
    async def panic(self, ctx: commands.Context, minutes: int = 10) -> None:
        """Emergency response: raid mode, lock channels, quarantine recent joins."""
        await self._defer(ctx)
        minutes = max(1, min(minutes, 1440))
        since = int(time.time()) - minutes * 60

        rows = await self.bot.db.fetchall(
            "SELECT user_id, user_tag, risk_score FROM join_log "
            "WHERE guild_id = ? AND joined_at >= ? AND left_at IS NULL "
            "ORDER BY joined_at DESC",
            (ctx.guild.id, since),
        )
        lockable = [
            c for c in ctx.guild.text_channels
            if c.permissions_for(ctx.guild.me).manage_channels
        ]

        # Dry run first. Nobody should press a panic button without being told
        # exactly how many people it touches.
        if not await confirm(
            ctx,
            title="Emergency response",
            description=(
                f"This will:\n"
                f"• enable raid mode\n"
                f"• lock **{len(lockable)}** channel(s)\n"
                f"• quarantine **{len(rows)}** member(s) who joined in the last "
                f"{minutes} minute(s)"
            ),
            confirm_label="Continue",
            fields={"Members affected": str(len(rows))},
        ):
            return

        if not await confirm(
            ctx,
            title="Are you certain?",
            description=(
                f"**{len(rows)}** members lose every role and **{len(lockable)}** "
                "channels close. Roles are recorded and can be restored with "
                "`?unquarantine`, and `?lockdown end` reopens the channels."
            ),
            confirm_label="Panic",
        ):
            return

        config = await self.bot.guild_config.get(ctx.guild.id)
        if not config["raid_mode"]:
            await self.set_raid_mode(
                ctx.guild, True, reason=f"Panic invoked by {ctx.author}"
            )

        moderation = self.bot.get_cog("Moderation")
        locked = 0
        if moderation is not None:
            for channel in lockable:
                try:
                    locked += await moderation._lock_channel(
                        channel, ctx.author, reason="Panic", from_lockdown=True
                    )
                except commands.BotMissingPermissions:
                    continue

        quarantined = 0
        failed = 0
        for row in rows:
            member = await self.bot.get_or_fetch_member(ctx.guild, row["user_id"])
            if member is None:
                continue
            try:
                await self.quarantine_member(member, ctx.author, "Panic response")
                quarantined += 1
            except (DeezeeError, commands.BotMissingPermissions, discord.HTTPException):
                failed += 1

        await modlog.create_case(
            self.bot, ctx.guild, action="lockdown", target=ctx.author,
            moderator=ctx.author,
            reason=f"Panic: {locked} channels locked, {quarantined} quarantined",
        )
        await ctx.send(embed=self._ok(
            f"Raid mode on. Locked **{locked}** channel(s). "
            f"Quarantined **{quarantined}** member(s)"
            + (f", **{failed}** failed" if failed else "")
            + ".\nUse `?lockdown end` and `?unquarantine` to reverse this."
        ))

    @commands.hybrid_command(name="verification", with_app_command=False, aliases=["verifyconfig"])
    @permissions.admin_only()
    @permissions.guild_only()
    async def verification(self, ctx: commands.Context) -> None:
        """Configure the verification gate."""
        await open_verification(ctx, self)

    @commands.hybrid_command(name="verify")
    @permissions.guild_only()
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def verify(self, ctx: commands.Context) -> None:
        """Start or retry verification."""
        config = await self.bot.guild_config.get(ctx.guild.id)
        if config["verification_mode"] == "off":
            raise DeezeeError("Verification is not enabled in this server.")

        if ctx.interaction is not None:
            await self.begin_verification(ctx.interaction)
            return

        # Prefix path: a modal needs an interaction, so a button provides one.
        view = VerifyStartView(self)
        await ctx.send(
            embed=discord.Embed(
                title="Verification",
                description="Press the button below to begin.",
                colour=COLOUR_INFO,
            ),
            view=view,
        )

    @commands.hybrid_command(name="forceverify", with_app_command=False)
    @app_commands.describe(target="Member to verify", revoke="Remove their verification")
    @permissions.mod_only()
    @permissions.guild_only()
    async def forceverify(
        self, ctx: commands.Context, target: discord.Member, revoke: bool = False
    ) -> None:
        """Mark a member verified by hand, or revoke it."""
        config = await self.bot.guild_config.get(ctx.guild.id)

        if revoke:
            verified_id = config["verified_role_id"]
            unverified_id = config["unverified_role_id"]
            changed = False
            if verified_id:
                role = ctx.guild.get_role(int(verified_id))
                if role and role in target.roles:
                    await target.remove_roles(role, reason=f"Revoked by {ctx.author}")
                    changed = True
            if unverified_id:
                role = ctx.guild.get_role(int(unverified_id))
                if role and permissions.can_manage_role(ctx.guild, role)[0]:
                    await target.add_roles(role, reason=f"Revoked by {ctx.author}")
                    changed = True
            await self.bot.db.execute(
                "UPDATE verification SET verified_at = NULL, answer = NULL "
                "WHERE guild_id = ? AND user_id = ?",
                (ctx.guild.id, target.id),
            )
            if not changed:
                raise DeezeeError(f"**{target}** was not verified.")
            await ctx.send(embed=self._ok(f"Revoked verification for **{target}**."))
            return

        added, removed = await self.apply_verified_roles(target, config)
        now = int(time.time())
        await self.bot.db.execute(
            "INSERT INTO verification (guild_id, user_id, answer, issued_at, expires_at, "
            "verified_at) VALUES (?, ?, NULL, ?, ?, ?) ON CONFLICT(guild_id, user_id) "
            "DO UPDATE SET answer = NULL, verified_at = excluded.verified_at",
            (ctx.guild.id, target.id, now, now, now),
        )
        detail = "" if added or not config["verified_role_id"] else (
            " I could not give them the verified role -- check my role position."
        )
        await ctx.send(embed=self._ok(f"**{target}** is now verified.{detail}"))

    @commands.hybrid_command(name="altcheck", aliases=["risk"])
    @app_commands.describe(target="Member to score")
    @permissions.mod_only()
    @permissions.guild_only()
    async def altcheck(self, ctx: commands.Context, target: discord.Member) -> None:
        """Score a member's alt-account risk, showing every signal."""
        await self._defer(ctx)

        row = await self.bot.db.fetchone(
            "SELECT joined_at, invite_code, inviter_id FROM join_log "
            "WHERE guild_id = ? AND user_id = ? ORDER BY joined_at DESC LIMIT 1",
            (ctx.guild.id, target.id),
        )
        burst = 0
        if row is not None:
            burst = await self.bot.db.fetchval(
                "SELECT COUNT(*) FROM join_log WHERE guild_id = ? "
                "AND joined_at BETWEEN ? AND ? AND user_id != ?",
                (ctx.guild.id, row["joined_at"] - 60, row["joined_at"] + 60, target.id),
            )

        result = await self._score_member(
            target, burst=burst, invite_known=bool(row and row["invite_code"])
        )

        colour = (
            COLOUR_ERROR if result.score >= 80
            else COLOUR_WARNING if result.score >= 55
            else COLOUR_SUCCESS
        )
        embed = discord.Embed(
            title=f"Risk score: {result.score}/100 ({result.level})",
            description=f"{target.mention} `{target}` (`{target.id}`)",
            colour=colour,
        )
        embed.add_field(name="Signals", value="\n".join(result.reasons)[:1024], inline=False)
        embed.add_field(
            name="Account created",
            value=f"<t:{int(target.created_at.timestamp())}:{TS_RELATIVE}>",
            inline=True,
        )
        if target.joined_at:
            embed.add_field(
                name="Joined",
                value=f"<t:{int(target.joined_at.timestamp())}:{TS_RELATIVE}>",
                inline=True,
            )
        if row and row["invite_code"]:
            embed.add_field(
                name="Invite",
                value=f"`{row['invite_code']}`"
                + (f" by <@{row['inviter_id']}>" if row["inviter_id"] else ""),
                inline=True,
            )
        embed.set_footer(
            text="Heuristic only. Discord never gives bots IP addresses, so this "
            "cannot detect VPNs or shared devices. Treat it as evidence, not proof."
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="altconfig", with_app_command=False)
    @permissions.admin_only()
    @permissions.guild_only()
    async def altconfig(self, ctx: commands.Context) -> None:
        """Set the alt-risk thresholds and the automatic action."""
        await open_altconfig(ctx, self)

    @commands.hybrid_command(name="minage", with_app_command=False, aliases=["accountage"])
    @app_commands.describe(
        duration="Minimum account age, e.g. 7d. Use 'off' to disable",
        action="kick, ban or quarantine",
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def minage(
        self, ctx: commands.Context, duration: str, action: str = "kick"
    ) -> None:
        """Reject joins from accounts younger than a set age."""
        if duration.lower() in {"off", "none", "0", "disable"}:
            await self.bot.guild_config.set(ctx.guild.id, "min_account_age", 0)
            await ctx.send(embed=self._ok("Minimum account age disabled."))
            return

        delta = parse_duration(duration)
        if delta is None:
            raise DeezeeError(f"`{duration}` is not a duration. Try `7d` or `24h`.")
        if action.lower() not in {"kick", "ban", "quarantine"}:
            raise DeezeeError("Action must be `kick`, `ban` or `quarantine`.")

        await self.bot.guild_config.set_many(ctx.guild.id, {
            "min_account_age": int(delta.total_seconds()),
            "min_age_action": action.lower(),
        })
        await ctx.send(embed=self._ok(
            f"Accounts younger than **{format_duration(delta)}** will be "
            f"**{action.lower()}** on join."
        ))

    @commands.hybrid_command(name="joins", aliases=["joinwatch", "recentjoins"])
    @app_commands.describe(minutes="How far back to look. Default 60")
    @permissions.mod_only()
    @permissions.guild_only()
    async def joins(self, ctx: commands.Context, minutes: int = 60) -> None:
        """List recent joins with account age, invite and risk score."""
        minutes = max(1, min(minutes, 10080))
        since = int(time.time()) - minutes * 60

        rows = await self.bot.db.fetchall(
            "SELECT user_id, user_tag, joined_at, account_created, invite_code, "
            "risk_score, left_at FROM join_log WHERE guild_id = ? AND joined_at >= ? "
            "ORDER BY joined_at DESC",
            (ctx.guild.id, since),
        )

        lines = []
        for row in rows:
            age = format_duration(row["joined_at"] - row["account_created"], brief=True)
            marker = "" if row["left_at"] is None else " *(left)*"
            invite = f" • `{row['invite_code']}`" if row["invite_code"] else ""
            lines.append(
                f"**{row['user_tag']}** `{row['user_id']}`{marker}\n"
                f"risk **{row['risk_score']}** • account {age} old • "
                f"joined <t:{row['joined_at']}:{TS_RELATIVE}>{invite}"
            )

        pages = paginate_lines(
            lines,
            title=f"Joins in the last {minutes} minute(s)",
            per_page=6,
            description=f"{len(lines)} join(s).",
            empty_message="Nobody joined in that window.",
        )
        await Paginator(pages, ctx.author).start(ctx)

    @commands.hybrid_command(name="flagged", aliases=["altlist"])
    @permissions.mod_only()
    @permissions.guild_only()
    async def flagged(self, ctx: commands.Context) -> None:
        """List members currently flagged by the alt heuristics."""
        rows = await self.bot.db.fetchall(
            "SELECT user_id, user_tag, joined_at, risk_score, risk_reasons FROM join_log "
            "WHERE guild_id = ? AND flagged = 1 AND left_at IS NULL "
            "ORDER BY risk_score DESC, joined_at DESC LIMIT 200",
            (ctx.guild.id,),
        )

        lines = []
        for row in rows:
            try:
                reasons = json.loads(row["risk_reasons"])
            except (ValueError, TypeError):
                reasons = []
            lines.append(
                f"**{row['user_tag']}** `{row['user_id']}` — risk **{row['risk_score']}**\n"
                + "\n".join(f"• {r}" for r in reasons[:3])
            )

        pages = paginate_lines(
            lines,
            title="Flagged members",
            per_page=4,
            description=f"{len(lines)} still in the server. "
            "Scores are heuristic -- review before acting.",
            empty_message="Nobody is currently flagged.",
        )
        await Paginator(pages, ctx.author).start(ctx)

    @commands.hybrid_command(name="quarantine")
    @app_commands.describe(target="Member to hold", reason="Why")
    @permissions.mod_only()
    @permissions.guild_only()
    async def quarantine(
        self, ctx: commands.Context, target: discord.Member, *, reason: str = "Pending review"
    ) -> None:
        """Strip a member's roles and apply the quarantine role."""
        await permissions.enforce_hierarchy(
            ctx, target, action="quarantine", bot_permission="manage_roles"
        )

        existing = await self.bot.db.fetchone(
            "SELECT user_id FROM quarantine WHERE guild_id = ? AND user_id = ?",
            (ctx.guild.id, target.id),
        )
        if existing is not None:
            raise DeezeeError(f"**{target}** is already quarantined.")

        role = await self.quarantine_member(target, ctx.author, reason)
        await modlog.create_case(
            self.bot, ctx.guild, action="mute", target=target,
            moderator=ctx.author, reason=f"Quarantined: {reason}",
        )
        await ctx.send(embed=self._ok(
            f"**{target}** quarantined with {role.mention}. "
            "Their roles are recorded and `?unquarantine` restores them."
        ))

    @commands.hybrid_command(name="unquarantine")
    @app_commands.describe(target="Member to release")
    @permissions.mod_only()
    @permissions.guild_only()
    async def unquarantine(self, ctx: commands.Context, target: discord.Member) -> None:
        """Release a member from quarantine and restore their previous roles."""
        row = await self.bot.db.fetchone(
            "SELECT roles FROM quarantine WHERE guild_id = ? AND user_id = ?",
            (ctx.guild.id, target.id),
        )
        if row is None:
            raise DeezeeError(f"**{target}** is not quarantined.")

        try:
            role_ids = json.loads(row["roles"])
        except (ValueError, TypeError):
            role_ids = []

        restorable = [
            role for role in (ctx.guild.get_role(int(r)) for r in role_ids)
            if role is not None and permissions.can_manage_role(ctx.guild, role)[0]
        ]
        missing = len(role_ids) - len(restorable)

        quarantine_id = await self.bot.guild_config.value(
            ctx.guild.id, "quarantine_role_id"
        )
        try:
            if restorable:
                await target.add_roles(*restorable, reason=f"Released by {ctx.author}")
            if quarantine_id:
                role = ctx.guild.get_role(int(quarantine_id))
                if role is not None and role in target.roles:
                    await target.remove_roles(role, reason=f"Released by {ctx.author}")
        except discord.Forbidden:
            raise commands.BotMissingPermissions(["manage_roles"]) from None

        await self.bot.db.execute(
            "DELETE FROM quarantine WHERE guild_id = ? AND user_id = ?",
            (ctx.guild.id, target.id),
        )
        await modlog.create_case(
            self.bot, ctx.guild, action="unmute", target=target,
            moderator=ctx.author, reason="Released from quarantine",
        )
        note = f" {missing} role(s) could not be restored." if missing else ""
        await ctx.send(embed=self._ok(
            f"**{target}** released, {len(restorable)} role(s) restored.{note}"
        ))

    @commands.hybrid_command(name="invitetrack", with_app_command=False, aliases=["invites"])
    @app_commands.describe(target="Optional: which invite this member joined on")
    @permissions.admin_only()
    @permissions.guild_only()
    async def invitetrack(
        self, ctx: commands.Context, target: Optional[discord.Member] = None
    ) -> None:
        """Show which invite a member joined on, and per-inviter counts."""
        if not ctx.guild.me.guild_permissions.manage_guild:
            raise commands.BotMissingPermissions(["manage_guild"])

        if target is not None:
            row = await self.bot.db.fetchone(
                "SELECT invite_code, inviter_id, joined_at FROM join_log "
                "WHERE guild_id = ? AND user_id = ? ORDER BY joined_at DESC LIMIT 1",
                (ctx.guild.id, target.id),
            )
            if row is None:
                raise DeezeeError(
                    f"No join record for **{target}**. Only joins since I was added "
                    "are tracked."
                )
            embed = discord.Embed(title=f"Invite used by {target}", colour=COLOUR_DEFAULT)
            embed.add_field(
                name="Invite", value=f"`{row['invite_code']}`" if row["invite_code"]
                else "could not be determined", inline=True,
            )
            embed.add_field(
                name="Inviter",
                value=f"<@{row['inviter_id']}>" if row["inviter_id"] else "unknown",
                inline=True,
            )
            embed.add_field(
                name="Joined", value=f"<t:{row['joined_at']}:{TS_RELATIVE}>", inline=True
            )
            await ctx.send(embed=embed)
            return

        rows = await self.bot.db.fetchall(
            "SELECT inviter_id, COUNT(*) AS total FROM join_log "
            "WHERE guild_id = ? AND inviter_id IS NOT NULL "
            "GROUP BY inviter_id ORDER BY total DESC LIMIT 50",
            (ctx.guild.id,),
        )
        lines = [
            f"**{i}.** <@{row['inviter_id']}> — **{row['total']}** join(s)"
            for i, row in enumerate(rows, start=1)
        ]
        pages = paginate_lines(
            lines,
            title="Invites by inviter",
            per_page=15,
            description="Counts only joins recorded since I was added.",
            empty_message="No tracked joins yet.",
        )
        await Paginator(pages, ctx.author).start(ctx)

    # =======================================================================
    # Helpers
    # =======================================================================

    @staticmethod
    def _ok(description: str) -> discord.Embed:
        return discord.Embed(
            description=f"{EMOJI_SUCCESS}  {description}", colour=COLOUR_SUCCESS
        )

    @staticmethod
    async def _defer(ctx: commands.Context) -> None:
        """Acknowledge a slash command before slow work."""
        if ctx.interaction is not None and not ctx.interaction.response.is_done():
            await ctx.defer()


async def setup(bot: commands.Bot) -> None:
    """discord.py extension entry point."""
    await bot.add_cog(AntiRaid(bot))
