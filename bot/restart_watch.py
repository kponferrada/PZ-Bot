"""restart_watch.py — detects PhunServer 2 mod-update restarts and coordinates.

PhunServer 2 (Workshop 3792193021) is a Lua mod that runs *inside* the game
server. Its `modcheck` cron job polls the Steam Workshop every few minutes and,
on finding an outdated item, runs a shutdown countdown (default 5 minutes)
broadcasting "Server will restart in N minutes/seconds" to chat, then does a
clean `save` + `quit`. Bringing the server back up is the host's job.

This cog watches the server logs over SFTP and reacts:

    mod-update / countdown detected  ->  announce in Discord
    "Server will restart in 10s"    ->  RCON `save` (world saved before shutdown)

The bot cannot *trigger* a PhunServer restart via RCON (that shutdown is an
in-process Lua call, not an RCON command); it only coordinates the save +
announcement. See the module docstring notes in README for the full picture.

Config (config.env):
    WORKSHOP_UPDATE_CHANNEL_ID=  (Discord channel for workshop-update / restart announcements)
    WORKSHOP_UPDATE_ROLE_ID=     (optional role to @mention)
    RESTART_SAVE_AT_SECONDS=10    (countdown mark at which to RCON `save`)
"""

import os
import re
import discord
from discord.ext import commands, tasks

import sftp_client

# PhunServer 2 "Outdated workshop items detected, restarting in N minute(s)" (server log).
MOD_UPDATE_RE = re.compile(r"Outdated workshop items? detected", re.IGNORECASE)

# PhunServer 2 countdown "Server will restart in N minutes/seconds" (chat).
COUNTDOWN_RE = re.compile(
    r"Server will restart in\s+(\d+|one)\s+(minute|minutes|second|seconds)", re.IGNORECASE
)


class RestartWatch(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._channel_id = int(getattr(bot.config, "WORKSHOP_UPDATE_CHANNEL_ID", 0) or 0)
        self._role_id = int(getattr(bot.config, "WORKSHOP_UPDATE_ROLE_ID", 0) or 0)
        self._save_at = int(os.getenv("RESTART_SAVE_AT_SECONDS", "10") or "10")
        self._log_dir = getattr(bot.config, "SFTP_LOGS_DIR", None) or os.getenv("SFTP_LOGS_DIR")

        self._chat_file = None
        self._chat_pos = 0
        self._dbg_file = None
        self._dbg_pos = 0

        self._announced = False
        self._saved = False

        self._active = bool(self._log_dir)
        if not self._active:
            print("[RestartWatch] Disabled (SFTP_LOGS_DIR not set).")
        else:
            self._tail.start()
            print(f"[RestartWatch] Watching logs; announce ch={self._channel_id or 'notification'}, "
                  f"role={self._role_id or 'none'}, save at T-{self._save_at}s")

    def cog_unload(self):
        if self._active:
            self._tail.cancel()

    def _channel(self):
        if self._channel_id:
            return self.bot.get_channel(self._channel_id)
        return self.bot.get_notification_channel()

    async def _announce(self, description: str, colour: discord.Colour) -> None:
        channel = self._channel()
        if not channel:
            print("[RestartWatch] no announcement channel available")
            return
        mention = f"<@&{self._role_id}> " if self._role_id else ""
        embed = discord.Embed(title="Server Restart", description=description, colour=colour)
        try:
            await channel.send(content=mention, embed=embed)
        except discord.HTTPException as e:
            print(f"[RestartWatch] announce error: {e}")

    async def _handle_line(self, line: str) -> None:
        # 1) Mod-update signal (server log) -> announce specifically as a mod update.
        if MOD_UPDATE_RE.search(line):
            if not self._announced:
                self._announced = True
                self._saved = False
                self.bot.state.expect_restart()
                await self._announce(
                    "\U0001f527 **Mod update detected** \u2014 the server will restart to apply it.",
                    discord.Colour.orange(),
                )
            return

        # 2) Countdown (chat) -> announce once, then save at the T-10s mark.
        m = COUNTDOWN_RE.search(line)
        if not m:
            return

        num = m.group(1)
        unit = m.group(2).lower()
        if num.lower() == "one":
            seconds = 60
        else:
            n = int(num)
            seconds = n * 60 if unit.startswith("minute") else n

        if not self._announced:
            self._announced = True
            self._saved = False
            self.bot.state.expect_restart()
            await self._announce(
                f"\U0001f504 **Server restarting** in {num} {unit}.",
                discord.Colour.orange(),
            )

        if seconds <= self._save_at and not self._saved:
            self._saved = True
            await self.bot.rcon.send_command("save")
            await self._announce(
                f"\U0001f4be World saved (T-{seconds}s before restart).",
                discord.Colour.green(),
            )

    @tasks.loop(seconds=2.0)
    async def _tail(self):
        if not self._active:
            return
        try:
            sftp = sftp_client.get()

            # Chat log (countdown messages)
            chat = await sftp.newest_matching(self._log_dir, "*chat*.txt")
            if chat:
                if chat != self._chat_file:
                    self._chat_file = chat
                    st = await sftp.stat(chat)
                    self._chat_pos = st[0] if st else 0
                    self._announced = False  # new server session
                    self._saved = False
                else:
                    try:
                        text, self._chat_pos = await sftp.tail(chat, self._chat_pos)
                        for line in text.splitlines():
                            await self._handle_line(line)
                    except sftp_client.SftpError:
                        pass

            # Debug log ("Outdated workshop items detected")
            dbg = await sftp.newest_matching(self._log_dir, "*DebugLog*.txt")
            if dbg:
                if dbg != self._dbg_file:
                    self._dbg_file = dbg
                    st = await sftp.stat(dbg)
                    self._dbg_pos = st[0] if st else 0
                else:
                    try:
                        text, self._dbg_pos = await sftp.tail(dbg, self._dbg_pos)
                        for line in text.splitlines():
                            await self._handle_line(line)
                    except sftp_client.SftpError:
                        pass

        except Exception as e:
            print(f"[RestartWatch] tail error: {e}")

    @_tail.before_loop
    async def _before_tail(self):
        await self.bot.wait_until_ready()
        sftp = sftp_client.get()
        chat = await sftp.newest_matching(self._log_dir, "*chat*.txt")
        if chat:
            st = await sftp.stat(chat)
            self._chat_pos = st[0] if st else 0
            self._chat_file = chat
        dbg = await sftp.newest_matching(self._log_dir, "*DebugLog*.txt")
        if dbg:
            st = await sftp.stat(dbg)
            self._dbg_pos = st[0] if st else 0
            self._dbg_file = dbg
        print("[RestartWatch] tail positions set")


async def setup(bot: commands.Bot):
    await bot.add_cog(RestartWatch(bot))
    print("[RestartWatch] Extension loaded.")
