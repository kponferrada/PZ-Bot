"""restart_watch.py — bot-driven workshop mod update checker and controlled restart.

Polls the Steam Workshop (see mod_checker.ModChecker) on an interval. When any
subscribed item is updated, it announces the update and stops the server cleanly
over RCON (no event deferral):

    no players online   ->  restart immediately (save, quit)
    players online      ->  countdown: save at T-2min, kick at T-1min, quit at T-0

`/forcemodupdate` forces the same sequence immediately.

Scheduled restarts (RESTART_SCHEDULE_UTC) run the exact same countdown + kick +
quit flow as a workshop update (notify T-2min, save T-1:30, kick T-1min).
"""

import os
import asyncio
import datetime
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

from mod_checker import ModChecker

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
        self._scheduled_trigger_key = None

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

    def _server_responsive(self) -> bool:
        """True if the server answers RCON right now.

        A host-driven restart (PhunServer 2) or an outage drops RCON, so this is
        the signal that the bot must NOT run its own restart flow — stacking a
        second restart on top of one already in progress fails the RCON save/quit
        and can interrupt the host's sequence. The mod check is likewise skipped
        while the server is down so it doesn't queue a restart that can't run.
        """
        return self.bot.rcon.is_server_online(timeout=5)

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
        await self._kick_all_players()

    async def _announce_kick_notification(self) -> None:
        """Announce (Discord + in-game) that players will be kicked."""
        if self.bot.features.is_enabled("restart"):
            await self._announce(
                "🔔 Kicking players in 1 minute.",
                discord.Colour.orange(),
            )
            await self._servermsg("Players will be kicked in 1 minute, please seek shelter!")

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

    async def _run_countdown(self, duration=None) -> None:
        """Countdown: kick-notification T-2min, save T-1:30, kick T-1min, save+quit T-0.

        `duration` (seconds) overrides the default restart delay (unused by the
        normal flows, which rely on the default countdown)."""
        remaining = self._restart_delay if duration is None else duration
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
                if self.bot.features.is_enabled("restart"):
                    await self._servermsg("World saved.")
            if not kicked and remaining <= self._kick_at:
                kicked = True
                self.bot.state.restart_shutdown_started = True
                await self._kick_players()
        await self._quit_server()

    async def _start_restart(self, reason: str, image: str = None, duration=None) -> bool:
        """Start the restart sequence. Immediate if no players online, otherwise a
        countdown (kick-notification T-2min, save T-1:30, kick T-1min).

        Returns False (and does nothing) when the server isn't answering RCON —
        i.e. it's already down or mid-restart via another mechanism — so the bot
        never stacks a second restart on top of one in progress.

        `image` overrides the announcement banner (defaults to the mod-update
        banner; scheduled restarts pass the generic restart banner).
        `duration` (seconds) overrides the countdown length."""
        if not self._server_responsive():
            print(f"[RestartWatch] Restart skipped — server not responding to RCON ({reason}).")
            return False
        self.bot.state.expect_restart()
        # The pending update is only "applied" once the server actually restarts.
        # Mark the baseline stale so the next poll re-seeds against the applied
        # state instead of re-announcing the same update.
        self._seeded = False
        if self.bot.features.is_enabled("restart"):
            await self._announce_banner(
                image or self.bot.config.ANNOUNCE_MOD_UPDATE_IMAGE,
                f"🔧 {reason}.",
            )
            await self._servermsg(f"{reason} — server will restart.")
        if await self._get_player_count() <= 0:
            await self._restart_server()
            return True
        asyncio.create_task(self._run_countdown(duration))
        return True

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

        # Only poll when the server is answering RCON. If it's down (a host /
        # PhunServer-driven restart or an outage), skip — there's no point queueing
        # a restart the server can't honour, and it would stack on top of the one
        # already running.
        if not self._server_responsive():
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
        """At the scheduled time, run the full restart sequence (countdown + kick +
        quit) — the exact same flow a workshop update uses. An advance warning is
        posted first if the warning window is longer than the restart countdown."""
        if not self.bot.features.is_enabled("restart"):
            return
        # Don't stack a scheduled restart on top of one already in progress.
        if self.bot.state.restart_expected():
            return
        # Skip when the server is already down/restarting via another mechanism —
        # there's nothing to schedule a restart against, and it'd collide.
        if not self._server_responsive():
            return
        nxt = self._next_scheduled_restart()
        delta = (nxt - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        key = int(nxt.timestamp())

        # Within the countdown window -> run the same restart sequence as a
        # workshop update (announce, countdown, save, kick, quit).
        if delta <= self._restart_delay:
            if self._scheduled_trigger_key != key:
                self._scheduled_trigger_key = key
                # Same path as a mod-update restart: full countdown (notify
                # T-2min, save T-1:30, kick T-1min, quit T-0), no duration
                # override — so the kick runs exactly like a mod update.
                await self._start_restart(
                    "Scheduled restart",
                    image=self.bot.config.ANNOUNCE_RESTART_IMAGE,
                )
            return

        # Advance warning (only reached when the warning window exceeds the
        # restart countdown, e.g. warn 10 min out, restart 5 min out).
        if delta > self._scheduled_warn:
            return
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

    @app_commands.command(name="forcemodupdate", description="Force a mod update restart now.")
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
        if not await self._start_restart("Mod update forced by admin"):
            await interaction.followup.send(
                "⚠️ Server is not responding to RCON (already down or restarting) — restart skipped.",
                ephemeral=True,
            )

    @app_commands.command(name="restart", description="Force a server restart now (in-game announcement + countdown).")
    async def cmd_restart(self, interaction: discord.Interaction) -> None:
        role = discord.utils.get(interaction.guild.roles, name=self.bot.config.DEFAULT_ROLE)
        if role is None or role not in interaction.user.roles:
            await interaction.response.send_message(embed=discord.Embed(
                title="Permission Denied",
                description=f"You need the **{self.bot.config.DEFAULT_ROLE}** role.",
                colour=discord.Colour.red(),
            ), ephemeral=True)
            return
        await interaction.response.send_message("⏳ Forcing server restart...", ephemeral=True)
        if not await self._start_restart(
            "Restart forced by admin",
            image=self.bot.config.ANNOUNCE_RESTART_IMAGE,
        ):
            await interaction.followup.send(
                "⚠️ Server is not responding to RCON (already down or restarting) — restart skipped.",
                ephemeral=True,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(RestartWatch(bot))
    print("[RestartWatch] Extension loaded.")
