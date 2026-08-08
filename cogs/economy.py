"""Currency, daily rewards, the shop and gambling.

Covers 10 of the 19 commands in the command sheet's "Leveling & Economy"
section; the other 9 are in ``cogs/leveling.py``. Split because the two share
only a user ID, and splitting means the economy can be switched off without
touching anyone's XP.

Everything here is **off until an administrator turns it on**. A currency is a
decision about what a server is, not a default anyone should inherit.

Two rules the code holds to:

* **Cooldowns live in the database.** ``last_daily`` and ``last_work`` are epoch
  seconds in a row, not timers in memory. A restart is not a way to claim twice.
* **Every balance change is a single UPDATE with its own guard.** Spending checks
  the balance in the same statement that decrements it, so two simultaneous
  purchases cannot both succeed against one balance.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from core import permissions
from core.constants import (
    COLOUR_DEFAULT,
    COLOUR_ERROR,
    COLOUR_INFO,
    COLOUR_SUCCESS,
    EMOJI_SUCCESS,
    TS_RELATIVE,
)
from core.errors import DeezeeError
from services.timeparse import format_duration
from ui.paginator import Paginator, paginate_lines
from ui.views import confirm

log = logging.getLogger(__name__)

#: Flavour text for ``?work``. Purely cosmetic; the payout comes from config.
JOBS: tuple[str, ...] = (
    "sorted the emoji drawer",
    "moderated a heated debate about pineapple",
    "swept the voice channels",
    "wrote documentation nobody will read",
    "reset someone's forgotten password",
    "untangled the role hierarchy",
    "answered the same question for the ninth time",
    "found the missing semicolon",
    "restarted the server and blamed the cache",
    "reviewed a pull request properly",
    "labelled every wire in the rack",
    "talked a member out of pinging everyone",
)

#: Slot faces and their multiplier when three match.
SLOTS: dict[str, int] = {
    "\N{CHERRIES}": 3,
    "\N{LEMON}": 4,
    "\N{GRAPES}": 5,
    "\N{BELL}": 8,
    "\N{GEM STONE}": 15,
}

#: A day, for the daily claim. Deliberately a rolling 20 hours rather than a
#: calendar day: timezones make "a day" ambiguous and a rolling window is the
#: same for everyone.
DAILY_WINDOW = 20 * 3600

#: Claiming later than this after the last one breaks the streak.
STREAK_WINDOW = 48 * 3600


class Economy(commands.Cog):
    """Server currency, rewards, shop and gambling."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    @staticmethod
    def _ok(description: str) -> discord.Embed:
        return discord.Embed(
            description=f"{EMOJI_SUCCESS}  {description}", colour=COLOUR_SUCCESS
        )

    # =======================================================================
    # State
    # =======================================================================

    async def require_enabled(self, guild_id: int) -> dict[str, Any]:
        """Return the config, refusing if the economy is switched off."""
        config = await self.bot.guild_config.get(guild_id)
        if not config["economy_enabled"]:
            raise DeezeeError(
                "The economy is switched off in this server. "
                "An administrator can turn it on with `?ecoadmin enable`."
            )
        return config

    async def account(self, guild_id: int, user_id: int) -> dict[str, Any]:
        """One member's economy row, created at zero if absent."""
        row = await self.bot.db.fetchone(
            "SELECT * FROM economy WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        if row is not None:
            return dict(row)
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO economy (guild_id, user_id) VALUES (?, ?)",
            (guild_id, user_id),
        )
        return {
            "guild_id": guild_id, "user_id": user_id, "balance": 0, "streak": 0,
            "last_daily": 0, "last_work": 0, "total_earned": 0, "total_spent": 0,
        }

    async def credit(self, guild_id: int, user_id: int, amount: int) -> None:
        """Add currency. Negative amounts are rejected -- use :meth:`debit`."""
        if amount < 0:
            raise ValueError("credit() takes a positive amount")
        await self.account(guild_id, user_id)
        await self.bot.db.execute(
            "UPDATE economy SET balance = balance + ?, total_earned = total_earned + ? "
            "WHERE guild_id = ? AND user_id = ?",
            (amount, amount, guild_id, user_id),
        )

    async def debit(self, guild_id: int, user_id: int, amount: int) -> bool:
        """Take currency if the member has it.

        The balance check is inside the UPDATE rather than a separate SELECT, so
        two simultaneous purchases cannot both pass a check that only one of them
        can afford.

        Returns:
            True if the money was taken.
        """
        if amount < 0:
            raise ValueError("debit() takes a positive amount")
        await self.account(guild_id, user_id)
        cursor = await self.bot.db.execute(
            "UPDATE economy SET balance = balance - ?, total_spent = total_spent + ? "
            "WHERE guild_id = ? AND user_id = ? AND balance >= ?",
            (amount, amount, guild_id, user_id, amount),
        )
        return cursor.rowcount > 0

    @staticmethod
    def money(config: dict[str, Any], amount: int) -> str:
        """Render an amount with the guild's symbol and name."""
        return f"{config['currency_symbol']} **{amount:,}** {config['currency_name']}"

    # =======================================================================
    # Balance and rewards
    # =======================================================================

    @commands.hybrid_command(name="balance", aliases=["bal", "coins"])
    @app_commands.describe(target="Whose balance. Defaults to you")
    @permissions.guild_only()
    async def balance(
        self, ctx: commands.Context, target: Optional[discord.Member] = None
    ) -> None:
        """Show a member's currency balance."""
        config = await self.require_enabled(ctx.guild.id)
        member = target or ctx.author
        row = await self.account(ctx.guild.id, member.id)

        rank = int(
            await self.bot.db.fetchval(
                "SELECT COUNT(*) FROM economy WHERE guild_id = ? AND balance > ?",
                (ctx.guild.id, row["balance"]),
            )
            or 0
        ) + 1

        embed = discord.Embed(
            title=f"{member.display_name}'s wallet",
            description=self.money(config, int(row["balance"])),
            colour=COLOUR_INFO,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Rank", value=f"#{rank}", inline=True)
        embed.add_field(
            name="Earned", value=f"{int(row['total_earned']):,}", inline=True
        )
        embed.add_field(name="Spent", value=f"{int(row['total_spent']):,}", inline=True)
        if row["streak"]:
            embed.add_field(
                name="Daily streak", value=f"{row['streak']} day(s)", inline=True
            )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="daily")
    @permissions.guild_only()
    async def daily(self, ctx: commands.Context) -> None:
        """Claim a daily reward, with a bonus for consecutive days."""
        config = await self.require_enabled(ctx.guild.id)
        row = await self.account(ctx.guild.id, ctx.author.id)
        now = int(time.time())
        since = now - int(row["last_daily"])

        if since < DAILY_WINDOW:
            ready = int(row["last_daily"]) + DAILY_WINDOW
            raise DeezeeError(
                f"You have already claimed. Next claim <t:{ready}:{TS_RELATIVE}> "
                f"(in {format_duration(ready - now)})."
            )

        # A rolling 48-hour window keeps the streak, so a claim at 9am and the
        # next at 9pm does not silently break it.
        streak = int(row["streak"]) + 1 if since <= STREAK_WINDOW else 1
        bonus = min(streak - 1, int(config["daily_streak_cap"])) * int(
            config["daily_streak_bonus"]
        )
        amount = int(config["daily_amount"]) + bonus

        await self.credit(ctx.guild.id, ctx.author.id, amount)
        await self.bot.db.execute(
            "UPDATE economy SET last_daily = ?, streak = ? WHERE guild_id = ? "
            "AND user_id = ?",
            (now, streak, ctx.guild.id, ctx.author.id),
        )

        embed = discord.Embed(
            title="Daily claimed",
            description=f"You received {self.money(config, amount)}.",
            colour=COLOUR_SUCCESS,
        )
        embed.add_field(name="Streak", value=f"{streak} day(s)", inline=True)
        if bonus:
            embed.add_field(name="Streak bonus", value=f"+{bonus}", inline=True)
        if since > STREAK_WINDOW and row["last_daily"]:
            embed.set_footer(text="Your streak reset -- the last claim was over 48h ago.")
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="work")
    @permissions.guild_only()
    async def work(self, ctx: commands.Context) -> None:
        """Earn currency on a cooldown."""
        config = await self.require_enabled(ctx.guild.id)
        row = await self.account(ctx.guild.id, ctx.author.id)
        now = int(time.time())
        cooldown = int(config["work_cooldown"])

        if now - int(row["last_work"]) < cooldown:
            ready = int(row["last_work"]) + cooldown
            raise DeezeeError(
                f"You are still resting. Back to work <t:{ready}:{TS_RELATIVE}>."
            )

        amount = random.randint(int(config["work_min"]), int(config["work_max"]))
        await self.credit(ctx.guild.id, ctx.author.id, amount)
        await self.bot.db.execute(
            "UPDATE economy SET last_work = ? WHERE guild_id = ? AND user_id = ?",
            (now, ctx.guild.id, ctx.author.id),
        )

        await ctx.send(
            embed=discord.Embed(
                title="Payday",
                description=f"You {random.choice(JOBS)} and earned "
                f"{self.money(config, amount)}.",
                colour=COLOUR_SUCCESS,
            ).set_footer(text=f"Next shift in {format_duration(cooldown)}")
        )

    @commands.hybrid_command(name="pay", aliases=["give"])
    @app_commands.describe(target="Who to pay", amount="How much")
    @permissions.guild_only()
    async def pay(
        self, ctx: commands.Context, target: discord.Member, amount: int
    ) -> None:
        """Transfer currency to another member."""
        config = await self.require_enabled(ctx.guild.id)

        if amount <= 0:
            raise DeezeeError("The amount must be positive.")
        if target.id == ctx.author.id:
            raise DeezeeError("Paying yourself moves nothing.")
        if target.bot:
            raise DeezeeError("Bots have no wallet.")

        row = await self.account(ctx.guild.id, ctx.author.id)
        if int(row["balance"]) < amount:
            raise DeezeeError(
                f"You have {self.money(config, int(row['balance']))} and need "
                f"{amount:,}."
            )

        threshold = int(config["pay_confirm_threshold"])
        if threshold and amount >= threshold:
            proceed = await confirm(
                ctx,
                title=f"Pay {target.display_name} {amount:,}?",
                description=(
                    f"You are sending {self.money(config, amount)} to "
                    f"{target.mention}.\n**Transfers cannot be reversed.**"
                ),
                confirm_label=f"Send {amount:,}",
                danger=False,
            )
            if not proceed:
                return

        if not await self.debit(ctx.guild.id, ctx.author.id, amount):
            raise DeezeeError(
                "Your balance changed while that was confirming. Nothing was sent."
            )
        await self.credit(ctx.guild.id, target.id, amount)

        await ctx.send(
            embed=self._ok(
                f"Sent {self.money(config, amount)} to {target.mention}."
            )
        )

    # =======================================================================
    # Shop
    # =======================================================================

    @commands.hybrid_command(name="shop")
    @permissions.guild_only()
    async def shop(self, ctx: commands.Context) -> None:
        """Browse purchasable roles and items."""
        config = await self.require_enabled(ctx.guild.id)
        rows = await self.bot.db.fetchall(
            "SELECT * FROM shop_items WHERE guild_id = ? AND enabled = 1 "
            "ORDER BY price",
            (ctx.guild.id,),
        )

        lines = []
        for row in rows:
            stock = (
                "unlimited" if row["stock"] < 0
                else f"{row['stock']} left" if row["stock"] else "**sold out**"
            )
            kind = f" — grants <@&{row['role_id']}>" if row["role_id"] else ""
            description = f"\n> {row['description']}" if row["description"] else ""
            lines.append(
                f"**{row['name']}** — {config['currency_symbol']} "
                f"{row['price']:,} ({stock}){kind}{description}"
            )

        pages = paginate_lines(
            lines,
            title=f"Shop — {ctx.guild.name}",
            per_page=8,
            description=f"Buy with `?buy <name>`. Your balance: "
            f"{self.money(config, int((await self.account(ctx.guild.id, ctx.author.id))['balance']))}",
            empty_message="The shop is empty. An administrator can stock it with "
            "`?shopconfig add`.",
        )
        await Paginator(pages, ctx.author).start(ctx)

    @commands.hybrid_command(name="buy")
    @app_commands.describe(item="Item name from the shop")
    @permissions.guild_only()
    async def buy(self, ctx: commands.Context, *, item: str) -> None:
        """Purchase a shop item."""
        config = await self.require_enabled(ctx.guild.id)

        row = await self.bot.db.fetchone(
            "SELECT * FROM shop_items WHERE guild_id = ? AND LOWER(name) = ? "
            "AND enabled = 1",
            (ctx.guild.id, item.strip().lower()),
        )
        if row is None:
            raise DeezeeError(f"There is no shop item called `{item}`. Try `?shop`.")
        if row["stock"] == 0:
            raise DeezeeError(f"**{row['name']}** is sold out.")

        role: discord.Role | None = None
        if row["role_id"]:
            role = ctx.guild.get_role(row["role_id"])
            if role is None:
                raise DeezeeError(
                    f"**{row['name']}** grants a role that no longer exists. "
                    "Tell an administrator to fix or remove the item."
                )
            usable, why = permissions.can_manage_role(ctx.guild, role)
            if not usable:
                raise DeezeeError(f"I cannot grant that role: {why}")
            if role in ctx.author.roles:
                raise DeezeeError(f"You already have {role.mention}.")

        # Money first, then the goods. If the role grant fails afterwards the
        # refund below puts it back -- the reverse order would let a failed
        # payment still hand out the role.
        if not await self.debit(ctx.guild.id, ctx.author.id, int(row["price"])):
            balance = (await self.account(ctx.guild.id, ctx.author.id))["balance"]
            raise DeezeeError(
                f"**{row['name']}** costs {row['price']:,} and you have "
                f"{int(balance):,}."
            )

        if role is not None:
            try:
                await ctx.author.add_roles(role, reason=f"Bought {row['name']}")
            except discord.HTTPException as exc:
                await self.credit(ctx.guild.id, ctx.author.id, int(row["price"]))
                raise DeezeeError(
                    f"I could not grant the role, so you have been refunded. ({exc})"
                ) from exc

        await self.bot.db.execute(
            "INSERT INTO inventory (guild_id, user_id, item_id, quantity, acquired_at) "
            "VALUES (?, ?, ?, 1, ?) ON CONFLICT(guild_id, user_id, item_id) "
            "DO UPDATE SET quantity = quantity + 1",
            (ctx.guild.id, ctx.author.id, row["id"], int(time.time())),
        )
        await self.bot.db.execute(
            "UPDATE shop_items SET sold = sold + 1, "
            "stock = CASE WHEN stock < 0 THEN -1 ELSE stock - 1 END WHERE id = ?",
            (row["id"],),
        )

        await ctx.send(
            embed=self._ok(
                f"Bought **{row['name']}** for {self.money(config, int(row['price']))}."
                + (f" You now have {role.mention}." if role else "")
            )
        )

    @commands.hybrid_command(name="inventory", aliases=["inv"])
    @app_commands.describe(target="Whose inventory. Defaults to you")
    @permissions.guild_only()
    async def inventory(
        self, ctx: commands.Context, target: Optional[discord.Member] = None
    ) -> None:
        """List items a member owns."""
        await self.require_enabled(ctx.guild.id)
        member = target or ctx.author

        rows = await self.bot.db.fetchall(
            "SELECT i.quantity, i.acquired_at, s.name, s.description, s.role_id "
            "FROM inventory i JOIN shop_items s ON s.id = i.item_id "
            "WHERE i.guild_id = ? AND i.user_id = ? ORDER BY i.acquired_at DESC",
            (ctx.guild.id, member.id),
        )
        lines = [
            f"**{row['name']}** ×{row['quantity']}"
            + (f" — <@&{row['role_id']}>" if row["role_id"] else "")
            + f"\n> bought <t:{row['acquired_at']}:{TS_RELATIVE}>"
            for row in rows
        ]
        pages = paginate_lines(
            lines,
            title=f"{member.display_name}'s inventory",
            per_page=8,
            empty_message="Nothing owned yet.",
        )
        await Paginator(pages, ctx.author).start(ctx)

    @commands.hybrid_group(name="shopconfig", with_app_command=False, fallback="list")
    @permissions.admin_only()
    @permissions.guild_only()
    async def shopconfig(self, ctx: commands.Context) -> None:
        """Manage shop items, including disabled ones."""
        rows = await self.bot.db.fetchall(
            "SELECT * FROM shop_items WHERE guild_id = ? ORDER BY price",
            (ctx.guild.id,),
        )
        lines = [
            f"**#{row['id']}** {row['name']} — {row['price']:,} • "
            f"{'enabled' if row['enabled'] else 'disabled'} • "
            f"stock {'∞' if row['stock'] < 0 else row['stock']} • "
            f"{row['sold']} sold"
            + (f" • grants <@&{row['role_id']}>" if row["role_id"] else "")
            for row in rows
        ]
        pages = paginate_lines(
            lines,
            title="Shop configuration",
            per_page=12,
            description="`?shopconfig add <price> <name> | <description>`",
            empty_message="No items configured.",
        )
        await Paginator(pages, ctx.author).start(ctx)

    @shopconfig.command(name="add")
    @app_commands.describe(
        price="Cost in currency",
        role="Optional: a role the item grants",
        spec="name | description",
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def shopconfig_add(
        self, ctx: commands.Context, price: int, role: Optional[discord.Role] = None,
        *, spec: str,
    ) -> None:
        """Add a shop item, optionally granting a role."""
        if price < 0:
            raise DeezeeError("The price cannot be negative.")

        name, _, description = spec.partition("|")
        name = name.strip()
        if not name:
            raise DeezeeError("Give the item a name.")
        if len(name) > 60:
            raise DeezeeError("An item name must be 60 characters or fewer.")

        if role is not None:
            usable, why = permissions.can_manage_role(ctx.guild, role)
            if not usable:
                raise DeezeeError(f"I could never grant that role: {why}")

        existing = await self.bot.db.fetchone(
            "SELECT 1 FROM shop_items WHERE guild_id = ? AND name = ?",
            (ctx.guild.id, name),
        )
        if existing:
            raise DeezeeError(f"An item called **{name}** already exists.")

        await self.bot.db.execute(
            "INSERT INTO shop_items (guild_id, name, description, price, role_id, "
            "created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ctx.guild.id, name, description.strip()[:200], price,
                role.id if role else None, ctx.author.id, int(time.time()),
            ),
        )
        await ctx.send(
            embed=self._ok(
                f"Added **{name}** at {price:,}."
                + (f" It grants {role.mention}." if role else "")
            )
        )

    @shopconfig.command(name="edit")
    @app_commands.describe(
        item_id="Number from ?shopconfig",
        field="price, name, description, stock or enabled",
        value="New value",
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def shopconfig_edit(
        self, ctx: commands.Context, item_id: int, field: str, *, value: str
    ) -> None:
        """Change one field of a shop item."""
        row = await self.bot.db.fetchone(
            "SELECT * FROM shop_items WHERE guild_id = ? AND id = ?",
            (ctx.guild.id, item_id),
        )
        if row is None:
            raise DeezeeError(f"There is no shop item **#{item_id}**.")

        column = field.strip().lower()
        # The column name is matched against this fixed set before it reaches
        # the SQL, because a column cannot be a bound parameter.
        numeric = {"price", "stock"}
        if column in numeric:
            cleaned = value.strip()
            if not cleaned.lstrip("-").isdigit():
                raise DeezeeError(f"`{field}` needs a number.")
            new_value: Any = int(cleaned)
        elif column == "enabled":
            new_value = int(value.strip().lower() in {"1", "yes", "on", "true"})
        elif column in {"name", "description"}:
            new_value = value.strip()[:200]
        else:
            raise DeezeeError(
                "Field must be `price`, `name`, `description`, `stock` or `enabled`."
            )

        await self.bot.db.execute(
            f"UPDATE shop_items SET {column} = ? WHERE id = ?", (new_value, row["id"])
        )
        await ctx.send(
            embed=self._ok(f"**{row['name']}**: `{column}` is now `{new_value}`.")
        )

    @shopconfig.command(name="remove")
    @app_commands.describe(item_id="Number from ?shopconfig")
    @permissions.admin_only()
    @permissions.guild_only()
    async def shopconfig_remove(self, ctx: commands.Context, item_id: int) -> None:
        """Delete a shop item."""
        row = await self.bot.db.fetchone(
            "SELECT * FROM shop_items WHERE guild_id = ? AND id = ?",
            (ctx.guild.id, item_id),
        )
        if row is None:
            raise DeezeeError(f"There is no shop item **#{item_id}**.")

        owned = await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM inventory WHERE item_id = ?", (item_id,)
        )
        proceed = await confirm(
            ctx,
            title=f"Delete {row['name']}?",
            description=(
                f"**{owned or 0}** member(s) own it, and their copies disappear with "
                "the item.\nDisable it instead with `?shopconfig edit "
                f"{item_id} enabled no` if you only want it off the shelf.\n"
                "**Nobody is refunded.**"
            ),
            confirm_label="Delete it",
        )
        if not proceed:
            return

        await self.bot.db.execute("DELETE FROM shop_items WHERE id = ?", (item_id,))
        await ctx.send(embed=self._ok(f"Deleted **{row['name']}**."))

    # =======================================================================
    # Gambling
    # =======================================================================

    @commands.hybrid_command(name="gamble", aliases=["slots", "bet"])
    @app_commands.describe(amount="How much to wager")
    @commands.cooldown(1, 5, commands.BucketType.member)
    @permissions.guild_only()
    async def gamble(self, ctx: commands.Context, amount: int) -> None:
        """Wager currency on a slot spin.

        The payout table is fixed and shown in the result, so the odds are not a
        secret. Expected return is below 1 by design -- it is a sink, not an
        earner.
        """
        config = await self.require_enabled(ctx.guild.id)
        if not config["gamble_enabled"]:
            raise DeezeeError(
                "Gambling is switched off in this server "
                "(`?ecoadmin gamble on` to allow it)."
            )
        if amount <= 0:
            raise DeezeeError("The wager must be positive.")

        maximum = int(config["gamble_max_bet"])
        if maximum and amount > maximum:
            raise DeezeeError(f"The maximum wager here is {maximum:,}.")

        if not await self.debit(ctx.guild.id, ctx.author.id, amount):
            balance = (await self.account(ctx.guild.id, ctx.author.id))["balance"]
            raise DeezeeError(
                f"You have {self.money(config, int(balance))} and wagered {amount:,}."
            )

        faces = list(SLOTS)
        spin = [random.choice(faces) for _ in range(3)]

        if spin[0] == spin[1] == spin[2]:
            multiplier = SLOTS[spin[0]]
            outcome = f"Three of a kind — **{multiplier}×**"
        elif len(set(spin)) == 2:
            multiplier = 2
            outcome = "Two of a kind — **2×**"
        else:
            multiplier = 0
            outcome = "No match"

        payout = amount * multiplier
        if payout:
            await self.credit(ctx.guild.id, ctx.author.id, payout)

        row = await self.account(ctx.guild.id, ctx.author.id)
        embed = discord.Embed(
            title="  ".join(spin),
            description=f"{outcome}\n"
            + (
                f"You won {self.money(config, payout)} "
                f"(net {payout - amount:+,})."
                if payout
                else f"You lost {self.money(config, amount)}."
            ),
            colour=COLOUR_SUCCESS if payout > amount else COLOUR_ERROR,
        )
        embed.add_field(
            name="Balance", value=f"{int(row['balance']):,}", inline=True
        )
        embed.add_field(
            name="Payout table",
            value="  ".join(f"{face}×3 = {mult}×" for face, mult in SLOTS.items())
            + "\nAny two matching = 2×",
            inline=False,
        )
        await ctx.send(embed=embed)

    # =======================================================================
    # Administration
    # =======================================================================

    @commands.hybrid_command(name="ecoadmin", with_app_command=False)
    @app_commands.describe(
        action="enable, disable, add, remove, set, reset, gamble, config",
        target="Member, for add/remove/set/reset",
        amount="How much, or on/off for gamble",
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def ecoadmin(
        self,
        ctx: commands.Context,
        action: str,
        target: Optional[discord.Member] = None,
        amount: str = "0",
    ) -> None:
        """Turn the economy on or off, adjust balances, and configure rates."""
        choice = action.lower()
        config = await self.bot.guild_config.get(ctx.guild.id)

        if choice in {"enable", "on"}:
            await self.bot.guild_config.set(ctx.guild.id, "economy_enabled", 1)
            await ctx.send(
                embed=self._ok(
                    f"The economy is **on**. Currency: "
                    f"{config['currency_symbol']} {config['currency_name']}.\n"
                    "Stock the shop with `?shopconfig add`."
                )
            )
            return

        if choice in {"disable", "off"}:
            await self.bot.guild_config.set(ctx.guild.id, "economy_enabled", 0)
            await ctx.send(
                embed=self._ok(
                    "The economy is **off**. Balances are kept -- turning it back "
                    "on restores everything exactly."
                )
            )
            return

        if choice == "gamble":
            on = amount.lower() in {"on", "yes", "true", "1", "enable"}
            await self.bot.guild_config.set(ctx.guild.id, "gamble_enabled", int(on))
            await ctx.send(
                embed=self._ok(f"Gambling is **{'on' if on else 'off'}**.")
            )
            return

        if choice == "config":
            embed = discord.Embed(
                title="Economy settings",
                colour=COLOUR_SUCCESS if config["economy_enabled"] else COLOUR_DEFAULT,
            )
            embed.add_field(
                name="Status",
                value="On" if config["economy_enabled"] else "Off",
                inline=True,
            )
            embed.add_field(
                name="Currency",
                value=f"{config['currency_symbol']} {config['currency_name']}",
                inline=True,
            )
            embed.add_field(
                name="Daily",
                value=f"{config['daily_amount']:,} + "
                f"{config['daily_streak_bonus']}/day, capped at day "
                f"{config['daily_streak_cap']}",
                inline=False,
            )
            embed.add_field(
                name="Work",
                value=f"{config['work_min']}–{config['work_max']} every "
                f"{format_duration(config['work_cooldown'])}",
                inline=False,
            )
            embed.add_field(
                name="Gambling",
                value=("On" if config["gamble_enabled"] else "Off")
                + f", max wager {config['gamble_max_bet']:,}",
                inline=False,
            )
            embed.add_field(
                name="Pay confirmation",
                value=f"Asked above {config['pay_confirm_threshold']:,}",
                inline=False,
            )
            embed.set_footer(text="Change any of these with ?ecofield <name> <value>")
            await ctx.send(embed=embed)
            return

        if choice in {"add", "remove", "set", "reset"}:
            if target is None:
                raise DeezeeError("Name the member.")
            if target.bot:
                raise DeezeeError("Bots have no wallet.")

            row = await self.account(ctx.guild.id, target.id)
            current = int(row["balance"])

            if choice == "reset":
                new_balance = 0
            else:
                if not amount.lstrip("-").isdigit():
                    raise DeezeeError(f"`{amount}` is not a number.")
                value = abs(int(amount))
                if choice == "add":
                    new_balance = current + value
                elif choice == "remove":
                    new_balance = max(0, current - value)
                else:
                    new_balance = value

            await self.bot.db.execute(
                "UPDATE economy SET balance = ? WHERE guild_id = ? AND user_id = ?",
                (new_balance, ctx.guild.id, target.id),
            )
            await ctx.send(
                embed=self._ok(
                    f"{target.mention}: **{current:,}** → **{new_balance:,}** "
                    f"{config['currency_name']}."
                )
            )
            return

        raise DeezeeError(
            "Use `enable`, `disable`, `gamble on|off`, `config`, or "
            "`add|remove|set|reset <member> <amount>`."
        )

    @commands.command(name="ecofield")
    @permissions.admin_only()
    @permissions.guild_only()
    async def ecofield(self, ctx: commands.Context, field: str, value: str) -> None:
        """Change one numeric or text economy setting.

        Prefix-only, and not one of the sheet's rows: it exists because
        ``?ecoadmin`` already takes a member as its second argument, and a single
        command cannot take either a member or a setting name in the same slot.
        """
        allowed = {
            "currency_name": str,
            "currency_symbol": str,
            "daily_amount": int,
            "daily_streak_bonus": int,
            "daily_streak_cap": int,
            "work_min": int,
            "work_max": int,
            "work_cooldown": int,
            "gamble_max_bet": int,
            "pay_confirm_threshold": int,
        }
        key = field.strip().lower()
        if key not in allowed:
            raise DeezeeError(
                "Settable fields: " + ", ".join(f"`{name}`" for name in allowed)
            )

        if allowed[key] is int:
            if not value.strip().isdigit():
                raise DeezeeError(f"`{field}` takes a whole number.")
            parsed: Any = int(value.strip())
        else:
            parsed = value.strip()[:32]
            if not parsed:
                raise DeezeeError("That cannot be empty.")

        await self.bot.guild_config.set(ctx.guild.id, key, parsed)
        await ctx.send(embed=self._ok(f"`{key}` is now `{parsed}`."))


async def setup(bot: commands.Bot) -> None:
    """discord.py extension entry point."""
    await bot.add_cog(Economy(bot))
