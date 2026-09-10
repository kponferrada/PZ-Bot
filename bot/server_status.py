"""
Server Status Dashboard Extension

Maintains a single auto-updating Discord embed in a dedicated channel showing:
  - Server status (Online/Offline)
  - Active player count
  - Next scheduled restart (countdown)
  - In-game time and date with day/night indicator
  - Server age (day count)
  - Weather conditions, wind, and temperature
  - Next horde night (days remaining)

Resilience features:
  - Grace period: 3 consecutive RCON failures before showing offline
  - Lua file freshness: treats recently-written bridge files as a secondary
    online signal even if RCON is momentarily unresponsive
  - Last-known-good data: retains and displays cached world/horde data during
    brief outages instead of blanking the panel
  - Horde status: shows event count and current day alongside next horde info

Config:
  STATUS_CHANNEL_ID=  (Discord channel ID for the status embed)
"""

import os
import sys
import time
import asyncio
import datetime
import json
import discord
from pathlib import Path
from discord.ext import commands, tasks

import lua_bridge
import status_card
import server_config

# ============================================================================
# Constants
# ============================================================================

ICON_URL = ""   # dashboard thumbnail / author icon (set from config)
IMAGE_URL = ""  # dashboard banner image (set from config)

DASHBOARD_TITLE = "PZ TAMBAYAN"  # overridden from config in the cog's __init__

_ASSET_CACHE = Path(__file__).parent / "dashboard_assets.json"


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
    fingerprint = f"{path.name}:{path.stat().st_size}:{int(path.stat().st_mtime)}"
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

RESTART_HOURS_UTC = [1, 5, 9, 13, 17, 21]

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

def _next_restart_str(skip_active):
    # The bot no longer schedules restarts (host-managed now).
    return "—"


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

def build_embed(server_online, world, horde, skip_active, stale=False, max_players=32):
    """Build the PZ Tambayan status embed (copyable text + PZ aesthetic)."""
    world = world or {}
    horde = horde or {}

    if server_online and not stale:
        colour = 0x00E676
    elif server_online and stale:
        colour = 0xFFB300
    else:
        colour = 0xFF5252

    embed = discord.Embed(colour=colour)

    if ICON_URL:
        embed.set_author(name=DASHBOARD_TITLE, icon_url=ICON_URL)
    else:
        embed.set_author(name=DASHBOARD_TITLE)
    if IMAGE_URL:
        embed.set_image(url=IMAGE_URL)

    pc = world.get("playerCount", 0)

    # --- status bar (description) ---
    if server_online:
        head = "\U0001f7e2 **ONLINE** \u2014 SERVER IS RUNNING"
    else:
        head = "\U0001f534 **OFFLINE** \u2014 SERVER IS DOWN"
    embed.description = (
        f"{head}\n"
        f"*Good luck out there, survivor.*"
    )

    if server_online or world:
        # Row 1
        hour = world.get("hour", 0)
        mins = world.get("minutes", 0)
        period = "AM" if hour < 12 else "PM"
        hh = hour % 12 or 12
        embed.add_field(name="\u23f0 Time", value=f"{hh}:{mins:02d} {period}", inline=True)

        month = world.get("month", 0)
        day = world.get("day", 1)
        mn = MONTH_NAMES[month][:3] if 0 <= month < 12 else "?"
        year = world.get("year")
        date_val = f"{mn} {day}, {year}" if year else f"{mn} {day}"
        embed.add_field(name="\U0001f4c5 Date", value=date_val, inline=True)

        elapsed = world.get("elapsedDays", 0)
        age_raw = world.get("worldAgeDays", 0)
        if elapsed and elapsed > 0:
            age = int(elapsed)
        elif age_raw >= 1:
            age = int(age_raw)
        else:
            age = max(1, int(age_raw))
        embed.add_field(name="\U0001f382 Server Age", value=f"Day {age}", inline=True)

        # Row 2
        embed.add_field(name="\U0001f465 Players", value=f"{pc} / {max_players}", inline=True)

        weather = world.get("weather", "Clear")
        temp = world.get("temperature", 0)
        embed.add_field(name="\U0001f324\ufe0f Weather", value=f"{weather} ({_temp_c_f(temp)})", inline=True)

        ws = world.get("windSpeed", 0)
        if ws > 0.6:
            wd = "Strong"
        elif ws > 0.3:
            wd = "Moderate"
        elif ws > 0.05:
            wd = "Light"
        else:
            wd = "Calm"
        wind_val = wd if ws <= 0.05 else f"{wd} ({ws * 10:.0f} mph)"
        embed.add_field(name="\U0001f4a8 Wind", value=wind_val, inline=True)

        # Row 3
        is_night = world.get("isNight", False)
        cyc_icon = "\U0001f319" if is_night else "\u2600\ufe0f"
        embed.add_field(name=f"{cyc_icon} Cycle", value="Night" if is_night else "Day", inline=True)

        horde_day, horde_status, horde_completed = _horde_fields(horde)
        embed.add_field(name="\U0001f480 Horde", value=horde_day, inline=True)
        embed.add_field(name="\U0001f504 Restart", value="\u2014", inline=True)

        # Row 4
        embed.add_field(name="\U0001f4cb Status", value=horde_status, inline=True)
        embed.add_field(name="\U0001f3c6 Completed", value=horde_completed, inline=True)

        raw_players = str(world.get("players", "") or "")
        names = [n.strip() for n in raw_players.split(",") if n.strip()]
        embed.add_field(name="\U0001f3ae Online Now", value=", ".join(sorted(names)) if names else "None", inline=False)
    else:
        embed.add_field(name="\u200b", value="\U0001f534 **Server Offline**", inline=False)

    footer = "KNOX COUNTY NEVER FORGETS. \u2022 STAY ALIVE."
    if stale:
        footer += " \u2022 Data may be stale"
    embed.set_footer(text=footer)
    embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    return embed


def _horde_fields(horde):
    """Return (horde_day, horde_status, completed) for three inline fields."""
    if not horde:
        return ("—", "Idle", "0")

    phase = horde.get("phase", "")
    event_count = horde.get("eventCount")
    next_day = horde.get("nextHordeDay")

    # Horde day — the Lua scheduler's day counter runs 1 ahead of what
    # players see in-game. Subtract 1 to match the in-game display.
    if next_day is not None:
        horde_day = f"Day {next_day - 1}"
    else:
        horde_day = "—"

    # Status
    if phase == "active":
        horde_status = "\u26a0\ufe0f **ACTIVE**"
    elif phase == "ended":
        horde_status = "Idle"
    elif phase == "scheduled":
        horde_status = "Scheduled"
    elif phase == "status":
        horde_status = "Idle"
    elif phase:
        horde_status = phase.capitalize()
    else:
        horde_status = "Idle"

    # Completed count
    horde_completed = str(event_count) if event_count is not None else "0"

    return (horde_day, horde_status, horde_completed)


# ============================================================================
# Discord Cog
# ============================================================================

def _build_card_fields(world: dict, horde: dict, online: bool, max_players: int) -> list:
    """Map world/horde data into (label, value, subtext) tuples for the card."""
    world = world or {}
    horde = horde or {}
    fields = []

    hour = world.get("hour", 0)
    mins = world.get("minutes", 0)
    period = "AM" if hour < 12 else "PM"
    hh = hour % 12 or 12
    fields.append(("TIME", f"{hh}:{mins:02d} {period}", "In-game"))

    month = world.get("month", 0)
    day = world.get("day", 1)
    mn = MONTH_NAMES[month] if 0 <= month < 12 else "?"
    year = world.get("year")
    date_val = f"{mn} {day}, {year}" if year else f"{mn} {day}"
    fields.append(("DATE", date_val, "In-game"))

    elapsed = world.get("elapsedDays", 0)
    age_raw = world.get("worldAgeDays", 0)
    if elapsed and elapsed > 0:
        age = int(elapsed)
    elif age_raw >= 1:
        age = int(age_raw)
    else:
        age = max(1, int(age_raw))
    fields.append(("SERVER AGE", f"Day {age}", f"Since {mn} {day}"))

    pc = world.get("playerCount", 0)
    raw_players = str(world.get("players", "") or "")
    names = [n.strip() for n in raw_players.split(",") if n.strip()]
    players_sub = ", ".join(sorted(names))[:22] if names else "Online"
    fields.append(("PLAYERS", f"{pc} / {max_players}", players_sub))

    weather = world.get("weather", "Clear")
    temp = world.get("temperature", 0)
    fields.append(("WEATHER", weather, _temp_c_f(temp)))

    ws = world.get("windSpeed", 0)
    if ws > 0.6:
        wdesc = "Strong"
    elif ws > 0.3:
        wdesc = "Moderate"
    elif ws > 0.05:
        wdesc = "Light"
    else:
        wdesc = "Calm"
    fields.append(("WIND", wdesc, f"{ws * 10:.0f} mph"))

    is_night = world.get("isNight", False)
    fields.append(("CYCLE", "Night" if is_night else "Day", f"Day {age}"))

    next_day = horde.get("nextHordeDay")
    phase = horde.get("phase", "")
    if next_day is not None:
        fields.append(("HORDE", f"Day {next_day - 1}", phase.capitalize() if phase else "None detected"))
    else:
        fields.append(("HORDE", "\u2014", "None detected"))

    fields.append(("RESTART", "\u2014", "Host-managed"))

    h_status = "Idle"
    if phase == "active":
        h_status = "ACTIVE"
    elif phase == "scheduled":
        h_status = "Scheduled"
    fields.append(("STATUS", h_status, "Running smoothly" if online else "Offline"))

    event_count = horde.get("eventCount")
    fields.append(("COMPLETED", str(event_count) if event_count is not None else "0", "Events"))

    return fields


class ServerStatusCog(commands.Cog):

    def __init__(self, bot):
        global DASHBOARD_TITLE, ICON_URL, IMAGE_URL
        DASHBOARD_TITLE = getattr(bot.config, 'DASHBOARD_TITLE', 'PZ TAMBAYAN')
        ICON_URL = getattr(bot.config, 'DASHBOARD_ICON_URL', '')
        IMAGE_URL = getattr(bot.config, 'DASHBOARD_BANNER_URL', '')
        self.bot = bot
        self._channel_id = int(os.getenv('STATUS_CHANNEL_ID', '0'))
        self._message_id = None
        self._channel = None
        self._image_path = str(Path(__file__).parent / "status_card.png")
        self._mode = getattr(bot.config, "STATUS_MODE", "embed").strip().lower()

        # Grace period state
        self._rcon_fail_count = 0

        # Last known good data (retained across brief outages)
        self._last_world = None
        self._last_horde = None

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

    async def _send_or_edit_image(self):
        channel = await self._get_channel()
        if not channel:
            return

        def _new_file():
            return discord.File(self._image_path, filename="status.png")

        # Try to edit existing message
        if self._message_id:
            try:
                msg = await channel.fetch_message(self._message_id)
                await msg.edit(attachments=[_new_file()])
                return
            except (discord.NotFound, discord.HTTPException):
                self._message_id = None

        # Search for our previous status message to reuse (after a bot restart)
        try:
            async for msg in channel.history(limit=20):
                if msg.author == self.bot.user and msg.attachments:
                    self._message_id = msg.id
                    await msg.edit(attachments=[_new_file()])
                    print(f"[ServerStatus] Found existing status message: {msg.id}")
                    return
        except discord.HTTPException:
            pass

        # Send new
        try:
            msg = await channel.send(file=_new_file())
            self._message_id = msg.id
            print(f"[ServerStatus] Created status message: {msg.id}")
        except discord.HTTPException as e:
            print(f"[ServerStatus] Failed to send status: {e}")

    async def _send_or_edit_embed(self, embed):
        channel = await self._get_channel()
        if not channel:
            return
        if self._message_id:
            try:
                msg = await channel.fetch_message(self._message_id)
                await msg.edit(embed=embed)
                return
            except (discord.NotFound, discord.HTTPException):
                self._message_id = None
        try:
            async for msg in channel.history(limit=20):
                if msg.author == self.bot.user and msg.embeds:
                    self._message_id = msg.id
                    await msg.edit(embed=embed)
                    print(f"[ServerStatus] Found existing status message: {msg.id}")
                    return
        except discord.HTTPException:
            pass
        try:
            msg = await channel.send(embed=embed)
            self._message_id = msg.id
            print(f"[ServerStatus] Created status message: {msg.id}")
        except discord.HTTPException as e:
            print(f"[ServerStatus] Failed to send status: {e}")

    @tasks.loop(seconds=30)
    async def status_loop(self):
        try:
            rcon_ok = self.bot.rcon.is_server_online()
            world, world_age = await lua_bridge.read_world_status_with_age()
            horde = await lua_bridge.read_horde_status()

            if world_age is not None:
                d = world or {}
                print(f"[ServerStatus] world file age={world_age:.0f}s "
                      f"({'STALE' if world_age > 120 else 'fresh'}) "
                      f"day={d.get('day')} month={d.get('month')} ts={d.get('timestamp')}")

            # online determination with grace period
            world_fresh = _bridge_file_is_fresh(world)
            horde_fresh = _bridge_file_is_fresh(horde)
            bridge_fresh = world_fresh or horde_fresh

            if rcon_ok or bridge_fresh:
                self._rcon_fail_count = 0
                server_online = True
            else:
                self._rcon_fail_count += 1
                server_online = self._rcon_fail_count < OFFLINE_GRACE_COUNT

            if world:
                self._last_world = world
            if horde:
                self._last_horde = horde
            world = world or self._last_world or {}
            horde = horde or self._last_horde or {}

            max_players = await server_config.read_max_players(self.bot, getattr(self.bot.config, "MAX_PLAYERS", 32))

            if self._mode == "image":
                pc = world.get("playerCount", 0)
                fields = _build_card_fields(world, horde, server_online, max_players)
                now = datetime.datetime.now().strftime("%b %d, %I:%M %p")
                status_card.render_status_card(
                    self._image_path,
                    title=DASHBOARD_TITLE,
                    online=server_online,
                    players=f"{pc} / {max_players}",
                    fields=fields,
                    last_update=f"Last update {now} \u2022 Updates every 30 seconds",
                    background=getattr(self.bot.config, "DASHBOARD_BANNER_IMAGE", "") or "",
                )
                await self._send_or_edit_image()
            else:
                stale = server_online and not rcon_ok
                embed = build_embed(server_online, world, horde, False,
                                    stale=stale, max_players=max_players)
                await self._send_or_edit_embed(embed)

        except Exception as e:
            print(f"[ServerStatus] Error in status loop: {e}")

    @status_loop.before_loop
    async def _before_status(self):
        await self.bot.wait_until_ready()
        global ICON_URL, IMAGE_URL, DASHBOARD_TITLE
        cfg = self.bot.config
        ICON_URL = await _resolve_image_url(
            self.bot,
            getattr(cfg, "DASHBOARD_ICON_IMAGE", ""),
            getattr(cfg, "DASHBOARD_ICON_URL", ""),
        )
        IMAGE_URL = await _resolve_image_url(
            self.bot,
            getattr(cfg, "DASHBOARD_BANNER_IMAGE", ""),
            getattr(cfg, "DASHBOARD_BANNER_URL", ""),
        )
        DASHBOARD_TITLE = await server_config.read_server_name(
            self.bot, getattr(cfg, "DASHBOARD_TITLE", "PZ TAMBAYAN")
        )
        await asyncio.sleep(5)
        print("[ServerStatus] Dashboard started")


async def setup(bot):
    await bot.add_cog(ServerStatusCog(bot))
