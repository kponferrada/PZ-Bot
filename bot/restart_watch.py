"""restart_watch.py — bot-driven workshop mod update checker and controlled restart.

Polls the Steam Workshop (see mod_checker.ModChecker) on an interval. When any
subscribed item is updated, it announces the update, checks the horde-night guard
(if `restart_defer` is on), then stops the server cleanly over RCON:

    no players online   ->  restart immediately (save, quit)
    players online      ->  countdown: save at T-2min, kick at T-1min, quit at T-0

`/forcemodupdate` forces the same sequence and bypasses the horde deferral.
"""

import os
import time
import asyncio
import datetime
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

import lua_bridge
from mod_checker import ModChecker

# ---- Horde guard constants (ported from Jeeves) -----------------------------

# A horde runs ~2 in-game hours; a file older than this stuck on "active" is not
# a live horde, it's one nobody is updating. Without this cap a stale file would
# block every future restart silently.
_HORDE_ACTIVE_MAX_AGE = 45 * 60          # seconds
# Phases that mean the horde is over but its tail (lure window) is not. They
# block for a bounded grace period measured from the status file's timestamp.
_HORDE_TAIL_PHASES = ("ended", "dropped", "expired")
_HORDE_TAIL_GRACE = 20 * 60              # seconds

DEFAULT_MOD_CHECK_INTERVAL = 300         # seconds between workshop polls (5 min)
DEFAULT_MOD_RESTART_DELAY = 300          # seconds of countdown before the restart (5 min)
DEFAULT_KICK_NOTIFY_AT = 120             # seconds-remaining at which to announce the kick (2 min)
DEFAULT_SAVE_AT = 90                     # seconds-remaining at which to RCON `save` (1:30)
DEFAULT_KICK_AT = 60                     # seconds-remaining at which to kick players (1 min)
DEFAULT_SCHEDULE_HOURS = [4, 10, 16, 22] # UTC restart hours (fallback if RESTART_SCHEDULE_UTC unset)
DEFAULT_SCHEDULED_WARN = 300             # advance warning (seconds) before a scheduled restart (5 min)


class RestartWatch(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Restart/mod-update announcements use the same channel + @role as server up/down.
        self._channel_id = int(getattr(bot.config, "SERVER_NOTIFICATION_CHANNEL_ID", 0) or 0)
        self._role_id = int(getattr(bot.config, "NOTIFY_ROLE_ID", 0) or 0)
        self._mod_check_interval = int(os.getenv("MOD_CHECK_INTERVAL_SECONDS", str(DEFAULT_MOD_CHECK_INTERVAL)) or DEFAULT_MOD_CHECK_INTERVAL)
        self._restart_delay = int(os.getenv("MOD_CHECK_RESTART_DELAY_SECONDS", str(DEFAULT_MOD_RESTART_DELAY)) or DEFAULT_MOD_RESTART_DELAY)
        self._kick_notify_at = int(os.getenv("RESTART_KICK_NOTIFY_AT_SECONDS", str(DEFAULT_KICK_NOTIFY_AT)) or DEFAULT_KICK_NOTIFY_AT)
        self._save_at = int(os.getenv("RESTART_SAVE_AT_SECONDS", str(DEFAULT_SAVE_AT)) or DEFAULT_SAVE_AT)
        self._kick_at = int(os.getenv("RESTART_KICK_AT_SECONDS", str(DEFAULT_KICK_AT)) or DEFAULT_KICK_AT)
        self._schedule_hours = self._parse_schedule_hours()
        self._scheduled_warn = int(os.getenv("SCHEDULED_RESTART_WARN_SECONDS", str(DEFAULT_SCHEDULED_WARN)) or DEFAULT_SCHEDULED_WARN)
        self._scheduled_announced_key = None

        self._checker = ModChecker(bot)
        self._seeded = False
        self._mod_check.start()
        self._scheduled_check.start()
        print(f"[RestartWatch] Mod check every {self._mod_check_interval}s; "
              f"countdown {self._restart_delay}s (notify T-{self._kick_notify_at}s, save T-{self._save_at}s, kick T-{self._kick_at}s); "
              f"scheduled restarts @ {self._schedule_hours} UTC (warn {self._scheduled_warn}s); "
              f"announce ch={self._channel_id or 'notification'}, role={self._role_id or 'none'}")

    def cog_unload(self):
        self._mod_check.cancel()
        self._scheduled_check.cancel()

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

    async def _get_player_count(self) -> int:
        """Current player count from RCON, falling back to the tracked state."""
        resp = await self.bot.rcon.send_command("players")
        if resp:
            _, count = self.bot.rcon.parse_players(resp)
            return count
        return self.bot.state.player_count

    # ---- Horde guard ---------------------------------------------------------

    async def _is_event_blocking_restart(self):
        """Return (blocked, reason) if a horde night should block a restart."""
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
        return False, ""

    # ---- Restart (RCON) ------------------------------------------------------

    async def _save_world(self) -> None:
        await self.bot.rcon.send_command("save")

    async def _kick_players(self) -> None:
        await self._kick_all_players()

    async def _announce_kick_notification(self) -> None:
        """Announce that players will be kicked (no server-restarting image)."""
        if self.bot.features.is_enabled("restart"):
            await self._announce(
                "🔔 Kicking players in 1 minute.",
                discord.Colour.orange(),
            )

    async def _quit_server(self) -> None:
        """Save, announce the restart, then quit over RCON."""
        await self._save_world()
        if self.bot.features.is_enabled("restart"):
            await self._announce_banner(
                self.bot.config.ANNOUNCE_RESTART_IMAGE,
                "🔄 Server is restarting...",
            )
        await self.bot.rcon.send_command("quit")

    async def _restart_server(self) -> None:
        """Immediate restart (no players online): save, announce, quit."""
        await self._quit_server()

    async def _run_countdown(self) -> None:
        """Countdown: kick-notification T-2min, save T-1:30, kick T-1min, save+quit T-0."""
        remaining = self._restart_delay
        notified = False
        saved = False
        kicked = False
        while remaining > 0:
            await asyncio.sleep(1)
            remaining -= 1
            if not notified and remaining <= self._kick_notify_at:
                notified = True
                await self._announce_kick_notification()
            if not saved and remaining <= self._save_at:
                saved = True
                await self._save_world()
            if not kicked and remaining <= self._kick_at:
                kicked = True
                self.bot.state.restart_shutdown_started = True
                await self._kick_players()
        await self._quit_server()

    async def _start_restart(self, reason: str) -> None:
        """Start the restart sequence. Immediate if no players online, otherwise a
        countdown (kick-notification T-2min, save T-1:30, kick T-1min)."""
        self.bot.state.expect_restart()
        if self.bot.features.is_enabled("restart"):
            await self._announce_banner(
                self.bot.config.ANNOUNCE_MOD_UPDATE_IMAGE,
                f"🔧 {reason}.",
            )
        if await self._get_player_count() <= 0:
            await self._restart_server()
            return
        asyncio.create_task(self._run_countdown())

    # ---- Mod update checker --------------------------------------------------

    async def _run_mod_check(self) -> None:
        if not self.bot.features.is_enabled("mod_check"):
            return

        # Pause while a restart is in progress (forced or detected), so a
        # recurring check can't fire a second restart mid-countdown. The flag is
        # cleared by monitor_server_state on the next server-up transition.
        if self.bot.state.restart_expected():
            print("[RestartWatch] Mod check paused (restart in progress).")
            return

        # Seed the baseline once on startup so an update applied while the bot
        # was down isn't re-announced as if it just happened.
        if not self._seeded:
            await self._checker.seed_state()
            self._seeded = True

        updated = await self._checker.check_for_updates()
        if not updated:
            return

        names = ", ".join(updated)
        print(f"[RestartWatch] {len(updated)} outdated workshop item(s): {names}")

        if self.bot.features.is_enabled("restart_defer"):
            blocked, reason = await self._is_event_blocking_restart()
            if blocked:
                print(f"[RestartWatch] Mod update deferred: {reason}")
                await self._announce(
                    f"🔧 Mod update detected but deferred — {reason}. Will retry on the next check.",
                    discord.Colour.orange(),
                )
                return

        await self._start_restart(f"Mod update detected ({names})")

    @tasks.loop(seconds=300.0)
    async def _mod_check(self):
        try:
            await self._run_mod_check()
        except Exception as e:
            print(f"[RestartWatch] mod check error: {e}")

    @_mod_check.before_loop
    async def _before_mod_check(self):
        await self.bot.wait_until_ready()
        self._mod_check.change_interval(seconds=self._mod_check_interval)

    # ---- Scheduled restart announcement --------------------------------------

    def _parse_schedule_hours(self) -> list:
        """Parse RESTART_SCHEDULE_UTC into sorted unique hours (UTC)."""
        raw = os.environ.get("RESTART_SCHEDULE_UTC", "")
        if raw:
            try:
                hours = [int(x.strip()) for x in raw.split(",") if x.strip()]
                if hours:
                    return sorted(set(hours))
            except ValueError:
                pass
        return list(DEFAULT_SCHEDULE_HOURS)

    def _next_scheduled_restart(self) -> datetime.datetime:
        """Return the next scheduled restart time (UTC, top of the hour)."""
        now = datetime.datetime.now(datetime.timezone.utc)
        candidates = []
        for h in self._schedule_hours:
            t = now.replace(hour=h, minute=0, second=0, microsecond=0)
            if t <= now:
                t += datetime.timedelta(days=1)
            candidates.append(t)
        return min(candidates)

    async def _run_scheduled_check(self) -> None:
        """Announce an upcoming scheduled restart once, inside the warning window."""
        if not self.bot.features.is_enabled("restart"):
            return
        nxt = self._next_scheduled_restart()
        delta = (nxt - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        if delta > self._scheduled_warn:
            return
        key = int(nxt.timestamp())
        if self._scheduled_announced_key == key:
            return
        self._scheduled_announced_key = key
        minutes = max(1, int(delta // 60))
        await self._announce(
            f"🔧 Scheduled restart in ~{minutes} minute{'s' if minutes != 1 else ''}.",
            discord.Colour.orange(),
        )
        print(f"[RestartWatch] Scheduled restart announced (~{minutes}m)")

    @tasks.loop(seconds=60.0)
    async def _scheduled_check(self):
        try:
            await self._run_scheduled_check()
        except Exception as e:
            print(f"[RestartWatch] scheduled check error: {e}")

    @_scheduled_check.before_loop
    async def _before_scheduled_check(self):
        await self.bot.wait_until_ready()

    # ---- Slash command -------------------------------------------------------

    @app_commands.command(name="forcemodupdate", description="Force a mod update restart now, bypassing the horde deferral.")
    async def cmd_force_mod_update(self, interaction: discord.Interaction) -> None:
        role = discord.utils.get(interaction.guild.roles, name=self.bot.config.DEFAULT_ROLE)
        if role is None or role not in interaction.user.roles:
            await interaction.response.send_message(embed=discord.Embed(
                title="Permission Denied",
                description=f"You need the **{self.bot.config.DEFAULT_ROLE}** role.",
                colour=discord.Colour.red(),
            ), ephemeral=True)
            return
        await interaction.response.send_message("⏳ Forcing mod update restart...", ephemeral=True)
        await self._start_restart("Mod update forced by admin")


async def setup(bot: commands.Bot):
    await bot.add_cog(RestartWatch(bot))
    print("[RestartWatch] Extension loaded.")
