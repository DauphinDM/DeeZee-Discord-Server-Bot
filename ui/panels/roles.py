"""The reaction-role builder and the self-role picker.

Two surfaces live here.

:class:`MenuBuilder` is the staff-facing builder behind ``?reactionrole create``
and ``?reactionrole edit``: pick the roles, write the embed text, choose how
members interact with it, then publish. Nothing is posted to the channel until
the Publish button is pressed, so a half-finished menu never appears in public.

:class:`SelfRolePicker` is the member-facing dropdown behind a bare
``?selfrole``. It shows only roles the bot can actually assign, and marks the
ones the member already holds.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import discord

from core.constants import (
    COLOUR_DEFAULT,
    COLOUR_SUCCESS,
    COLOUR_WARNING,
    EMOJI_SUCCESS,
    MAX_SELECT_OPTIONS,
    PANEL_TIMEOUT,
)
from core.permissions import can_manage_role

from ..views import BaseView

if TYPE_CHECKING:
    from discord.ext import commands

log = logging.getLogger(__name__)

#: How members interact with a published menu.
STYLES: dict[str, str] = {
    "button": "One button per role. Best up to 10 roles",
    "select": "A single dropdown. Best above 5 roles",
    "reaction": "Emoji reactions on the message, Carl-bot style",
}

#: What happens when they do.
MODES: dict[str, str] = {
    "normal": "Add and remove freely",
    "unique": "One role at a time; picking another drops the first",
    "verify": "Add only -- roles from this menu can never be removed",
    "drop": "Remove only -- roles from this menu can never be added",
    "limit": "At most a set number of roles from this menu",
}


class MenuTextModal(discord.ui.Modal, title="Menu text"):
    """Title, description and role limit for a menu."""

    heading = discord.ui.TextInput(
        label="Title",
        placeholder="Pick your roles",
        max_length=256,
        required=True,
    )
    body = discord.ui.TextInput(
        label="Description",
        style=discord.TextStyle.paragraph,
        placeholder="Shown above the roles. Leave blank for none.",
        max_length=2000,
        required=False,
    )
    limit = discord.ui.TextInput(
        label="Max roles (only used by 'limit' mode)",
        placeholder="0 for no limit",
        max_length=2,
        required=False,
    )

    def __init__(self, panel: MenuBuilder) -> None:
        super().__init__(timeout=300.0)
        self.panel = panel
        self.heading.default = panel.menu["title"]
        self.body.default = panel.menu["description"]
        self.limit.default = str(panel.menu["max_roles"])

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.limit.value.strip() or "0"
        if not raw.isdigit():
            await interaction.response.send_message(
                f"`{raw}` is not a number. Use 0 for no limit.", ephemeral=True
            )
            return

        await self.panel.cog.update_menu(
            self.panel.menu["id"],
            title=self.heading.value.strip(),
            description=self.body.value.strip(),
            max_roles=int(raw),
        )
        await self.panel.refresh(interaction)


class StyleSelect(discord.ui.Select):
    """Chooses how members interact with the menu."""

    def __init__(self, panel: MenuBuilder) -> None:
        super().__init__(
            placeholder="How members pick roles",
            row=1,
            options=[
                discord.SelectOption(
                    label=name.title(),
                    value=name,
                    description=help_text,
                    default=name == panel.menu["style"],
                )
                for name, help_text in STYLES.items()
            ],
        )
        self.panel = panel

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.panel.cog.update_menu(self.panel.menu["id"], style=self.values[0])
        await self.panel.refresh(interaction)


class ModeSelect(discord.ui.Select):
    """Chooses the menu's add/remove behaviour."""

    def __init__(self, panel: MenuBuilder) -> None:
        super().__init__(
            placeholder="What happens when they pick",
            row=2,
            options=[
                discord.SelectOption(
                    label=name.title(),
                    value=name,
                    description=help_text,
                    default=name == panel.menu["mode"],
                )
                for name, help_text in MODES.items()
            ],
        )
        self.panel = panel

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.panel.cog.update_menu(self.panel.menu["id"], mode=self.values[0])
        await self.panel.refresh(interaction)


class MenuRoleSelect(discord.ui.RoleSelect):
    """Sets the whole role list in one action.

    Replacing the list rather than appending to it means the picker always shows
    the truth: what is selected is what the menu contains. Appending would need a
    separate remove flow for something the picker can express directly.
    """

    def __init__(self, panel: MenuBuilder) -> None:
        super().__init__(
            placeholder="Roles in this menu (replaces the current list)",
            min_values=0,
            max_values=MAX_SELECT_OPTIONS,
            row=0,
        )
        self.panel = panel

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        usable: list[discord.Role] = []
        refused: list[str] = []

        for role in self.values:
            ok, why = can_manage_role(guild, role)
            if ok:
                usable.append(role)
            else:
                refused.append(f"{role.name}: {why}")

        await self.panel.cog.set_menu_roles(self.panel.menu["id"], usable)
        await self.panel.refresh(interaction, warnings=refused)


class MenuBuilder(BaseView):
    """Build one reaction-role menu, then publish it.

    Every change is written to SQLite immediately. A menu that is being built is
    a real row with no ``message_id``; publishing is what gives it one. That way
    a timed-out panel loses nothing but the buttons.
    """

    def __init__(self, cog: Any, author: discord.abc.User, menu: dict[str, Any],
                 options: list[dict[str, Any]]) -> None:
        super().__init__(author, timeout=PANEL_TIMEOUT)
        self.cog = cog
        self.menu = menu
        self.options = options
        self.warnings: list[str] = []
        self._build()

    def _build(self) -> None:
        self.clear_items()
        self.add_item(MenuRoleSelect(self))
        self.add_item(StyleSelect(self))
        self.add_item(ModeSelect(self))
        self.add_item(self.text)
        self.add_item(self.publish)
        self.add_item(self.close)

        published = self.menu["message_id"] is not None
        self.publish.label = "Republish" if published else "Publish"
        self.publish.disabled = not self.options

    def embed(self, guild: discord.Guild) -> discord.Embed:
        """Show the menu as it will look, plus its settings."""
        lines = []
        for option in self.options:
            role = guild.get_role(option["role_id"])
            name = role.mention if role else f"*deleted role {option['role_id']}*"
            emoji = f"{option['emoji']} " if option["emoji"] else ""
            lines.append(f"{emoji}{name}")

        embed = discord.Embed(
            title=self.menu["title"],
            description=self.menu["description"] or "​",
            colour=self.menu["colour"] or COLOUR_DEFAULT,
        )
        embed.add_field(
            name=f"Roles ({len(self.options)})",
            value="\n".join(lines) if lines else "None yet. Pick some above.",
            inline=False,
        )

        settings = [
            f"Style: **{self.menu['style']}** — {STYLES[self.menu['style']]}",
            f"Mode: **{self.menu['mode']}** — {MODES[self.menu['mode']]}",
        ]
        if self.menu["mode"] == "limit":
            settings.append(f"Limit: **{self.menu['max_roles'] or 'not set'}**")
        channel = guild.get_channel(self.menu["channel_id"])
        settings.append(f"Posts to: {channel.mention if channel else 'a deleted channel'}")
        if self.menu["message_id"]:
            settings.append(f"Published as message `{self.menu['message_id']}`")
        embed.add_field(name="Settings", value="\n".join(settings), inline=False)

        if self.warnings:
            embed.add_field(
                name="Skipped",
                value="\n".join(f"• {w}" for w in self.warnings[:5]),
                inline=False,
            )
            embed.colour = COLOUR_WARNING

        embed.set_footer(
            text=f"Menu #{self.menu['id']} • nothing is posted until you press "
            f"Publish • expires in {PANEL_TIMEOUT:.0f}s"
        )
        return embed

    async def refresh(
        self, interaction: discord.Interaction, *, warnings: list[str] | None = None
    ) -> None:
        """Reload from the database and redraw in place."""
        self.warnings = warnings or []
        self.menu, self.options = await self.cog.load_menu(self.menu["id"])
        self._build()
        await interaction.response.edit_message(
            embed=self.embed(interaction.guild), view=self
        )

    @discord.ui.button(label="Edit text", style=discord.ButtonStyle.primary, row=3)
    async def text(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Open the title/description/limit modal."""
        await interaction.response.send_modal(MenuTextModal(self))

    @discord.ui.button(label="Publish", style=discord.ButtonStyle.success, row=3)
    async def publish(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        """Post or repost the menu into its channel."""
        await interaction.response.defer()
        try:
            message = await self.cog.publish_menu(interaction.guild, self.menu["id"])
        except Exception as exc:  # Surfaced in the panel rather than swallowed.
            log.exception("Could not publish menu %s", self.menu["id"])
            self.warnings = [str(exc)]
            self.menu, self.options = await self.cog.load_menu(self.menu["id"])
            self._build()
            await interaction.edit_original_response(
                embed=self.embed(interaction.guild), view=self
            )
            return

        self.menu, self.options = await self.cog.load_menu(self.menu["id"])
        self._build()
        embed = self.embed(interaction.guild)
        embed.colour = COLOUR_SUCCESS
        embed.set_footer(text=f"{EMOJI_SUCCESS} Published: {message.jump_url}")
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary, row=3)
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Close the builder. The menu row is already saved."""
        self.disable_all()
        await interaction.response.edit_message(view=self)
        self.stop()


class SelfRoleSelect(discord.ui.Select):
    """Member-facing picker: everything they may self-assign."""

    def __init__(self, cog: Any, member: discord.Member, rows: list[dict[str, Any]]) -> None:
        held = {role.id for role in member.roles}
        options: list[discord.SelectOption] = []

        for row in rows[:MAX_SELECT_OPTIONS]:
            role = member.guild.get_role(row["role_id"])
            if role is None:
                continue
            options.append(
                discord.SelectOption(
                    label=role.name[:100],
                    value=str(role.id),
                    description=(row["description"] or "")[:100] or None,
                    default=role.id in held,
                )
            )

        super().__init__(
            placeholder="Pick the roles you want",
            min_values=0,
            max_values=max(1, len(options)),
            options=options or [discord.SelectOption(label="None available", value="0")],
            disabled=not options,
        )
        self.cog = cog
        self.member = member
        self.available = {int(option.value) for option in options}

    async def callback(self, interaction: discord.Interaction) -> None:
        wanted = {int(value) for value in self.values if value != "0"}
        added, removed, refused = await self.cog.apply_self_roles(
            interaction.user, self.available, wanted
        )

        parts = []
        if added:
            parts.append("**Added:** " + ", ".join(r.mention for r in added))
        if removed:
            parts.append("**Removed:** " + ", ".join(r.mention for r in removed))
        if refused:
            parts.append("**Skipped:** " + "; ".join(refused))
        if not parts:
            parts.append("Nothing changed.")

        await interaction.response.send_message(
            embed=discord.Embed(
                description="\n".join(parts),
                colour=COLOUR_SUCCESS if (added or removed) else COLOUR_DEFAULT,
            ),
            ephemeral=True,
        )


class SelfRolePicker(BaseView):
    """Wraps :class:`SelfRoleSelect` so a bare ``?selfrole`` is usable."""

    def __init__(self, cog: Any, member: discord.Member, rows: list[dict[str, Any]]) -> None:
        super().__init__(member, timeout=PANEL_TIMEOUT)
        self.total = len(rows)
        self.add_item(SelfRoleSelect(cog, member, rows))

    def embed(self, guild: discord.Guild) -> discord.Embed:
        embed = discord.Embed(
            title="Self-assignable roles",
            description=(
                "Pick any you want and unpick any you do not. "
                "Roles you already hold are ticked."
            ),
            colour=COLOUR_DEFAULT,
        )
        if self.total > MAX_SELECT_OPTIONS:
            embed.add_field(
                name="Note",
                value=f"{self.total} roles are self-assignable but a dropdown holds "
                f"{MAX_SELECT_OPTIONS}. Use `?selfrole <name>` for the rest.",
                inline=False,
            )
        embed.set_footer(text=f"{guild.name} • expires in {PANEL_TIMEOUT:.0f}s")
        return embed


async def open_builder(
    ctx: commands.Context, cog: Any, menu: dict[str, Any], options: list[dict[str, Any]]
) -> None:
    """Send the menu builder."""
    view = MenuBuilder(cog, ctx.author, menu, options)
    view.message = await ctx.send(embed=view.embed(ctx.guild), view=view)


async def open_self_roles(ctx: commands.Context, cog: Any, rows: list[dict[str, Any]]) -> None:
    """Send the member-facing self-role picker."""
    view = SelfRolePicker(cog, ctx.author, rows)
    view.message = await ctx.send(embed=view.embed(ctx.guild), view=view)
