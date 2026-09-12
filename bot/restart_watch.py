"""restart_watch.py — bot-driven workshop mod update checker and controlled restart.

Polls the Steam Workshop (see mod_checker.ModChecker) on an interval. When any
subscribed item is updated, it announces the update, checks the horde /
air-drop / supply guard (if `restart_defer` is on), then stops the server cleanly
over RCON (save → kick players → quit) so the host applies the update and brings
it back up.
"""

import os
import time
from pathlib import Path

import discord
from discord.ext import commands, tasks

import lua_bridge
from mod_checker import ModChecker

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

DEFAULT_MOD_CHECK_INTERVAL = 300         # seconds between workshop polls (5 min)


class RestartWatch(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Restart/mod-update announcements use the same channel + @role as server up/down.
        self._channel_id = int(getattr(bot.config, "SERVER_NOTIFICATION_CHANNEL_ID", 0) or 0)
        self._role_id = int(getattr(bot.config, "NOTIFY_ROLE_ID", 0) or 0)
        self._mod_check_interval = int(os.getenv("MOD_CHECK_INTERVAL_SECONDS", str(DEFAULT_MOD_CHECK_INTERVAL)) or DEFAULT_MOD_CHECK_INTERVAL)

        self._checker = ModChecker(bot)
        self._seeded_started_at = None
        self._mod_check.start()
        print(f"[RestartWatch] Mod check every {self._mod_check_interval}s; "
              f"announce ch={self._channel_id or 'notification'}, role={self._role_id or 'none'}")

    def cog_unload(self):
        self._mod_check.cancel()

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

    # ---- Restart (RCON) -------------------------------------------------------

    async def _restart_server(self) -> None:
        """Stop the server cleanly so the host applies the update and restarts:
        save the world, kick players, then quit."""
        await self.bot.rcon.send_command("save")
        if self.bot.features.is_enabled("restart"):
            await self.bot.rcon.send_command('servermsg "World saved. Server restarting to apply updates."')
        await self._kick_all_players()
        await self.bot.rcon.send_command("quit")

    # ---- Mod update checker ---------------------------------------------------

    async def _run_mod_check(self) -> None:
        if not self.bot.features.is_enabled("mod_check"):
            return

        # Re-seed the baseline whenever the server (re)starts, so an update that
        # was already applied while the bot was down isn't re-announced.
        started = self.bot.state.server_started_at
        if started and started != self._seeded_started_at:
            await self._checker.seed_state()
            self._seeded_started_at = started

        updated = await self._checker.check_for_updates()
        if not updated:
            return

        names = ", ".join(updated)
        print(f"[RestartWatch] {len(updated)} outdated workshop item(s): {names}")

        # Deferral during critical activities (horde / air-drop / supply).
        if self.bot.features.is_enabled("restart_defer"):
            blocked, reason = await self._is_event_blocking_restart()
            if blocked:
                print(f"[RestartWatch] Mod update deferred: {reason}")
                await self._announce(
                    f"🔧 Mod update detected but deferred — {reason}. Will retry on the next check.",
                    discord.Colour.orange(),
                )
                return

        if self.bot.features.is_enabled("restart"):
            await self._announce_banner(
                self.bot.config.ANNOUNCE_MOD_UPDATE_IMAGE,
                f"🔧 Mod update detected — server restarting to apply it ({names}).",
            )

        self.bot.state.expect_restart()
        await self._restart_server()

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


async def setup(bot: commands.Bot):
    await bot.add_cog(RestartWatch(bot))
    print("[RestartWatch] Extension loaded.")
