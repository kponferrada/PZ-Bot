"""
PZ Tambayan Discord Bot — remote server monitor & Jeeves bridge.

A slim, remote-only refactor of JeevesBot. Talks to the Project Zomboid server
over **RCON** (commands) and **SFTP** (file bridge to the Jeeves mods).

Deliberately has NO process control: it does not start, stop, or restart the
game server process. Starting a stopped server and running SteamCMD are
same-server operations that stay on the host panel. The bot monitors, reports,
and drives the Jeeves mod bridge.

Retained features: player tracking + welcome/death notes, the world/player
status dashboard, chat relay, rank sync, horde/drop events, playsound, and
/modlist. Removed: server lifecycle, auto-restart, SteamCMD /update, mod
add/remove, and crash-recovery.
"""

import os
import sys
import asyncio
import socket
import time
from pathlib import Path
from typing import Optional, List, Dict, Tuple, Set

# ---- load config.env ---------------------------------------------------------

try:
    from dotenv import load_dotenv
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    _config_path = os.path.join(_script_dir, "config.env")
    if os.path.isfile(_config_path):
        load_dotenv(_config_path)
        print(f"Loaded {_config_path}")
    else:
        print(f"WARNING: config.env not found ({_config_path})")
except ImportError:
    print("WARNING: python-dotenv not installed, using system env vars only.")

import discord
from discord import app_commands
from discord.ext import commands, tasks

try:
    import rcon.source
    from rcon.source import Client
except ImportError:
    sys.exit("ERROR: rcon package not installed. Install with: pip install rcon")

import lua_bridge
import sftp_client


# =============================================================================
# CONFIGURATION
# =============================================================================

def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _env_int(key: str, default: int = 0) -> int:
    return int(os.getenv(key, str(default)))


def _join(root: str, sub: str) -> str:
    if not root:
        return ""
    return f"{root.rstrip('/')}/{sub}"


class Config:
    """Bot and server configuration (all remote)."""

    def __init__(self):
        # Discord
        self.TOKEN = _env("DISCORD_TOKEN")
        self.CHANNEL_ID = _env_int("DISCORD_CHANNEL_ID")
        self.GUILD_ID = _env_int("DISCORD_GUILD_ID")

        # RCON (unchanged from Jeeves)
        self.RCON_HOST = _env("RCON_HOST", "127.0.0.1")
        self.RCON_PORT = _env_int("RCON_PORT", 27015)
        self.RCON_PASSWORD = _env("RCON_PASSWORD")

        # Roles
        self.DEFAULT_ROLE = _env("DEFAULT_ROLE", "Admin")
        self.RANKS = {i: _env(f"RANK_{i}", f"Rank {i}") for i in range(1, 7)}

        # Announcement banners (local file paths, relative to the bot folder)
        self.ANNOUNCE_UP_IMAGE = _env("ANNOUNCE_UP_IMAGE", "assets/server-up.png")
        self.ANNOUNCE_RESTART_IMAGE = _env("ANNOUNCE_RESTART_IMAGE", "assets/server-restarting.png")
        self.ANNOUNCE_MOD_UPDATE_IMAGE = _env("ANNOUNCE_MOD_UPDATE_IMAGE", "assets/server-mod-update.png")
        self.ANNOUNCE_DOWN_IMAGE = _env("ANNOUNCE_DOWN_IMAGE", "assets/server-down.png")

        # Role to @-mention in every status announcement (0 = disabled)
        self.NOTIFY_ROLE_ID = _env_int("NOTIFY_ROLE_ID", 0)

        # Dashboard
        self.DASHBOARD_TITLE = _env("DASHBOARD_TITLE", "PZ TAMBAYAN")
        self.DASHBOARD_ICON_URL = _env("DASHBOARD_ICON_URL", "")
        self.DASHBOARD_BANNER_URL = _env("DASHBOARD_BANNER_URL", "")
        self.DASHBOARD_ICON_IMAGE = _env("DASHBOARD_ICON_IMAGE", "assets/profile-pic.png")
        self.DASHBOARD_BANNER_IMAGE = _env("DASHBOARD_BANNER_IMAGE", "assets/profile-banner.png")
        self.MAX_PLAYERS = _env_int("MAX_PLAYERS", 32)
        self.STATUS_MODE = _env("STATUS_MODE", "embed").strip().lower()

        # SFTP (NEW — remote server paths)
        self.SFTP_HOST = _env("SFTP_HOST")
        self.SFTP_PORT = _env_int("SFTP_PORT", 22)
        self.SFTP_USER = _env("SFTP_USER")
        self.SFTP_PASSWORD = _env("SFTP_PASSWORD")
        self.SFTP_KEY_PATH = _env("SFTP_KEY_PATH")
        self.SFTP_ZOMBOID_ROOT = _env("SFTP_ZOMBOID_ROOT")
        self.SFTP_LUA_DIR = _env("SFTP_LUA_DIR") or _join(self.SFTP_ZOMBOID_ROOT, "Lua")
        self.SFTP_LOGS_DIR = _env("SFTP_LOGS_DIR") or _join(self.SFTP_ZOMBOID_ROOT, "Logs")
        self.SFTP_SERVER_INI = _env("SFTP_SERVER_INI")
        self.SFTP_MODS_DIR = _env("SFTP_MODS_DIR")

        print(f"Config loaded: RCON={self.RCON_HOST}:{self.RCON_PORT} "
              f"Guild={self.GUILD_ID} Channel={self.CHANNEL_ID} "
              f"SFTP={self.SFTP_HOST}:{self.SFTP_PORT}")

    def validate(self) -> List[str]:
        errors = []
        if not self.TOKEN:
            errors.append("DISCORD_TOKEN is not set")
        if not self.CHANNEL_ID:
            errors.append("DISCORD_CHANNEL_ID is not set")
        if not self.GUILD_ID:
            errors.append("DISCORD_GUILD_ID is not set")
        if not self.RCON_PASSWORD:
            errors.append("RCON_PASSWORD is not set")
        if not self.SFTP_HOST:
            errors.append("SFTP_HOST is not set")
        if not self.SFTP_USER:
            errors.append("SFTP_USER is not set")
        if not self.SFTP_PASSWORD and not self.SFTP_KEY_PATH:
            errors.append("SFTP_PASSWORD or SFTP_KEY_PATH is not set")
        return errors


# =============================================================================
# CUSTOM EMOJIS
# =============================================================================

class Emojis:
    HAPPY          = _env("EMOJI_HAPPY")          or "\U0001f7e2"   # 🟢
    DIZZY          = _env("EMOJI_DIZZY")          or "\U0001f635"   # 😵
    PANIC          = _env("EMOJI_PANIC")          or "\U0001f534"   # 🔴
    ANGRY          = _env("EMOJI_ANGRY")          or "\u26d4"       # ⛔
    JEEVES         = _env("EMOJI_JEEVES")         or "\U0001f9d1"   # 🧑
    SPIFFO_POP     = _env("EMOJI_SPIFFO_POP")     or "\U0001f389"   # 🎉
    SPIFFO_WAVE    = _env("EMOJI_SPIFFO_WAVE")    or "\U0001f44b"   # 👋
    SPIFFO_EDUCATE = _env("EMOJI_SPIFFO_EDUCATE") or "\u2757"       # ❗
    SPIFFO_KATANA  = _env("EMOJI_SPIFFO_KATANA")  or "\u26a0\ufe0f" # ⚠️
    SPIFFO_STOP    = _env("EMOJI_SPIFFO_STOP")    or "\U0001f6d1"   # 🛑


# =============================================================================
# SERVER STATE
# =============================================================================

class ServerState:
    def __init__(self):
        self.server_ready = False        # bot is monitoring (set True on startup)
        self.skip_next_restart = False   # inert — restart scheduling is host-managed
        self.players_online = False
        self.player_count = 0
        self.player_names: Set[str] = set()
        self.last_rcon_ok = False
        self.last_alive_ts: float = 0.0
        self.alive_source: str = "none"

    def mark_alive(self, source: str) -> None:
        """Record that the server was observably alive just now."""
        self.last_alive_ts = time.time()
        self.alive_source = source

    def seconds_since_alive(self) -> Optional[float]:
        if self.last_alive_ts <= 0:
            return None
        return max(0.0, time.time() - self.last_alive_ts)


# =============================================================================
# RCON HELPER
# =============================================================================

class RCONHelper:
    def __init__(self, config: Config):
        self.host = config.RCON_HOST
        self.port = config.RCON_PORT
        self.password = config.RCON_PASSWORD

    async def send_command(self, command: str, timeout: int = 10) -> Optional[str]:
        try:
            return await asyncio.wait_for(
                rcon.source.rcon(command, host=self.host, port=self.port, passwd=self.password),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            print(f"RCON timeout ({timeout}s): {command}")
            return None
        except (socket.timeout, ConnectionRefusedError, OSError) as e:
            print(f"RCON error: {e}")
            return None

    async def broadcast(self, message: str) -> None:
        """Red-alert servermsg + alert sound via the Lua bridge."""
        await self.send_command(f'servermsg "{message}"')
        await lua_bridge.broadcast(message, sound_only=True)

    async def save_and_quit(self) -> None:
        await self.send_command("save")
        await asyncio.sleep(8)
        await self.send_command("quit")

    def is_server_online(self, timeout: int = 5) -> bool:
        try:
            with Client(self.host, self.port, passwd=self.password, timeout=timeout) as client:
                client.run("players")
                return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            return False

    @staticmethod
    def parse_players(response: str) -> Tuple[Set[str], int]:
        """Parse player names and count from RCON 'players' output."""
        names: Set[str] = set()
        count = 0
        if not response:
            return names, count
        for line in response.splitlines():
            stripped = line.strip()
            if stripped.startswith("-"):
                name = stripped.lstrip("-").strip()
                if name:
                    names.add(name)
            elif "Players connected (" in stripped:
                try:
                    count = int(stripped[stripped.index("(") + 1:stripped.index(")")])
                except (ValueError, IndexError):
                    pass
        return names, count


# =============================================================================
# DISCORD BOT
# =============================================================================

class PZBot(commands.Bot):
    Emojis = Emojis

    def __init__(self, config: Config):
        super().__init__(command_prefix="!", intents=discord.Intents.all())
        self.config = config
        self.state = ServerState()
        self.rcon = RCONHelper(config)
        self._was_online = None

    async def setup_hook(self) -> None:
        guild = discord.Object(id=self.config.GUILD_ID)

        # Initialize the SFTP client and the Lua bridge (must happen before extensions load).
        sftp_client.init(
            host=self.config.SFTP_HOST,
            port=self.config.SFTP_PORT,
            username=self.config.SFTP_USER,
            password=self.config.SFTP_PASSWORD,
            key_path=self.config.SFTP_KEY_PATH,
        )
        lua_bridge.init(self)

        for ext in ("player_tracker", "rank_sync", "chat_relay", "horde_events",
                    "jeeves_drops", "jeeves_modmanager", "server_status", "horde_leaderboard",
                    "restart_watch"):
            try:
                await self.load_extension(ext)
                print(f"Loaded {ext}")
            except Exception as e:
                print(f"Failed to load {ext}: {e}")
                import traceback
                traceback.print_exc()

        self.tree.copy_global_to(guild=guild)
        try:
            await self.tree.sync(guild=guild)
            print(f"Commands synced to guild {guild.id}")
        except discord.Forbidden as e:
            print(f"WARNING: Could not sync slash commands — {e}")
            print("  The bot is likely missing the 'applications.commands' scope.")
            print("  Re-invite it with an OAuth2 URL that includes BOTH 'bot' AND 'applications.commands'.")
        except discord.HTTPException as e:
            print(f"WARNING: Could not sync slash commands — {e}")

    def get_notification_channel(self) -> Optional[discord.TextChannel]:
        return self.get_channel(self.config.CHANNEL_ID)

    async def send_notification(self, title: str, colour: discord.Colour = discord.Colour.purple(),
                                description: Optional[str] = None) -> None:
        channel = self.get_notification_channel()
        if not channel:
            print(f"Warning: notification channel {self.config.CHANNEL_ID} not found")
            return
        try:
            await channel.send(embed=discord.Embed(title=title, colour=colour, description=description))
        except discord.Forbidden:
            print(f"Warning: Missing 'Send Messages' permission in notification channel "
                  f"{self.config.CHANNEL_ID}. Re-invite the bot with 'Send Messages' and 'Embed Links'.")
        except discord.HTTPException as e:
            print(f"Warning: failed to send notification: {e}")

    async def send_banner(self, image_path: str, caption: str = None) -> None:
        """Send a banner image (local file) + @-mention to the notification channel."""
        channel = self.get_notification_channel()
        if not channel:
            print(f"[Announce] No notification channel for banner: {image_path}")
            return
        path = Path(image_path)
        if not path.is_absolute():
            path = Path(__file__).parent / path
        mention = f"<@&{self.config.NOTIFY_ROLE_ID}> " if self.config.NOTIFY_ROLE_ID else ""
        content = (mention + (caption or "")).strip() or None
        if not path.is_file():
            print(f"[Announce] Banner image not found: {image_path} — falling back to text")
            if content:
                await channel.send(content=content)
            else:
                await self.send_notification(caption or "Announcement", discord.Colour.purple())
            return
        try:
            await channel.send(content=content, file=discord.File(str(path)))
            print(f"[Announce] Sent banner: {image_path}")
        except Exception as e:
            print(f"[Announce] Failed to send banner {image_path}: {e}")

    # ---- Monitoring ----------------------------------------------------------

    async def poll_players(self) -> Optional[str]:
        """Update player state, preferring the mod bridge and falling back to RCON.

        The mod's `jeeves_world_status.txt` (written by `getOnlinePlayers()`) is the
        authoritative roster; RCON `players` is the English-prose fallback that has to
        be re-parsed. A fresh status file also proves the game loop is ticking, which
        RCON alone cannot.
        """
        status, age = await lua_bridge.read_world_status_with_age()

        if (status is not None and age is not None
                and age <= 90.0
                and status.get("playerCount") is not None):
            try:
                count = int(status["playerCount"])
            except (TypeError, ValueError):
                count = None
            if count is not None:
                raw = str(status.get("players", "") or "")
                names = {n.strip() for n in raw.split(",") if n.strip()}
                self.state.player_names = names
                self.state.player_count = count
                self.state.players_online = count > 0
                self.state.mark_alive("bridge")
                return None

        response = await self.rcon.send_command("players")
        if response is not None:
            self.state.last_rcon_ok = True
            self.state.player_names, self.state.player_count = self.rcon.parse_players(response)
            self.state.players_online = self.state.player_count > 0
            self.state.mark_alive("rcon")
        else:
            self.state.last_rcon_ok = False
        return response

    @tasks.loop(seconds=30)
    async def monitor_server_state(self):
        """Announce server up/down transitions with the configured banners."""
        online = self.rcon.is_server_online()
        prev = self._was_online
        if prev is not None:
            if online and not prev:
                await self.send_banner(self.config.ANNOUNCE_UP_IMAGE, f"{Emojis.HAPPY} Server is back online!")
                print("[Announce] Server UP transition")
            elif not online and prev:
                await self.send_banner(self.config.ANNOUNCE_DOWN_IMAGE, f"{Emojis.PANIC} Server went offline!")
                print("[Announce] Server DOWN transition")
        self._was_online = online

    @monitor_server_state.before_loop
    async def _before_monitor(self):
        await self.wait_until_ready()


# =============================================================================
# BOT INSTANCE
# =============================================================================

print("=" * 50)
print("PZ Tambayan Discord Bot starting...")
print("=" * 50)

config = Config()
errors = config.validate()
if errors:
    for e in errors:
        print(f"  ❌ {e}")
    sys.exit(1)
print("\n✅ Configuration validated successfully!\n")

bot = PZBot(config)


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    # The bot monitors regardless of server state; SFTP connects lazily on first use.
    bot.state.server_ready = True
    if not bot.monitor_server_state.is_running():
        bot.monitor_server_state.start()
    await bot.send_notification(f"{Emojis.JEEVES} Bot online — monitoring the server...", discord.Colour.purple())
    if bot.rcon.is_server_online(timeout=10):
        bot._was_online = True
        await bot.send_banner(bot.config.ANNOUNCE_UP_IMAGE, f"{Emojis.HAPPY} Server is Online!")
    else:
        bot._was_online = False
        await bot.send_banner(
            bot.config.ANNOUNCE_DOWN_IMAGE,
            f"{Emojis.PANIC} Server appears Offline (RCON unreachable). "
            "Use the host panel to start it.")


# =============================================================================
# ROLE CHECKS
# =============================================================================

def require_role(role_name: str):
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False
        role = discord.utils.get(interaction.guild.roles, name=role_name)
        if role is None or role not in interaction.user.roles:
            raise app_commands.MissingRole(role_name)
        return True
    return app_commands.check(predicate)


async def _send_error(interaction: discord.Interaction, embed: discord.Embed) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.NotFound:
        pass
    except discord.HTTPException:
        pass


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.MissingRole):
        embed = discord.Embed(
            title=f"{Emojis.ANGRY} Permission Denied",
            description=f"You need the **{error.missing_role}** role to use this command.",
            colour=discord.Colour.red())
    elif isinstance(error, app_commands.CheckFailure):
        embed = discord.Embed(
            title=f"{Emojis.ANGRY} Permission Denied",
            description="You don't have permission to use this command.",
            colour=discord.Colour.red())
    else:
        print(f"[CommandError] {type(error).__name__}: {error}")
        embed = discord.Embed(
            title=f"{Emojis.PANIC} An error occurred", description=str(error),
            colour=discord.Colour.red())
    await _send_error(interaction, embed)


async def _respond(interaction: discord.Interaction, title: str,
                   colour: discord.Colour = discord.Colour.purple(),
                   ephemeral: bool = True, description: str = None) -> None:
    embed = discord.Embed(title=title, colour=colour, description=description)
    await interaction.response.send_message(embed=embed, ephemeral=ephemeral)


# =============================================================================
# SLASH COMMANDS
# =============================================================================

@bot.tree.command(name="hello", description="Health check — the bot replies if it is alive.")
@require_role(config.DEFAULT_ROLE)
async def cmd_hello(interaction: discord.Interaction) -> None:
    await _respond(interaction, "Hello!")


@bot.tree.command(name="online", description="Checks if the game server is online.")
@require_role(config.DEFAULT_ROLE)
async def cmd_online(interaction: discord.Interaction) -> None:
    if bot.rcon.is_server_online():
        await bot.send_notification(f"{Emojis.HAPPY} Server is Online!", discord.Colour.green())
    else:
        await bot.send_notification(f"{Emojis.PANIC} Server is Offline!", discord.Colour.red())
    await _respond(interaction, "Check complete — see the notification channel.")


@bot.tree.command(name="players", description="List players currently connected.")
@require_role(config.DEFAULT_ROLE)
async def cmd_players(interaction: discord.Interaction) -> None:
    response = await bot.rcon.send_command("players")
    await _respond(interaction, "Connected players", description=response or "Failed to get player list")


@bot.tree.command(name="playerlist", description="Show everyone who has ever joined.")
@require_role(config.DEFAULT_ROLE)
async def cmd_playerlist(interaction: discord.Interaction) -> None:
    from player_tracker import get_all_players
    players = get_all_players()
    if not players:
        await _respond(interaction, "No players recorded yet.")
        return
    lines = [f"**{u}** — {c} session(s), {d} death(s), first seen {f[:10]}"
             for u, f, _, c, d in players[:25]]
    desc = "\n".join(lines)
    if len(players) > 25:
        desc += f"\n\n*...and {len(players) - 25} more*"
    await _respond(interaction, f"Player Database ({len(players)} total)", description=desc)


@bot.tree.command(name="teleport", description="Teleport player1 to player2's location.")
@require_role(config.DEFAULT_ROLE)
async def cmd_teleport(interaction: discord.Interaction, player1: str, player2: str) -> None:
    await bot.rcon.send_command(f'teleport ("{player1}", "{player2}")')
    await _respond(interaction, f"Attempting to teleport {player1} to {player2}.")


@bot.tree.command(name="setaccesslevel", description="Set a player's server access level.")
@require_role(config.DEFAULT_ROLE)
async def cmd_setaccesslevel(interaction: discord.Interaction, player: str, level: str) -> None:
    valid_levels = ["admin", "moderator", "overseer", "gm", "observer", "none"]
    if level.lower() not in valid_levels:
        await _respond(interaction, f"Invalid access level: {level}. Valid: {', '.join(valid_levels)}",
                       discord.Colour.red())
        return
    await bot.rcon.send_command(f'setaccesslevel "{player}" "{level.lower()}"')
    await _respond(interaction, f"Set {player}'s access level to **{level.lower()}**.")


@cmd_setaccesslevel.autocomplete("level")
async def _accesslevel_autocomplete(interaction: discord.Interaction, current: str):
    levels = ["admin", "moderator", "overseer", "gm", "observer", "none"]
    return [app_commands.Choice(name=lvl, value=lvl) for lvl in levels if current.lower() in lvl.lower()]


@bot.tree.command(name="msg", description="Broadcast a message to the server.")
@require_role(config.DEFAULT_ROLE)
async def cmd_msg(interaction: discord.Interaction, message: str) -> None:
    await interaction.response.defer(ephemeral=True)
    await bot.rcon.broadcast(message)
    await interaction.followup.send(embed=discord.Embed(title="Message sent", colour=discord.Colour.purple()),
                                    ephemeral=True)


@bot.tree.command(name="stop", description="Gracefully shut the game server down (RCON save + quit).")
@require_role(config.DEFAULT_ROLE)
async def cmd_stop(interaction: discord.Interaction) -> None:
    await _respond(interaction, "Saving and shutting down via RCON...", ephemeral=False)
    await bot.rcon.save_and_quit()
    await bot.send_notification(
        f"{Emojis.PANIC} Shutdown command sent (RCON save + quit). Start it again via the host panel.",
        discord.Colour.red())


@bot.tree.command(name="announce", description="Post a server announcement banner.")
@require_role(config.DEFAULT_ROLE)
@app_commands.choices(kind=[
    app_commands.Choice(name="Server Up", value="up"),
    app_commands.Choice(name="Server Restarting", value="restarting"),
    app_commands.Choice(name="Server Down", value="down"),
    app_commands.Choice(name="Mod Update", value="modupdate"),
])
async def cmd_announce(interaction: discord.Interaction, kind: app_commands.Choice[str]) -> None:
    banners = {
        "up": (bot.config.ANNOUNCE_UP_IMAGE, f"{Emojis.HAPPY} Server is up!"),
        "restarting": (bot.config.ANNOUNCE_RESTART_IMAGE, f"{Emojis.SPIFFO_STOP} Server restarting — find shelter!"),
        "down": (bot.config.ANNOUNCE_DOWN_IMAGE, f"{Emojis.PANIC} Server is down!"),
        "modupdate": (bot.config.ANNOUNCE_MOD_UPDATE_IMAGE, f"{Emojis.SPIFFO_EDUCATE} Mod update — server restarting soon!"),
    }
    image, caption = banners[kind.value]
    await _respond(interaction, f"Posting {kind.name} announcement...", ephemeral=False)
    await bot.send_banner(image, caption)


@bot.tree.command(name="playsound", description="Trigger a Jeeves Alerts sound on all connected players.")
@require_role(config.DEFAULT_ROLE)
@app_commands.choices(sound=[
    app_commands.Choice(name="Alarm 1", value="1"),
    app_commands.Choice(name="Alarm 2", value="2"),
    app_commands.Choice(name="Alarm 3", value="3"),
    app_commands.Choice(name="Alarm 4", value="4"),
    app_commands.Choice(name="Alarm 5", value="5"),
    app_commands.Choice(name="Ambient Creak", value="6"),
    app_commands.Choice(name="Ambient Keyboard", value="7"),
    app_commands.Choice(name="Chime Peaceful", value="8"),
    app_commands.Choice(name="Chime Sword", value="9"),
    app_commands.Choice(name="Chime Walkie", value="10"),
    app_commands.Choice(name="Chime Wrong", value="11"),
])
async def cmd_playsound(interaction: discord.Interaction, sound: app_commands.Choice[str], message: str = None) -> None:
    await interaction.response.defer(ephemeral=True)
    success = await lua_bridge.playsound(int(sound.value), message)
    desc = f"Sound: **{sound.name}**"
    if message:
        desc += f"\nMessage: *{message}*"
    colour = discord.Colour.purple() if success else discord.Colour.red()
    title = "\U0001f50a Sound triggered!" if success else "Failed to trigger sound"
    await interaction.followup.send(embed=discord.Embed(title=title, description=desc, colour=colour),
                                    ephemeral=True)


# =============================================================================
# RANK COMMAND
# =============================================================================

_RANK_INFO = {
    0: ("Default", "No color", "\u2b1c"),
    1: ("Fuel", "Green", "\U0001f7e9"),
    2: ("Spark", "Blue", "\U0001f7e6"),
    3: ("Cinder", "Violet", "\U0001f7ea"),
    4: ("Flame", "Yellow", "\U0001f7e8"),
    5: ("Blaze", "Orange", "\U0001f7e7"),
    6: ("Inferno", "Red", "\U0001f7e5"),
}


@bot.tree.command(name="myrank", description="Show your current in-game rank and chat color.")
async def cmd_myrank(interaction: discord.Interaction) -> None:
    highest = 0
    for role in interaction.user.roles:
        for rank_num, rank_role in config.RANKS.items():
            if role.name == rank_role and rank_num > highest:
                highest = rank_num
    name, color, emoji = _RANK_INFO.get(highest, ("Unknown", "None", "\u2753"))
    await _respond(
        interaction,
        f"{emoji} {name} — Rank {highest}",
        description=f"Your in-game chat name color: **{color}**\n"
                    f"Your rank shows in-game as your chat name color.")


# =============================================================================
# ENTRY POINT
# =============================================================================

LOCK_FILE = Path(__file__).parent / "jeeves.lock"


def enforce_single_instance() -> None:
    my_pid = os.getpid()
    if LOCK_FILE.exists():
        try:
            old_pid = int(LOCK_FILE.read_text().strip())
        except (ValueError, OSError):
            old_pid = None
        if old_pid and old_pid != my_pid:
            # Check if the old PID is still alive (bot-local, same VPS).
            try:
                os.kill(old_pid, 0)
                print(f"[SingleInstance] Killing previous instance (PID {old_pid})...")
                import signal
                os.kill(old_pid, signal.SIGTERM)
                time.sleep(2)
            except ProcessLookupError:
                print(f"[SingleInstance] Stale lock file (PID {old_pid} gone).")
            except PermissionError:
                print(f"[SingleInstance] Cannot kill PID {old_pid} (permission denied).")
    try:
        LOCK_FILE.write_text(str(my_pid))
    except OSError as e:
        print(f"[SingleInstance] Warning: {e}")


if __name__ == "__main__":
    enforce_single_instance()
    try:
        print("Starting bot...")
        bot.run(config.TOKEN)
    finally:
        try:
            LOCK_FILE.unlink(missing_ok=True)
        except OSError:
            pass
