"""
jeeves_modmanager.py — /modlist only (remote).

Shows the server's configured mods and Workshop items by reading the server
`.ini` over SFTP.

Removed from the original Jeeves mod manager:
  - /modadd, /modremove  -> require SteamCMD (same-server)
  - /modreorder          -> requires recursive workshop-folder reads (mod_sorter)

Mod management on a managed host happens in the panel; this command is a
read-only view of what is configured.
"""

import discord
from discord import app_commands
from discord.ext import commands

import sftp_client


class JeevesModManagerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _check_role(self, interaction: discord.Interaction) -> bool:
        role = discord.utils.get(interaction.guild.roles, name=self.bot.config.DEFAULT_ROLE)
        return role is not None and role in interaction.user.roles

    def _server_dir(self) -> str | None:
        """Derive the remote Server/ folder (sibling of Lua/)."""
        root = getattr(self.bot.config, "SFTP_ZOMBOID_ROOT", None)
        if root:
            return f"{root.rstrip('/')}/Server"
        lua = getattr(self.bot.config, "SFTP_LUA_DIR", None)
        if lua:
            parent = "/".join(lua.rstrip('/').split('/')[:-1])
            return f"{parent}/Server"
        return None

    async def _read_ini(self) -> str | None:
        sftp = sftp_client.get()
        # 1. Explicit path, if configured and present.
        ini = getattr(self.bot.config, "SFTP_SERVER_INI", None)
        if ini and await sftp.exists(ini):
            return await sftp.read_text(ini)
        # 2. Auto-detect: the main server ini is the .ini file in Server/.
        server_dir = self._server_dir()
        if server_dir:
            try:
                for name in await sftp.list_dir(server_dir):
                    if name.endswith(".ini"):
                        path = f"{server_dir.rstrip('/')}/{name}"
                        print(f"[ModManager] Auto-detected server INI: {path}")
                        return await sftp.read_text(path)
            except sftp_client.SftpError:
                pass
        print("[ModManager] Could not find a server INI. Set SFTP_SERVER_INI explicitly.")
        return None

    @staticmethod
    def _ini_value(text: str, key: str) -> list[str]:
        for line in text.splitlines():
            if line.strip().startswith(f"{key}="):
                raw = line.split("=", 1)[1].strip()
                values = [v.strip() for v in raw.split(";") if v.strip()]
                if key == "Mods":
                    values = [v.lstrip("\\") for v in values]  # b42 backslash-prefixed IDs
                return values
        return []

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

        text = await self._read_ini()
        if text is None:
            await interaction.followup.send(embed=discord.Embed(
                title="📋 Mod List",
                description="Could not read the server INI over SFTP.\n"
                            "Check `SFTP_SERVER_INI` in config.env.",
                colour=discord.Colour.red(),
            ))
            return

        mods = self._ini_value(text, "Mods")
        workshop = self._ini_value(text, "WorkshopItems")
        maps = self._ini_value(text, "Map")

        # Build (field_name, field_value) pairs, each value <= 1000 chars.
        fields = []

        def add_category(label, items):
            if not items:
                return
            chunks = []
            cur = []
            cur_len = 0
            for it in items:
                rendered = f"`{it}`"
                sep = 2 if cur else 0
                if cur and cur_len + sep + len(rendered) > 1000:
                    chunks.append(", ".join(f"`{x}`" for x in cur))
                    cur = []
                    cur_len = 0
                    sep = 0
                cur.append(it)
                cur_len += sep + len(rendered)
            if cur:
                chunks.append(", ".join(f"`{x}`" for x in cur))
            for i, chunk in enumerate(chunks):
                name = f"{label} ({len(items)})" if i == 0 else f"{label} (cont.)"
                fields.append((name, chunk))

        add_category("Mods", mods)
        add_category("Maps", maps)
        add_category("Workshop items", workshop)

        if not fields:
            await interaction.followup.send(embed=discord.Embed(
                title="📋 Server Mod List", description="No mods configured.",
                colour=discord.Colour.purple()))
            return

        # Pack fields into embeds, each under Discord's 6000-char total limit.
        embeds = []
        current = discord.Embed(title="📋 Server Mod List", colour=discord.Colour.purple())
        total = len(current.title or "")

        for name, value in fields:
            size = len(name) + len(value) + 8  # field overhead + margin
            if current.fields and (total + size > 5800 or len(current.fields) >= 24):
                embeds.append(current)
                current = discord.Embed(title="📋 Server Mod List (cont.)", colour=discord.Colour.purple())
                total = len(current.title or "")
            current.add_field(name=name, value=value, inline=False)
            total += size

        embeds.append(current)
        await interaction.followup.send(embeds=embeds)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(JeevesModManagerCog(bot))
    print("[ModManager] /modlist loaded.")
