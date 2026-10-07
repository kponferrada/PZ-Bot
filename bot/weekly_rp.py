"""weekly_rp.py — the Barangay Tales weekly leaderboards.

/rpleaderboard shows this week's personal Reputation Points standings with the
weekly title each top-5 place earns when the week ends, plus last week's
winners and the titles they were granted. Each player carries their fire rank
(BT's leaderboard stars, the in-game chat colour).

/factionleaderboard is the same for factions: this week's Faction Reputation
standings with the faction title each top-5 place earns (its members wear it
next week), plus last week's faction title holders.

Data comes from the Barangay Tales export (see bt_progression).

/repboard draws the top 5 survivors and/or factions on the PZ Tambayan
"Reputation Ranking" poster (rep_board.py), with the Discord avatars of players
linked with /linkme.

Public, like /leaderboard: anyone can run them and the result posts in-channel.
"""

from __future__ import annotations

import asyncio
import datetime
from typing import Dict, Iterable, Optional

import discord
from discord import app_commands
from discord.ext import commands

import bt_progression
import ranks
import rep_board
from stats import _badge

# Barangay Tales weeks end Monday 00:00 GMT+8 (BT Config.WeeklyRanking.timezoneOffsetHours).
_WEEK_TZ = datetime.timezone(datetime.timedelta(hours=8))


def _week_end_epoch(now: datetime.datetime | None = None) -> int:
    """Next Monday 00:00 GMT+8 as epoch seconds."""
    now = (now or datetime.datetime.now(_WEEK_TZ)).astimezone(_WEEK_TZ)
    monday = (now - datetime.timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return int((monday + datetime.timedelta(days=7)).timestamp())


def _row_line(row: dict) -> str:
    rank = row["rank"]
    badge = _badge(rank) if rank <= 10 else f"`#{rank}`"
    name = discord.utils.escape_markdown(row["displayName"])
    rp = f"{row['rp']:,} RP"
    fire = ranks.RANKS.get(row.get("fire") or 0)
    if fire and row.get("fire"):
        name = f"{fire.emoji} {name}"
    line = f"{badge} **{name}** — **{rp}**" if rank <= 3 else f"{badge} **{name}** — {rp}"
    if row.get("title"):
        line += f" · \U0001f396️ *{row['title']}*"
    return line


def build_embed(board: dict) -> discord.Embed:
    week = board.get("weekId") or "this week"
    rows = board["rows"]
    lines = [_row_line(r) for r in rows] or ["*No Reputation earned yet this week.*"]
    lines.append("")
    lines.append(f"Week ends <t:{_week_end_epoch()}:R>")
    if board.get("generatedAt"):
        lines.append(f"Updated <t:{board['generatedAt']}:R>")

    embed = discord.Embed(
        title=f"\U0001f3c6 Weekly RP Leaderboard — {week}",
        description="\n".join(lines),
        colour=discord.Colour.gold(),
    )

    titles = board.get("titles") or []
    if titles:
        embed.add_field(
            name="Weekly titles (top 5, held for next week)",
            value="\n".join(f"{_badge(i)} {t}" for i, t in enumerate(titles, 1) if t),
            inline=False,
        )

    last = board.get("lastWeek")
    if last and last.get("rows"):
        value = "\n".join(
            f"{_badge(r['rank'])} **{discord.utils.escape_markdown(r['displayName'])}** "
            f"— {r['rp']:,} RP · *{r['title'] or 'no title'}*"
            for r in last["rows"] if 1 <= r["rank"] <= 10
        )
        embed.add_field(name=f"Last week ({last['weekId']}) — titles granted",
                        value=value or "—", inline=False)

    legend = " ".join(f"{ranks.RANKS[n].emoji} {ranks.RANKS[n].name}" for n in range(6, 0, -1))
    embed.set_footer(text="Barangay Tales · RP from wealth, kills, quests and event wins\n"
                          f"Fire ranks: {legend} (title holders 1st-5th, else Fuel)")
    return embed


def _faction_line(row: dict) -> str:
    rank = row["rank"]
    badge = _badge(rank) if rank <= 10 else f"`#{rank}`"
    name = discord.utils.escape_markdown(row["name"])
    rp = f"{row['rp']:,} RP"
    members = row.get("members") or 0
    line = f"{badge} **{name}** — **{rp}**" if rank <= 3 else f"{badge} **{name}** — {rp}"
    line += f" · {members} member{'s' if members != 1 else ''}"
    if row.get("title"):
        line += f" · \U0001f396️ *{row['title']}*"
    return line


def build_faction_embed(board: dict) -> discord.Embed:
    """Embed for `bt_progression.faction_leaderboard(...)` plus weekId/generatedAt."""
    week = board.get("weekId") or "this week"
    lines = [_faction_line(r) for r in board["rows"]] or ["*No Faction Reputation earned yet this week.*"]
    lines.append("")
    lines.append(f"Week ends <t:{_week_end_epoch()}:R>")
    if board.get("generatedAt"):
        lines.append(f"Updated <t:{board['generatedAt']}:R>")

    embed = discord.Embed(
        title=f"\U0001f6e1️ Weekly Faction Leaderboard — {week}",
        description="\n".join(lines),
        colour=discord.Colour.gold(),
    )

    titles = board.get("titles") or []
    if titles:
        embed.add_field(
            name="Faction titles (top 5, worn by members next week)",
            value="\n".join(f"{_badge(i)} {t}" for i, t in enumerate(titles, 1) if t),
            inline=False,
        )

    last = board.get("lastWeek")
    if last and last.get("rows"):
        value = "\n".join(
            f"{_badge(r['rank'])} **{discord.utils.escape_markdown(r['name'])}** "
            f"— {r['rp']:,} RP · *{r['title'] or 'no title'}*"
            for r in last["rows"] if 1 <= r["rank"] <= 10
        )
        embed.add_field(name=f"Last week ({last['weekId']}) — titles granted",
                        value=value or "—", inline=False)

    embed.set_footer(text="Barangay Tales · ranked by Faction Reputation earned this week. "
                          "The faction leader picks the title's EXP boost skill.")
    return embed


class WeeklyRPCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _avatar(self, pz_username: str) -> Optional[bytes]:
        """Discord avatar of the user linked to `pz_username`, or None."""
        rank_cog = self.bot.get_cog("RankSync")
        discord_id = rank_cog.discord_id_for_pz_username(pz_username) if rank_cog else None
        if not discord_id:
            return None
        try:
            guild = self.bot.get_guild(self.bot.config.GUILD_ID)
            user = (guild.get_member(discord_id) if guild else None) \
                or self.bot.get_user(discord_id) \
                or await self.bot.fetch_user(discord_id)
            asset = user.display_avatar.replace(size=256, static_format="png")
            return await asset.read()
        except (discord.DiscordException, ValueError) as e:
            print(f"[WeeklyRP] Could not fetch avatar for {pz_username}: {e}")
            return None

    async def _avatars(self, usernames: Iterable[str]) -> Dict[str, bytes]:
        names = list(dict.fromkeys(usernames))
        pics = await asyncio.gather(*(self._avatar(n) for n in names))
        return {n: p for n, p in zip(names, pics) if p}

    @app_commands.command(name="rpleaderboard",
                          description="Barangay Tales weekly RP leaderboard (players) and the titles each place earns.")
    @app_commands.describe(limit="How many places to show (default 10, max 25)")
    async def cmd_rpleaderboard(self, interaction: discord.Interaction, limit: int = 10) -> None:
        await interaction.response.defer()
        limit = max(1, min(int(limit), 25))
        # The export changes every 5-10 min; the 60 s cache is fresh enough and
        # keeps a burst of public /rpleaderboard calls from each hitting SFTP.
        board = await bt_progression.get_weekly_leaderboard(self.bot, limit)
        if board is None:
            await interaction.followup.send(embed=discord.Embed(
                title="\U0001f50d Weekly leaderboard unavailable",
                description="Couldn't read the Barangay Tales progression export from the server.",
                colour=discord.Colour.orange(),
            ))
            return
        await interaction.followup.send(embed=build_embed(board))

    @app_commands.command(name="factionleaderboard",
                          description="Barangay Tales weekly faction leaderboard and the titles each place earns.")
    @app_commands.describe(limit="How many places to show (default 10, max 25)")
    async def cmd_factionleaderboard(self, interaction: discord.Interaction, limit: int = 10) -> None:
        await interaction.response.defer()
        limit = max(1, min(int(limit), 25))
        board = await bt_progression.get_faction_leaderboard(self.bot, limit)
        if board is None:
            await interaction.followup.send(embed=discord.Embed(
                title="\U0001f50d Faction leaderboard unavailable",
                description="Couldn't read the Barangay Tales progression export from the server.",
                colour=discord.Colour.orange(),
            ))
            return
        await interaction.followup.send(embed=build_faction_embed(board))

    @app_commands.command(name="repboard",
                          description="Barangay Tales reputation board: top 5 survivors and factions as a poster.")
    @app_commands.describe(board="Which board to draw (default both)",
                           period="This week's RP (default) or all-time Reputation")
    @app_commands.choices(
        board=[app_commands.Choice(name="Survivors and factions", value="both"),
               app_commands.Choice(name="Survivors", value="personal"),
               app_commands.Choice(name="Factions", value="faction")],
        period=[app_commands.Choice(name="This week (RP)", value="week"),
                app_commands.Choice(name="All time (Reputation)", value="alltime")],
    )
    async def cmd_repboard(self, interaction: discord.Interaction,
                           board: Optional[app_commands.Choice[str]] = None,
                           period: Optional[app_commands.Choice[str]] = None) -> None:
        await interaction.response.defer()
        which = board.value if board else "both"
        when = period.value if period else "week"
        data = await bt_progression.read_progression(self.bot)
        if data is None:
            await interaction.followup.send(embed=discord.Embed(
                title="\U0001f50d Reputation board unavailable",
                description="Couldn't read the Barangay Tales progression export from the server.",
                colour=discord.Colour.orange(),
            ))
            return
        kinds = ("personal", "faction") if which == "both" else (which,)
        boards = [bt_progression.reputation_board(data, k, when) for k in kinds]
        avatars = await self._avatars(p for b in boards for r in b["rows"]
                                      for p in r["players"][:8 if b["kind"] == "faction" else 1])
        files = []
        for b in boards:
            buf = await asyncio.to_thread(rep_board.render_board_png, b, avatars)
            name = "reputation-survivors.png" if b["kind"] == "personal" else "reputation-factions.png"
            files.append(discord.File(buf, filename=name))
        await interaction.followup.send(files=files)


async def setup(bot: commands.Bot):
    await bot.add_cog(WeeklyRPCog(bot))
    print("[WeeklyRP] Extension loaded.")
