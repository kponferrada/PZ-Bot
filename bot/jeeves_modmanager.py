"""
jeeves_modmanager.py — /modlist only (remote).

Shows the server's configured mods and Workshop items by reading the server
`.ini` over SFTP (via the shared `server_config` helper).

Removed from the original Jeeves mod manager:
  - /modadd, /modremove  -> require SteamCMD (same-server)
  - /modreorder          -> requires recursive workshop-folder reads (mod_sorter)

Mod management on a managed host happens in the panel; this command is a
read-only view of what is configured.
"""

import io

import discord
from discord import app_commands
from discord.ext import commands

import server_config


class JeevesModManagerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _check_role(self, interaction: discord.Interaction) -> bool:
        role = discord.utils.get(interaction.guild.roles, name=self.bot.config.DEFAULT_ROLE)
        return role is not None and role in interaction.user.roles

    @staticmethod
    def _split_items(items) -> list:
        """Flatten a mod/workshop list, splitting on ';' and ',' (hosts vary)."""
        out = []
        for item in items:
            for part in str(item).replace(",", ";").split(";"):
                p = part.strip().lstrip("\\")
                if p:
                    out.append(p)
        return out

    @app_commands.command(
        name="modlist",
        description="Show all mods and Workshop items in the server config.",
    )
    async def cmd_modlist(self, interaction: discord.Interaction) -> None:
        if not self._check_role(interaction):
            await interaction.response.send_message(embed=discord.Embed(
                title="Permission Denied",
                description=f"You need the **{self.bot.config.DEFAULT_ROLE}** role.",
                colour=discord.Colour.red(),
            ), ephemeral=True)
            return

        await interaction.response.defer()

        text = await server_config.read_ini(self.bot)
        if text is None:
            await interaction.followup.send(embed=discord.Embed(
                title="📋 Mod List",
                description="Could not read the server INI over SFTP.\n"
                            "Check `SFTP_SERVER_INI` in config.env.",
                colour=discord.Colour.red(),
            ))
            return

        mods = self._split_items(server_config.ini_value(text, "Mods"))
        workshop = self._split_items(server_config.ini_value(text, "WorkshopItems"))
        maps = [m for m in server_config.ini_value(text, "Map") if m.strip()]

        if not mods and not workshop and not maps:
            await interaction.followup.send(embed=discord.Embed(
                title="📋 Server Mod List", description="No mods configured.",
                colour=discord.Colour.purple()))
            return

        # A short summary embed (never trips Discord's size limits) plus the
        # full list as an attached text file (files have no size limits), so a
        # long mod list can never overflow an embed.
        summary = []
        if mods:
            preview = ", ".join(f"`{m}`" for m in mods[:12])
            summary.append(f"**Mods ({len(mods)})**\n{preview}{' …' if len(mods) > 12 else ''}")
        if maps:
            summary.append(f"**Maps ({len(maps)})**\n" + ", ".join(f"`{m}`" for m in maps))
        if workshop:
            summary.append(f"**Workshop items ({len(workshop)})** — see file")

        embed = discord.Embed(
            title="📋 Server Mod List",
            description="\n\n".join(summary),
            colour=discord.Colour.purple(),
        )

        lines = []
        if mods:
            lines.append(f"MODS ({len(mods)})")
            lines.extend(f"  {m}" for m in mods)
        if maps:
            lines.append(f"\nMAPS ({len(maps)})")
            lines.extend(f"  {m}" for m in maps)
        if workshop:
            lines.append(f"\nWORKSHOP ITEMS ({len(workshop)})")
            lines.extend(f"  {w}" for w in workshop)
        content = "\n".join(lines)
        file = discord.File(io.BytesIO(content.encode("utf-8")), filename="server-modlist.txt")

        await interaction.followup.send(embed=embed, file=file)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(JeevesModManagerCog(bot))
    print("[ModManager] /modlist loaded.")
