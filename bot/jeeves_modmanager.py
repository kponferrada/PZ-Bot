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

    @staticmethod
    def _chunk_text(s: str, limit: int) -> list:
        """Split `s` into pieces <= `limit` chars, preferring breaks on ', '."""
        if len(s) <= limit:
            return [s]
        out = []
        while len(s) > limit:
            cut = s.rfind(", ", 0, limit)
            if cut < 0:
                cut = s.rfind(" ", 0, limit)
            if cut < 0:
                cut = limit
            out.append(s[:cut].rstrip(", "))
            s = s[cut:].lstrip(", ")
        if s:
            out.append(s)
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

        sections = []
        if mods:
            sections.append(f"**Mods ({len(mods)})**\n" + ", ".join(f"`{m}`" for m in mods))
        if maps:
            sections.append(f"**Maps ({len(maps)})**\n" + ", ".join(f"`{m}`" for m in maps))
        if workshop:
            sections.append(f"**Workshop items ({len(workshop)})**\n" + ", ".join(f"`{w}`" for w in workshop))

        if not sections:
            await interaction.followup.send(embed=discord.Embed(
                title="📋 Server Mod List", description="No mods configured.",
                colour=discord.Colour.purple()))
            return

        # Chunk the full text into pieces small enough that a single embed never
        # trips Discord's limits — description (4096) and total (6000) — then
        # send one embed per piece (Discord allows up to 10 embeds per message).
        full = "\n\n".join(sections)
        parts = self._chunk_text(full, 2000)
        embeds = []
        for i, part in enumerate(parts):
            title = "📋 Server Mod List" if i == 0 else "📋 Server Mod List (cont.)"
            embeds.append(discord.Embed(title=title, description=part, colour=discord.Colour.purple()))
        await interaction.followup.send(embeds=embeds)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(JeevesModManagerCog(bot))
    print("[ModManager] /modlist loaded.")
