"""
Player Tracker Extension (SFTP).

Tails the PZ server `*_user.txt` log over SFTP for join/leave events and sends
welcome / welcome-back messages to Discord and in-game. Also detects player
deaths and posts a death notification.

"Known players" (for first-time vs returning detection) is read from the game
server's own player database (SFTP_SERVER_DB, e.g. /server-data/db/pzserver.db)
over SFTP and cached in memory — the bot keeps NO local player DB.

Log file detection:
  - Looks in SFTP_LOGS_DIR for the newest file ending in _user.txt
  - Handles log rotation (a new file each server session)

Trigger lines:
  "<STEAMID> \"Name\" fully connected (x,y,z)."  -> join / welcome-back
  "<STEAMID> \"Name\" disconnected."              -> leave notification
  (death line)                                    -> death notification (see _DEATH_RE)

IMPORTANT: the exact death log line varies by PZ build and may not live in
`_user.txt` at all. Confirm against your live `Zomboid/Logs/` and set _DEATH_RE.
"""

import asyncio
import os
import re
import time
from typing import Optional, Set

import discord
from discord.ext import commands, tasks

import lua_bridge
import server_config
import sftp_client

_DEFAULT_LOG_DIR = "Logs"

# ---- log-line regexes --------------------------------------------------------

_CONNECTED_RE = re.compile(r'^\[\S+\s+\S+\]\s+\d+\s+"(.+?)"\s+fully connected \(')
# Leave: <STEAMID> "Name" disconnected. — verify exact wording against your live log if needed.
_DISCONNECTED_RE = re.compile(r'^\[\S+\s+\S+\]\s+\d+\s+"(.+?)"\s+(?:disconnected|left the game|timed out)')

# TODO: confirm the death line format on your live server and set this. Examples that
# have appeared in the wild (verify!):
#   r'^\[\S+\s+\S+\]\s+.*?"(.+?)"\s+died\.'
#   r'^\[\S+\s+\S+\]\s+.*?Player\s+"(.+?)"\s+has died'
# Leave None to disable death detection until verified.
_DEATH_RE: Optional[re.Pattern] = None

_KNOWN_PLAYERS_REFRESH = 300  # seconds between refreshes of the known-players set


# ---- Cog ---------------------------------------------------------------------

class PlayerTrackerCog(commands.Cog):

    def __init__(self, bot):
        self.bot = bot

        # Remote Logs folder (over SFTP).
        self._log_dir: str = getattr(bot.config, "SFTP_LOGS_DIR", None) or _DEFAULT_LOG_DIR

        # Tail state
        self._current_log: Optional[str] = None
        self._file_pos: int = 0
        self._welcome_sent: dict = {}

        # Known players (in-memory cache, from the server's own player DB).
        self._known_players: Set[str] = set()
        self._last_refresh = 0.0

        self._tail_user_log.start()

    def cog_unload(self):
        self._tail_user_log.cancel()

    # ---- file helpers --------------------------------------------------------

    async def _find_latest_user_log(self) -> Optional[str]:
        sftp = sftp_client.get()
        return await sftp.newest_matching(self._log_dir, "*_user.txt")

    async def _refresh_known_players(self) -> None:
        """Refresh the in-memory known-players set from the server's player DB."""
        try:
            self._known_players = await server_config.read_server_players(self.bot, force=True)
            print(f"[PlayerTracker] Known players -> {len(self._known_players)}")
        except Exception as e:
            print(f"[PlayerTracker] Could not refresh known players: {e}")

    # ---- delayed-action helpers ----------------------------------------------

    async def _delayed_in_game_welcome(self, name: str, is_new: bool, delay: float = 10.0):
        await asyncio.sleep(delay)
        now = time.time()
        last = self._welcome_sent.get(name, 0)
        if now - last < 30:
            print(f"[PlayerTracker] Skipping duplicate welcome for {name}")
            return
        self._welcome_sent[name] = now

        # Customize these strings for PZ Tambayan.
        if is_new:
            msg = f"Welcome to PZ Tambayan, {name}! Enjoy your stay and be safe out there!"
        else:
            msg = f"Welcome back, {name}!"
        await lua_bridge.write_command("display", message=msg)
        label = "First-time" if is_new else "Returning"
        print(f"[PlayerTracker] In-game welcome ({label}) -> {name}")

    async def _handle_death(self, name: str):
        """Post a death notification to Discord and broadcast in-game."""
        await self.bot.send_notification(
            f"\u2620\ufe0f **{name}** has died!",
            discord.Colour.red(),
        )
        # Customize this string for PZ Tambayan.
        await lua_bridge.write_command("display", message=f"RIP {name}. The dead tell no tales.")
        print(f"[PlayerTracker] Death -> {name}")

    # ---- main tail loop ------------------------------------------------------

    @tasks.loop(seconds=2.0)
    async def _tail_user_log(self):
        try:
            # Refresh the known-players set periodically (and on first run).
            if time.time() - self._last_refresh > _KNOWN_PLAYERS_REFRESH:
                self._last_refresh = time.time()
                await self._refresh_known_players()

            sftp = sftp_client.get()
            log_file = await self._find_latest_user_log()
            if not log_file:
                return

            # Log rotation: new file detected — seek to its end.
            if log_file != self._current_log:
                self._current_log = log_file
                st = await sftp.stat(log_file)
                self._file_pos = st[0] if st else 0
                print(f"[PlayerTracker] Now tailing: {log_file} (from {self._file_pos})")
                return

            # Read any new bytes.
            try:
                text, self._file_pos = await sftp.tail(log_file, self._file_pos)
            except sftp_client.SftpError:
                return
            if not text:
                return

            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue

                # "fully connected" -> Discord join notification + in-game welcome
                m = _CONNECTED_RE.match(line)
                if m:
                    name = m.group(1)
                    is_new = name not in self._known_players
                    if is_new:
                        self._known_players.add(name)
                    rank_cog = self.bot.get_cog("RankSync")
                    if rank_cog:
                        await rank_cog.sync_by_pz_username(name)
                    if is_new:
                        await self.bot.send_notification(
                            f"{self.bot.Emojis.SPIFFO_WAVE} New player **{name}** joined for the first time!",
                            discord.Colour.blue(),
                        )
                    else:
                        await self.bot.send_notification(
                            f"{self.bot.Emojis.HAPPY} **{name}** joined the server.",
                            discord.Colour.green(),
                        )
                    asyncio.ensure_future(self._delayed_in_game_welcome(name, is_new))
                    print(f"[PlayerTracker] Join -> {name} ({'new' if is_new else 'returning'})")
                    continue

                # "disconnected" -> Discord leave notification
                m = _DISCONNECTED_RE.match(line)
                if m:
                    name = m.group(1)
                    await self.bot.send_notification(
                        f"{self.bot.Emojis.SPIFFO_WAVE} **{name}** left the server.",
                        discord.Colour.dark_grey(),
                    )
                    print(f"[PlayerTracker] Leave -> {name}")
                    continue

                # Death (disabled until _DEATH_RE is confirmed)
                if _DEATH_RE:
                    m = _DEATH_RE.match(line)
                    if m:
                        name = m.group(1)
                        asyncio.ensure_future(self._handle_death(name))
                        continue

        except Exception as e:
            print(f"[PlayerTracker] Tail error: {e}")

    @_tail_user_log.before_loop
    async def _before_tail(self):
        await self.bot.wait_until_ready()
        await self._refresh_known_players()
        print(f"[PlayerTracker] Started — watching {self._log_dir} for *_user.txt over SFTP")


async def setup(bot):
    await bot.add_cog(PlayerTrackerCog(bot))
