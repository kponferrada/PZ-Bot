"""
siege_night.py — Discord bot cog for "Siege Night" (Workshop 3669589584).

Provides /siegestatus, /siegestart, /siegestop, and /siegeschedule slash
commands that write commands via the unified lua_bridge module, which BLib
Admin's Siege Night bridge reads and answers (the older `siege-night-bridge`
mod reads the same files but never answers).

Also runs a background task that polls siege_night_status.txt written by the
companion mod, and sends Discord notifications at three key siege phases:
  1) "warning" — Siege night approaching (announced the day of, during daylight)
  2) "active"  — Siege night has begun (zombies spawning)
  3) "ended"   — Siege night has concluded

Wave tracking is EVENT-DRIVEN: the bridge mod records each wave via Siege
Night's SN.onWaveStart API and writes them to the status file's `waves`
section (sessionId + waveCount + wave1..waveN). We read that section, so a wave
cannot be missed by the 15s poll — dedup is by (sessionId, wave number).

Deduplication ensures each phase/wave is only announced once per siege, even
across bot restarts.

All commands that call lua_bridge defer the interaction first, since
write_command may sleep up to 3s waiting for the mod to consume the previous
command file. Discord interactions expire after 3s, so we must acknowledge
immediately and use followup.send for the actual reply.
"""

import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import lua_bridge
from game_calendar import siege_date_string
from checks import admin_only

# The bridge can write the first "active" status before Siege Night has set
# the siege's targetZombies (it read 0 on the first poll). Hold the "begun"
# notice this long for a real target, then post it without the number.
_START_WAIT_SECONDS = 60


def _int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def siege_target(siege: dict, waves: dict, wave_count: int) -> int:
    """The siege's zombie target: `targetZombies`, else the sum of the wave
    records' `expected` counts when every wave has one, else 0 (unknown)."""
    target = _int((siege or {}).get("targetZombies"))
    if target > 0:
        return target
    expected = [_int(((waves or {}).get(f"wave{i}") or {}).get("expected"))
                for i in range(1, _int(wave_count) + 1)]
    return sum(expected) if expected and all(expected) else 0


def start_description(target: int, players: int, direction: str, wave_count: int) -> str:
    """Body of the "Siege Night Has Begun!" embed (target 0 = not known yet)."""
    who = f"**{players}** survivor(s)"
    desc = (f"**{target}** zombies are descending on {who}." if target > 0
            else f"The horde is descending on {who}.")
    if direction:
        desc += f"\nHorde direction: **{direction}**."
    if wave_count:
        desc += f"\n**{wave_count}** surge waves incoming."
    return desc + "\n\nHold the line."


# Siege Night's `lastDirection` index -> compass name (matches SN.DIR_NAMES).
_DIR_NAMES = ("North", "Northeast", "East", "Southeast",
              "South", "Southwest", "West", "Northwest")


def _direction_name(direction):
    """Map Siege Night's lastDirection (-1..7) to a compass name, or None."""
    try:
        d = int(direction)
    except (TypeError, ValueError):
        return None
    if d < 0 or d >= len(_DIR_NAMES):
        return None
    return _DIR_NAMES[d]


# What each refusal code from BLib's bridge means for the person who asked.
_REFUSALS = {
    "already_active": "A siege is already running.",
    "not_active": "No siege is running.",
    "not_idle": "A siege is coming tonight or running; schedule once it is over.",
    "no_players": "Nobody is online; Siege Night builds a siege around players.",
    "disabled": "Siege Night is turned off in the server's sandbox options.",
    "unavailable": "Siege Night has no world data yet; try again in a minute.",
    "past_day": "That day has already passed.",
    "too_far": "That day is too far ahead (at most a year).",
    "rate_limited": "Too many siege commands; wait a minute.",
    "stale": "The command reached the server too late and was dropped.",
}

_ACTIONS = {
    "start": ("\U0001f6e1\ufe0f Siege Night Started!", "start the siege"),
    "stop": ("\U0001f6d1 Siege Stopped", "stop the siege"),
    "schedule": ("\U0001f4c5 Siege Day Changed", "change the siege day"),
}


def bridge_line(bridge: dict | None) -> str:
    """Which mod answers siege commands, for /siegestatus."""
    if not bridge:
        return "Bridge: old Siege Night Bridge mod (commands get no answer)"
    owner = bridge.get("owner") or "?"
    blib = bridge.get("blib") or "?"
    return f"Bridge: BLib {blib} ({owner}), protocol {bridge.get('protocol', '?')}"


def describe_siege_outcome(action: str, outcome: dict, user: str) -> tuple[str, str, bool | None]:
    """(title, description, ok) for a command's outcome; ok None = no answer."""
    title, verb = _ACTIONS.get(action, ("Siege Night", "send the command"))
    outcome = outcome or {}
    if not outcome.get("sent"):
        return (f"Could not {verb}",
                "Could not write the command file. Check SFTP and lua_bridge initialization.", False)
    result = outcome.get("result")
    if result is None:
        return ("\u23f3 Command Sent",
                f"Sent by **{user}**, but the server didn't answer. If BLib Admin's Siege Night "
                "bridge is on it may be busy or down; the old Siege Night Bridge mod never answers.",
                None)
    message = str(result.get("message") or "")
    if result.get("ok"):
        return (title, f"By **{user}**\n\n{message}".rstrip(), True)
    code = str(result.get("code") or "error")
    reason = _REFUSALS.get(code, message or code)
    detail = f"\n\n`{code}`: {message}" if message and reason != message else ""
    return (f"Could not {verb}", reason + detail, False)


class SiegeNightCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Dedup state (survive across bot restarts via seeding in before_loop).
        self._announced_warning_days: set[int] = set()   # eventDay
        self._active_announced: bool = False             # current siege's start announced?
        self._announced_ended_sieges: set[int] = set()   # totalSiegesCompleted
        self._announced_session: str | None = None       # sessionId of the announced siege
        self._announced_waves: set[int] = set()          # wave numbers already announced
        self._active_seen_at: float | None = None        # monotonic time this siege was first seen active
        self._last_poller_key = None

    @staticmethod
    def _split_status(status):
        """Return (schedule, siege, waves) sub-dicts from a nested siege status dict.

        The bridge mod writes `schedule = { … }` (always-refreshed state),
        `siege = { … }` (last/on-going siege aggregate), and `waves = { … }`
        (event-driven per-wave records: sessionId, waveCount, currentWave, and
        wave1..waveN each with { number, expected, startedAt, status }).
        """
        if not status:
            return {}, {}, {}
        return (status.get("schedule") or {},
                status.get("siege") or {},
                status.get("waves") or {})

    async def cog_load(self):
        self.siege_status_poller.start()

    async def cog_unload(self):
        self.siege_status_poller.cancel()

    # ── helpers ──────────────────────────────────────────────────────────

    # ── background poller ───────────────────────────────────────────────

    @tasks.loop(seconds=15)
    async def siege_status_poller(self):
        try:
            status = await lua_bridge.read_siege_status()
            if not status:
                return
            sched, siege, waves = self._split_status(status)

            phase = sched.get("phase") or "idle"
            event_day = sched.get("eventDay", 0)
            siege_count = sched.get("siegeCount", 0)
            completed = sched.get("totalSiegesCompleted", 0)

            poller_key = (phase, event_day, completed, siege_count)
            if poller_key != self._last_poller_key:
                print(f"[SiegeNight] Poller: phase={phase}, day={event_day}, "
                      f"siege={siege_count}, completed={completed}")
                self._last_poller_key = poller_key

            channel = self.bot.get_siege_channel()

            if phase == "warning":
                if event_day and event_day not in self._announced_warning_days:
                    self._announced_warning_days.add(event_day)
                    world = await lua_bridge.read_world_status()
                    date_str = siege_date_string(world, sched) or ""
                    desc = f"A siege night is scheduled for **Day {event_day}**"
                    if date_str:
                        desc += f" ({date_str})"
                    desc += ("\n\nUse the daylight hours to prepare \u2014 fortify your "
                             "base, stock up on supplies, and gather your group.")
                    embed = discord.Embed(
                        title="\U0001f319 Siege Night Is Tonight!",
                        description=desc,
                        colour=discord.Colour.dark_red(),
                    )
                    if self.bot.features.is_enabled("siege_night"):
                        await self.bot.send_to_channel(channel, self.bot.config.SIEGE_ROLE_ID, embed)
                    print(f"[SiegeNight] Siege tonight announced: day {event_day}")

            elif phase == "active":
                players = sched.get("playerCount", 0)
                direction = _direction_name(siege.get("lastDirection"))
                cur_phase = siege.get("currentPhase", "")
                spawned = siege.get("spawnedThisSiege", 0)

                # Event-driven wave records from the bridge (populated via
                # Siege Night's SN.onWaveStart API — cannot be missed by polling).
                session_id = waves.get("sessionId") or ""
                wave_count = waves.get("waveCount", 0) or siege.get("totalWaves", 0)

                # Detect a NEW siege (sessionId changed) and reset per-siege dedup.
                if session_id and session_id != self._announced_session:
                    self._announced_session = session_id
                    self._active_announced = False
                    self._active_seen_at = None
                    self._announced_waves.clear()
                    # Seed waves already started so we don't retro-announce them.
                    cur = waves.get("currentWave", 0) or siege.get("currentWaveIndex", 0)
                    for i in range(1, int(cur) + 1):
                        self._announced_waves.add(i)

                target = siege_target(siege, waves, wave_count)
                if self._active_seen_at is None:
                    self._active_seen_at = time.monotonic()
                waiting_for_target = (not target and
                                      time.monotonic() - self._active_seen_at < _START_WAIT_SECONDS)

                # Start notification — fires once per siege, once the target is known
                # (or after _START_WAIT_SECONDS without one).
                if not self._active_announced and not waiting_for_target:
                    self._active_announced = True
                    desc = start_description(target, players, direction, wave_count)
                    embed = discord.Embed(
                        title="\U0001f9df Siege Night Has Begun!",
                        description=desc,
                        colour=discord.Colour.red(),
                    )
                    intel = [f"Siege **#{siege_count + 1}**"]
                    if cur_phase:
                        intel.append(f"Phase **{cur_phase.title()}**")
                    if spawned:
                        intel.append(f"Spawned **{spawned}**")
                    if len(intel) > 1:
                        embed.add_field(name="\U0001f4a1 Intel", value=" · ".join(intel), inline=False)
                    if self.bot.features.is_enabled("siege_night"):
                        await self.bot.send_to_channel(channel, self.bot.config.SIEGE_ROLE_ID, embed)
                    print(f"[SiegeNight] Notification sent: active, siege={siege_count}")
                    # In-game red-alert (servermsg + alert sound) — best-effort, after the
                    # Discord message so a slow RCON can never suppress the notification.
                    try:
                        await self.bot.rcon.broadcast(
                            "SIEGE NIGHT HAS BEGUN! Zombies are attacking. Hold the line!"
                        )
                    except Exception as e:
                        print(f"[SiegeNight] Red-alert broadcast failed: {e}")

                # Per-wave notifications — driven by the wave records (wave 2+; wave 1
                # is covered by the "begun" embed above).
                for i in range(1, wave_count + 1 if self._active_announced else 1):
                    wrec = waves.get(f"wave{i}")
                    if not wrec or i in self._announced_waves:
                        continue
                    self._announced_waves.add(i)
                    if i < 2:
                        continue
                    expected = wrec.get("expected", 0)
                    wave_title = f"\U0001f30a Wave {i}"
                    if wave_count:
                        wave_title += f"/{wave_count}"
                    wave_desc = "A new surge wave has begun. The horde intensifies."
                    if expected:
                        wave_desc += f"\n**{expected}** zombies incoming."
                    wave_embed = discord.Embed(
                        title=wave_title,
                        description=wave_desc,
                        colour=discord.Colour.orange(),
                    )
                    wave_intel = []
                    if direction:
                        wave_intel.append(f"Direction **{direction}**")
                    if spawned:
                        wave_intel.append(f"Spawned so far **{spawned}**")
                    if wave_intel:
                        wave_embed.add_field(
                            name="\U0001f4a1 Intel",
                            value=" · ".join(wave_intel),
                            inline=False,
                        )
                    if self.bot.features.is_enabled("siege_night"):
                        await self.bot.send_to_channel(
                            channel, self.bot.config.SIEGE_ROLE_ID, wave_embed
                        )
                    print(f"[SiegeNight] Notification sent: wave {i}/{wave_count}")

            elif phase in ("idle", "dawn"):
                # Reset per-siege tracking once the siege is no longer active, so the
                # next siege's start and waves are announced again.
                self._active_announced = False
                self._active_seen_at = None
                # "Ended" fires once when the completed count increments past
                # what we've already announced.
                if completed and completed not in self._announced_ended_sieges:
                    self._announced_ended_sieges.add(completed)
                    next_day = sched.get("nextSiegeDay")
                    kills = siege.get("killsThisSiege", 0)
                    bonus = siege.get("bonusKills", 0)
                    specials = siege.get("specialKillsThisSiege", 0)
                    spawned = siege.get("spawnedThisSiege", 0)
                    total_kills = siege.get("totalKillsAllTime", 0)
                    embed = discord.Embed(
                        title="\u2705 Siege Night Has Ended",
                        description="The siege has been repelled. The night is quiet once more.",
                        colour=discord.Colour.green(),
                    )
                    results = [f"Kills **{kills}**", f"Specials **{specials}**"]
                    if bonus:
                        results.append(f"Bonus **{bonus}**")
                    if spawned:
                        results.append(f"Spawned **{spawned}**")
                    if total_kills:
                        results.append(f"All-time **{total_kills}**")
                    embed.add_field(
                        name="\U0001f4ca Siege Results",
                        value=" · ".join(results),
                        inline=False,
                    )
                    # Per-wave summary (expected surge counts) from the wave records.
                    wave_count = waves.get("waveCount", 0)
                    wave_lines = []
                    for i in range(1, wave_count + 1):
                        wrec = waves.get(f"wave{i}")
                        if wrec and wrec.get("expected"):
                            wave_lines.append(f"Wave {i} — **{wrec.get('expected')}** zombies")
                    if wave_lines:
                        embed.add_field(
                            name="\U0001f30a Waves",
                            value="\n".join(wave_lines),
                            inline=False,
                        )
                    if next_day:
                        world = await lua_bridge.read_world_status()
                        date_str = siege_date_string(world, sched)
                        nd = f"Next siege night: **Day {next_day}**"
                        if date_str:
                            nd += f" ({date_str})"
                        embed.add_field(name="\U0001f319 Next", value=nd, inline=False)
                    if self.bot.features.is_enabled("siege_night"):
                        await self.bot.send_to_channel(channel, self.bot.config.SIEGE_ROLE_ID, embed)
                    print(f"[SiegeNight] Notification sent: ended, completed={completed}")

        except Exception as e:
            print(f"[SiegeNight] Siege status poller error: {e}")

    @siege_status_poller.before_loop
    async def before_siege_poller(self):
        await self.bot.wait_until_ready()
        # Seed dedup sets with the current status so stale notifications from
        # before the bot started are suppressed.
        try:
            status = await lua_bridge.read_siege_status()
            if status:
                sched, siege, waves = self._split_status(status)
                phase = sched.get("phase") or "idle"
                event_day = sched.get("eventDay", 0)
                completed = sched.get("totalSiegesCompleted", 0)
                if phase == "warning" and event_day:
                    self._announced_warning_days.add(event_day)
                if phase == "active":
                    self._active_announced = True
                    self._announced_session = waves.get("sessionId")
                    cur = waves.get("currentWave", 0) or siege.get("currentWaveIndex", 0)
                    self._announced_waves = set(range(1, int(cur) + 1))
                if completed:
                    self._announced_ended_sieges.add(completed)
                print(f"[SiegeNight] Seeded dedup: phase={phase}, day={event_day}, "
                      f"siege={sched.get('siegeCount', 0)}, completed={completed}")
        except Exception:
            pass

    # ── /siegestatus ────────────────────────────────────────────────────

    @app_commands.command(name="siegestatus", description="Show the current Siege Night status.")
    @admin_only()
    async def cmd_siegestatus(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

        status = await lua_bridge.read_siege_status()
        if not status:
            await interaction.followup.send(embed=discord.Embed(
                title="\U0001f4cb Siege Status",
                description="No siege status available. Is the Siege Night bridge mod installed?",
                colour=discord.Colour.orange(),
            ))
            return

        sched, siege, waves = self._split_status(status)
        phase = sched.get("phase") or "idle"
        event_day = sched.get("eventDay", "?")
        next_day = sched.get("nextSiegeDay", 0)
        completed = sched.get("totalSiegesCompleted", 0)

        world = await lua_bridge.read_world_status()
        date_str = siege_date_string(world, sched)

        if phase == "active":
            state = "\U0001f534 **ACTIVE** — Siege in progress"
            kills = siege.get("killsThisSiege", 0)
            spawned = siege.get("spawnedThisSiege", 0)
            wave_count = waves.get("waveCount", 0) or siege.get("totalWaves", 0)
            target = siege_target(siege, waves, wave_count)
            state += (f"\n\U0001f9df Spawned: **{spawned}** | Kills: **{kills}** | "
                      f"Target: **{target if target else 'not set yet'}**")
            cur = waves.get("currentWave", 0) or siege.get("currentWaveIndex", 0)
            if wave_count:
                state += f"\n\U0001f30a Wave: **{cur}/{wave_count}**"
        elif phase == "warning":
            state = "\U0001f7e1 **Warning Active** — Siege night approaching tonight"
        elif phase == "dawn":
            state = "\U0001f7e0 **Dawn** — Siege wrapping up"
        else:
            state = "\U0001f7e2 **Idle** — No active siege"

        if next_day:
            next_line = f"\U0001f319 Next Siege Night: **Day {next_day}**"
            if date_str:
                next_line += f" ({date_str})"
        else:
            next_line = "\U0001f319 Next Siege Night: **Not scheduled**"

        lines = [
            state,
            "",
            f"\U0001f4c5 Current Day: **{event_day}**",
            next_line,
            f"\U0001f4ca Sieges Completed: **{completed}**",
            bridge_line(lua_bridge.siege_bridge_info(status)),
        ]

        await interaction.followup.send(embed=discord.Embed(
            title="\U0001f4cb Siege Night Status",
            description="\n".join(lines),
            colour=discord.Colour.red() if phase == "active" else discord.Colour.blue(),
        ))

    # ── /siegestart, /siegestop, /siegeschedule ─────────────────────────
    # BLib's bridge (blib-admin) checks every command against Siege Night's
    # state, logs it in BLib's staff log under the Discord name, and answers;
    # the reply shows that answer. The old bridge mod never answers.

    async def _send_outcome(self, interaction: discord.Interaction, action: str, outcome: dict) -> None:
        title, desc, ok = describe_siege_outcome(action, outcome, interaction.user.display_name)
        colour = discord.Colour.green() if ok else (discord.Colour.red() if ok is False else discord.Colour.orange())
        await interaction.followup.send(embed=discord.Embed(title=title, description=desc, colour=colour),
                                        ephemeral=ok is False)

    @app_commands.command(name="siegestart", description="Force a siege night to start immediately.")
    @admin_only()
    async def cmd_siegestart(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        outcome = await lua_bridge.siege_start(by=interaction.user.display_name, by_id=str(interaction.user.id))
        await self._send_outcome(interaction, "start", outcome)

    @app_commands.command(name="siegestop", description="Stop the currently active siege night.")
    @admin_only()
    async def cmd_siegestop(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        outcome = await lua_bridge.siege_stop(by=interaction.user.display_name, by_id=str(interaction.user.id))
        await self._send_outcome(interaction, "stop", outcome)

    @app_commands.command(name="siegeschedule", description="Change the next siege night to a specific world day.")
    @app_commands.describe(day="The world day number to schedule the next siege on")
    @admin_only()
    async def cmd_siegeschedule(self, interaction: discord.Interaction, day: int) -> None:
        if day < 1:
            await interaction.response.send_message(embed=discord.Embed(
                title="Invalid Day",
                description="Day must be 1 or greater.",
                colour=discord.Colour.red(),
            ), ephemeral=True)
            return

        await interaction.response.defer()
        outcome = await lua_bridge.siege_schedule(day, by=interaction.user.display_name,
                                                  by_id=str(interaction.user.id))
        await self._send_outcome(interaction, "schedule", outcome)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SiegeNightCog(bot))
    print("[SiegeNight] Bot cog loaded")
