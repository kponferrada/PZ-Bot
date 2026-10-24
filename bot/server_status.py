"""
Server Status Dashboard Extension

Maintains a single auto-updating Discord embed in a dedicated channel showing:
  - Server status (Online/Offline)
  - Active player count
  - Next scheduled restart (countdown)
  - In-game time and date with day/night indicator
  - Server age (day count)
  - Weather conditions, wind, and temperature
  - Next siege night (days remaining)

Resilience features:
  - Grace period: 3 consecutive RCON failures before showing offline
  - Lua file freshness: treats recently-written bridge files as a secondary
    online signal even if RCON is momentarily unresponsive
  - Last-known-good data: retains and displays cached world/siege data during
    brief outages instead of blanking the panel
  - Siege status: shows completed count and current day alongside next siege info

In `embed` mode the message is one embed laid out like the user's mockup
(2026-10-25): status line, three rows of fields (players, world, next siege)
and the 24-hour players-online graph (player_graph.py) as its image. The
names of who is online are behind the "Online players" button
(/onlineplayers). `image` mode posts the rendered status card. Either way a
row of player buttons (player_panel.py `status_rows`; private replies) sits
under the message. A classic embed can't hold buttons inside it, and a V2
card can't lay fields out in columns, so the buttons sit just below.

Config:
  STATUS_CHANNEL_ID=  (Discord channel ID for the status embed)
"""

import os
import time
import asyncio
import datetime
import hashlib
import io
import json
import discord
from pathlib import Path
from discord import app_commands
from discord.ext import commands, tasks

import lua_bridge
import player_graph
import status_card
import server_config
from command_panel import walk_components as _walk
import status_icons
from game_calendar import GameClockRate, siege_date_string, siege_real_time

# ============================================================================
# Constants
# ============================================================================

ICON_URL = ""   # dashboard thumbnail / author icon (set from config)

DASHBOARD_TITLE = "PZ TAMBAYAN"  # overridden from config in the cog's __init__

_ASSET_CACHE = Path(__file__).parent / "dashboard_assets.json"
_STATUS_FILE = "status.jpg"  # attachment name of the image-mode card
_GRAPH_FILE = "players.png"  # the 24-hour player graph (embed mode)
_BLANK = "\u200b"  # an empty embed field (ends a row of inline fields)


def _load_asset_cache() -> dict:
    try:
        return json.loads(_ASSET_CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_asset_cache(cache: dict) -> None:
    try:
        _ASSET_CACHE.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"[ServerStatus] Could not save asset cache: {e}")


def asset_fingerprint(path: Path) -> str:
    """Cache key for an uploaded dashboard image: name + content hash."""
    return f"{path.name}:sha1:{hashlib.sha1(path.read_bytes()).hexdigest()}"


async def _resolve_image_url(bot, local_path: str, url_override: str = "") -> str:
    """Return a CDN URL for a dashboard image, uploading the local file on first use."""
    if url_override:
        return url_override
    if not local_path:
        return ""
    path = Path(local_path)
    if not path.is_absolute():
        path = Path(__file__).parent / path
    if not path.is_file():
        print(f"[ServerStatus] Dashboard image not found: {local_path}")
        return ""
    # Keyed by content, not mtime: a fresh checkout or `git pull` rewrites
    # mtimes, and an mtime key re-uploaded (posted) the images on every deploy.
    fingerprint = asset_fingerprint(path)
    cache = _load_asset_cache()
    if cache.get(fingerprint):
        return cache[fingerprint]
    channel = bot.get_notification_channel()
    if not channel:
        print("[ServerStatus] No channel to upload dashboard image to")
        return ""
    try:
        msg = await channel.send(file=discord.File(str(path)))
        url = msg.attachments[0].url
        cache[fingerprint] = url
        _save_asset_cache(cache)
        print(f"[ServerStatus] Uploaded dashboard image -> {url}")
        return url
    except Exception as e:
        print(f"[ServerStatus] Failed to upload dashboard image {local_path}: {e}")
        return ""

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
]

def _parse_restart_schedule():
    raw = os.environ.get("RESTART_SCHEDULE_UTC", "")
    if raw:
        try:
            hours = [int(x.strip()) for x in raw.split(",") if x.strip()]
            if hours:
                return hours
        except ValueError:
            pass
    return [4, 10, 16, 22]


RESTART_HOURS_UTC = _parse_restart_schedule()

WEATHER_EMOJI = {
    "Clear": "\u2600\ufe0f",
    "Partly Cloudy": "\u26c5",
    "Overcast": "\u2601\ufe0f",
    "Light Rain": "\U0001f326\ufe0f",
    "Rain": "\U0001f327\ufe0f",
    "Heavy Rain": "\u26c8\ufe0f",
    "Foggy": "\U0001f32b\ufe0f",
    "Snowing": "\u2744\ufe0f",
}

# How many consecutive RCON failures before declaring offline
OFFLINE_GRACE_COUNT = 3

# How old (seconds) a Lua bridge file can be and still count as "fresh"
# (i.e., the server was writing data recently even if RCON timed out)
BRIDGE_FRESHNESS_SECONDS = 120

# ============================================================================
# Data helpers
# ============================================================================

def _next_restart():
    """Return (countdown, clock_time) for the next scheduled restart.

    Schedule hours are interpreted in UTC (override via RESTART_SCHEDULE_UTC);
    the clock time shown is Philippine Time (UTC+8). The countdown itself is
    timezone-independent.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    candidates = []
    for h in RESTART_HOURS_UTC:
        t = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if t <= now:
            t += datetime.timedelta(days=1)
        candidates.append(t)
    nxt = min(candidates)
    delta = nxt - now
    total_minutes = int(delta.total_seconds() // 60)
    hours, minutes = total_minutes // 60, total_minutes % 60
    countdown = f"in {hours}h {minutes}m" if hours else f"in {minutes}m"
    pht = nxt + datetime.timedelta(hours=8)
    time_str = _format_time(pht.hour, pht.minute) + " PHT"
    return countdown, time_str
def _format_time(hour, minutes):
    period = "AM" if hour < 12 else "PM"
    display_hour = hour % 12
    if display_hour == 0:
        display_hour = 12
    return f"{display_hour}:{minutes:02d} {period}"


def _temp_c_f(celsius):
    return f"{celsius:.0f}\u00b0C / {celsius * 9 / 5 + 32:.0f}\u00b0F"


def _bridge_file_is_fresh(data, max_age=BRIDGE_FRESHNESS_SECONDS):
    """Check if a Lua bridge dict has a recent timestamp."""
    if not data:
        return False
    ts = data.get("timestamp")
    if ts is None:
        return False
    try:
        age = time.time() - float(ts)
        return age < max_age
    except (TypeError, ValueError):
        return False


# ============================================================================
# Embed builder
# ============================================================================

def status_values(world, siege) -> dict:
    """The status values as text, keyed like status_card.PILLS. Shared by the
    embed and the illustrated card so both always say the same thing."""
    world = world or {}
    hour = world.get("hour", 0)
    mins = world.get("minutes", 0)
    period = "AM" if hour < 12 else "PM"
    hh = hour % 12 or 12

    month = world.get("month", 0)
    day = world.get("day", 0) + 1  # PZ in-game day is 0-indexed (0 = 1st of month)
    mn = MONTH_NAMES[month][:3] if 0 <= month < 12 else "?"
    year = world.get("year")

    elapsed = world.get("elapsedDays", 0)
    age_raw = world.get("worldAgeDays", 0)
    if elapsed and elapsed > 0:
        age = int(elapsed)
    elif age_raw >= 1:
        age = int(age_raw)
    else:
        age = max(1, int(age_raw))

    ws = world.get("windSpeed", 0)
    if ws > 0.6:
        wd = "Strong"
    elif ws > 0.3:
        wd = "Moderate"
    elif ws > 0.05:
        wd = "Light"
    else:
        wd = "Calm"

    siege_day, siege_status, siege_completed = _siege_fields(siege, world)
    restart_cd, restart_tm = _next_restart()
    return {
        "SERVER AGE": f"Day {age}",
        "DATE": f"{mn} {day}, {year}" if year else f"{mn} {day}",
        "TIME": f"{hh}:{mins:02d} {period}",
        "WEATHER": f"{world.get('weather', 'Clear')} ({_temp_c_f(world.get('temperature', 0))})",
        "WIND": wd if ws <= 0.05 else f"{wd} ({ws * 10:.0f} mph)",
        "CYCLE": "Night" if world.get("isNight", False) else "Day",
        "NEXT SIEGE": siege_day,
        "SIEGE STATUS": siege_status,
        "COMPLETED": siege_completed,
        "RESTART": f"{restart_cd} · {restart_tm}",
    }


def _next_restart_ts() -> float:
    """The next restart in the schedule (fallback when RestartWatch isn't loaded)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    candidates = []
    for h in RESTART_HOURS_UTC:
        t = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if t <= now:
            t += datetime.timedelta(days=1)
        candidates.append(t)
    return min(candidates).timestamp()


def restart_line(restart) -> str:
    """The restart part of the status line. `restart` is (epoch, running) or
    None. Discord timestamps show in each viewer's own timezone, and the
    relative one counts down by itself between updates."""
    if restart is None:
        return "No restart scheduled"
    ts, running = int(restart[0]), restart[1]
    if running:
        return f"**Restarting <t:{ts}:R>**"
    return f"Restart at <t:{ts}:t> <t:{ts}:R>"


def siege_when_line(siege_at, now: float) -> str:
    """The real-world start of the next siege as Discord timestamps ("" if unknown)."""
    if not siege_at:
        return ""
    ts = int(siege_at)
    # Within a day the clock time is enough; further out it needs the date.
    return f"<t:{ts}:{'t' if ts - now < 20 * 3600 else 'f'}> <t:{ts}:R>"


ACCENT = 0xB7791F  # the card's amber edge while the server is up


def build_embed(server_online, world, siege, skip_active, stale=False, max_players=32, restart=...,
                siege_at=None, peak_today=None):
    """Build the PZ Tambayan status embed.

    Layout (user's mockup, 2026-10-25): the server name as the author, "Server
    status" as the title, one status line (state, server age, next restart),
    then three rows of fields: Survivors / Peak today / Next restart, In-game
    date / In-game time / Weather, and Next siege night with its real-world
    start beside it. The 24-hour graph is the image (set by the cog).
    `restart` is (epoch, running), None for none coming, or left out to read
    the schedule. `siege_at` is the next siege's real start (epoch) if known;
    `peak_today` the most players online since midnight PHT."""
    world = world or {}
    siege = siege or {}

    if server_online and not stale:
        colour, state = ACCENT, "🟢 Online"
    elif server_online:
        colour, state = 0xFFB300, "🟡 Online · data delayed"
    else:
        colour, state = 0xFF5252, "🔴 Offline"

    embed = discord.Embed(colour=colour, title="Server status")
    if ICON_URL:
        embed.set_author(name=DASHBOARD_TITLE, icon_url=ICON_URL)
    else:
        embed.set_author(name=DASHBOARD_TITLE)

    pc = world.get("playerCount", 0) if server_online else 0
    if restart is ...:
        restart = (_next_restart_ts(), False)
    has_world = bool(server_online or world)
    v = status_values(world, siege) if has_world else {}
    status = [state]
    if has_world:
        status.append(v["SERVER AGE"])
    status.append(restart_line(restart))
    embed.description = " · ".join(status)

    if restart is None:
        next_restart = "—"
    elif restart[1]:
        next_restart = f"Restarting <t:{int(restart[0])}:R>"
    else:
        next_restart = f"<t:{int(restart[0])}:t>"
    embed.add_field(name="Survivors", value=f"{pc} / {max_players}", inline=True)
    embed.add_field(name="Peak today", value=str(max(peak_today or 0, pc)), inline=True)
    embed.add_field(name="Next restart", value=next_restart, inline=True)

    if has_world:
        siege_day, siege_status, siege_completed = _siege_fields(siege, world)
        # "Day 665\n(May 4, 1995)" -> "**Day 665** · May 4 in-game"
        when = siege_day.split("\n")
        siege_when = f"**{when[0]}**" if when[0] != "—" else when[0]
        if len(when) > 1:
            siege_when += f" · {when[1].strip('()').rsplit(', ', 1)[0]} in-game"
        survived = f"{siege_completed} {'siege' if siege_completed == '1' else 'sieges'} survived"
        siege_lines = [siege_when, survived if siege_status == "Idle" else f"{siege_status} · {survived}"]
        embed.add_field(name="In-game date", value=v["DATE"], inline=True)
        embed.add_field(name="In-game time", value=f"{v['TIME']} · {v['CYCLE']}", inline=True)
        temp = world.get("temperature", 0)
        embed.add_field(name="Weather", value=f"{world.get('weather', 'Clear')} · {temp:.0f}°C", inline=True)
        # Two inline fields share a row half and half: the siege, then when it starts.
        embed.add_field(name="Next siege night", value="\n".join(siege_lines), inline=True)
        when_real = siege_when_line(siege_at, time.time())
        if when_real:
            embed.add_field(name=_BLANK, value=when_real, inline=True)

    footer = "Knox County never forgets · Updated every 30 s"
    embed.set_footer(text=footer)
    embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    return embed


def _siege_fields(siege, world=None):
    """Return (siege_day, siege_status, completed) for three inline fields."""
    if not siege:
        return ("—", "Idle", "0")

    phase = siege.get("phase", "")
    completed = siege.get("totalSiegesCompleted")
    next_day = siege.get("nextSiegeDay")

    # Siege day — nextSiegeDay is already the in-game siege day.
    if next_day is not None:
        siege_day = f"Day {next_day}"
        date_str = siege_date_string(world, siege)
        if date_str:
            siege_day += f"\n({date_str})"
    else:
        siege_day = "—"

    # Status
    if phase == "active":
        siege_status = "\u26a0\ufe0f **ACTIVE**"
    elif phase == "warning":
        siege_status = "\u26a0\ufe0f **Tonight**"
    elif phase == "dawn":
        siege_status = "Winding down"
    elif phase == "idle":
        siege_status = "Idle"
    elif phase:
        siege_status = phase.capitalize()
    else:
        siege_status = "Idle"

    # Completed count
    siege_completed = str(completed) if completed is not None else "0"

    return (siege_day, siege_status, siege_completed)


def online_list_text(names: list, offline: bool) -> str:
    """The /onlineplayers answer body."""
    if offline:
        return "🔴 The server is offline."
    if not names:
        return "*No survivors online.*"
    text = "\n".join(f"• {n}" for n in names)
    if len(text) > 4000:  # embed description limit
        text = text[:3990].rsplit("\n", 1)[0] + "\n…"
    return text


# ============================================================================
# Discord Cog
# ============================================================================

class ServerStatusCog(commands.Cog):

    def __init__(self, bot):
        global DASHBOARD_TITLE, ICON_URL
        DASHBOARD_TITLE = getattr(bot.config, 'DASHBOARD_TITLE', 'PZ TAMBAYAN')
        ICON_URL = getattr(bot.config, 'DASHBOARD_ICON_URL', '')
        self.bot = bot
        self._channel_id = int(os.getenv('STATUS_CHANNEL_ID', '0'))
        self._message_id = None
        self._channel = None
        self._mode = getattr(bot.config, "STATUS_MODE", "embed").strip().lower()

        # Grace period state
        self._rcon_fail_count = 0

        # Players-online history for the 24-hour graph (embed mode)
        self._history = player_graph.PlayerHistory()
        self._graph_png_cache = None
        self._graph_dirty = True
        self._card_key = None  # what the posted image-mode card shows (skip unchanged renders)

        # Last known good data (retained across brief outages)
        self._last_world = None
        self._last_world_at = 0.0
        self._clock = GameClockRate()  # in-game clock speed, for the siege's real start time
        self._siege_at = None          # ((nextSiegeDay, start hour), epoch): worked out once per siege
        self._last_siege = None

        if not self._channel_id:
            print("[ServerStatus] WARNING: STATUS_CHANNEL_ID not set. Dashboard disabled.")
        else:
            print(f"[ServerStatus] Dashboard channel: {self._channel_id}")
            self.status_loop.start()

    def cog_unload(self):
        self.status_loop.cancel()

    async def _get_channel(self):
        if self._channel:
            return self._channel
        if not self._channel_id:
            return None
        ch = self.bot.get_channel(self._channel_id)
        if not ch:
            try:
                ch = await self.bot.fetch_channel(self._channel_id)
            except (discord.NotFound, discord.Forbidden) as e:
                print(f"[ServerStatus] Could not access channel {self._channel_id}: {e}")
                return None
        self._channel = ch
        return ch

    def _player_view(self):
        """The player buttons in a row under the status card (None if the player
        panel isn't loaded). Fresh buttons each update; their custom_ids match
        the persistent player panel, so they keep working after a bot restart."""
        panel = self.bot.get_cog("PlayerPanel")
        if panel is None:
            return None
        view = discord.ui.View(timeout=None)
        for row in panel.status_rows():
            for button in row:
                view.add_item(button)
        return view

    def _is_status_message(self, msg) -> bool:
        """Our status message: an embed (embed mode) or image (image mode) the bot
        left here, or the V2 card v0.6.0 posted (replaced on the next update)."""
        if msg.author != self.bot.user:
            return False
        if not msg.flags.components_v2:
            return bool(msg.attachments if self._mode == "image" else msg.embeds)
        if any(a.filename == _STATUS_FILE for a in msg.attachments):
            return True
        head = f"### {DASHBOARD_TITLE}"
        return any((getattr(c, "content", "") or "").startswith(head) for c in _walk(msg.components))

    async def _publish(self, make_files, embeds=None) -> None:
        """Send or edit the status message. `make_files` builds fresh attachments
        for each attempt (a discord.File can only be sent once)."""
        channel = await self._get_channel()
        if not channel:
            return
        view = self._player_view()

        def kwargs():
            out = {"attachments": make_files(), "view": view}
            if embeds is not None:
                out["embeds"] = embeds
            return out

        if self._message_id:
            try:
                # Edit by id: one API call instead of fetch + edit.
                await channel.get_partial_message(self._message_id).edit(**kwargs())
                return
            except (discord.NotFound, discord.HTTPException):
                self._message_id = None

        # Reuse our previous status message after a bot restart. A V2 card can't
        # go back to embeds, so that one is replaced by a new message.
        old = None
        try:
            async for msg in channel.history(limit=20):
                if self._is_status_message(msg):
                    old = msg
                    break
        except discord.HTTPException:
            pass
        if old is not None and not old.flags.components_v2:
            try:
                await old.edit(**kwargs())
                self._message_id = old.id
                print(f"[ServerStatus] Found existing status message: {old.id}")
                return
            except discord.HTTPException as e:
                print(f"[ServerStatus] Could not update status message {old.id} ({e}); posting a new one.")

        try:
            send = {"files": make_files(), "view": view}
            if embeds is not None:
                send["embeds"] = embeds
            msg = await channel.send(**{k: v for k, v in send.items() if v is not None})
            self._message_id = msg.id
            print(f"[ServerStatus] Created status message: {msg.id}")
        except discord.HTTPException as e:
            print(f"[ServerStatus] Failed to send status: {e}")
            return
        if old is not None:
            try:
                await old.delete()
            except discord.HTTPException:
                pass

    async def _send_or_edit_card(self, state: str, world: dict, siege: dict, max_players: int):
        """Render the illustrated card and post it, only when something on it changed."""
        values = status_values(world, siege)
        # One line in the pill: "Day 5\n(Jul 13, 1993)" -> "Day 5 · Jul 13, 1993".
        values["NEXT SIEGE"] = " · ".join(
            p.strip("()") for p in values["NEXT SIEGE"].split("\n") if p.strip())
        players = world.get("playerCount", 0) if state != "offline" else 0
        points = self._history.points()
        key = (DASHBOARD_TITLE, state, tuple(sorted(values.items())), players, max_players, tuple(points))
        if key == self._card_key:
            return
        jpeg = await asyncio.to_thread(
            status_card.render_status_card, title=DASHBOARD_TITLE, state=state, values=values,
            players=players, max_players=max_players, graph_points=points)
        await self._publish(lambda: [discord.File(io.BytesIO(jpeg), filename=_STATUS_FILE)])
        # Remembered only once it is posted, so a failed post is retried next loop.
        self._card_key = key if self._message_id else None

    async def _siege_time(self, server_online: bool, world: dict, siege: dict) -> float | None:
        """The next siege's real-world start, worked out once and then kept.

        It is worked out again only when the siege it belongs to changes (a new
        nextSiegeDay or start hour), when that time has passed without the siege
        starting, or after the server was down: the game clock stops while the
        server is down, so the siege really does move later."""
        if not server_online:
            self._siege_at = None
            self._clock.samples = []  # a speed measured across the downtime would be too slow
            return None
        if not siege or str(siege.get("phase", "")).lower() in ("active", "dawn"):
            self._siege_at = None
            return None
        start_hour = await server_config.read_siege_start_hour(self.bot)
        key = (siege.get("nextSiegeDay"), start_hour)
        if self._siege_at is not None and self._siege_at[0] == key and self._siege_at[1] > time.time():
            return self._siege_at[1]
        at = siege_real_time(world, siege, start_hour, self._clock.rate(), time.time())
        self._siege_at = (key, at) if at else None
        return at

    async def _graph_png(self, max_players: int) -> bytes | None:
        """The 24-hour player chart, re-rendered only when the history changed."""
        if self._graph_png_cache is None or self._graph_dirty:
            try:
                self._graph_png_cache = await asyncio.to_thread(
                    player_graph.render, self._history.points(), None, max_players)
                self._graph_dirty = False
            except Exception as e:
                print(f"[ServerStatus] player graph failed: {e}")
        return self._graph_png_cache

    async def _send_or_edit_embed(self, embed, max_players: int):
        png = await self._graph_png(max_players)
        if png is None:
            await self._publish(lambda: [], [embed])
            return
        # The graph is the embed's image: inside the same card, under the fields.
        embed.set_image(url=f"attachment://{_GRAPH_FILE}")
        await self._publish(lambda: [discord.File(io.BytesIO(png), filename=_GRAPH_FILE)], [embed])

    @app_commands.command(name="onlineplayers", description="List the players online right now.")
    async def cmd_online_players(self, interaction: discord.Interaction) -> None:
        world = self._last_world or {}
        names = sorted((n.strip() for n in str(world.get("players", "") or "").split(",") if n.strip()),
                       key=str.lower)
        offline = self._rcon_fail_count >= OFFLINE_GRACE_COUNT
        embed = discord.Embed(title=f"👥 Online now ({0 if offline else len(names)})", colour=0x00E676,
                              description=online_list_text(names, offline))
        if self._last_world_at:
            embed.set_footer(text="From the status card")
            embed.timestamp = datetime.datetime.fromtimestamp(self._last_world_at, datetime.timezone.utc)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tasks.loop(seconds=30)
    async def status_loop(self):
        if not self.bot.features.is_enabled("status_dashboard"):
            return
        try:
            # Reuse the up/down monitor's probe (every 15 s) when it's fresh;
            # otherwise probe ourselves, off the event loop (blocking socket).
            rcon_ok = self.bot.state.recent_rcon_probe(20.0)
            if rcon_ok is None:
                rcon_ok = await asyncio.to_thread(self.bot.rcon.is_server_online)
            world, world_age = await lua_bridge.read_world_status_with_age()
            siege = await lua_bridge.read_siege_status()
            # The bridge now writes a nested {schedule, siege} table. The dashboard
            # only surfaces schedule/state fields, so read the schedule section.
            siege = (siege or {}).get("schedule")

            if world_age is not None:
                d = world or {}
                print(f"[ServerStatus] world file age={world_age:.0f}s "
                      f"({'STALE' if world_age > 120 else 'fresh'}) "
                      f"day={d.get('day')} month={d.get('month')} ts={d.get('timestamp')}")

            # online determination with grace period. The world file's age comes
            # from its write time (skew-immune); the siege file only has the
            # timestamp the mod wrote into it.
            world_fresh = world_age is not None and world_age < BRIDGE_FRESHNESS_SECONDS
            siege_fresh = _bridge_file_is_fresh(siege)
            bridge_fresh = world_fresh or siege_fresh

            if rcon_ok or bridge_fresh:
                self._rcon_fail_count = 0
                server_online = True
            else:
                self._rcon_fail_count += 1
                server_online = self._rcon_fail_count < OFFLINE_GRACE_COUNT

            if world:
                self._last_world = world
                self._last_world_at = time.time()
                self._clock.record(self._last_world_at, world)
            if siege:
                self._last_siege = siege
            world = world or self._last_world or {}
            siege = siege or self._last_siege or {}

            max_players = await server_config.read_max_players(self.bot, getattr(self.bot.config, "MAX_PLAYERS", 32))
            if self._history.record(world.get("playerCount", 0) if server_online else 0):
                self._graph_dirty = True

            if self._mode == "image":
                state = "offline" if not server_online else ("stale" if not rcon_ok else "online")
                await self._send_or_edit_card(state, world, siege, max_players)
            else:
                stale = server_online and not rcon_ok
                watch = self.bot.get_cog("RestartWatch")
                restart = watch.next_restart() if watch is not None else (_next_restart_ts(), False)
                siege_at = await self._siege_time(server_online, world, siege)
                points = self._history.points()
                peak_today = player_graph.peak_since(points, player_graph.day_start(time.time()))
                embed = build_embed(server_online, world, siege, False, stale=stale,
                                    max_players=max_players, restart=restart, siege_at=siege_at,
                                    peak_today=peak_today)
                await self._send_or_edit_embed(embed, max_players)

        except Exception as e:
            print(f"[ServerStatus] Error in status loop: {e}")

    @status_loop.before_loop
    async def _before_status(self):
        await self.bot.wait_until_ready()
        global ICON_URL, DASHBOARD_TITLE
        cfg = self.bot.config
        ICON_URL = await _resolve_image_url(
            self.bot,
            getattr(cfg, "DASHBOARD_ICON_IMAGE", ""),
            getattr(cfg, "DASHBOARD_ICON_URL", ""),
        )
        try:
            uploaded = await status_icons.sync(self.bot)
            print(f"[ServerStatus] Flat status icons ready ({uploaded} uploaded)")
        except Exception as e:
            print(f"[ServerStatus] Flat icons unavailable, using plain emoji: {e}")
        DASHBOARD_TITLE = await server_config.read_server_name(
            self.bot, getattr(cfg, "DASHBOARD_TITLE", "PZ TAMBAYAN")
        )
        await asyncio.sleep(5)
        print("[ServerStatus] Dashboard started")


async def setup(bot):
    await bot.add_cog(ServerStatusCog(bot))
