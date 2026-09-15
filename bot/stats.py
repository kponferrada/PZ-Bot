"""stats.py — Aegis Panel player-stats commands (/stats, /leaderboard).

Reads from Aegis Panel's ledger (via `aegis_stats`) so the bot never duplicates
the kill/death/playtime tracking the panel already does.
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


class StatsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        role = discord.utils.get(interaction.guild.roles, name=self.bot.config.DEFAULT_ROLE)
        return role is not None and role in interaction.user.roles

    @app_commands.command(name="stats", description="Show a player's Aegis Panel stats.")
    async def cmd_stats(self, interaction: discord.Interaction, username: str) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "\u274c You don't have permission.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        stats = await aegis_stats.get(self.bot, username, force=True)
        if stats is None:
            await interaction.followup.send(
                embed=discord.Embed(
                    title=f"\U0001f50d No stats for {username}",
                    description="Aegis Panel has no ledger entry for that name.",
                    colour=discord.Colour.orange(),
                ), ephemeral=True)
            return
        embed = discord.Embed(
            title=f"\U0001f4ca {username} \u2014 Aegis Stats",
            colour=discord.Colour.blue(),
        )
        for field, label in _FIELD_LABELS.items():
            embed.add_field(name=label, value=_fmt(field, stats.get(field, 0)), inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

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
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "\u274c You don't have permission.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        rows = await aegis_stats.top(self.bot, kind.value, 10, force=True)
        if not rows:
            await interaction.followup.send(
                "No data from Aegis Panel yet.", ephemeral=True)
            return
        lines = [f"`{i}.` **{user}** \u2014 {_fmt(kind.value, value)}"
                 for i, (user, value) in enumerate(rows, 1)]
        embed = discord.Embed(
            title=f"\U0001f3c6 {_FIELD_LABELS[kind.value]} Leaderboard",
            description="\n".join(lines),
            colour=discord.Colour.gold(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(StatsCog(bot))
    print("[Stats] Extension loaded.")
