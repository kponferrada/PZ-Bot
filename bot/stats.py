"""stats.py — Aegis Panel player-stats commands (/stats, /leaderboard).

Reads from Aegis Panel's ledger (via `aegis_stats`) so the bot never duplicates
the kill/death/playtime tracking the panel already does.

Both commands are public — any Discord member can look up a player's stats or
the leaderboard, and results post in-channel (not ephemeral) so everyone sees.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import aegis_stats

_FIELD_LABELS = {
    "deaths": "Deaths",
    "zkills": "Zombie Kills",
    "bandits": "Bandit Kills",
    "pvp": "PvP Kills",
    "distM": "Distance",
    "bestHours": "Best Life",
    "bestKills": "Best Life Kills",
    "totalHours": "Survived",
    "playedMin": "Playtime",
}


def _fmt(field: str, value) -> str:
    if field == "distM":
        return f"{value:,} m"
    if field in ("bestHours", "totalHours"):
        return f"{value:.1f} h"
    if field == "playedMin":
        return f"{value:,} min ({value / 60:.1f} h)"
    return f"{value:,}"


# Position badges for the leaderboard: medals on the podium, keycaps after.
_MEDALS = ("\U0001f947", "\U0001f948", "\U0001f949")  # 🥇 🥈 🥉
_KEYCAPS = ("4\ufe0f\u20e3", "5\ufe0f\u20e3", "6\ufe0f\u20e3",
            "7\ufe0f\u20e3", "8\ufe0f\u20e3", "9\ufe0f\u20e3", "\U0001f51f")  # 4️⃣…🔟


def _badge(position: int) -> str:
    if position <= 3:
        return _MEDALS[position - 1]
    return _KEYCAPS[position - 4]


class StatsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="stats", description="Show a player's Aegis Panel stats.")
    async def cmd_stats(self, interaction: discord.Interaction, username: str) -> None:
        await interaction.response.defer()
        stats = await aegis_stats.get(self.bot, username, force=True)
        if stats is None:
            await interaction.followup.send(
                embed=discord.Embed(
                    title=f"\U0001f50d No stats for {username}",
                    description="Aegis Panel has no ledger entry for that name.",
                    colour=discord.Colour.orange(),
                ))
            return
        embed = discord.Embed(
            title=f"\U0001f4ca {username} \u2014 Aegis Stats",
            colour=discord.Colour.blue(),
        )
        for field, label in _FIELD_LABELS.items():
            embed.add_field(name=label, value=_fmt(field, stats.get(field, 0)), inline=True)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="leaderboard", description="Top players by an Aegis stat.")
    @app_commands.choices(kind=[
        app_commands.Choice(name="Zombie Kills", value="zkills"),
        app_commands.Choice(name="Deaths", value="deaths"),
        app_commands.Choice(name="Bandit Kills", value="bandits"),
        app_commands.Choice(name="PvP Kills", value="pvp"),
        app_commands.Choice(name="Distance", value="distM"),
        app_commands.Choice(name="Survived (h)", value="totalHours"),
        app_commands.Choice(name="Playtime (min)", value="playedMin"),
    ])
    async def cmd_leaderboard(self, interaction: discord.Interaction,
                              kind: app_commands.Choice[str]) -> None:
        await interaction.response.defer()
        rows = await aegis_stats.top(self.bot, kind.value, 10, force=True)
        if not rows:
            await interaction.followup.send("No data from Aegis Panel yet.")
            return
        lines = []
        for i, (user, value) in enumerate(rows, 1):
            badge = _badge(i)
            disp = _fmt(kind.value, value)
            if i <= 3:
                lines.append(f"{badge} **{user}** \u2014 **{disp}**")
            else:
                lines.append(f"{badge} **{user}** \u2014 {disp}")

        embed = discord.Embed(
            title=f"\U0001f3c6 {_FIELD_LABELS[kind.value]} \u2014 Top 10",
            description="\n".join(lines),
            colour=discord.Colour.gold(),
        )
        embed.set_footer(text="Stats by Aegis Panel")
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(StatsCog(bot))
    print("[Stats] Extension loaded.")
