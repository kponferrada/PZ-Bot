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
#   SteamID: 76561198252998080
#   Character Name: baz
#   Gender: Male
#   Profession: Unemployed
#   Infected: true
#   Cause of Death: Zombie
#   Injuries: Torso: Bitten
#   Zombie Kills: 147
#   Survival Time: 23 days, 5 hours
#   Location: X: 12345, Y: 67890, Z: 0
#   Game Date Time: 1993-7-22 14:32
#   =================================


class DeathLogCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._lua_dir = getattr(bot.config, "SFTP_LUA_DIR", None)
        self._file: Optional[str] = None
        self._pos = 0
        self._buffer = ""
        self._saw_absent = False  # True once we've observed the file missing
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

    @staticmethod
    def _simplify_position(position: str) -> str:
        """Round the X/Y/Z floats in a position string to whole numbers."""
        m = re.search(
            r"X:\s*(-?\d+(?:\.\d+)?)\s*,\s*Y:\s*(-?\d+(?:\.\d+)?)\s*,\s*Z:\s*(-?\d+(?:\.\d+)?)",
            position,
        )
        if m:
            x, y, z = (round(float(v)) for v in m.groups())
            return f"X: {x}, Y: {y}, Z: {z}"
        return position

    async def _handle_block(self, d: dict) -> None:
        # "Survivor" is the stable Steam username — that's what the death
        # counter keys on, since the character name changes each run.
        survivor = d.get("username") or d.get("character name") or d.get("steam name")
        if not survivor:
            return  # not a death block

        character_name = d.get("character name") or ""
        cause = d.get("cause of death") or "Unknown"
        injuries = d.get("injuries") or "None"
        survived = d.get("survival time") or ""
        kills = d.get("zombie kills") or "0"
        position = self._simplify_position(d.get("location") or "")
        game_date_time = d.get("game date time") or ""
        infected = (d.get("infected") or "").strip().lower() in ("true", "yes", "1")

        death_count = record_death(survivor, cause)
        if not self.bot.features.is_enabled("deaths"):
            print(f"[DeathLog] Death -> {survivor} (#{death_count}) (notifications disabled)")
            return

        channel = self.bot.get_death_logs_channel()
        if not channel:
            return

        lines = [f"👤 Survivor: {survivor}"]
        if character_name:
            lines.append(f"🎭 Character Name: {character_name}")
        lines += [
            f"🦠 Infected: {'true' if infected else 'false'}",
            f"💀 Cause of Death: {cause}",
            f"🩸 Injuries: {injuries}",
            f"🧟 Zombie Kills: {kills}",
            f"⏳ Survival Time: {survived}",
            f"📍 Location: {position}",
            f"📅 Game Date Time: {game_date_time}",
            f"☠️ Death Counter: {death_count}",
        ]

        embed = discord.Embed(
            title="☠️ Death Notification",
            description="\n".join(lines),
            colour=discord.Colour.red(),
        )

        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"[DeathLog] Failed to send death log: {e}")
        print(f"[DeathLog] Death -> {survivor} (#{death_count}) cause={cause}")

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
                self._saw_absent = True
                return

            size = st[0]
            self.bot.state.death_log_active = True

            if self._file is None:
                # File just appeared. If we had seen it missing (it was freshly
                # created by a death), read from the start so that first death
                # isn't dropped; on startup with an already-populated log, skip
                # historical entries instead.
                self._pos = 0 if self._saw_absent else size
                self._file = path
                self._buffer = ""
            elif size < self._pos:
                # File truncated (server restart / rotation) — start over.
                self._pos = 0
                self._buffer = ""

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
