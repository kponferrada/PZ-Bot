"""jamies_fortune.py — jackpot announcements from the Jamie's Fortune mod.

Jamie's Fortune appends one line per winning spin to
`Lua/JamiesFortune_JackpotLog.txt`:

    2026-09-27 12:32:33 | username=synk | rarity=Legendary | reward=Benelli M4 Semi-Shotgun | spinId=JF-1790512353-729096

This cog tails that file over SFTP (like `death_log`) and posts a
congratulations message to the jackpot channel. The winner is @-mentioned when
their PZ username is linked to a Discord account (`/linkme`), otherwise the
plain username is shown.
"""

from collections import deque
from typing import Optional

import discord
from discord.ext import commands, tasks

import lua_bridge
import sftp_client


class JamiesFortuneCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._lua_dir = getattr(bot.config, "SFTP_LUA_DIR", None)
        self._file: Optional[str] = None
        self._pos = 0
        self._buffer = ""
        self._saw_absent = False  # True once we've observed the file missing
        # Recently announced spin IDs — guards against re-posting when the file
        # is truncated/rewritten and we re-read it from the start.
        self._seen: deque = deque(maxlen=500)
        self._active = bool(self._lua_dir)
        if not self._active:
            print("[JamiesFortune] Disabled (SFTP_LUA_DIR not set).")
        else:
            self._tail.start()
            print(f"[JamiesFortune] Watching {lua_bridge.JAMIES_JACKPOT_FILE}")

    def cog_unload(self):
        if self._active:
            self._tail.cancel()

    def _log_path(self) -> str:
        return f"{self._lua_dir.rstrip('/')}/{lua_bridge.JAMIES_JACKPOT_FILE}"

    @staticmethod
    def _parse_line(line: str) -> dict:
        """Parse one jackpot line into {lowercased key: value}."""
        d = {}
        for part in line.split("|"):
            key, sep, value = part.partition("=")
            if sep:
                d[key.strip().lower()] = value.strip()
        return d

    def _winner_mention(self, username: str) -> str:
        """`<@id>` for a Discord-linked PZ username, else the plain name."""
        rank_cog = self.bot.get_cog("RankSync")
        discord_id = rank_cog.discord_id_for_pz_username(username) if rank_cog else None
        return f"<@{discord_id}>" if discord_id else f"@{username}"

    async def _handle_line(self, line: str) -> None:
        d = self._parse_line(line)
        username = d.get("username")
        reward = d.get("reward")
        if not username or not reward:
            return  # not a jackpot line

        spin_id = d.get("spinid") or line
        if spin_id in self._seen:
            return
        self._seen.append(spin_id)

        rarity = (d.get("rarity") or "").upper()
        if not self.bot.features.is_enabled("jackpots"):
            print(f"[JamiesFortune] Jackpot -> {username}: [{rarity}] {reward} (notifications disabled)")
            return

        channel = self.bot.get_jackpot_channel()
        if not channel:
            return

        rarity_tag = f"[{rarity}] " if rarity else ""
        content = (
            f"🎉 Congratulations {self._winner_mention(username)}! "
            f"You’ve won the {rarity_tag}{reward} from Jamie’s Fortune! 🎰✨"
        )
        try:
            await channel.send(
                content,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"[JamiesFortune] Failed to send jackpot notification: {e}")
            return
        print(f"[JamiesFortune] Jackpot -> {username}: [{rarity}] {reward} ({spin_id})")

    @tasks.loop(seconds=2.0)
    async def _tail(self):
        if not self._active:
            return
        try:
            sftp = sftp_client.get()
            path = self._log_path()
            st = await sftp.stat(path)
            if st is None:
                # Mod not installed / nobody has won yet.
                self._file = None
                self._pos = 0
                self._buffer = ""
                self._saw_absent = True
                return

            size = st[0]
            if self._file is None:
                # File just appeared. If we had seen it missing (freshly created
                # by a win), read from the start so that first win isn't dropped;
                # on startup with an already-populated log, skip history.
                self._pos = 0 if self._saw_absent else size
                self._file = path
                self._buffer = ""
            elif size < self._pos:
                # File truncated (server restart / rotation) — start over.
                self._pos = 0
                self._buffer = ""

            if size == self._pos:
                return  # nothing new; skip opening the file

            text, self._pos = await sftp.tail(path, self._pos)
            if not text:
                return

            self._buffer += text
            lines = self._buffer.split("\n")
            self._buffer = lines[-1]  # keep any trailing (incomplete) line
            for line in lines[:-1]:
                line = line.strip()
                if line:
                    await self._handle_line(line)

        except sftp_client.SftpError:
            pass
        except Exception as e:
            print(f"[JamiesFortune] tail error: {e}")

    @_tail.before_loop
    async def _before_tail(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(JamiesFortuneCog(bot))
    print("[JamiesFortune] Extension loaded.")
