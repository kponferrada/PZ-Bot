"""
Player Tracker Extension (SFTP).

Tails the PZ server `*_user.txt` log over SFTP for join events and sends
welcome / welcome-back messages to Discord only (in-game join/leave is handled
by PhunServer 2). Also detects player deaths and posts a death notification +
records it in the local SQLite DB.

Log file detection:
  - Looks in SFTP_LOGS_DIR for the newest file ending in _user.txt
  - Handles log rotation (a new file each server session)

Trigger lines:
  "<STEAMID> \"Name\" attempting to join."       -> first-time Discord welcome
  "<STEAMID> \"Name\" fully connected (x,y,z)."  -> Discord welcome / welcome-back
  (death line)                                    -> death notification (see _DEATH_RE)

IMPORTANT: the exact death log line varies by PZ build and may not live in
`_user.txt` at all. Confirm against your live `Zomboid/Logs/` and set _DEATH_RE.
The robust alternative is a small server-side Lua mod that writes a deaths file;
the tail loop is source-agnostic so pointing it at that file is a one-line change.
"""

import asyncio
import datetime
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands, tasks

import sftp_client

_DEFAULT_LOG_DIR = "Logs"

# When a player dies, PZ logs a spurious "left the game" + "fully connected" as the
# character is removed and the player respawns. Treat an immediate leave-then-join
# (within this many seconds) as a respawn and suppress both notifications.
_RESPAWN_WINDOW = 15

# ---- DB ----------------------------------------------------------------------

DB_PATH = Path(__file__).parent / "players.db"


def _db():
    return sqlite3.connect(DB_PATH)


def init_db():
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS players (
                username    TEXT PRIMARY KEY,
                first_seen  TEXT NOT NULL,
                last_seen   TEXT NOT NULL,
                join_count  INTEGER NOT NULL DEFAULT 1,
                death_count INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS deaths (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                username  TEXT NOT NULL,
                died_at   TEXT NOT NULL,
                cause     TEXT
            )
        """)


def upsert_player(username: str) -> bool:
    """Insert or update a player. Returns True if this is their first visit."""
    now = datetime.datetime.utcnow().isoformat()
    with _db() as conn:
        existing = conn.execute(
            "SELECT 1 FROM players WHERE username = ?", (username,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO players (username, first_seen, last_seen, join_count, death_count) "
                "VALUES (?, ?, ?, 1, 0)",
                (username, now, now),
            )
            return True
        conn.execute(
            "UPDATE players SET last_seen = ?, join_count = join_count + 1 WHERE username = ?",
            (now, username),
        )
        return False


def record_death(username: str, cause: Optional[str] = None) -> int:
    """Record a death; return the player's new death count."""
    now = datetime.datetime.utcnow().isoformat()
    with _db() as conn:
        conn.execute(
            "INSERT INTO deaths (username, died_at, cause) VALUES (?, ?, ?)",
            (username, now, cause),
        )
        conn.execute(
            "UPDATE players SET death_count = death_count + 1 WHERE username = ?",
            (username,),
        )
        row = conn.execute(
            "SELECT death_count FROM players WHERE username = ?", (username,)
        ).fetchone()
        return row[0] if row else 0


def is_known_player(username: str) -> bool:
    """True if this username has actually connected before (i.e. a real player).

    PZ has a vanilla bug where entities with a placeholder name (e.g. "Bob")
    that are really animals/zombies still emit `user <name> died` lines. Those
    never go through "fully connected", so they are absent from `players`; we
    use that to filter them out of death notifications.
    """
    with _db() as conn:
        row = conn.execute("SELECT 1 FROM players WHERE username = ?", (username,)).fetchone()
        return row is not None


def get_all_players() -> list:
    with _db() as conn:
        return conn.execute(
            "SELECT username, first_seen, last_seen, join_count, death_count "
            "FROM players ORDER BY join_count DESC"
        ).fetchall()


def _extract_usernames(data: bytes) -> set:
    """Extract known-player names from a PZ SQLite DB.

    Targets the known-player tables specifically (whitelist in the server DB,
    networkPlayers/players in the world DB) so role/capability/other `name`
    columns are not mistaken for players.
    """
    found = set()
    try:
        conn = sqlite3.connect(":memory:")
        conn.deserialize(data)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    except Exception:
        return found

    preferred = {
        "whitelist": ("username", "displayName"),
        "networkplayers": ("username", "name"),
        "players": ("username", "name"),
    }
    for tbl in tables:
        key = tbl.lower()
        if key not in preferred:
            continue
        try:
            cols = [c[1] for c in conn.execute(f'PRAGMA table_info("{tbl}")').fetchall()]
        except sqlite3.OperationalError:
            continue
        col_lookup = {c.lower(): c for c in cols}
        for want in preferred[key]:
            actual = col_lookup.get(want.lower())
            if not actual:
                continue
            try:
                for (v,) in conn.execute(f'SELECT "{actual}" FROM "{tbl}"').fetchall():
                    v = str(v).strip()
                    if v:
                        found.add(v)
            except sqlite3.OperationalError:
                pass
    conn.close()
    return found

# ---- log-line regexes --------------------------------------------------------

_ATTEMPTING_RE = re.compile(r'^\[\S+\s+\S+\]\s+\d+\s+"(.+?)"\s+attempting to join\.')
_CONNECTED_RE = re.compile(r'^\[\S+\s+\S+\]\s+\d+\s+"(.+?)"\s+fully connected \(')

# Leave: <STEAMID> "Name" disconnected. — verify exact wording against your live log if needed.
_DISCONNECTED_RE = re.compile(r'^\[\S+\s+\S+\]\s+\d+\s+"(.+?)"\s+(?:disconnected|left the game|timed out)')

# PZ Build 42 logs a death to `_user.txt` as:
#   [date time] user <Name> died at (x, y, z) (non pvp|pvp)
# The name is captured without surrounding quotes. If a death still isn't
# detected, paste the raw `_user.txt` death line and adjust this regex.
_DEATH_RE: Optional[re.Pattern] = re.compile(
    r'^\[\S+\s+\S+\]\s+user\s+"?([^"]+?)"?\s+died\b'
    r'(?:\s+at\s+\(\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\))?'
    r'(?:\s*\(([^)]*)\))?',
    re.IGNORECASE,
)


# ---- Cog ---------------------------------------------------------------------

class PlayerTrackerCog(commands.Cog):

    def __init__(self, bot):
        self.bot = bot

        # Remote Logs folder (over SFTP).
        self._log_dir: str = getattr(bot.config, "SFTP_LOGS_DIR", None) or _DEFAULT_LOG_DIR

        # Tail state
        self._current_log: Optional[str] = None
        self._file_pos: int = 0
        self._seeded = False
        self._last_seed_attempt = 0.0
        self._pending_leave: dict = {}

        init_db()
        self._tail_user_log.start()

    def cog_unload(self):
        self._tail_user_log.cancel()

    # ---- file helpers --------------------------------------------------------

    async def _find_latest_user_log(self) -> Optional[str]:
        sftp = sftp_client.get()
        return await sftp.newest_matching(self._log_dir, "*_user.txt")

    # ---- delayed-action helpers ----------------------------------------------

    async def _handle_death(self, name: str, details: Optional[dict] = None):
        """Post a death notification to the death-logs Discord channel (no in-game broadcast)."""
        if not is_known_player(name):
            print(f"[PlayerTracker] Skipping death for unknown entity {name!r} (vanilla-bug false positive)")
            return
        details = details or {}
        death_count = record_death(name, details.get("cause"))
        if not self.bot.features.is_enabled("deaths"):
            print(f"[PlayerTracker] Death -> {name} (#{death_count}) (notifications disabled)")
            return
        channel = self.bot.get_death_logs_channel()
        if channel:
            embed = discord.Embed(
                title=f"\u2620\ufe0f **{name}** has died! (death #{death_count})",
                colour=discord.Colour.red(),
            )
            if details.get("location"):
                embed.add_field(name="Location", value=details["location"], inline=True)
            if details.get("pvp") is not None:
                embed.add_field(name="Cause", value=("Player kill" if details["pvp"] else "PvE"), inline=True)
            try:
                await channel.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException) as e:
                print(f"[PlayerTracker] Failed to send death log: {e}")
        print(f"[PlayerTracker] Death -> {name} (#{death_count})")

    async def _delayed_leave(self, name: str) -> None:
        """Send a leave notification after the respawn window, unless the player
        rejoined in the meantime (an immediate leave-then-join is a death respawn)."""
        try:
            await asyncio.sleep(_RESPAWN_WINDOW)
            if self._pending_leave.pop(name, None) is None:
                return  # a join followed -> respawn, leave notification suppressed
            if self.bot.features.is_enabled("join_leave"):
                await self.bot.send_notification_to(
                    self.bot.get_join_leave_channel(),
                    f"{self.bot.Emojis.SPIFFO_WAVE} **{name}**'s signal was lost.",
                    discord.Colour.dark_grey(),
                )
            print(f"[PlayerTracker] Leave -> {name}")
        except Exception as e:
            print(f"[PlayerTracker] delayed leave error: {e}")


    # ---- main tail loop ------------------------------------------------------

    @tasks.loop(seconds=2.0)
    async def _tail_user_log(self):
        try:
            if not self._seeded and time.time() - self._last_seed_attempt > 300:
                self._last_seed_attempt = time.time()
                await self._seed_known_players()
            sftp = sftp_client.get()
            log_file = await self._find_latest_user_log()
            if not log_file:
                return

            # Log rotation: new file detected — seek to its end.
            if log_file != self._current_log:
                self._current_log = log_file
                st = await sftp.stat(log_file)
                self._file_pos = st[0] if st else 0
                print(f"[PlayerTracker] Now tailing: {log_file} (from {self._file_pos})")
                return

            # Read any new bytes.
            try:
                text, self._file_pos = await sftp.tail(log_file, self._file_pos)
            except sftp_client.SftpError:
                return
            if not text:
                return

            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue

                # "fully connected" -> Discord join notification
                m = _CONNECTED_RE.match(line)
                if m:
                    name = m.group(1)
                    if self._pending_leave.pop(name, None) is not None:
                        print(f"[PlayerTracker] Join -> {name} (respawn after leave, suppressed)")
                        continue
                    is_new = upsert_player(name)
                    rank_cog = self.bot.get_cog("RankSync")
                    if rank_cog:
                        await rank_cog.sync_by_pz_username(name)
                    if is_new:
                        if self.bot.features.is_enabled("join_leave"):
                            await self.bot.send_notification_to(
                                self.bot.get_join_leave_channel(),
                                f"{self.bot.Emojis.SPIFFO_WAVE} **{name}**'s signal was found!",
                                discord.Colour.blue(),
                            )
                    else:
                        if self.bot.features.is_enabled("join_leave"):
                            await self.bot.send_notification_to(
                                self.bot.get_join_leave_channel(),
                                f"{self.bot.Emojis.HAPPY} **{name}**'s signal is back.",
                                discord.Colour.green(),
                            )
                    print(f"[PlayerTracker] Join -> {name} ({'new' if is_new else 'returning'})")
                    continue

                # "disconnected" -> Discord leave notification
                m = _DISCONNECTED_RE.match(line)
                if m:
                    name = m.group(1)
                    self._pending_leave[name] = time.time()
                    asyncio.create_task(self._delayed_leave(name))
                    continue

                # Death -> death notification (to the death-logs channel).
                # The Death Log mod (death_log.py) is the rich source when present;
                # otherwise fall back to this vanilla line (name + location + pvp).
                if _DEATH_RE and not self.bot.state.death_log_active:
                    m = _DEATH_RE.match(line)
                    if m:
                        name = m.group(1)
                        details = {}
                        if m.group(2) is not None:
                            details["location"] = f"X: {m.group(2)}, Y: {m.group(3)}"
                        if m.group(5) is not None:
                            details["pvp"] = "pvp" in m.group(5).lower()
                        asyncio.ensure_future(self._handle_death(name, details))
                        continue
                    if "died" in line.lower():
                        print(f"[PlayerTracker] Unmatched death line: {line}")

        except Exception as e:
            print(f"[PlayerTracker] Tail error: {e}")

    async def _read_server_known_players(self) -> set:
        """Read the game server's player database (over SFTP) for known players.

        Path comes from SFTP_SERVER_DB (default /server-data/db/pzserver.db on
        Indifferent Broccoli). Falls back to the vanilla PZ world-player DBs
        (<root>/Saves/Multiplayer/<world>/players.db) if that file is absent.
        """
        known = set()
        sftp = sftp_client.get()
        root = getattr(self.bot.config, "SFTP_ZOMBOID_ROOT", None) or "/server-data"
        db = getattr(self.bot.config, "SFTP_SERVER_DB", "") or f"{root.rstrip('/')}/db/pzserver.db"

        candidates = [db]
        try:
            mp = f"{root.rstrip('/')}/Saves/Multiplayer"
            for name in await sftp.list_dir(mp):
                candidates.append(f"{mp}/{name}/players.db")
        except sftp_client.SftpError:
            pass

        for pdb in candidates:
            try:
                if not await sftp.exists(pdb):
                    continue
                data = await sftp.read_bytes(pdb)
            except sftp_client.SftpError as e:
                print(f"[PlayerTracker] Cannot read {pdb}: {e}")
                continue
            found = _extract_usernames(data)
            if found:
                known |= found
                print(f"[PlayerTracker] Found {len(found)} known player(s) in {pdb}")
        return known

    async def _seed_known_players(self):
        known = await self._read_server_known_players()
        if not known:
            print("[PlayerTracker] No known players found on the server; starting with an empty list")
            return
        now = datetime.datetime.utcnow().isoformat()
        with _db() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO players (username, first_seen, last_seen, join_count, death_count) "
                "VALUES (?, ?, ?, 1, 0)",
                [(u, now, now) for u in known],
            )
        self._seeded = True
        print(f"[PlayerTracker] Seeded {len(known)} known player(s) from the server's players.db")

    @_tail_user_log.before_loop
    async def _before_tail(self):
        await self.bot.wait_until_ready()
        await self._seed_known_players()
        print(f"[PlayerTracker] Started — watching {self._log_dir} for *_user.txt over SFTP")


async def setup(bot):
    await bot.add_cog(PlayerTrackerCog(bot))
