"""feature_controls.py — runtime /disable, /enable, and /features commands.

Admins can turn individual notification features on/off without restarting the bot.
Requires the DEFAULT_ROLE (same permission model as the other admin commands).
"""

import discord
from discord import app_commands
from discord.ext import commands

import features

FEATURE_CHOICES = [
    app_commands.Choice(name=desc, value=key)
    for key, desc in features.FEATURES.items()
]


class FeatureControls(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _check_role(self, interaction: discord.Interaction) -> bool:
        role = discord.utils.get(
            interaction.guild.roles, name=self.bot.config.DEFAULT_ROLE
        )
        return role is not None and role in interaction.user.roles

    async def _set(self, interaction: discord.Interaction, feature_key: str, enabled: bool) -> None:
        if not self._check_role(interaction):
            await interaction.response.send_message(embed=discord.Embed(
                title="Permission Denied",
                description=f"You need the **{self.bot.config.DEFAULT_ROLE}** role.",
                colour=discord.Colour.red(),
            ), ephemeral=True)
            return

        if enabled:
            self.bot.features.enable(feature_key)
            label = "Enabled"
            colour = discord.Colour.green()
        else:
            self.bot.features.disable(feature_key)
            label = "Disabled"
            colour = discord.Colour.orange()

        name = features.FEATURES.get(feature_key, feature_key)
        await interaction.response.send_message(embed=discord.Embed(
            title=f"Feature {label}",
            description=f"**{name}** is now {label.lower()}.",
            colour=colour,
        ))

    @app_commands.command(name="disable", description="Disable a notification feature.")
    @app_commands.describe(feature="Which feature to disable")
    @app_commands.choices(feature=FEATURE_CHOICES)
    async def cmd_disable(self, interaction: discord.Interaction, feature: app_commands.Choice[str]) -> None:
        await self._set(interaction, feature.value, enabled=False)

    @app_commands.command(name="enable", description="Enable a notification feature.")
    @app_commands.describe(feature="Which feature to enable")
    @app_commands.choices(feature=FEATURE_CHOICES)
    async def cmd_enable(self, interaction: discord.Interaction, feature: app_commands.Choice[str]) -> None:
        await self._set(interaction, feature.value, enabled=True)

    @app_commands.command(name="features", description="List notification features and their on/off state.")
    async def cmd_features(self, interaction: discord.Interaction) -> None:
        lines = []
        for key, desc in features.FEATURES.items():
            state = "\U0001f7e2" if self.bot.features.is_enabled(key) else "\U0001f534"
            lines.append(f"{state} **{desc}** \u2014 `{key}`")
        await interaction.response.send_message(embed=discord.Embed(
            title="Notification Features",
            description="\n".join(lines),
            colour=discord.Colour.blue(),
        ))


async def setup(bot: commands.Bot):
    await bot.add_cog(FeatureControls(bot))
    print("[FeatureControls] Extension loaded.")
