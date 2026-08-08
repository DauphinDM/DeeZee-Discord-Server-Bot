"""Tags, custom commands and autoresponses.

Covers the 13 commands in the command sheet's "Tags & Custom Commands" section.

Three related but deliberately separate things live here:

* **Tags** are content anybody may recall by name. Owned by whoever made them.
* **Custom commands** are prefix commands that can also add or remove roles, and
  can be restricted to a role or a channel. Kept apart from tags because "can
  hand out a role" is a different permission story from "can store text".
* **Autoresponses** fire on message content with no prefix at all.

All three render through ``services/tagvars.py``, which is a substituter over a
fixed table of names -- not a scripting language. See DESIGN.md decision 4.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from core import permissions
from core.constants import (
    COLOUR_DEFAULT,
    COLOUR_INFO,
    COLOUR_SUCCESS,
    EMOJI_OFF,
    EMOJI_ON,
    EMOJI_SUCCESS,
    TS_LONG_DATE_TIME,
)
from core.errors import DeezeeError
from services import tagvars
from services.filters import is_regex_safe
from ui.paginator import Paginator, paginate_lines, paginate_text
from ui.views import confirm

log = logging.getLogger(__name__)

#: Names that would shadow a real command or a subcommand of ``?tag``.
RESERVED_TAG_NAMES = frozenset({
    "create", "add", "edit", "delete", "remove", "list", "info", "raw",
    "search", "alias", "transfer", "claim",
})

#: How a trigger is matched against a message.
AR_MODES = ("contains", "exact", "startswith", "endswith", "regex")

#: Autoresponses considered per message. A guild with hundreds would otherwise
#: be a per-message loop over all of them.
MAX_AR_PER_GUILD = 100

_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,32}$")


class TagContentModal(discord.ui.Modal):
    """Multi-line editor for a tag body.

    A modal rather than a command argument because tag bodies are routinely
    several lines, and typing a newline into a chat box is awkward on every
    platform.
    """

    def __init__(self, cog: Tags, name: str, existing: str = "") -> None:
        super().__init__(title=f"Tag: {name}"[:45], timeout=600.0)
        self.cog = cog
        self.name = name
        self.body = discord.ui.TextInput(
            label="Content",
            style=discord.TextStyle.paragraph,
            max_length=2000,
            default=existing,
            required=True,
        )
        self.add_item(self.body)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.save_tag(interaction, self.name, self.body.value)


class Tags(commands.Cog):
    """Stored text, custom prefix commands and automatic replies."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        #: Compiled regex autoresponses, keyed by row ID. Compiled once at first
        #: use rather than per message.
        self._patterns: dict[int, re.Pattern[str]] = {}
        #: Per-guild autoresponse rows, cached. The listener runs on every
        #: message, and a query per message is what the CPU budget forbids.
        self._responses: dict[int, list[dict[str, Any]]] = {}
        self._ar_ignores: dict[int, set[tuple[int, str]]] = {}
        #: Per-guild custom commands, cached for the same reason.
        self._custom: dict[int, dict[str, dict[str, Any]]] = {}

    @staticmethod
    def _ok(description: str) -> discord.Embed:
        return discord.Embed(
            description=f"{EMOJI_SUCCESS}  {description}", colour=COLOUR_SUCCESS
        )

    def invalidate(self, guild_id: int) -> None:
        self._responses.pop(guild_id, None)
        self._ar_ignores.pop(guild_id, None)
        self._custom.pop(guild_id, None)

    # =======================================================================
    # Rendering
    # =======================================================================

    def context_for(self, message_or_ctx: Any, args: str = "") -> dict[str, str]:
        """Build the substitution mapping from a message or a command context."""
        author = getattr(message_or_ctx, "author", None)
        guild = getattr(message_or_ctx, "guild", None)
        channel = getattr(message_or_ctx, "channel", None)

        return tagvars.build_context(
            user_name=getattr(author, "display_name", ""),
            user_username=getattr(author, "name", ""),
            user_tag=str(author) if author else "",
            user_id=getattr(author, "id", 0),
            user_mention=getattr(author, "mention", ""),
            user_avatar=str(author.display_avatar.url) if author else "",
            server_name=getattr(guild, "name", ""),
            server_id=getattr(guild, "id", 0),
            server_count=getattr(guild, "member_count", 0) or 0,
            server_icon=str(guild.icon.url) if guild and guild.icon else "",
            channel_name=getattr(channel, "name", ""),
            channel_id=getattr(channel, "id", 0),
            channel_mention=getattr(channel, "mention", ""),
            args=args,
        )

    # =======================================================================
    # Tags
    # =======================================================================

    async def fetch_tag(self, guild_id: int, name: str) -> dict[str, Any] | None:
        """Look a tag up by name, following one alias hop."""
        row = await self.bot.db.fetchone(
            "SELECT * FROM tags WHERE guild_id = ? AND name = ?",
            (guild_id, name.strip().lower()),
        )
        if row is None:
            return None
        if row["alias_of"] is None:
            return dict(row)

        target = await self.bot.db.fetchone(
            "SELECT * FROM tags WHERE id = ?", (row["alias_of"],)
        )
        if target is None:
            return None
        # One hop only. An alias may not point at another alias, so a cycle is
        # impossible rather than merely unlikely.
        resolved = dict(target)
        resolved["requested_name"] = row["name"]
        return resolved

    async def save_tag(
        self, interaction: discord.Interaction, name: str, content: str
    ) -> None:
        """Create or update a tag from the modal."""
        now = int(time.time())
        existing = await self.bot.db.fetchone(
            "SELECT * FROM tags WHERE guild_id = ? AND name = ?",
            (interaction.guild.id, name),
        )

        unknown = tagvars.find_unknown(content)
        warning = (
            f"\n\nUnrecognised variable(s), left as literal text: "
            + ", ".join(f"`{{{u}}}`" for u in unknown)
            if unknown
            else ""
        )

        if existing is None:
            await self.bot.db.execute(
                "INSERT INTO tags (guild_id, name, content, owner_id, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (interaction.guild.id, name, content, interaction.user.id, now, now),
            )
            verb = "Created"
        else:
            await self.bot.db.execute(
                "UPDATE tags SET content = ?, updated_at = ? WHERE id = ?",
                (content, now, existing["id"]),
            )
            verb = "Updated"

        await interaction.response.send_message(
            embed=self._ok(f"{verb} the tag `{name}`.{warning}"), ephemeral=True
        )

    @commands.hybrid_group(name="tag", aliases=["t"], fallback="show")
    @app_commands.describe(name="Tag name", args="Text passed to the tag as {args}")
    @permissions.guild_only()
    async def tag(
        self, ctx: commands.Context, name: str, *, args: str = ""
    ) -> None:
        """Show a stored tag."""
        row = await self.fetch_tag(ctx.guild.id, name)
        if row is None:
            close = await self.bot.db.fetchall(
                "SELECT name FROM tags WHERE guild_id = ? AND name LIKE ? LIMIT 5",
                (ctx.guild.id, f"%{name.strip().lower()}%"),
            )
            hint = (
                "\nDid you mean: "
                + ", ".join(f"`{r['name']}`" for r in close)
                if close
                else ""
            )
            raise DeezeeError(f"There is no tag called `{name}`.{hint}")

        await self.bot.db.execute(
            "UPDATE tags SET uses = uses + 1 WHERE id = ?", (row["id"],)
        )

        text = tagvars.render(row["content"], self.context_for(ctx, args))
        await ctx.send(
            text[:2000],
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=False
            ),
        )

    @tag.command(name="create", aliases=["add"])
    @app_commands.describe(name="Tag name", content="Body. Blank opens an editor")
    @permissions.guild_only()
    async def tag_create(
        self, ctx: commands.Context, name: str, *, content: str = ""
    ) -> None:
        """Create a tag owned by you."""
        clean = name.strip().lower()
        self._validate_name(clean)
        if clean in RESERVED_TAG_NAMES:
            raise DeezeeError(
                f"`{clean}` is a `?tag` subcommand and cannot be a tag name."
            )

        existing = await self.bot.db.fetchone(
            "SELECT owner_id FROM tags WHERE guild_id = ? AND name = ?",
            (ctx.guild.id, clean),
        )
        if existing is not None:
            raise DeezeeError(f"A tag called `{clean}` already exists.")

        if not content:
            if ctx.interaction is None:
                raise DeezeeError(
                    "Give the content, or use the slash version to get a multi-line "
                    "editor: `/tag create`."
                )
            await ctx.interaction.response.send_modal(TagContentModal(self, clean))
            return

        now = int(time.time())
        await self.bot.db.execute(
            "INSERT INTO tags (guild_id, name, content, owner_id, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (ctx.guild.id, clean, content[:2000], ctx.author.id, now, now),
        )

        unknown = tagvars.find_unknown(content)
        note = (
            "\nUnrecognised variable(s), left as literal text: "
            + ", ".join(f"`{{{u}}}`" for u in unknown)
            if unknown
            else ""
        )
        await ctx.send(embed=self._ok(f"Created the tag `{clean}`.{note}"))

    @tag.command(name="edit")
    @app_commands.describe(name="Tag to edit", content="New body. Blank opens an editor")
    @permissions.guild_only()
    async def tag_edit(
        self, ctx: commands.Context, name: str, *, content: str = ""
    ) -> None:
        """Edit a tag you own."""
        row = await self._owned_tag(ctx, name)

        if not content:
            if ctx.interaction is None:
                raise DeezeeError(
                    "Give the new content, or use `/tag edit` for a multi-line editor."
                )
            await ctx.interaction.response.send_modal(
                TagContentModal(self, row["name"], row["content"])
            )
            return

        await self.bot.db.execute(
            "UPDATE tags SET content = ?, updated_at = ? WHERE id = ?",
            (content[:2000], int(time.time()), row["id"]),
        )
        await ctx.send(embed=self._ok(f"Updated `{row['name']}`."))

    @tag.command(name="delete", aliases=["remove"])
    @app_commands.describe(name="Tag to delete")
    @permissions.guild_only()
    async def tag_delete(self, ctx: commands.Context, name: str) -> None:
        """Delete a tag you own."""
        row = await self._owned_tag(ctx, name)

        aliases = await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM tags WHERE alias_of = ?", (row["id"],)
        )
        proceed = await confirm(
            ctx,
            title=f"Delete the tag {row['name']}?",
            description=(
                f"It has been used **{row['uses']}** time(s)."
                + (
                    f"\n**{aliases}** alias(es) point at it and will be deleted too."
                    if aliases
                    else ""
                )
            ),
            confirm_label="Delete",
        )
        if not proceed:
            return

        await self.bot.db.execute("DELETE FROM tags WHERE id = ?", (row["id"],))
        await ctx.send(embed=self._ok(f"Deleted `{row['name']}`."))

    @tag.command(name="list")
    @app_commands.describe(owner="Only tags owned by this member")
    @permissions.guild_only()
    async def tag_list(
        self, ctx: commands.Context, owner: Optional[discord.Member] = None
    ) -> None:
        """List tags, optionally filtered to one owner."""
        if owner is None:
            rows = await self.bot.db.fetchall(
                "SELECT name, uses, alias_of FROM tags WHERE guild_id = ? "
                "ORDER BY uses DESC, name",
                (ctx.guild.id,),
            )
            title = f"Tags in {ctx.guild.name}"
        else:
            rows = await self.bot.db.fetchall(
                "SELECT name, uses, alias_of FROM tags WHERE guild_id = ? "
                "AND owner_id = ? ORDER BY uses DESC, name",
                (ctx.guild.id, owner.id),
            )
            title = f"Tags owned by {owner.display_name}"

        lines = [
            f"`{row['name']}` — {row['uses']} use(s)"
            + ("  *(alias)*" if row["alias_of"] else "")
            for row in rows
        ]
        pages = paginate_lines(
            lines,
            title=title,
            per_page=20,
            description=f"{len(lines)} tag(s). Most-used first.",
            empty_message="No tags yet. Make one with `?tag create`.",
        )
        await Paginator(pages, ctx.author).start(ctx)

    @tag.command(name="info")
    @app_commands.describe(name="Tag to inspect")
    @permissions.guild_only()
    async def tag_info(self, ctx: commands.Context, name: str) -> None:
        """Show a tag's owner, use count and creation date."""
        row = await self.bot.db.fetchone(
            "SELECT * FROM tags WHERE guild_id = ? AND name = ?",
            (ctx.guild.id, name.strip().lower()),
        )
        if row is None:
            raise DeezeeError(f"There is no tag called `{name}`.")

        owner = ctx.guild.get_member(row["owner_id"])
        embed = discord.Embed(title=f"Tag: {row['name']}", colour=COLOUR_INFO)
        embed.add_field(
            name="Owner",
            value=owner.mention if owner else f"`{row['owner_id']}` *(left the server)*",
            inline=True,
        )
        embed.add_field(name="Uses", value=str(row["uses"]), inline=True)
        embed.add_field(
            name="Created",
            value=f"<t:{row['created_at']}:{TS_LONG_DATE_TIME}>",
            inline=True,
        )
        if row["alias_of"]:
            target = await self.bot.db.fetchone(
                "SELECT name FROM tags WHERE id = ?", (row["alias_of"],)
            )
            embed.add_field(
                name="Alias of",
                value=f"`{target['name']}`" if target else "*(deleted)*",
                inline=False,
            )
        else:
            embed.add_field(
                name="Length", value=f"{len(row['content'])} characters", inline=True
            )
        if owner is None:
            embed.set_footer(
                text="The owner has left -- anyone can take it with ?tag transfer."
            )
        await ctx.send(embed=embed)

    @tag.command(name="raw")
    @app_commands.describe(name="Tag whose source to show")
    @permissions.guild_only()
    async def tag_raw(self, ctx: commands.Context, name: str) -> None:
        """Show a tag's source with formatting escaped, for copying."""
        row = await self.fetch_tag(ctx.guild.id, name)
        if row is None:
            raise DeezeeError(f"There is no tag called `{name}`.")
        pages = paginate_text(
            row["content"], title=f"Source of {row['name']}", code_block=""
        )
        await Paginator(pages, ctx.author).start(ctx)

    @tag.command(name="search")
    @app_commands.describe(query="Text to look for in names and content")
    @permissions.guild_only()
    async def tag_search(self, ctx: commands.Context, *, query: str) -> None:
        """Search tag names and content."""
        needle = f"%{query.strip().lower()}%"
        rows = await self.bot.db.fetchall(
            "SELECT name, uses FROM tags WHERE guild_id = ? "
            "AND (name LIKE ? OR LOWER(content) LIKE ?) ORDER BY uses DESC LIMIT 100",
            (ctx.guild.id, needle, needle),
        )
        lines = [f"`{row['name']}` — {row['uses']} use(s)" for row in rows]
        pages = paginate_lines(
            lines,
            title=f"Tags matching “{query}”",
            per_page=20,
            empty_message=f"Nothing matches `{query}`.",
        )
        await Paginator(pages, ctx.author).start(ctx)

    @tag.command(name="alias")
    @app_commands.describe(alias="The new name", target="The existing tag")
    @permissions.guild_only()
    async def tag_alias(
        self, ctx: commands.Context, alias: str, target: str
    ) -> None:
        """Point a second name at an existing tag."""
        clean = alias.strip().lower()
        self._validate_name(clean)
        if clean in RESERVED_TAG_NAMES:
            raise DeezeeError(f"`{clean}` is a `?tag` subcommand.")

        original = await self.bot.db.fetchone(
            "SELECT * FROM tags WHERE guild_id = ? AND name = ?",
            (ctx.guild.id, target.strip().lower()),
        )
        if original is None:
            raise DeezeeError(f"There is no tag called `{target}`.")
        if original["alias_of"]:
            raise DeezeeError(
                f"`{target}` is itself an alias. Point at the real tag instead -- "
                "chains of aliases are refused so a cycle cannot exist."
            )
        existing = await self.bot.db.fetchone(
            "SELECT 1 FROM tags WHERE guild_id = ? AND name = ?", (ctx.guild.id, clean)
        )
        if existing:
            raise DeezeeError(f"`{clean}` is already taken.")

        now = int(time.time())
        await self.bot.db.execute(
            "INSERT INTO tags (guild_id, name, alias_of, owner_id, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (ctx.guild.id, clean, original["id"], ctx.author.id, now, now),
        )
        await ctx.send(
            embed=self._ok(f"`{clean}` now points at `{original['name']}`.")
        )

    @tag.command(name="transfer", aliases=["claim"])
    @app_commands.describe(
        name="Tag to transfer", target="New owner. Blank claims it for yourself"
    )
    @permissions.guild_only()
    async def tag_transfer(
        self, ctx: commands.Context, name: str,
        target: Optional[discord.Member] = None,
    ) -> None:
        """Transfer ownership, or claim a tag whose owner left."""
        row = await self.bot.db.fetchone(
            "SELECT * FROM tags WHERE guild_id = ? AND name = ?",
            (ctx.guild.id, name.strip().lower()),
        )
        if row is None:
            raise DeezeeError(f"There is no tag called `{name}`.")

        owner_present = ctx.guild.get_member(row["owner_id"]) is not None
        is_owner = row["owner_id"] == ctx.author.id
        is_staff = await permissions.has_tier(
            self.bot, ctx.author, ctx.guild, permissions.Tier.MOD
        )

        if target is None:
            # Claiming. Only allowed when the owner has actually left.
            if owner_present and not is_owner and not is_staff:
                raise DeezeeError(
                    f"`{row['name']}` still has an owner here. A tag can only be "
                    "claimed once its owner has left the server."
                )
            new_owner = ctx.author
        else:
            if not is_owner and not is_staff:
                raise DeezeeError("Only the owner or a moderator can transfer a tag.")
            new_owner = target

        await self.bot.db.execute(
            "UPDATE tags SET owner_id = ?, updated_at = ? WHERE id = ?",
            (new_owner.id, int(time.time()), row["id"]),
        )
        await ctx.send(
            embed=self._ok(f"`{row['name']}` now belongs to {new_owner.mention}.")
        )

    async def _owned_tag(self, ctx: commands.Context, name: str) -> dict[str, Any]:
        """Fetch a tag the invoker is allowed to change."""
        row = await self.bot.db.fetchone(
            "SELECT * FROM tags WHERE guild_id = ? AND name = ?",
            (ctx.guild.id, name.strip().lower()),
        )
        if row is None:
            raise DeezeeError(f"There is no tag called `{name}`.")
        if row["owner_id"] == ctx.author.id:
            return dict(row)
        if await permissions.has_tier(
            self.bot, ctx.author, ctx.guild, permissions.Tier.MOD
        ):
            return dict(row)
        raise DeezeeError(
            f"`{row['name']}` belongs to <@{row['owner_id']}>. "
            "Only its owner or a moderator can change it."
        )

    @staticmethod
    def _validate_name(name: str) -> None:
        if not _NAME_RE.match(name):
            raise DeezeeError(
                "A name must be 1-32 characters, letters, numbers, hyphen or "
                "underscore only."
            )

    # =======================================================================
    # Custom commands
    # =======================================================================

    async def custom_commands(self, guild_id: int) -> dict[str, dict[str, Any]]:
        """Every enabled custom command in a guild, keyed by name."""
        cached = self._custom.get(guild_id)
        if cached is not None:
            return cached
        rows = await self.bot.db.fetchall(
            "SELECT * FROM custom_commands WHERE guild_id = ? AND enabled = 1",
            (guild_id,),
        )
        result = {row["name"]: dict(row) for row in rows}
        self._custom[guild_id] = result
        return result

    @commands.hybrid_group(name="customcommand", aliases=["cc"], fallback="list")
    @permissions.admin_only()
    @permissions.guild_only()
    async def customcommand(self, ctx: commands.Context) -> None:
        """Prefix commands that reply with text and can change roles."""
        rows = await self.bot.db.fetchall(
            "SELECT * FROM custom_commands WHERE guild_id = ? ORDER BY name",
            (ctx.guild.id,),
        )
        lines = []
        for row in rows:
            adds = json.loads(row["add_roles"] or "[]")
            removes = json.loads(row["remove_roles"] or "[]")
            bits = [f"{row['uses']} use(s)"]
            if adds:
                bits.append(f"+{len(adds)} role(s)")
            if removes:
                bits.append(f"-{len(removes)} role(s)")
            if row["required_role_id"]:
                bits.append(f"needs <@&{row['required_role_id']}>")
            if row["channel_id"]:
                bits.append(f"only in <#{row['channel_id']}>")
            state = EMOJI_ON if row["enabled"] else EMOJI_OFF
            lines.append(f"{state} `{row['name']}` — {', '.join(bits)}")

        pages = paginate_lines(
            lines,
            title="Custom commands",
            per_page=12,
            description="`?cc add <name> <response>`. Unlike tags, these can grant "
            "roles.",
            empty_message="No custom commands yet.",
        )
        await Paginator(pages, ctx.author).start(ctx)

    @customcommand.command(name="add")
    @app_commands.describe(
        name="What members will type after the prefix",
        response="What the bot replies. Supports the tag variables",
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def cc_add(
        self, ctx: commands.Context, name: str, *, response: str
    ) -> None:
        """Create a custom command."""
        clean = name.strip().lower()
        self._validate_name(clean)
        if self.bot.get_command(clean) is not None:
            raise DeezeeError(
                f"`{clean}` is already a built-in command. Pick another name -- a "
                "custom command never overrides a real one."
            )
        existing = await self.bot.db.fetchone(
            "SELECT 1 FROM custom_commands WHERE guild_id = ? AND name = ?",
            (ctx.guild.id, clean),
        )
        if existing:
            raise DeezeeError(f"A custom command called `{clean}` already exists.")

        await self.bot.db.execute(
            "INSERT INTO custom_commands (guild_id, name, response, created_by, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (ctx.guild.id, clean, response[:1900], ctx.author.id, int(time.time())),
        )
        self.invalidate(ctx.guild.id)

        prefix = await self.bot.guild_config.prefix(
            ctx.guild.id, self.bot.config.default_prefix
        )
        await ctx.send(
            embed=self._ok(
                f"Created `{prefix}{clean}`.\n"
                f"Add role actions with `?cc roles {clean} add @Role`."
            )
        )

    @customcommand.command(name="edit")
    @app_commands.describe(name="Which custom command", response="New reply")
    @permissions.admin_only()
    @permissions.guild_only()
    async def cc_edit(
        self, ctx: commands.Context, name: str, *, response: str
    ) -> None:
        """Change a custom command's reply."""
        cursor = await self.bot.db.execute(
            "UPDATE custom_commands SET response = ? WHERE guild_id = ? AND name = ?",
            (response[:1900], ctx.guild.id, name.strip().lower()),
        )
        self.invalidate(ctx.guild.id)
        if not cursor.rowcount:
            raise DeezeeError(f"There is no custom command called `{name}`.")
        await ctx.send(embed=self._ok(f"Updated `{name}`."))

    @customcommand.command(name="remove")
    @app_commands.describe(name="Which custom command to delete")
    @permissions.admin_only()
    @permissions.guild_only()
    async def cc_remove(self, ctx: commands.Context, name: str) -> None:
        """Delete a custom command."""
        cursor = await self.bot.db.execute(
            "DELETE FROM custom_commands WHERE guild_id = ? AND name = ?",
            (ctx.guild.id, name.strip().lower()),
        )
        self.invalidate(ctx.guild.id)
        if not cursor.rowcount:
            raise DeezeeError(f"There is no custom command called `{name}`.")
        await ctx.send(embed=self._ok(f"Deleted `{name}`."))

    @customcommand.command(name="roles")
    @app_commands.describe(
        name="Which custom command",
        action="add, remove or clear",
        role="Role the command should grant or take",
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def cc_roles(
        self, ctx: commands.Context, name: str, action: str,
        role: Optional[discord.Role] = None,
    ) -> None:
        """Attach role actions to a custom command."""
        row = await self.bot.db.fetchone(
            "SELECT * FROM custom_commands WHERE guild_id = ? AND name = ?",
            (ctx.guild.id, name.strip().lower()),
        )
        if row is None:
            raise DeezeeError(f"There is no custom command called `{name}`.")

        choice = action.lower()
        if choice == "clear":
            await self.bot.db.execute(
                "UPDATE custom_commands SET add_roles = '[]', remove_roles = '[]' "
                "WHERE id = ?",
                (row["id"],),
            )
            self.invalidate(ctx.guild.id)
            await ctx.send(embed=self._ok(f"Cleared role actions on `{name}`."))
            return

        if role is None:
            raise DeezeeError("Name the role.")
        if choice not in {"add", "remove"}:
            raise DeezeeError("Use `add`, `remove` or `clear`.")

        usable, why = permissions.can_manage_role(ctx.guild, role)
        if not usable:
            raise DeezeeError(why)

        column = "add_roles" if choice == "add" else "remove_roles"
        current = json.loads(row[column] or "[]")
        if role.id in current:
            raise DeezeeError(f"{role.mention} is already in that list.")
        current.append(role.id)

        await self.bot.db.execute(
            f"UPDATE custom_commands SET {column} = ? WHERE id = ?",
            (json.dumps(current), row["id"]),
        )
        self.invalidate(ctx.guild.id)
        await ctx.send(
            embed=self._ok(
                f"`{name}` will now **{choice}** {role.mention} when it runs."
            )
        )

    @customcommand.command(name="show")
    @app_commands.describe(name="Which custom command")
    @permissions.admin_only()
    @permissions.guild_only()
    async def cc_show(self, ctx: commands.Context, name: str) -> None:
        """Show a custom command's full configuration."""
        row = await self.bot.db.fetchone(
            "SELECT * FROM custom_commands WHERE guild_id = ? AND name = ?",
            (ctx.guild.id, name.strip().lower()),
        )
        if row is None:
            raise DeezeeError(f"There is no custom command called `{name}`.")

        adds = json.loads(row["add_roles"] or "[]")
        removes = json.loads(row["remove_roles"] or "[]")

        embed = discord.Embed(title=f"Custom command: {row['name']}", colour=COLOUR_INFO)
        embed.add_field(
            name="Response", value=f"```{row['response'][:900]}```", inline=False
        )
        embed.add_field(name="Uses", value=str(row["uses"]), inline=True)
        embed.add_field(
            name="Enabled", value="Yes" if row["enabled"] else "No", inline=True
        )
        embed.add_field(
            name="Adds", value=", ".join(f"<@&{r}>" for r in adds) or "Nothing",
            inline=False,
        )
        embed.add_field(
            name="Removes", value=", ".join(f"<@&{r}>" for r in removes) or "Nothing",
            inline=False,
        )
        embed.add_field(
            name="Restrictions",
            value=(
                (f"Needs <@&{row['required_role_id']}>. "
                 if row["required_role_id"] else "Anyone may run it. ")
                + (f"Only in <#{row['channel_id']}>."
                   if row["channel_id"] else "Works in any channel.")
            ),
            inline=False,
        )
        embed.add_field(
            name="Preview", value=tagvars.preview(row["response"])[:1000], inline=False
        )
        await ctx.send(embed=embed)

    # =======================================================================
    # Autoresponses
    # =======================================================================

    async def autoresponses(self, guild_id: int) -> list[dict[str, Any]]:
        """Enabled autoresponses for a guild, cached."""
        cached = self._responses.get(guild_id)
        if cached is not None:
            return cached
        rows = await self.bot.db.fetchall(
            "SELECT * FROM autoresponses WHERE guild_id = ? AND enabled = 1 "
            "ORDER BY id LIMIT ?",
            (guild_id, MAX_AR_PER_GUILD),
        )
        result = [dict(row) for row in rows]
        self._responses[guild_id] = result
        return result

    async def ar_ignores(self, guild_id: int) -> set[tuple[int, str]]:
        cached = self._ar_ignores.get(guild_id)
        if cached is not None:
            return cached
        rows = await self.bot.db.fetchall(
            "SELECT entity_id, entity_type FROM autoresponse_ignores WHERE guild_id = ?",
            (guild_id,),
        )
        result = {(row["entity_id"], row["entity_type"]) for row in rows}
        self._ar_ignores[guild_id] = result
        return result

    @commands.hybrid_group(name="autoresponse", aliases=["ar", "autoresponder"],
                           fallback="list")
    @permissions.admin_only()
    @permissions.guild_only()
    async def autoresponse(self, ctx: commands.Context) -> None:
        """Automatic replies to messages matching a trigger."""
        rows = await self.bot.db.fetchall(
            "SELECT * FROM autoresponses WHERE guild_id = ? ORDER BY id",
            (ctx.guild.id,),
        )
        lines = [
            f"{EMOJI_ON if row['enabled'] else EMOJI_OFF} **#{row['id']}** "
            f"`{row['trigger'][:40]}` ({row['mode']}) → "
            f"{row['response'][:60]}  *({row['uses']} use(s))*"
            for row in rows
        ]
        ignores = await self.ar_ignores(ctx.guild.id)
        description = (
            "`?ar add <trigger> | <response>`. Modes: "
            + ", ".join(f"`{m}`" for m in AR_MODES)
        )
        if ignores:
            description += f"\n{len(ignores)} channel(s)/role(s) ignored."

        pages = paginate_lines(
            lines,
            title="Autoresponses",
            per_page=10,
            description=description,
            empty_message="No autoresponses configured.",
        )
        await Paginator(pages, ctx.author).start(ctx)

    @autoresponse.command(name="add")
    @app_commands.describe(
        mode="contains, exact, startswith, endswith or regex",
        spec="trigger | response",
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def ar_add(
        self, ctx: commands.Context, mode: str, *, spec: str
    ) -> None:
        """Add an autoresponse. Separate trigger and response with a pipe."""
        chosen = mode.lower()
        if chosen not in AR_MODES:
            raise DeezeeError(
                "Mode must be one of: " + ", ".join(f"`{m}`" for m in AR_MODES)
            )
        if "|" not in spec:
            raise DeezeeError(
                "Separate the trigger from the response with `|`. "
                "Example: `?ar add contains hello | Hi {user}!`"
            )

        trigger, response = (part.strip() for part in spec.split("|", 1))
        if not trigger or not response:
            raise DeezeeError("Both the trigger and the response must have text.")
        if len(trigger) < 2:
            raise DeezeeError("A trigger of one character matches nearly everything.")

        if chosen == "regex":
            safe, why = is_regex_safe(trigger)
            if not safe:
                raise DeezeeError(
                    f"That pattern is refused: {why}\n"
                    "A pattern here runs against every message in the server, so an "
                    "unsafe one is not a slow command but an unresponsive bot."
                )
            try:
                re.compile(trigger)
            except re.error as exc:
                raise DeezeeError(f"That is not a valid regular expression: {exc}") from exc

        total = await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM autoresponses WHERE guild_id = ?", (ctx.guild.id,)
        )
        if (total or 0) >= MAX_AR_PER_GUILD:
            raise DeezeeError(
                f"This server already has {MAX_AR_PER_GUILD} autoresponses, which is "
                "the cap. Every one is checked against every message."
            )

        existing = await self.bot.db.fetchone(
            "SELECT 1 FROM autoresponses WHERE guild_id = ? AND trigger = ? AND mode = ?",
            (ctx.guild.id, trigger, chosen),
        )
        if existing:
            raise DeezeeError(f"`{trigger}` already has a `{chosen}` autoresponse.")

        cursor = await self.bot.db.execute(
            "INSERT INTO autoresponses (guild_id, trigger, response, mode, created_by, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (ctx.guild.id, trigger, response[:1900], chosen, ctx.author.id,
             int(time.time())),
        )
        self.invalidate(ctx.guild.id)
        await ctx.send(
            embed=self._ok(
                f"Autoresponse **#{cursor.lastrowid}** added.\n"
                f"Preview: {tagvars.preview(response)[:500]}"
            )
        )

    @autoresponse.command(name="remove")
    @app_commands.describe(response_id="Number from ?ar list")
    @permissions.admin_only()
    @permissions.guild_only()
    async def ar_remove(self, ctx: commands.Context, response_id: int) -> None:
        """Delete an autoresponse."""
        cursor = await self.bot.db.execute(
            "DELETE FROM autoresponses WHERE guild_id = ? AND id = ?",
            (ctx.guild.id, response_id),
        )
        self._patterns.pop(response_id, None)
        self.invalidate(ctx.guild.id)
        if not cursor.rowcount:
            raise DeezeeError(f"There is no autoresponse **#{response_id}**.")
        await ctx.send(embed=self._ok(f"Deleted autoresponse **#{response_id}**."))

    @autoresponse.command(name="ignore")
    @app_commands.describe(target="Channel or role autoresponses should skip")
    @permissions.admin_only()
    @permissions.guild_only()
    async def ar_ignore(self, ctx: commands.Context, *, target: str) -> None:
        """Stop autoresponses firing for a channel or role."""
        entity, kind = await self._resolve_channel_or_role(ctx, target)
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO autoresponse_ignores (guild_id, entity_id, "
            "entity_type) VALUES (?, ?, ?)",
            (ctx.guild.id, entity.id, kind),
        )
        self.invalidate(ctx.guild.id)
        await ctx.send(
            embed=self._ok(f"Autoresponses will skip {entity.mention}.")
        )

    @autoresponse.command(name="unignore")
    @app_commands.describe(target="Channel or role to include again")
    @permissions.admin_only()
    @permissions.guild_only()
    async def ar_unignore(self, ctx: commands.Context, *, target: str) -> None:
        """Undo an autoresponse ignore."""
        entity, kind = await self._resolve_channel_or_role(ctx, target)
        cursor = await self.bot.db.execute(
            "DELETE FROM autoresponse_ignores WHERE guild_id = ? AND entity_id = ? "
            "AND entity_type = ?",
            (ctx.guild.id, entity.id, kind),
        )
        self.invalidate(ctx.guild.id)
        if not cursor.rowcount:
            raise DeezeeError(f"{entity.mention} was not ignored.")
        await ctx.send(embed=self._ok(f"Autoresponses apply to {entity.mention} again."))

    @staticmethod
    async def _resolve_channel_or_role(
        ctx: commands.Context, query: str
    ) -> tuple[Any, str]:
        """Turn a mention, ID or name into a channel or a role."""
        try:
            return await commands.TextChannelConverter().convert(ctx, query), "channel"
        except commands.BadArgument:
            pass
        try:
            return await commands.RoleConverter().convert(ctx, query), "role"
        except commands.BadArgument:
            raise DeezeeError(f"`{query}` is not a channel or role here.") from None

    @commands.hybrid_command(name="variables", aliases=["vars"])
    @permissions.guild_only()
    async def variables(self, ctx: commands.Context) -> None:
        """List every variable usable in tags, custom commands and greetings."""
        embed = discord.Embed(
            title="Template variables",
            description=(
                "Usable in tags, custom commands, autoresponses, welcome and "
                "goodbye messages, and level-up announcements."
            ),
            colour=COLOUR_INFO,
        )
        embed.add_field(
            name="Names",
            value="\n".join(
                f"`{{{name}}}` — {meaning}" for name, meaning in tagvars.VARIABLES.items()
            ),
            inline=False,
        )
        embed.add_field(
            name="Pickers",
            value="\n".join(f"`{name}` — {meaning}" for name, meaning in tagvars.PICKERS.items()),
            inline=False,
        )
        embed.add_field(
            name="What this deliberately is not",
            value=(
                "A scripting language. There are no loops, no conditionals and no "
                "nesting -- a tag cannot behave differently for staff, cannot spin "
                "the CPU, and cannot execute anything. Carl-bot's TagScript is more "
                "powerful; it is also arbitrary execution inside your server."
            ),
            inline=False,
        )
        embed.add_field(
            name="Unknown names",
            value="An unrecognised `{name}` is left as literal text rather than "
            "deleted, so a typo is visible instead of silent.",
            inline=False,
        )
        await ctx.send(embed=embed)

    # =======================================================================
    # Listeners
    # =======================================================================

    @commands.Cog.listener("on_message")
    async def run_custom_and_autoresponses(self, message: discord.Message) -> None:
        """Dispatch custom commands and autoresponses.

        One listener for both, because both need the same early exits and doing
        it twice would double the per-message cost for no benefit.
        """
        if message.guild is None or message.author.bot or not message.content:
            return
        if not isinstance(message.author, discord.Member):
            return

        ignores = await self.ar_ignores(message.guild.id)
        if (message.channel.id, "channel") in ignores:
            return
        if any((role.id, "role") in ignores for role in message.author.roles):
            return

        prefix = await self.bot.guild_config.prefix(
            message.guild.id, self.bot.config.default_prefix
        )
        if message.content.startswith(prefix):
            invoked = message.content[len(prefix):].split(" ", 1)
            name = invoked[0].lower()
            rest = invoked[1] if len(invoked) > 1 else ""
            custom = (await self.custom_commands(message.guild.id)).get(name)
            if custom is not None:
                await self._run_custom(message, custom, rest)
                return
            # A real command starting with the prefix is not an autoresponse
            # trigger. Anything else falls through.
            if self.bot.get_command(name) is not None:
                return

        await self._run_autoresponses(message)

    async def _run_custom(
        self, message: discord.Message, row: dict[str, Any], args: str
    ) -> None:
        """Execute one custom command."""
        member = message.author

        if row["channel_id"] and message.channel.id != row["channel_id"]:
            return
        if row["required_role_id"]:
            if not any(r.id == row["required_role_id"] for r in member.roles):
                return

        adds = [
            message.guild.get_role(rid) for rid in json.loads(row["add_roles"] or "[]")
        ]
        removes = [
            message.guild.get_role(rid)
            for rid in json.loads(row["remove_roles"] or "[]")
        ]
        adds = [r for r in adds if r and permissions.can_manage_role(message.guild, r)[0]]
        removes = [
            r for r in removes if r and permissions.can_manage_role(message.guild, r)[0]
        ]

        try:
            if adds:
                await member.add_roles(*adds, reason=f"Custom command {row['name']}")
            if removes:
                await member.remove_roles(
                    *removes, reason=f"Custom command {row['name']}"
                )
        except discord.HTTPException as exc:
            log.warning("Custom command role change failed: %s", exc)

        await self.bot.db.execute(
            "UPDATE custom_commands SET uses = uses + 1 WHERE id = ?", (row["id"],)
        )
        cached = self._custom.get(message.guild.id)
        if cached and row["name"] in cached:
            cached[row["name"]]["uses"] += 1

        text = tagvars.render(row["response"], self.context_for(message, args))
        allowed = discord.AllowedMentions(everyone=False, roles=False, users=False)

        try:
            if row["is_embed"]:
                await message.channel.send(
                    embed=discord.Embed(description=text[:4000], colour=COLOUR_DEFAULT)
                )
            else:
                await message.channel.send(text[:2000], allowed_mentions=allowed)
            if row["delete_invocation"]:
                await message.delete()
        except discord.HTTPException:
            pass

    async def _run_autoresponses(self, message: discord.Message) -> None:
        """Fire the first matching autoresponse, if any."""
        rows = await self.autoresponses(message.guild.id)
        if not rows:
            return

        for row in rows:
            content = message.content
            trigger = row["trigger"]
            if not row["case_sensitive"]:
                content = content.lower()
                if row["mode"] != "regex":
                    trigger = trigger.lower()

            if row["mode"] == "contains":
                hit = trigger in content
            elif row["mode"] == "exact":
                hit = content.strip() == trigger
            elif row["mode"] == "startswith":
                hit = content.startswith(trigger)
            elif row["mode"] == "endswith":
                hit = content.endswith(trigger)
            else:
                pattern = self._patterns.get(row["id"])
                if pattern is None:
                    flags = 0 if row["case_sensitive"] else re.IGNORECASE
                    try:
                        pattern = re.compile(row["trigger"], flags)
                    except re.error:
                        continue
                    self._patterns[row["id"]] = pattern
                hit = pattern.search(message.content) is not None

            if not hit:
                continue

            # First match only. Firing every match turns one message into a wall
            # of replies, which is how an autoresponder becomes the spam.
            await self.bot.db.execute(
                "UPDATE autoresponses SET uses = uses + 1 WHERE id = ?", (row["id"],)
            )
            row["uses"] += 1

            text = tagvars.render(row["response"], self.context_for(message))
            try:
                if row["reply"]:
                    await message.reply(
                        text[:2000],
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions(
                            everyone=False, roles=False, users=False
                        ),
                    )
                else:
                    await message.channel.send(
                        text[:2000],
                        allowed_mentions=discord.AllowedMentions(
                            everyone=False, roles=False, users=False
                        ),
                    )
            except discord.HTTPException:
                pass
            return


async def setup(bot: commands.Bot) -> None:
    """discord.py extension entry point."""
    await bot.add_cog(Tags(bot))
