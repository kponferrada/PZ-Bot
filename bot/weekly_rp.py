"""weekly_rp.py — /rpleaderboard: the Barangay Tales weekly RP leaderboard.

Shows this week's Reputation Points standings with the weekly title each top-5
place earns when the week ends, plus last week's winners and the titles they
were granted. Data comes from the Barangay Tales export (see bt_progression).

Public, like /leaderboard: anyone can run it and the result posts in-channel.
"""

from __future__ import annotations

import datetime

import discord
from discord import app_commands
from discord.ext import commands

import bt_progression
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

    embed.set_footer(text="Barangay Tales · RP from wealth, kills, quests and event wins")
    return embed


class WeeklyRPCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="rpleaderboard",
                          description="Barangay Tales weekly RP leaderboard and the titles each place earns.")
    @app_commands.describe(limit="How many places to show (default 10, max 25)")
    async def cmd_rpleaderboard(self, interaction: discord.Interaction, limit: int = 10) -> None:
        await interaction.response.defer()
        limit = max(1, min(int(limit), 25))
        board = await bt_progression.get_weekly_leaderboard(self.bot, limit, force=True)
        if board is None:
            await interaction.followup.send(embed=discord.Embed(
                title="\U0001f50d Weekly leaderboard unavailable",
                description="Couldn't read the Barangay Tales progression export from the server.",
                colour=discord.Colour.orange(),
            ))
            return
        await interaction.followup.send(embed=build_embed(board))


async def setup(bot: commands.Bot):
    await bot.add_cog(WeeklyRPCog(bot))
    print("[WeeklyRP] Extension loaded.")
