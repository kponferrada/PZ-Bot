"""restart_watch.py — detects PhunServer 2 restarts, coordinates save/kick, and
can defer the restart while horde / air-drop / supply events are in progress.

PhunServer 2 (Workshop 3792193021) drives restarts via cron jobs in
`/Lua/PhunServer2Cron.json` (a `shutdown` action for scheduled restarts and a
`modcheck` action for mod updates). Each triggers a countdown, then `save` +
`quit`; bringing the server back up is the host's job.

This cog watches the logs over SFTP and reacts:

    mod-update / countdown detected  ->  announce + (optionally) defer
    "Server will restart in 10s"    ->  RCON `save`
    "Server will restart in 5s"     ->  RCON `kickuser` for every player

Deferral: when a restart is signalled, `_is_event_blocking_restart()` checks the
horde / supply / air-drop status files (ported from Jeeves's horde guard). If an
event is in progress and the `restart_defer` feature is enabled, the bot cancels
PhunServer's in-flight shutdown via `setlua`, disables the cron jobs in
`PhunServer2Cron.json`, announces the deferral, then polls until the event
clears (or `DEFER_TIMEOUT`) and re-schedules the restart.

Config (config.env):
    SERVER_NOTIFICATION_CHANNEL_ID=  (channel for restart banners — same as server up/down)
    NOTIFY_ROLE_ID=                  (role to @mention — same as server up/down)
    RESTART_SAVE_AT_SECONDS=10       (countdown mark at which to RCON `save`)
    RESTART_KICK_AT_SECONDS=5        (countdown mark at which to kick remaining players)
    DEFER_TIMEOUT=10800              (max seconds to hold a restart for an event)
    DEFER_POLL=60                    (seconds between deferral re-checks)
"""

import os
import re
import json
import time
import asyncio
from pathlib import Path
import discord
from discord.ext import commands, tasks

import sftp_client
import lua_bridge

# PhunServer 2 "Outdated workshop items detected, restarting in N minute(s)" (server log).
MOD_UPDATE_RE = re.compile(r"Outdated workshop items? detected", re.IGNORECASE)

# PhunServer 2 countdown "Server will restart in N minutes/seconds" (chat).
COUNTDOWN_RE = re.compile(
    r"Server will restart in\s+(\d+|one)\s+(minute|minutes|second|seconds)", re.IGNORECASE
)

# ---- Deferral guard constants (ported from Jeeves) ---------------------------

# A horde runs ~2 in-game hours; a file older than this stuck on "active" is not
# a live horde, it's one nobody is updating. Without this cap a stale file would
# block every future restart silently.
_HORDE_ACTIVE_MAX_AGE = 45 * 60          # seconds
# Phases that mean the horde is over but its tail (lure window / boosted airdrop)
# is not. They block for a bounded grace period measured from the status file's
# own timestamp.
_HORDE_TAIL_PHASES = ("ended", "dropped", "expired")
_HORDE_TAIL_GRACE = 20 * 60              # seconds
# Supply-event phases where crates are on the ground or inbound.
_SUPPLY_ACTIVE_PHASES = ("active", "materialized")
# Air-drop crates live until their despawn window; a fresh "dropped" status means
# crates are on the ground and a restart would destroy them.
_DROP_ACTIVE_GRACE = 24 * 60 * 60        # seconds

_CRON_JSON_FILE = "PhunServer2Cron.json"

DEFAULT_DEFER_TIMEOUT = 3 * 60 * 60      # seconds (3 hours, matching Jeeves)
DEFAULT_DEFER_POLL = 60                  # seconds


class RestartWatch(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Restart/mod-update announcements use the same channel + @role as server up/down.
        self._channel_id = int(getattr(bot.config, "SERVER_NOTIFICATION_CHANNEL_ID", 0) or 0)
        self._role_id = int(getattr(bot.config, "NOTIFY_ROLE_ID", 0) or 0)
        self._save_at = int(os.getenv("RESTART_SAVE_AT_SECONDS", "10") or "10")
        self._kick_at = int(os.getenv("RESTART_KICK_AT_SECONDS", "5") or "5")
        self._log_dir = getattr(bot.config, "SFTP_LOGS_DIR", None) or os.getenv("SFTP_LOGS_DIR")
        self._lua_dir = getattr(bot.config, "SFTP_LUA_DIR", None)

        self._chat_file = None
        self._chat_pos = 0
        self._dbg_file = None
        self._dbg_pos = 0

        self._announced = False
        self._saved = False
        self._kicked = False
        self._deferring = False
        self._defer_timeout = int(os.getenv("DEFER_TIMEOUT", str(DEFAULT_DEFER_TIMEOUT)) or DEFAULT_DEFER_TIMEOUT)
        self._defer_poll = int(os.getenv("DEFER_POLL", str(DEFAULT_DEFER_POLL)) or DEFAULT_DEFER_POLL)

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

    async def _announce_banner(self, image_path: str, caption: str) -> None:
        """Send an image banner (local file) + @-mention to the server-notification channel."""
        channel = self._channel()
        if not channel:
            print("[RestartWatch] no announcement channel available")
            return
        mention = f"<@&{self._role_id}> " if self._role_id else ""
        path = Path(image_path)
        if not path.is_absolute():
            path = Path(__file__).parent / path
        content = (mention + caption).strip() or None
        if path.is_file():
            try:
                await channel.send(content=content, file=discord.File(str(path)))
            except discord.HTTPException as e:
                print(f"[RestartWatch] banner error: {e}")
        else:
            print(f"[RestartWatch] banner image not found: {image_path} - falling back to text")
            await self._announce(caption, discord.Colour.orange())

    async def _kick_all_players(self) -> int:
        """Force-kick every connected player via RCON `kickuser`."""
        names = set(self.bot.state.player_names)
        if not names:
            resp = await self.bot.rcon.send_command("players")
            if resp:
                names, _ = self.bot.rcon.parse_players(resp)
        kicked = 0
        for name in sorted(names):
            if not name:
                continue
            clean = name.replace('"', "")
            await self.bot.rcon.send_command(f'kickuser "{clean}" -r "Server restarting"')
            kicked += 1
            print(f"[RestartWatch] Kicked player: {clean}")
        return kicked

    # ---- Deferral guard -------------------------------------------------------

    async def _is_event_blocking_restart(self):
        """Return (blocked, reason) if a horde / air-drop / supply event should
        block a restart."""
        # Horde (ported from Jeeves).
        horde = await lua_bridge.read_horde_status()
        if horde:
            phase = horde.get("phase", "")
            written = horde.get("timestamp")
            age = int(time.time()) - int(written) if isinstance(written, (int, float)) else None

            if phase == "active" or horde.get("active") is True:
                if not (phase == "active" and age is not None and age > _HORDE_ACTIVE_MAX_AGE):
                    return True, "Horde night is currently active"
            if horde.get("lurePhase") is True:
                return True, "Horde lure window still running"
            if phase in _HORDE_TAIL_PHASES:
                if age is None or age <= _HORDE_TAIL_GRACE:
                    return True, f"Horde aftermath still settling (phase {phase})"
            if phase == "scheduled":
                event_day = horde.get("eventDay")
                next_day = horde.get("nextHordeDay")
                if event_day is not None and next_day is not None and event_day == next_day:
                    return True, f"Horde night tonight (day {next_day})"

        # Supply event in progress.
        supply = await lua_bridge.read_supply_event_status()
        if supply and supply.get("phase") in _SUPPLY_ACTIVE_PHASES:
            return True, "Supply drop event in progress"

        # Air-drop crates on the ground.
        drops = await lua_bridge.read_drops_status()
        if drops and drops.get("phase") == "dropped":
            written = drops.get("timestamp")
            age = int(time.time()) - int(written) if isinstance(written, (int, float)) else None
            if age is None or age <= _DROP_ACTIVE_GRACE:
                return True, "Air-drop crates still on the ground"

        return False, ""

    # ---- PhunServer control via RCON setlua -----------------------------------

    async def _phun_lua(self, statement: str) -> bool:
        resp = await self.bot.rcon.send_command(f"setlua {statement}")
        return resp is not None

    async def _cancel_shutdown(self, reason: str) -> bool:
        stmt = f'require("PhunServer2/core").cancelShutdown({json.dumps(reason)})'
        return await self._phun_lua(stmt)

    async def _schedule_shutdown(self, seconds: int, reason: str) -> bool:
        stmt = (f'require("PhunServer2/core").scheduleShutdown(getTimestamp() + {seconds}, '
                f'{{reason={json.dumps(reason)}, countdown="600;300;60;30;10;5"}})')
        return await self._phun_lua(stmt)

    # ---- Cron JSON helpers ----------------------------------------------------

    def _cron_path(self) -> str:
        return f"{self._lua_dir.rstrip('/')}/{_CRON_JSON_FILE}"

    async def _set_cron_jobs_enabled(self, enabled: bool) -> bool:
        """Toggle the shutdown/modcheck cron jobs so they don't re-trigger a
        restart mid-deferral. Returns True on success (or no-op)."""
        if not self._lua_dir:
            return False
        sftp = sftp_client.get()
        try:
            raw = await sftp.read_text(self._cron_path())
            cron = json.loads(raw)
        except Exception as e:
            print(f"[RestartWatch] cron read failed: {e}")
            return False
        data = cron.get("data", {})
        changed = False
        for job in data.values():
            if isinstance(job, dict) and job.get("action") in ("shutdown", "modcheck"):
                if job.get("enabled") != enabled:
                    job["enabled"] = enabled
                    changed = True
        if not changed:
            return True
        try:
            await sftp.write_text(self._cron_path(), json.dumps(cron, indent=2))
        except Exception as e:
            print(f"[RestartWatch] cron write failed: {e}")
            return False
        await self._phun_lua('require("PhunServer2Cron/core").reloadJobs()')
        return True

    # ---- Deferral flow --------------------------------------------------------

    async def _defer_restart(self, reason: str) -> None:
        """Cancel the in-flight shutdown, disable cron re-triggers, announce, and
        poll for the event to clear in the background."""
        await self._cancel_shutdown(reason)
        await self._set_cron_jobs_enabled(False)
        await self._announce(
            f"⏸️ Scheduled restart deferred — {reason}. Will restart after the event.",
            discord.Colour.orange(),
        )
        await self.bot.rcon.send_command(
            f'servermsg "Restart deferred — {reason}. Server will restart after the event."'
        )
        print(f"[RestartWatch] Restart deferred: {reason}")
        asyncio.create_task(self._wait_and_reschedule())

    async def _wait_and_reschedule(self) -> None:
        try:
            waited = 0
            cleared = True
            while True:
                blocked, reason = await self._is_event_blocking_restart()
                if not blocked:
                    break
                if waited >= self._defer_timeout:
                    cleared = False
                    break
                await asyncio.sleep(self._defer_poll)
                waited += self._defer_poll

            await self._set_cron_jobs_enabled(True)
            await self._schedule_shutdown(600, "deferred restart (post-event)")

            msg = ("Event concluded — server restarting in 10 minutes."
                   if cleared else
                   f"Defer window exceeded {self._defer_timeout // 3600}h — restarting anyway in 10 minutes.")
            await self._announce(f"🔁 {msg}", discord.Colour.yellow())
            await self.bot.rcon.send_command(f'servermsg "{msg}"')

            # Reset so the new countdown is announced/handled fresh.
            self._deferring = False
            self._announced = False
            self._saved = False
            self._kicked = False
        except Exception as e:
            print(f"[RestartWatch] defer wait error: {e}")
            self._deferring = False
            self._announced = False
            self._saved = False
            self._kicked = False

    # ---- Line handling --------------------------------------------------------

    async def _handle_line(self, line: str) -> None:
        is_mod = bool(MOD_UPDATE_RE.search(line))
        m = COUNTDOWN_RE.search(line)

        if not is_mod and not m:
            return

        # Mid-deferral: keep cancelling any re-triggered shutdown (e.g. modcheck
        # re-running) and skip the normal save/kick.
        if self._deferring:
            await self._cancel_shutdown("still deferred")
            return

        # First sight of a restart: run the defer guard before announcing.
        if not self._announced:
            self._announced = True
            self._saved = False
            self._kicked = False
            self.bot.state.expect_restart()

            if self.bot.features.is_enabled("restart_defer"):
                blocked, reason = await self._is_event_blocking_restart()
                if blocked:
                    self._deferring = True
                    await self._defer_restart(reason)
                    return

            if self.bot.features.is_enabled("restart"):
                if is_mod:
                    await self._announce_banner(
                        self.bot.config.ANNOUNCE_MOD_UPDATE_IMAGE,
                        "🔧 Mod update detected — the server will restart to apply it.",
                    )
                elif m:
                    num = m.group(1)
                    unit = m.group(2).lower()
                    await self._announce_banner(
                        self.bot.config.ANNOUNCE_RESTART_IMAGE,
                        f"🔄 Server restarting in {num} {unit}.",
                    )

        # Countdown save/kick. These run as background tasks so the tail loop is
        # never blocked by a slow RCON save/response. If the `save` were awaited
        # here, the final "5 seconds" line would only be read after the save
        # finished (i.e. after the server had already quit), so the T-5s
        # "restarting now" banner would never be sent.
        if m:
            num = m.group(1)
            unit = m.group(2).lower()
            if num.lower() == "one":
                seconds = 60
            else:
                n = int(num)
                seconds = n * 60 if unit.startswith("minute") else n

            if seconds <= self._save_at and not self._saved:
                self._saved = True
                asyncio.create_task(self._save_world(seconds))

            if seconds <= self._kick_at and not self._kicked:
                self._kicked = True
                self.bot.state.restart_shutdown_started = True  # real shutdown imminent
                asyncio.create_task(self._kick_players())

    async def _save_world(self, seconds: int) -> None:
        await self.bot.rcon.send_command("save")
        if self.bot.features.is_enabled("restart"):
            await self.bot.rcon.send_command(
                'servermsg "World saved. Server restarting — players will be kicked shortly."'
            )
            await self._announce(
                f"💾 World saved (T-{seconds}s before restart).",
                discord.Colour.green(),
            )

    async def _kick_players(self) -> None:
        # Post the "restarting now" banner immediately, then kick. Kicking every
        # player is several RCON round-trips and must not delay the Discord
        # signal the user asked to see at the 5-second mark.
        if self.bot.features.is_enabled("restart"):
            await self._announce_banner(
                self.bot.config.ANNOUNCE_RESTART_IMAGE,
                "🔄 Server restarting now.",
            )
        await self._kick_all_players()

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
                    self._kicked = False
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
