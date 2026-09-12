"""death_log.py — rich player-death announcements from the "Death Log" mod.

The "[Server Tool] Death Log" mod (Steam Workshop 2972685375) writes detailed
death info to `Lua/player-death-logging.log` on the server. This cog tails that
file over SFTP and posts an enriched death announcement — cause of death,
survived time, location, zombie kills, favourite weapon, infected flag.

When the Death Log file is present this cog is the authoritative death source;
`player_tracker`'s vanilla `_user.txt` death detection is then skipped via
`ServerState.death_log_active`, so you never get a duplicate announcement.
"""

import re
from typing import Optional

import discord
from discord.ext import commands, tasks

import sftp_client
from player_tracker import record_death

_DEATH_LOG_NAME = "player-death-logging.log"

# A block in player-death-logging.log looks like:
#
#   Username: foo
#   Steam Name: bar
#   Character Name: baz
#   Death Cause: Zombie
#   Zombie Kills: 147
#   Survived Time: 23 days, 5 hours
#   Favorite Weapon: Axe
#   Position: X: 12345, Y: 67890, Z: 0
#   Game Date Time: 1993-7-22 14:32
#   Infected: true
#   =================================


class DeathLogCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._lua_dir = getattr(bot.config, "SFTP_LUA_DIR", None)
        self._file: Optional[str] = None
        self._pos = 0
        self._buffer = ""
        self._active = bool(self._lua_dir)
        if not self._active:
            print("[DeathLog] Disabled (SFTP_LUA_DIR not set).")
        else:
            self._tail.start()
            print("[DeathLog] Watching player-death-logging.log")

    def cog_unload(self):
        if self._active:
            self._tail.cancel()

    def _log_path(self) -> str:
        return f"{self._lua_dir.rstrip('/')}/{_DEATH_LOG_NAME}"

    @staticmethod
    def _parse_block(block: str) -> dict:
        """Parse one death block into {lowercased key: value}."""
        d = {}
        for line in block.splitlines():
            line = line.strip()
            if not line or line.startswith("="):
                continue
            if ":" in line:
                key, _, value = line.partition(":")
                key = key.strip().lower()
                if key:
                    d[key] = value.strip()
        return d

    async def _handle_block(self, d: dict) -> None:
        name = (d.get("character name") or d.get("username")
                or d.get("steam name"))
        if not name:
            return  # not a death block

        cause = d.get("death cause") or None
        survived = d.get("survived time") or None
        position = d.get("position") or None
        kills = d.get("zombie kills") or None
        weapon = d.get("favorite weapon") or None
        infected = (d.get("infected") or "").strip().lower() in ("true", "yes", "1")

        death_count = record_death(name, cause)
        if not self.bot.features.is_enabled("deaths"):
            print(f"[DeathLog] Death -> {name} (#{death_count}) (notifications disabled)")
            return

        channel = self.bot.get_death_logs_channel()
        if not channel:
            return

        embed = discord.Embed(
            title=f"\u2620\ufe0f **{name}** has died! (death #{death_count})",
            colour=discord.Colour.red(),
        )
        if cause:
            embed.add_field(name="Cause of Death", value=cause, inline=True)
        if survived:
            embed.add_field(name="Survived", value=survived, inline=True)
        if position:
            embed.add_field(name="Location", value=position, inline=True)
        if kills is not None and kills != "":
            embed.add_field(name="Zombie Kills", value=kills, inline=True)
        if weapon:
            embed.add_field(name="Favorite Weapon", value=weapon, inline=True)
        if infected:
            embed.add_field(name="Infected", value="\u26a0\ufe0f Yes", inline=True)

        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"[DeathLog] Failed to send death log: {e}")
        print(f"[DeathLog] Death -> {name} (#{death_count}) cause={cause}")

    @tasks.loop(seconds=2.0)
    async def _tail(self):
        if not self._active:
            return
        try:
            sftp = sftp_client.get()
            path = self._log_path()
            st = await sftp.stat(path)
            if st is None:
                # Mod not installed / file not written yet — fall back to _user.txt.
                self.bot.state.death_log_active = False
                self._file = None
                self._pos = 0
                self._buffer = ""
                return

            size = st[0]
            self.bot.state.death_log_active = True

            if path != self._file or size < self._pos:
                # First sight, rotation, or server restart (file truncated) —
                # skip historical/partial content and resume at the end.
                self._file = path
                self._pos = size
                self._buffer = ""
                return

            text, self._pos = await sftp.tail(path, self._pos)
            if not text:
                return

            self._buffer += text
            parts = self._buffer.split("=================================")
            self._buffer = parts[-1]  # keep any trailing (incomplete) block
            for block in parts[:-1]:
                d = self._parse_block(block)
                if d:
                    await self._handle_block(d)

        except sftp_client.SftpError:
            pass
        except Exception as e:
            print(f"[DeathLog] tail error: {e}")

    @_tail.before_loop
    async def _before_tail(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(DeathLogCog(bot))
    print("[DeathLog] Extension loaded.")
