"""restart_watch.py — bot-driven workshop mod update checker and controlled restart.

Polls the Steam Workshop (see mod_checker.ModChecker) on an interval. When any
subscribed item is updated, it announces the update and stops the server cleanly
over RCON (no event deferral):

    no players online   ->  restart immediately (save, quit)
    players online      ->  countdown: save at T-2min, kick at T-1min, quit at T-0

`/forcemodupdate` forces the same sequence immediately.

Scheduled restarts (RESTART_SCHEDULE_UTC) run the exact same countdown + kick +
quit flow as a workshop update (notify T-2min, save T-1:30, kick T-1min).

Stopping a restart:
  - `/deferrestart <minutes>` pauses any restart (mod update, scheduled, admin)
    and runs it when the time is up. A mod-update restart can only be deferred.
  - `/cancelrestart` stops a scheduled or admin restart (countdown or deferral).
    With nothing running it skips the next scheduled restart (`restore: True`
    brings it back).
  - A scheduled restart that comes within SCHEDULED_SKIP_AFTER_UPDATE_MINUTES
    of a mod-update restart is skipped; the schedule moves on to the next slot.
The last mod-update restart and an admin-skipped slot are kept in
restart_state.json so a bot restart doesn't forget them.
"""

import os
import asyncio
import datetime
import json
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

from mod_checker import ModChecker
from checks import admin_only

DEFAULT_MOD_CHECK_INTERVAL = 300         # seconds between workshop polls (5 min)
DEFAULT_MOD_RESTART_DELAY = 300          # seconds of countdown before the restart (5 min)
DEFAULT_KICK_NOTIFY_AT = 120             # seconds-remaining at which to announce the kick (2 min)
DEFAULT_SAVE_AT = 90                     # seconds-remaining at which to RCON `save` (1:30)
DEFAULT_KICK_AT = 60                     # seconds-remaining at which to kick players (1 min)
DEFAULT_SCHEDULE_HOURS = [4, 10, 16, 22] # UTC restart hours (fallback if RESTART_SCHEDULE_UTC unset)
KICK_ALL_DELAY = 60                      # /kickall: seconds between the notice and the kick
DEFAULT_SCHEDULED_WARN = 300             # advance warning (seconds) before a scheduled restart (5 min)
DEFAULT_SKIP_AFTER_UPDATE = 60           # minutes: a scheduled restart this soon after a mod-update restart is skipped
MAX_DEFER_MINUTES = 720
_STATE_FILE = Path(__file__).parent / "restart_state.json"

# What started a restart. Only an automatic mod-update restart can't be cancelled.
KIND_LABELS = {
    "mod_update": "Mod-update restart",
    "forced_update": "Forced mod-update restart",
    "scheduled": "Scheduled restart",
    "manual": "Admin restart",
}
UPDATE_KINDS = ("mod_update", "forced_update")


def next_slot(now: datetime.datetime, hours) -> datetime.datetime:
    """The first schedule slot (UTC, top of the hour) strictly after `now`."""
    candidates = []
    for h in hours:
        t = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if t <= now:
            t += datetime.timedelta(days=1)
        candidates.append(t)
    return min(candidates)


def skip_for_recent_update(slot_ts: float, last_update_ts: float, window_s: float) -> bool:
    """Skip the scheduled slot at `slot_ts`: a mod-update restart happened at most
    `window_s` seconds before it (0 window = never skip)."""
    if window_s <= 0 or not last_update_ts:
        return False
    return 0 <= slot_ts - last_update_ts <= window_s


def cancel_refusal(kind: str | None, shutdown_started: bool) -> str:
    """Why a running restart can't be cancelled, or "" when it can."""
    if shutdown_started:
        return "players are already being kicked; the restart can't be stopped now"
    if kind == "mod_update":
        return ("a mod-update restart can't be cancelled, only deferred: "
                "use Defer restart with a duration and it resumes by itself")
    return ""


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
        self._scheduled_trigger_key = None
        self._skip_after_update = 60 * int(os.getenv("SCHEDULED_SKIP_AFTER_UPDATE_MINUTES", str(DEFAULT_SKIP_AFTER_UPDATE)) or 0)
        self._update_skip_key = None  # slot already announced as skipped (recent mod update)

        self._checker = ModChecker(bot)
        self._seeded = False
        self._restart_task = None  # active countdown task (guards against duplicate restarts)
        self._restart_kind = None  # KIND_LABELS key of the running / last restart
        self._restart_at = 0.0     # epoch time the running countdown reaches T-0
        self._defer_until = 0.0    # epoch time until which auto-restarts are deferred (0 = none)
        self._defer_task = None    # auto-resume task for a deferred restart
        self._deferred_kind = None # kind that resumes when the deferral ends (None = only a pause)
        self._deferred_slot = None # schedule slot a deferred scheduled restart belongs to
        self._kick_all_task = None # pending /kickall (notice sent, kick after KICK_ALL_DELAY)
        # Persisted (restart_state.json): last mod-update restart, admin-skipped slot.
        self._last_update_restart = 0.0
        self._skip_slot = None
        self._load_state()
        self._mod_check.start()
        self._scheduled_check.start()
        print(f"[RestartWatch] Mod check every {self._mod_check_interval}s; "
              f"countdown {self._restart_delay}s (notify T-{self._kick_notify_at}s, save T-{self._save_at}s, kick T-{self._kick_at}s); "
              f"scheduled restarts @ {self._schedule_hours} UTC (warn {self._scheduled_warn}s, "
              f"skipped within {self._skip_after_update // 60} min after a mod update); "
              f"announce ch={self._channel_id or 'notification'}, role={self._role_id or 'none'}")

    def cog_unload(self):
        if self._restart_task is not None and not self._restart_task.done():
            self._restart_task.cancel()
        if self._defer_task is not None and not self._defer_task.done():
            self._defer_task.cancel()
        if self._kick_all_task is not None and not self._kick_all_task.done():
            self._kick_all_task.cancel()
        self._mod_check.cancel()
        self._scheduled_check.cancel()

    def _load_state(self) -> None:
        try:
            data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self._last_update_restart = float(data.get("last_update_restart") or 0)
        skip = data.get("skip_slot")
        # A skipped slot that has passed means nothing any more.
        self._skip_slot = int(skip) if skip and int(skip) > time.time() else None

    def _save_state(self) -> None:
        try:
            _STATE_FILE.write_text(json.dumps({"last_update_restart": self._last_update_restart,
                                               "skip_slot": self._skip_slot}), encoding="utf-8")
        except OSError as e:
            print(f"[RestartWatch] could not save {_STATE_FILE.name}: {e}")

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

    async def _kick_all_players(self, reason: str = "Server restarting", fresh: bool = False) -> int:
        """Force-kick every connected player via RCON `kickuser`.

        `fresh` asks RCON `players` first (so someone who joined a moment ago
        is kicked too) and falls back to the tracked list."""
        names = set()
        if fresh:
            resp = await self.bot.rcon.send_command("players")
            if resp:
                names, _ = self.bot.rcon.parse_players(resp)
        names = set(names) or set(self.bot.state.player_names)
        if not names and not fresh:
            resp = await self.bot.rcon.send_command("players")
            if resp:
                names, _ = self.bot.rcon.parse_players(resp)
        why = (reason or "Server restarting").replace('"', "'")
        kicked = 0
        for name in sorted(names):
            if not name:
                continue
            clean = name.replace('"', "")
            await self.bot.rcon.send_command(f'kickuser "{clean}" -r "{why}"')
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

    async def _server_responsive(self) -> bool:
        """True if the server answers RCON right now.

        A server outage, crash, or a restart already in progress drops RCON, so
        this is the signal that the bot must NOT run its own restart flow —
        stacking a second restart on top of one already in progress fails the
        RCON save/quit. The mod check is likewise skipped while the server is
        down so it doesn't queue a restart that can't run.
        """
        return await asyncio.to_thread(self.bot.rcon.is_server_online, 5)

    async def _server_seems_up(self) -> bool:
        """Like `_server_responsive`, but reuses the up/down monitor's probe
        when it's under 20 s old. For the periodic checks; starting a restart
        still probes fresh."""
        recent = self.bot.state.recent_rcon_probe(20.0)
        return recent if recent is not None else await self._server_responsive()

    # ---- Restart (RCON) ------------------------------------------------------

    async def _servermsg(self, message: str) -> None:
        """Send a red-alert server announcement + alert sound to all players."""
        clean = message.replace('"', "'")
        try:
            await self.bot.rcon.broadcast(clean)
        except Exception as e:
            print(f"[RestartWatch] servermsg error: {e}")

    async def _save_world(self) -> None:
        await self.bot.rcon.send_command("save")

    async def _kick_players(self) -> None:
        reason = "Server maintenance" if getattr(self.bot.state, "maintenance", False) else "Server restarting"
        await self._kick_all_players(reason)

    async def _announce_kick_notification(self) -> None:
        """Announce (Discord + in-game) that players will be kicked."""
        if self.bot.features.is_enabled("restarts"):
            await self._announce(
                "🔔 Kicking players in 1 minute.",
                discord.Colour.orange(),
            )
            await self._servermsg("Players will be kicked in 1 minute, please seek shelter!")

    async def _quit_server(self) -> None:
        """Save, announce the restart, then quit over RCON."""
        if self._restart_kind in UPDATE_KINDS:
            # A scheduled restart soon after this one is skipped (skip_for_recent_update).
            self._last_update_restart = time.time()
            self._save_state()
        await self._save_world()
        if self.bot.features.is_enabled("restarts"):
            await self._announce_banner(
                self.bot.config.ANNOUNCE_RESTART_IMAGE,
                "🔄 Server is restarting...",
            )
        await self.bot.rcon.send_command("quit")

    async def _restart_server(self) -> None:
        """Immediate restart (no players online): save, announce, quit."""
        await self._quit_server()

    async def _run_countdown(self, duration=None, finish=None) -> None:
        """Countdown: kick-notification T-2min, save T-1:30, kick T-1min, save+quit T-0.

        `duration` (seconds) overrides the default restart delay (unused by the
        normal flows, which rely on the default countdown). `finish` replaces
        the save+quit at T-0 (/maintenance saves and leaves the server up)."""
        total = self._restart_delay if duration is None else duration
        # Measure against a deadline: the RCON/bridge calls inside the loop
        # take seconds each, so counting 1-second ticks would run late.
        deadline = time.monotonic() + total
        self._restart_at = time.time() + total
        remaining = total
        notified = False
        saved = False
        kicked = False
        while remaining > 0:
            await asyncio.sleep(1)
            remaining = max(0.0, deadline - time.monotonic())
            if not notified and remaining <= self._kick_notify_at:
                notified = True
                await self._announce_kick_notification()
            if not saved and remaining <= self._save_at:
                saved = True
                await self._save_world()
                if self.bot.features.is_enabled("restarts"):
                    await self._servermsg("World saved.")
            if not kicked and remaining <= self._kick_at:
                kicked = True
                self.bot.state.restart_shutdown_started = True
                await self._kick_players()
        await (finish or self._quit_server)()

    async def _countdown_guarded(self, duration=None) -> None:
        """Run the countdown, but never let a failure leave the restart lock stuck.

        If the countdown raises (e.g. an RCON hiccup mid-sequence), release the
        restart state so a failed quit doesn't freeze every future restart until
        the 15-minute auto-expiry.
        """
        try:
            await self._run_countdown(duration)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[RestartWatch] countdown failed: {e}")
            self.bot.state.expecting_restart = False
            self.bot.state.restart_shutdown_started = False

    async def _start_restart(self, reason: str, image: str = None, duration=None,
                             kind: str = "mod_update") -> bool:
        """Start the restart sequence. Immediate if no players online, otherwise a
        countdown (kick-notification T-2min, save T-1:30, kick T-1min).

        Returns False (and does nothing) when a restart is already in progress or
        the server isn't answering RCON — i.e. it's already down or mid-restart
        via another mechanism — so the bot never stacks a second restart on top of
        one in progress. Callers use this to make the restart idempotent.

        `image` overrides the announcement banner (defaults to the mod-update
        banner; scheduled restarts pass the generic restart banner).
        `duration` (seconds) overrides the countdown length. `kind` (KIND_LABELS)
        decides whether it can be cancelled and whether it counts as a mod update."""
        if self.bot.state.restart_expected():
            print(f"[RestartWatch] Restart already in progress — ignoring trigger ({reason}).")
            return False
        # Claim the restart lock atomically (no await between the check and the
        # set) so two concurrent triggers can't both start a countdown.
        self.bot.state.expect_restart()
        self._restart_kind = kind
        if not await self._server_responsive():
            print(f"[RestartWatch] Restart skipped — server not responding to RCON ({reason}).")
            # Roll back the lock we just claimed.
            self.bot.state.expecting_restart = False
            self.bot.state.restart_shutdown_started = False
            return False
        # The pending update is only "applied" once the server actually restarts.
        # Mark the baseline stale so the next poll re-seeds against the applied
        # state instead of re-announcing the same update.
        self._seeded = False
        if self.bot.features.is_enabled("restarts"):
            await self._announce_banner(
                image or self.bot.config.ANNOUNCE_MOD_UPDATE_IMAGE,
                f"🔧 {reason}.",
            )
            await self._servermsg(f"{reason} — server will restart.")
        if await self._get_player_count() <= 0:
            await self._restart_server()
            return True
        self._restart_task = asyncio.create_task(self._countdown_guarded(duration))
        return True

    async def _start_immediate_restart(self) -> bool:
        """Immediate restart: announce + save now, kick players in 30s, then quit.

        Returns False (and does nothing) when a restart is already in progress or
        the server isn't answering RCON.
        """
        if self.bot.state.restart_expected():
            print("[RestartWatch] Restart already in progress — ignoring immediate restart.")
            return False
        self.bot.state.expect_restart()
        self._restart_kind = "manual"
        self._restart_at = time.time() + 30
        if not await self._server_responsive():
            print("[RestartWatch] Immediate restart skipped — server not responding to RCON.")
            self.bot.state.expecting_restart = False
            self.bot.state.restart_shutdown_started = False
            return False
        self._seeded = False
        # Announce immediately, then save the world right away.
        if self.bot.features.is_enabled("restarts"):
            await self._announce_banner(
                self.bot.config.ANNOUNCE_RESTART_IMAGE,
                "🔄 Restart forced — kicking players in 30 seconds.",
            )
            await self._servermsg("Restart forced — server restarting in 30 seconds!")
        await self._save_world()
        if self.bot.features.is_enabled("restarts"):
            await self._servermsg("World saved.")
        self._restart_task = asyncio.create_task(self._immediate_restart_guarded())
        return True

    async def _immediate_restart_sequence(self) -> None:
        """Kick players after 30s, then save + quit."""
        await asyncio.sleep(30)
        self.bot.state.restart_shutdown_started = True
        await self._kick_players()
        await self._quit_server()

    async def _immediate_restart_guarded(self) -> None:
        """Run the immediate restart, releasing the restart lock on failure."""
        try:
            await self._immediate_restart_sequence()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[RestartWatch] immediate restart failed: {e}")
            self.bot.state.expecting_restart = False
            self.bot.state.restart_shutdown_started = False

    # ---- Restart deferral / cancel -------------------------------------------

    def _is_deferred(self) -> bool:
        """True while restarts are manually deferred (suppress auto-triggers)."""
        return time.time() < self._defer_until

    def _cancel_deferral(self) -> None:
        """Cancel any pending deferral (e.g. an admin re-triggered manually)."""
        if self._defer_task is not None and not self._defer_task.done():
            self._defer_task.cancel()
        self._defer_task = None
        self._defer_until = 0.0
        self._deferred_kind = None
        self._deferred_slot = None

    def _countdown_running(self) -> bool:
        return self._restart_task is not None and not self._restart_task.done()

    def next_restart(self) -> tuple | None:
        """(epoch seconds, running) of the next restart, for the status card: a
        countdown in progress (running=True), a deferred restart, or the next
        scheduled slot that isn't skipped. None when nothing is coming."""
        if self._countdown_running() and not self._maintenance_active():
            return self._restart_at, True
        if self._is_deferred() and self._deferred_kind:
            return self._defer_until, False
        if not self.bot.features.is_enabled("scheduled_restarts"):
            return None
        slot = self._next_scheduled_restart()
        for _ in range(len(self._schedule_hours) * 2):
            if not self._slot_skipped(int(slot.timestamp())):
                return slot.timestamp(), False
            slot = self._next_scheduled_restart(slot)
        return None

    def _slot_skipped(self, key: int) -> str:
        """Why the scheduled slot `key` won't run ("" = it will)."""
        if key == self._skip_slot:
            return "cancelled by an admin"
        if skip_for_recent_update(key, self._last_update_restart, self._skip_after_update):
            return f"the server restarted for a mod update <t:{int(self._last_update_restart)}:R>"
        return ""

    def _scheduled_restart_imminent(self) -> bool:
        """True when a scheduled restart is due within the countdown window."""
        if not self.bot.features.is_enabled("scheduled_restarts"):
            return False
        nxt = self._next_scheduled_restart()
        if self._slot_skipped(int(nxt.timestamp())):
            return False
        delta = (nxt - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        return delta <= self._restart_delay

    async def _defer_restart(self, minutes: int) -> None:
        """Defer/cancel the current or upcoming restart for `minutes` minutes.

        Cancels any active countdown, suppresses scheduled + mod-update restarts
        during the window, and (when a restart was actually pending) re-triggers
        it automatically once the window ends — unless an admin re-triggers it
        manually first (which clears the deferral). The resumed restart keeps
        its kind, so a deferred mod update is still a mod update.
        """
        if self.bot.state.restart_expected() and self._restart_kind:
            kind = self._restart_kind
        elif self._is_deferred() and self._deferred_kind:
            kind = self._deferred_kind  # deferring again keeps what was waiting
        elif self._scheduled_restart_imminent():
            kind = "scheduled"
        else:
            kind = None
        slot = self._deferred_slot if self._is_deferred() else None
        if kind == "scheduled" and slot is None:
            slot = self._scheduled_trigger_key or int(self._next_scheduled_restart().timestamp())
        pending = kind is not None
        # Cancel any active countdown + release the restart lock.
        if self._restart_task is not None and not self._restart_task.done():
            self._restart_task.cancel()
        self._restart_task = None
        self.bot.state.expecting_restart = False
        self.bot.state.restart_shutdown_started = False
        # Allow a scheduled restart to re-fire after the deferral.
        self._scheduled_trigger_key = None
        self._scheduled_announced_key = None
        # Replace any prior deferral.
        self._cancel_deferral()
        self._defer_until = time.time() + minutes * 60
        self._deferred_kind = kind
        self._deferred_slot = slot
        if self.bot.features.is_enabled("restarts"):
            if pending:
                await self._announce(
                    f"⏸️ {KIND_LABELS[kind]} deferred for {minutes} minute{'s' if minutes != 1 else ''}. "
                    f"It will resume automatically <t:{int(self._defer_until)}:R>.",
                    discord.Colour.orange(),
                )
                await self._servermsg(f"Restart deferred for {minutes} minute{'s' if minutes != 1 else ''}.")
            else:
                await self._announce(
                    f"⏸️ Restarts paused for {minutes} minute{'s' if minutes != 1 else ''} "
                    "(scheduled/update restarts will be skipped).",
                    discord.Colour.orange(),
                )
        self._defer_task = asyncio.create_task(self._resume_deferred_restart(minutes, kind))

    async def _resume_deferred_restart(self, minutes: int, kind: str | None) -> None:
        """Auto-resume after the deferral window: re-trigger if a restart was pending."""
        try:
            await asyncio.sleep(minutes * 60)
        except asyncio.CancelledError:
            raise
        slot = self._deferred_slot
        self._defer_until = 0.0
        self._defer_task = None
        self._deferred_kind = None
        self._deferred_slot = None
        if kind:
            if self.bot.features.is_enabled("restarts"):
                await self._announce(
                    "▶️ Restart deferral ended — restarting now.",
                    discord.Colour.orange(),
                )
            started = await self._start_restart(
                f"Deferred {KIND_LABELS[kind].lower()}",
                image=self.bot.config.ANNOUNCE_RESTART_IMAGE,
                kind=kind,
            )
            if started and slot:
                self._scheduled_trigger_key = slot  # the slot it stood in for is used up

    async def _cancel_restart(self, by: str) -> tuple[bool, str]:
        """Stop a running or deferred restart, or (nothing running) skip the next
        scheduled one. -> (done, what to tell the admin)."""
        if self._maintenance_active():
            return False, "Maintenance is running. Use `/maintenance end: True` to end or cancel it."
        if self._countdown_running():
            kind = self._restart_kind
            refusal = cancel_refusal(kind, self.bot.state.restart_shutdown_started)
            if refusal:
                return False, f"⚠️ Not cancelled: {refusal}."
            self._restart_task.cancel()
            self._restart_task = None
            self.bot.state.expecting_restart = False
            self.bot.state.restart_shutdown_started = False
            self._seeded = False
            label = KIND_LABELS.get(kind, "Restart")
            if self.bot.features.is_enabled("restarts"):
                await self._announce(f"❌ {label} cancelled by {by}.", discord.Colour.green())
                await self._servermsg("The restart has been cancelled. Carry on!")
            print(f"[RestartWatch] {label} cancelled by {by}")
            return True, f"✅ {label} cancelled."
        if self._defer_task is not None and not self._defer_task.done():
            kind = self._deferred_kind
            if kind == "mod_update":
                return False, (f"⚠️ Not cancelled: {cancel_refusal(kind, False)}. "
                               f"It resumes <t:{int(self._defer_until)}:R>; defer again to push it back.")
            slot = self._deferred_slot
            self._cancel_deferral()
            if kind == "scheduled" and slot:
                self._skip_slot = slot  # don't let the scheduled check pick the slot up again
                self._save_state()
            label = KIND_LABELS[kind] if kind else "Restart pause"
            if self.bot.features.is_enabled("restarts"):
                await self._announce(f"❌ Deferred {label.lower()} cancelled by {by}." if kind
                                     else f"▶️ Restart pause lifted by {by}.", discord.Colour.green())
            print(f"[RestartWatch] deferral ({label}) cancelled by {by}")
            return True, (f"✅ Deferred {label.lower()} cancelled." if kind else "✅ Restart pause lifted.")
        if self.bot.state.restart_expected():
            return False, "⚠️ The server is already restarting; it's too late to cancel."
        if not self.bot.features.is_enabled("scheduled_restarts"):
            return False, "ℹ️ No restart is running, and scheduled restarts are off."
        slot = self._next_scheduled_restart()
        key = int(slot.timestamp())
        why = self._slot_skipped(key)
        if why:
            return False, f"ℹ️ No restart is running. The next scheduled one (<t:{key}:t>) is already skipped: {why}."
        self._skip_slot = key
        self._save_state()
        after = int(self._next_scheduled_restart(slot).timestamp())
        if self.bot.features.is_enabled("restarts"):
            await self._announce(f"⏭️ The scheduled restart at <t:{key}:t> is cancelled by {by}. "
                                 f"Next one: <t:{after}:t> (<t:{after}:R>).", discord.Colour.green())
        print(f"[RestartWatch] scheduled slot {key} skipped by {by}")
        return True, f"✅ Scheduled restart at <t:{key}:t> skipped. Next one: <t:{after}:t>."

    def _restore_schedule(self) -> str:
        if self._skip_slot is None:
            return "ℹ️ No scheduled restart is cancelled."
        key, self._skip_slot = self._skip_slot, None
        self._save_state()
        return f"✅ The scheduled restart at <t:{key}:t> is back on."

    def status_lines(self) -> list:
        """(name, value) rows describing the restart state, for /restartstatus and the admin panel."""
        rows = []
        if self._maintenance_active():
            rows.append(("Now", "🛠️ Maintenance is running"))
        elif self._countdown_running():
            label = KIND_LABELS.get(self._restart_kind, "Restart")
            stage = " (kicking players)" if self.bot.state.restart_shutdown_started else ""
            rows.append(("Now", f"🔄 {label} counting down, T-0 <t:{int(self._restart_at)}:R>{stage}"))
        elif self.bot.state.restart_expected():
            rows.append(("Now", "🔄 Server is restarting"))
        elif self._is_deferred():
            what = KIND_LABELS[self._deferred_kind] if self._deferred_kind else "Nothing pending (restarts paused)"
            rows.append(("Now", f"⏸️ Deferred until <t:{int(self._defer_until)}:t> (<t:{int(self._defer_until)}:R>): {what}"))
        else:
            rows.append(("Now", "✅ No restart running"))
        if self.bot.features.is_enabled("scheduled_restarts"):
            slot = self._next_scheduled_restart()
            lines = []
            for _ in range(2):
                key = int(slot.timestamp())
                why = self._slot_skipped(key)
                lines.append(f"<t:{key}:t> (<t:{key}:R>)" + (f" — skipped: {why}" if why else ""))
                slot = self._next_scheduled_restart(slot)
            rows.append(("Scheduled", "\n".join(lines)))
        else:
            rows.append(("Scheduled", "Off"))
        last = int(self._last_update_restart)
        window = self._skip_after_update // 60
        rows.append(("Last mod-update restart", (f"<t:{last}:R>" if last else "None recorded")
                     + (f"\nA scheduled restart within {window} min after one is skipped." if window else "")))
        return rows

    # ---- Mod update checker --------------------------------------------------

    async def _run_mod_check(self) -> None:
        if not self.bot.features.is_enabled("mod_updates"):
            return

        # Skip while restarts are manually deferred.
        if self._is_deferred():
            print("[RestartWatch] Mod check deferred.")
            return

        # Pause while a restart is in progress (forced or detected), so a
        # recurring check can't fire a second restart mid-countdown. The flag is
        # cleared by monitor_server_state on the next server-up transition.
        if self.bot.state.restart_expected():
            print("[RestartWatch] Mod check paused (restart in progress).")
            return

        # Only poll when the server is answering RCON. If it's down (an outage,
        # crash, or a restart already in progress), skip — there's no point
        # queueing a restart the server can't honour, and it would stack on top
        # of the one already running.
        if not await self._server_seems_up():
            print("[RestartWatch] Mod check skipped (server not responding to RCON).")
            return

        # Seed the baseline once on startup so an update applied while the bot
        # was down isn't re-announced as if it just happened. Also re-seeds after
        # a restart (marked by _start_restart resetting _seeded) so applied
        # updates become the new baseline instead of being re-detected.
        if not self._seeded:
            self._seeded = await self._checker.seed_state()

        updated = await self._checker.check_for_updates()
        if not updated:
            return

        names = ", ".join(updated)
        print(f"[RestartWatch] {len(updated)} outdated workshop item(s): {names}")

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

    def _next_scheduled_restart(self, after: datetime.datetime = None) -> datetime.datetime:
        """Return the next scheduled restart time (UTC, top of the hour), after
        `after` (default now)."""
        return next_slot(after or datetime.datetime.now(datetime.timezone.utc), self._schedule_hours)

    async def _run_scheduled_check(self) -> None:
        """At the scheduled time, run the full restart sequence (countdown + kick +
        quit) — the exact same flow a workshop update uses. An advance warning is
        posted first if the warning window is longer than the restart countdown."""
        if not self.bot.features.is_enabled("scheduled_restarts"):
            return
        # Skip while restarts are manually deferred.
        if self._is_deferred():
            return
        # Don't stack a scheduled restart on top of one already in progress.
        if self.bot.state.restart_expected():
            return
        # Skip when the server is already down/restarting via another mechanism —
        # there's nothing to schedule a restart against, and it'd collide.
        if not await self._server_seems_up():
            return
        nxt = self._next_scheduled_restart()
        delta = (nxt - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        key = int(nxt.timestamp())

        # Cancelled by an admin, or the server just restarted for a mod update:
        # this slot doesn't run and the schedule moves on to the next one.
        why = self._slot_skipped(key)
        if why:
            if key != self._skip_slot and self._update_skip_key != key and delta <= self._scheduled_warn:
                self._update_skip_key = key
                after = int(self._next_scheduled_restart(nxt).timestamp())
                if self.bot.features.is_enabled("restarts"):
                    await self._announce(
                        f"⏭️ Scheduled restart at <t:{key}:t> skipped: {why}. "
                        f"Next one: <t:{after}:t> (<t:{after}:R>).",
                        discord.Colour.green(),
                    )
                print(f"[RestartWatch] Scheduled slot {key} skipped ({why})")
            return

        # Within the countdown window -> run the same restart sequence as a
        # workshop update (announce, countdown, save, kick, quit).
        if delta <= self._restart_delay:
            if self._scheduled_trigger_key != key:
                # Only mark this scheduled slot consumed if the restart actually
                # started — otherwise retry on the next tick (e.g. RCON was
                # briefly unresponsive at the trigger moment).
                started = await self._start_restart(
                    "Scheduled restart",
                    image=self.bot.config.ANNOUNCE_RESTART_IMAGE,
                    kind="scheduled",
                )
                if started:
                    self._scheduled_trigger_key = key
            return

        # Advance warning (only reached when the warning window exceeds the
        # restart countdown, e.g. warn 10 min out, restart 5 min out).
        if delta > self._scheduled_warn:
            return
        if self._scheduled_announced_key == key:
            return
        self._scheduled_announced_key = key
        if self.bot.features.is_enabled("restarts"):
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

    @app_commands.command(name="forcemodupdate", description="Force a mod update restart now.")
    @admin_only()
    async def cmd_force_mod_update(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("⏳ Forcing mod update restart...", ephemeral=True)
        self._cancel_deferral()
        if not await self._start_restart("Mod update forced by admin", kind="forced_update"):
            await interaction.followup.send(
                "⚠️ Restart skipped — a restart is already in progress or the server is not responding to RCON.",
                ephemeral=True,
            )

    @app_commands.command(name="restart", description="Force a server restart now (in-game announcement + countdown).")
    @admin_only()
    async def cmd_restart(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("⏳ Forcing server restart...", ephemeral=True)
        self._cancel_deferral()
        if not await self._start_restart(
            "Restart forced by admin",
            image=self.bot.config.ANNOUNCE_RESTART_IMAGE,
            kind="manual",
        ):
            await interaction.followup.send(
                "⚠️ Restart skipped — a restart is already in progress or the server is not responding to RCON.",
                ephemeral=True,
            )

    @app_commands.command(name="restartnow", description="Immediate restart: announce + save now, kick players in 30s, then quit.")
    @admin_only()
    async def cmd_restart_now(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("⏳ Forcing immediate restart (30s)...", ephemeral=True)
        self._cancel_deferral()
        if not await self._start_immediate_restart():
            await interaction.followup.send(
                "⚠️ Restart skipped — a restart is already in progress or the server is not responding to RCON.",
                ephemeral=True,
            )

    # ---- Maintenance ---------------------------------------------------------

    def _maintenance_active(self) -> bool:
        return bool(getattr(self.bot.state, "maintenance", False)) and self.bot.state.restart_expected()

    def _clear_maintenance(self) -> None:
        self.bot.state.maintenance = False
        self.bot.state.expecting_restart = False
        self.bot.state.restart_shutdown_started = False

    async def _finish_maintenance(self) -> None:
        """T-0 of /maintenance: kick anyone left, save, and leave the server up
        for the admins to restart from the console."""
        self.bot.state.restart_shutdown_started = True
        await self._kick_all_players("Server maintenance", fresh=True)
        await self._save_world()
        if self.bot.features.is_enabled("restarts"):
            await self._announce_banner(
                self.bot.config.ANNOUNCE_RESTART_IMAGE,
                "🛠️ Players kicked, world saved. Maintenance in progress — "
                "the server will be restarted by the admins.",
            )
        print("[RestartWatch] Maintenance: players kicked, world saved; waiting for a console restart.")

    async def _maintenance_guarded(self) -> None:
        try:
            await self._run_countdown(finish=self._finish_maintenance)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[RestartWatch] maintenance countdown failed: {e}")
            self._clear_maintenance()

    async def _start_maintenance(self, reason: str, minutes: int) -> str:
        """Start the maintenance countdown. Returns "" or why it didn't start."""
        if self.bot.state.restart_expected():
            return "a restart or maintenance is already in progress"
        # Claim the lock before the first await (as _start_restart). It also
        # keeps scheduled / mod-update restarts and /kickall off, and makes the
        # up/down monitor treat the console restart as planned, for `minutes`.
        self.bot.state.expect_restart(minutes * 60)
        self.bot.state.maintenance = True
        if not await self._server_responsive():
            self._clear_maintenance()
            return "the server isn't answering RCON"
        players = await self._get_player_count()
        countdown = self._restart_delay if players > 0 else 0
        when = f"in {countdown // 60} minute{'s' if countdown // 60 != 1 else ''}" if countdown else "now"
        if self.bot.features.is_enabled("restarts"):
            await self._announce_banner(
                self.bot.config.ANNOUNCE_RESTART_IMAGE,
                f"🛠️ Server maintenance: {reason}. Players will be kicked {when}.",
            )
        await self._servermsg(f"Server maintenance: {reason}. Players will be kicked {when}.")
        if countdown:
            self._restart_task = asyncio.create_task(self._maintenance_guarded())
        else:
            await self._finish_maintenance()
        return ""

    @app_commands.command(name="maintenance",
                          description="Maintenance: warn players, kick everyone after the restart countdown, no restart.")
    @app_commands.describe(reason="Shown to players and in Discord (optional)",
                           minutes="How long the server may be down for it (default 60, max 720)",
                           end="End or cancel maintenance instead")
    @admin_only()
    async def cmd_maintenance(self, interaction: discord.Interaction, reason: str = "",
                              minutes: int = 60, end: bool = False) -> None:
        if end:
            if not self._maintenance_active():
                await interaction.response.send_message("ℹ️ No maintenance is running.", ephemeral=True)
                return
            counting_down = self._restart_task is not None and not self._restart_task.done()
            if counting_down:
                self._restart_task.cancel()
            self._clear_maintenance()
            await interaction.response.send_message("✅ Maintenance ended.", ephemeral=True)
            if counting_down:
                await self._servermsg("Maintenance cancelled. Carry on!")
            if self.bot.features.is_enabled("restarts"):
                await self._announce(
                    "✅ Maintenance cancelled." if counting_down else "✅ Maintenance finished. The server is open.",
                    discord.Colour.green())
            print(f"[RestartWatch] Maintenance ended by {interaction.user}")
            return
        minutes = max(5, min(int(minutes), 720))
        reason = " ".join((reason or "").split())[:100] or "scheduled maintenance"
        await interaction.response.defer(ephemeral=True)
        self._cancel_deferral()
        err = await self._start_maintenance(reason, minutes)
        if err:
            await interaction.followup.send(f"⚠️ Maintenance not started: {err}.", ephemeral=True)
            return
        await interaction.followup.send(
            "🛠️ Maintenance started. Players get the restart countdown warnings, then are kicked; "
            "the server stays up for you to restart from the console.\n"
            f"Restart alerts are held for {minutes} min. `/maintenance end: True` ends or cancels it.",
            ephemeral=True)
        print(f"[RestartWatch] Maintenance by {interaction.user}: {reason} ({minutes} min)")

    async def _kick_all_after_notice(self, reason: str, interaction: discord.Interaction) -> None:
        """Wait KICK_ALL_DELAY, then kick everyone online and report back."""
        try:
            await asyncio.sleep(KICK_ALL_DELAY)
            kicked = await self._kick_all_players(reason, fresh=True)
            print(f"[RestartWatch] /kickall: kicked {kicked} player(s) ({reason})")
            await interaction.followup.send(
                f"👢 Kicked {kicked} player{'s' if kicked != 1 else ''}.", ephemeral=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[RestartWatch] /kickall failed: {e}")
            try:
                await interaction.followup.send(f"⚠️ Kick failed: {e}", ephemeral=True)
            except discord.HTTPException:
                pass

    @app_commands.command(name="kickall", description="Warn everyone in game, then kick all online players 1 minute later.")
    @app_commands.describe(reason="Shown in the notice and the kick message (optional)",
                           cancel="Cancel a pending /kickall instead")
    @admin_only()
    async def cmd_kick_all(self, interaction: discord.Interaction,
                           reason: str = "", cancel: bool = False) -> None:
        pending = self._kick_all_task is not None and not self._kick_all_task.done()
        if cancel:
            if not pending:
                await interaction.response.send_message("ℹ️ No /kickall is pending.", ephemeral=True)
                return
            self._kick_all_task.cancel()
            await interaction.response.send_message("✅ Pending kick cancelled.", ephemeral=True)
            await self._servermsg("The kick has been cancelled.")
            return
        if pending:
            await interaction.response.send_message(
                "⚠️ A /kickall is already pending (use `cancel: True` to stop it).", ephemeral=True)
            return
        if self.bot.state.restart_expected():
            await interaction.response.send_message(
                "⚠️ A restart is in progress; it kicks everyone itself.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        if not await self._server_responsive():
            await interaction.followup.send("⚠️ The server isn't answering RCON.", ephemeral=True)
            return
        reason = " ".join((reason or "").split())[:100] or "Kicked by an admin"
        await self._servermsg(f"All players will be kicked in 1 minute: {reason}")
        count = await self._get_player_count()
        self._kick_all_task = asyncio.create_task(self._kick_all_after_notice(reason, interaction))
        await interaction.followup.send(
            f"📢 Notice sent. Kicking all online players ({count} now) in 1 minute.\n"
            "Run `/kickall cancel: True` to stop it.", ephemeral=True)
        print(f"[RestartWatch] /kickall by {interaction.user}: {reason} ({count} online)")

    @app_commands.command(name="deferrestart",
                          description="Defer a running or upcoming restart for N minutes; it resumes by itself.")
    @app_commands.describe(minutes=f"Minutes to defer (1-{MAX_DEFER_MINUTES})")
    @admin_only()
    async def cmd_defer_restart(self, interaction: discord.Interaction, minutes: int) -> None:
        if self._maintenance_active():
            # Deferring would cancel the maintenance countdown and later fire a
            # real restart (quit), which maintenance is meant to avoid.
            await interaction.response.send_message(
                "⚠️ Maintenance is running. Use `/maintenance end: True` to end or cancel it.",
                ephemeral=True)
            return
        minutes = max(1, min(int(minutes), MAX_DEFER_MINUTES))
        await interaction.response.send_message(
            f"⏸️ Deferring restart for {minutes} minute{'s' if minutes != 1 else ''}...",
            ephemeral=True,
        )
        await self._defer_restart(minutes)

    @app_commands.command(name="cancelrestart",
                          description="Cancel a scheduled/admin restart, or skip the next scheduled one.")
    @app_commands.describe(restore="Bring back a scheduled restart you cancelled instead")
    @admin_only()
    async def cmd_cancel_restart(self, interaction: discord.Interaction, restore: bool = False) -> None:
        if restore:
            await interaction.response.send_message(self._restore_schedule(), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        _done, message = await self._cancel_restart(interaction.user.display_name)
        await interaction.followup.send(message, ephemeral=True)

    @app_commands.command(name="restartstatus",
                          description="Show the running restart, deferral and the next scheduled restarts.")
    @admin_only()
    async def cmd_restart_status(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(title="🔄 Restart status", colour=discord.Colour.blurple())
        for name, value in self.status_lines():
            embed.add_field(name=name, value=value, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(RestartWatch(bot))
    print("[RestartWatch] Extension loaded.")
