"""Dice, text transforms, pictures, profiles and reputation.

Covers the 11 commands in the command sheet's "Fun/Social" section.

Three commands call an external API: ``?cat``, ``?dog`` and ``?urban``. All three
are free and keyless, all three have a hard timeout, and all three say plainly
when the service is down rather than pretending the command is broken. ``?cat``
additionally degrades to a bundled fallback so it always answers something.

``?fancy`` replaces Carl-bot's eight separate text commands with one styled
argument -- eight commands that differ only in a lookup table are eight places
for the same bug.
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Any, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from core import permissions
from core.constants import (
    COLOUR_DEFAULT,
    COLOUR_ERROR,
    COLOUR_INFO,
    COLOUR_SUCCESS,
    COLOUR_WARNING,
    EMOJI_SUCCESS,
    PANEL_TIMEOUT,
    TS_RELATIVE,
)
from core.errors import DeezeeError, ServiceUnavailable
from services import levelcurve
from services.timeparse import format_duration
from ui.paginator import Paginator, paginate_lines
from ui.views import BaseView

log = logging.getLogger(__name__)

#: Seconds before an external picture or dictionary request is abandoned. The
#: gateway heartbeat does not wait for a slow third party.
HTTP_TIMEOUT = 10

#: Free, keyless endpoints.
CAT_URL = "https://api.thecatapi.com/v1/images/search"
DOG_URL = "https://dog.ceo/api/breeds/image/random"
URBAN_URL = "https://api.urbandictionary.com/v0/define"

#: Used when TheCatAPI is unreachable, so ?cat always answers something.
CAT_FALLBACK = (
    "https://cdn.discordapp.com/embed/avatars/0.png",
)

#: 8ball answers, in the traditional proportions: ten yes, five maybe, five no.
EIGHTBALL: tuple[tuple[str, int], ...] = (
    ("It is certain.", COLOUR_SUCCESS),
    ("Without a doubt.", COLOUR_SUCCESS),
    ("Yes, definitely.", COLOUR_SUCCESS),
    ("You may rely on it.", COLOUR_SUCCESS),
    ("As I see it, yes.", COLOUR_SUCCESS),
    ("Most likely.", COLOUR_SUCCESS),
    ("Outlook good.", COLOUR_SUCCESS),
    ("Yes.", COLOUR_SUCCESS),
    ("Signs point to yes.", COLOUR_SUCCESS),
    ("Absolutely.", COLOUR_SUCCESS),
    ("Reply hazy, try again.", COLOUR_WARNING),
    ("Ask again later.", COLOUR_WARNING),
    ("Better not tell you now.", COLOUR_WARNING),
    ("Cannot predict now.", COLOUR_WARNING),
    ("Concentrate and ask again.", COLOUR_WARNING),
    ("Do not count on it.", COLOUR_ERROR),
    ("My reply is no.", COLOUR_ERROR),
    ("My sources say no.", COLOUR_ERROR),
    ("Outlook not so good.", COLOUR_ERROR),
    ("Very doubtful.", COLOUR_ERROR),
)

#: ``NdN`` with an optional ``+n`` or ``-n`` modifier.
_DICE_RE = re.compile(r"^(?P<count>\d{1,3})?d(?P<sides>\d{1,4})(?P<mod>[+-]\d{1,4})?$",
                      re.IGNORECASE)

#: Ceilings on ``?roll``. A thousand thousand-sided dice is not a roll, it is a
#: CPU bill.
MAX_DICE = 100
MAX_SIDES = 1000

#: Seconds between reputation grants, per giver.
REP_COOLDOWN = 24 * 3600

_SMALLCAPS = str.maketrans(
    "abcdefghijklmnopqrstuvwxyz",
    "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘqʀꜱᴛᴜᴠᴡxʏᴢ",
)

_FRAKTUR = {
    **{chr(ord("a") + i): chr(0x1D51E + i) for i in range(26)},
    **{chr(ord("A") + i): chr(0x1D504 + i) for i in range(26)},
}
# Five capitals have no Fraktur codepoint in that block; Unicode puts them in
# the letterlike-symbols block instead.
_FRAKTUR.update({"C": "ℭ", "H": "ℌ", "I": "ℑ", "R": "ℜ", "Z": "ℨ"})


def _fullwidth(text: str) -> str:
    """ASCII to its full-width equivalents."""
    return "".join(
        chr(ord(ch) + 0xFEE0) if "!" <= ch <= "~" else ("　" if ch == " " else ch)
        for ch in text
    )


def _owofy(text: str) -> str:
    """The usual substitutions, plus a stutter on some words."""
    out = re.sub(r"[rl]", "w", text)
    out = re.sub(r"[RL]", "W", out)
    out = re.sub(r"n([aeiou])", r"ny\1", out)
    out = re.sub(r"N([aeiou])", r"Ny\1", out)
    words = out.split(" ")
    for index, word in enumerate(words):
        if word and word[0].isalpha() and random.random() < 0.15:
            words[index] = f"{word[0]}-{word}"
    return " ".join(words) + random.choice((" owo", " uwu", " >w<", ""))


def _emojify(text: str) -> str:
    """Letters to regional indicators, digits to keycaps."""
    out = []
    for ch in text.lower():
        if "a" <= ch <= "z":
            out.append(chr(0x1F1E6 + ord(ch) - ord("a")))
        elif ch.isdigit():
            out.append(f"{ch}\N{COMBINING ENCLOSING KEYCAP}")
        elif ch == " ":
            out.append("   ")
        else:
            out.append(ch)
    return " ".join(out)


#: Every ``?fancy`` style. One table, so adding a style is one line.
STYLES: dict[str, Any] = {
    "fullwidth": _fullwidth,
    "aesthetic": _fullwidth,
    "smallcaps": lambda text: text.lower().translate(_SMALLCAPS),
    "fraktur": lambda text: "".join(_FRAKTUR.get(ch, ch) for ch in text),
    "owofy": _owofy,
    "clap": lambda text: " \N{CLAPPING HANDS SIGN} ".join(text.split()),
    "emojify": _emojify,
    "reverse": lambda text: text[::-1],
    "mock": lambda text: "".join(
        ch.upper() if index % 2 else ch.lower() for index, ch in enumerate(text)
    ),
}


class BioModal(discord.ui.Modal, title="Edit your bio"):
    """The one free-text field on a profile."""

    bio = discord.ui.TextInput(
        label="Bio",
        style=discord.TextStyle.paragraph,
        max_length=300,
        required=False,
        placeholder="Shown on your ?profile. Leave blank to clear it.",
    )

    def __init__(self, cog: Fun, existing: str) -> None:
        super().__init__(timeout=300.0)
        self.cog = cog
        self.bio.default = existing

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.set_bio(
            interaction.guild.id, interaction.user.id, self.bio.value.strip()
        )
        await interaction.response.send_message(
            embed=discord.Embed(
                description=f"{EMOJI_SUCCESS}  Bio updated.", colour=COLOUR_SUCCESS
            ),
            ephemeral=True,
        )


class ProfileView(BaseView):
    """A profile embed with an edit button, shown only to its owner."""

    def __init__(self, cog: Fun, author: discord.abc.User, target_id: int,
                 bio: str) -> None:
        super().__init__(author, timeout=PANEL_TIMEOUT)
        self.cog = cog
        self.bio = bio
        # Editing someone else's bio is not a thing, so the button only exists
        # when you are looking at your own.
        if author.id != target_id:
            self.remove_item(self.edit)

    @discord.ui.button(label="Edit bio", style=discord.ButtonStyle.primary)
    async def edit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Open the bio editor."""
        await interaction.response.send_modal(BioModal(self.cog, self.bio))


class RPSView(BaseView):
    """Rock-paper-scissors, against the bot or another member."""

    CHOICES = {
        "Rock": "\N{RAISED FIST}",
        "Paper": "\N{RAISED HAND WITH FINGERS SPLAYED}",
        "Scissors": "\N{VICTORY HAND}",
    }
    BEATS = {"Rock": "Scissors", "Paper": "Rock", "Scissors": "Paper"}

    def __init__(
        self, challenger: discord.Member, opponent: discord.Member | None
    ) -> None:
        # No author lock: two people have to be able to press. The per-press
        # check below does the gating instead.
        super().__init__(None, timeout=120.0)
        self.challenger = challenger
        self.opponent = opponent
        self.picks: dict[int, str] = {}

        for name, emoji in self.CHOICES.items():
            button = discord.ui.Button(label=name, emoji=emoji)
            button.callback = self._make_callback(name)
            self.add_item(button)

    def _make_callback(self, choice: str) -> Any:
        async def callback(interaction: discord.Interaction) -> None:
            await self.record(interaction, choice)

        return callback

    def players(self) -> tuple[int, ...]:
        if self.opponent is None:
            return (self.challenger.id,)
        return (self.challenger.id, self.opponent.id)

    async def record(self, interaction: discord.Interaction, choice: str) -> None:
        if interaction.user.id not in self.players():
            await interaction.response.send_message(
                "This game is not yours.", ephemeral=True
            )
            return
        if interaction.user.id in self.picks:
            await interaction.response.send_message(
                "You have already chosen.", ephemeral=True
            )
            return

        self.picks[interaction.user.id] = choice
        await interaction.response.send_message(
            f"You picked **{choice}**.", ephemeral=True
        )

        if len(self.picks) < len(self.players()):
            return

        if self.opponent is None:
            mine = random.choice(list(self.CHOICES))
            theirs = self.picks[self.challenger.id]
            result = self._judge(theirs, mine)
            body = (
                f"{self.challenger.mention} played **{theirs}**\n"
                f"I played **{mine}**\n\n{result}"
            )
        else:
            left = self.picks[self.challenger.id]
            right = self.picks[self.opponent.id]
            outcome = self._judge(left, right)
            body = (
                f"{self.challenger.mention} played **{left}**\n"
                f"{self.opponent.mention} played **{right}**\n\n"
                + outcome.replace("You", self.challenger.display_name)
            )

        self.disable_all()
        self.stop()
        try:
            await interaction.message.edit(
                embed=discord.Embed(
                    title="Rock, paper, scissors", description=body, colour=COLOUR_INFO
                ),
                view=self,
            )
        except discord.HTTPException:
            pass

    @classmethod
    def _judge(cls, left: str, right: str) -> str:
        if left == right:
            return "A draw."
        return "You win." if cls.BEATS[left] == right else "You lose."

    def embed(self) -> discord.Embed:
        who = (
            f"{self.challenger.mention} vs {self.opponent.mention}"
            if self.opponent
            else f"{self.challenger.mention} vs me"
        )
        return discord.Embed(
            title="Rock, paper, scissors",
            description=f"{who}\n\nBoth choices stay hidden until everyone has picked.",
            colour=COLOUR_INFO,
        )


class Fun(commands.Cog):
    """Dice, randomness, text transforms, pictures, profiles and reputation."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    @staticmethod
    def _ok(description: str) -> discord.Embed:
        return discord.Embed(
            description=f"{EMOJI_SUCCESS}  {description}", colour=COLOUR_SUCCESS
        )

    @staticmethod
    async def _fetch_json(url: str, params: dict[str, str] | None = None) -> Any:
        """GET a JSON endpoint with a hard timeout.

        Raises:
            ServiceUnavailable: On any network error or non-200 status. Callers
                let it reach the global handler, which renders it plainly.
        """
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, params=params) as response:
                    if response.status != 200:
                        raise ServiceUnavailable(
                            url.split("/")[2], f"It answered HTTP {response.status}."
                        )
                    return await response.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise ServiceUnavailable(url.split("/")[2], str(exc)) from exc

    # =======================================================================
    # Randomness
    # =======================================================================

    @commands.hybrid_command(name="8ball", aliases=["eightball"])
    @app_commands.describe(question="A yes or no question")
    async def eightball(self, ctx: commands.Context, *, question: str) -> None:
        """Answer a yes/no question at random."""
        answer, colour = random.choice(EIGHTBALL)
        embed = discord.Embed(colour=colour)
        embed.add_field(name="Question", value=question[:1000], inline=False)
        embed.add_field(name="\N{BILLIARDS} Answer", value=answer, inline=False)
        await ctx.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(name="coinflip", aliases=["flip"])
    async def coinflip(self, ctx: commands.Context) -> None:
        """Flip a coin."""
        heads = random.random() < 0.5
        await ctx.send(
            embed=discord.Embed(
                title="Heads" if heads else "Tails",
                description="\N{COIN}" if heads else "\N{COIN}",
                colour=COLOUR_INFO,
            )
        )

    @commands.hybrid_command(name="roll", aliases=["dice"])
    @app_commands.describe(dice="NdN notation, e.g. 2d6, 1d20+3")
    async def roll(self, ctx: commands.Context, dice: str = "1d6") -> None:
        """Roll dice in NdN notation, with an optional modifier."""
        match = _DICE_RE.match(dice.strip().replace(" ", ""))
        if match is None:
            raise DeezeeError(
                f"`{dice}` is not dice notation. Try `1d6`, `2d20` or `4d8+2`."
            )

        count = int(match["count"] or 1)
        sides = int(match["sides"])
        modifier = int(match["mod"] or 0)

        if not 1 <= count <= MAX_DICE:
            raise DeezeeError(f"Between 1 and {MAX_DICE} dice.")
        if not 2 <= sides <= MAX_SIDES:
            raise DeezeeError(f"Between 2 and {MAX_SIDES} sides.")

        rolls = [random.randint(1, sides) for _ in range(count)]
        total = sum(rolls) + modifier

        embed = discord.Embed(
            title=f"{count}d{sides}" + (f"{modifier:+d}" if modifier else ""),
            description=f"**{total}**",
            colour=COLOUR_INFO,
        )
        if count > 1:
            shown = ", ".join(str(r) for r in rolls[:50])
            if count > 50:
                shown += f", … (+{count - 50} more)"
            embed.add_field(name="Rolls", value=shown[:1024], inline=False)
        if modifier:
            embed.add_field(
                name="Breakdown", value=f"{sum(rolls)} {modifier:+d} = {total}",
                inline=False,
            )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="choose", aliases=["pick"])
    @app_commands.describe(options="Comma-separated options")
    async def choose(self, ctx: commands.Context, *, options: str) -> None:
        """Pick one option from a comma-separated list."""
        choices = [part.strip() for part in options.split(",") if part.strip()]
        if len(choices) < 2:
            raise DeezeeError(
                "Give at least two options, separated by commas. "
                "Example: `?choose pizza, pasta, salad`."
            )
        await ctx.send(
            embed=discord.Embed(
                title="I pick",
                description=f"**{random.choice(choices)}**",
                colour=COLOUR_INFO,
            ).set_footer(text=f"from {len(choices)} options"),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(name="rps")
    @app_commands.describe(opponent="Optional: challenge a member instead of me")
    @permissions.guild_only()
    async def rps(
        self, ctx: commands.Context, opponent: Optional[discord.Member] = None
    ) -> None:
        """Play rock-paper-scissors against the bot or another member."""
        if opponent is not None:
            if opponent.bot:
                raise DeezeeError("Leave the opponent blank to play against me.")
            if opponent.id == ctx.author.id:
                raise DeezeeError("You cannot challenge yourself.")

        view = RPSView(ctx.author, opponent)
        view.message = await ctx.send(
            content=opponent.mention if opponent else None,
            embed=view.embed(),
            view=view,
            allowed_mentions=discord.AllowedMentions(users=True),
        )

    # =======================================================================
    # Text
    # =======================================================================

    @commands.hybrid_command(name="fancy")
    @app_commands.describe(
        style="fullwidth, smallcaps, fraktur, owofy, clap, emojify, reverse, mock",
        text="What to transform",
    )
    async def fancy(self, ctx: commands.Context, style: str, *, text: str) -> None:
        """Transform text.

        One command with a style argument, replacing Carl-bot's eight separate
        ones. Eight commands differing only in a lookup table are eight places
        for the same bug.
        """
        chosen = style.strip().lower()
        if chosen not in STYLES:
            raise DeezeeError(
                "Style must be one of: " + ", ".join(f"`{s}`" for s in STYLES)
            )
        if len(text) > 400:
            raise DeezeeError(
                "400 characters or fewer -- some styles multiply the length by four."
            )

        result = STYLES[chosen](text)
        await ctx.send(
            result[:2000], allowed_mentions=discord.AllowedMentions.none()
        )

    # =======================================================================
    # Pictures and lookups
    # =======================================================================

    @commands.hybrid_command(name="cat")
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def cat(self, ctx: commands.Context) -> None:
        """Post a random cat picture."""
        if ctx.interaction is not None and not ctx.interaction.response.is_done():
            await ctx.defer()

        url = ""
        try:
            payload = await self._fetch_json(CAT_URL)
            if isinstance(payload, list) and payload:
                url = payload[0].get("url", "")
        except ServiceUnavailable as exc:
            log.info("TheCatAPI unavailable: %s", exc)

        embed = discord.Embed(title="Cat", colour=COLOUR_INFO)
        if url:
            embed.set_image(url=url)
        else:
            embed.set_image(url=random.choice(CAT_FALLBACK))
            embed.set_footer(
                text="TheCatAPI is unreachable, so this is the bundled fallback."
            )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="dog", aliases=["pug"])
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def dog(self, ctx: commands.Context) -> None:
        """Post a random dog picture."""
        if ctx.interaction is not None and not ctx.interaction.response.is_done():
            await ctx.defer()

        payload = await self._fetch_json(DOG_URL)
        url = payload.get("message") if isinstance(payload, dict) else None
        if not url:
            raise ServiceUnavailable("dog.ceo", "It returned no image.")

        embed = discord.Embed(title="Dog", colour=COLOUR_INFO)
        embed.set_image(url=url)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="urban", with_app_command=False, aliases=["ud"])
    @app_commands.describe(term="What to look up")
    @commands.cooldown(1, 10, commands.BucketType.channel)
    async def urban(self, ctx: commands.Context, *, term: str) -> None:
        """Look up an Urban Dictionary definition."""
        # Age-gated because Urban Dictionary is not a moderated source and the
        # top result for an innocuous word is frequently not innocuous.
        if not getattr(ctx.channel, "nsfw", False):
            raise DeezeeError(
                "`?urban` only works in an age-restricted channel. Urban Dictionary "
                "is unmoderated, and the top result for an ordinary word is often "
                "not an ordinary result."
            )

        if ctx.interaction is not None and not ctx.interaction.response.is_done():
            await ctx.defer()

        payload = await self._fetch_json(URBAN_URL, {"term": term.strip()})
        entries = payload.get("list") if isinstance(payload, dict) else None
        if not entries:
            raise DeezeeError(f"Urban Dictionary has nothing for `{term}`.")

        pages = []
        for entry in entries[:10]:
            definition = re.sub(r"[\[\]]", "", entry.get("definition", ""))[:1500]
            example = re.sub(r"[\[\]]", "", entry.get("example", ""))[:900]
            embed = discord.Embed(
                title=entry.get("word", term),
                url=entry.get("permalink"),
                description=definition,
                colour=COLOUR_WARNING,
            )
            if example:
                embed.add_field(name="Example", value=example, inline=False)
            embed.add_field(
                name="Votes",
                value=f"\N{THUMBS UP SIGN} {entry.get('thumbs_up', 0)}  "
                f"\N{THUMBS DOWN SIGN} {entry.get('thumbs_down', 0)}",
                inline=True,
            )
            embed.set_footer(text="Urban Dictionary is user-written and unmoderated.")
            pages.append(embed)

        await Paginator(pages, ctx.author).start(ctx)

    # =======================================================================
    # Profiles and reputation
    # =======================================================================

    async def profile_row(self, guild_id: int, user_id: int) -> dict[str, Any]:
        row = await self.bot.db.fetchone(
            "SELECT * FROM profiles WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        if row is not None:
            return dict(row)
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO profiles (guild_id, user_id, created_at) "
            "VALUES (?, ?, ?)",
            (guild_id, user_id, int(time.time())),
        )
        return {
            "guild_id": guild_id, "user_id": user_id, "bio": "",
            "reputation": 0, "last_rep_at": 0, "created_at": int(time.time()),
        }

    async def set_bio(self, guild_id: int, user_id: int, bio: str) -> None:
        await self.profile_row(guild_id, user_id)
        await self.bot.db.execute(
            "UPDATE profiles SET bio = ? WHERE guild_id = ? AND user_id = ?",
            (bio[:300], guild_id, user_id),
        )

    @commands.hybrid_command(name="profile")
    @app_commands.describe(target="Whose profile. Defaults to you")
    @permissions.guild_only()
    async def profile(
        self, ctx: commands.Context, target: Optional[discord.Member] = None
    ) -> None:
        """Show a member's social profile: bio, reputation, level and currency."""
        member = target or ctx.author
        row = await self.profile_row(ctx.guild.id, member.id)

        embed = discord.Embed(
            title=member.display_name,
            description=row["bio"] or "*No bio set.*",
            colour=member.colour.value or COLOUR_DEFAULT,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Reputation", value=str(row["reputation"]), inline=True)

        level_row = await self.bot.db.fetchone(
            "SELECT xp, messages FROM levels WHERE guild_id = ? AND user_id = ?",
            (ctx.guild.id, member.id),
        )
        if level_row is not None:
            level = levelcurve.level_from_xp(int(level_row["xp"]))
            embed.add_field(
                name="Level",
                value=f"{level}  ({int(level_row['xp']):,} XP)",
                inline=True,
            )
            embed.add_field(
                name="Messages", value=f"{int(level_row['messages']):,}", inline=True
            )

        config = await self.bot.guild_config.get(ctx.guild.id)
        if config["economy_enabled"]:
            balance = await self.bot.db.fetchval(
                "SELECT balance FROM economy WHERE guild_id = ? AND user_id = ?",
                (ctx.guild.id, member.id),
            )
            embed.add_field(
                name="Wallet",
                value=f"{config['currency_symbol']} {int(balance or 0):,}",
                inline=True,
            )

        if member.joined_at:
            embed.add_field(
                name="Joined",
                value=f"<t:{int(member.joined_at.timestamp())}:{TS_RELATIVE}>",
                inline=True,
            )

        view = ProfileView(self, ctx.author, member.id, row["bio"])
        view.message = await ctx.send(embed=embed, view=view)

    @commands.hybrid_command(name="rep")
    @app_commands.describe(target="Who to thank", reason="Optional: what for")
    @permissions.guild_only()
    async def rep(
        self, ctx: commands.Context, target: discord.Member, *, reason: str = ""
    ) -> None:
        """Give another member a reputation point, once every 24 hours."""
        if target.id == ctx.author.id:
            raise DeezeeError("You cannot give yourself reputation.")
        if target.bot:
            raise DeezeeError("Bots do not collect reputation.")

        giver = await self.profile_row(ctx.guild.id, ctx.author.id)
        now = int(time.time())
        since = now - int(giver["last_rep_at"])
        if since < REP_COOLDOWN:
            ready = int(giver["last_rep_at"]) + REP_COOLDOWN
            raise DeezeeError(
                f"You have already given reputation today. Next one "
                f"<t:{ready}:{TS_RELATIVE}> (in {format_duration(ready - now)})."
            )

        await self.profile_row(ctx.guild.id, target.id)
        async with self.bot.db.transaction() as conn:
            await conn.execute(
                "UPDATE profiles SET reputation = reputation + 1 "
                "WHERE guild_id = ? AND user_id = ?",
                (ctx.guild.id, target.id),
            )
            await conn.execute(
                "UPDATE profiles SET last_rep_at = ? WHERE guild_id = ? AND user_id = ?",
                (now, ctx.guild.id, ctx.author.id),
            )
            await conn.execute(
                "INSERT INTO reputation_log (guild_id, giver_id, target_id, reason, "
                "created_at) VALUES (?, ?, ?, ?, ?)",
                (ctx.guild.id, ctx.author.id, target.id, reason.strip()[:200] or None,
                 now),
            )

        total = await self.bot.db.fetchval(
            "SELECT reputation FROM profiles WHERE guild_id = ? AND user_id = ?",
            (ctx.guild.id, target.id),
        )
        await ctx.send(
            embed=self._ok(
                f"{target.mention} now has **{int(total or 0)}** reputation."
                + (f"\n> {reason.strip()[:200]}" if reason.strip() else "")
            )
        )

    @commands.hybrid_command(name="reptop", aliases=["replb", "reputationtop"])
    @commands.cooldown(1, 10, commands.BucketType.guild)
    @permissions.guild_only()
    async def reptop(self, ctx: commands.Context) -> None:
        """Reputation leaderboard, with each member's message count beside it.

        Reads the same ``profiles.reputation`` column ``?rep`` writes, so every
        point given since the feature existed is already counted -- there is
        nothing to backfill. Message counts come from the leveling table via a
        LEFT JOIN, because the question a rep leaderboard immediately raises is
        "who leads on both".
        """
        if ctx.interaction is not None and not ctx.interaction.response.is_done():
            await ctx.defer()

        lines: list[str] = []
        position = 0
        async for row in self.bot.db.iterate(
            "SELECT p.user_id, p.reputation, COALESCE(l.messages, 0) AS messages "
            "FROM profiles p "
            "LEFT JOIN levels l ON l.guild_id = p.guild_id AND l.user_id = p.user_id "
            "WHERE p.guild_id = ? AND p.reputation > 0 "
            "ORDER BY p.reputation DESC, messages DESC LIMIT 100",
            (ctx.guild.id,),
        ):
            position += 1
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(position, f"`{position:>3}`")
            lines.append(
                f"{medal} <@{row['user_id']}> — **{int(row['reputation']):,}** rep "
                f"({int(row['messages']):,} msgs)"
            )

        pages = paginate_lines(
            lines,
            title=f"Reputation leaderboard — {ctx.guild.name}",
            per_page=10,
            colour=COLOUR_INFO,
            description="Top 100 by reputation. Ties break on message count.",
            empty_message="Nobody has reputation yet.",
        )
        await Paginator(pages, ctx.author).start(ctx)


async def setup(bot: commands.Bot) -> None:
    """discord.py extension entry point."""
    await bot.add_cog(Fun(bot))
