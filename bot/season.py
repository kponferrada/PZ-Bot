"""season.py — start a new season: archive the bot's season data, then start it empty.

`/seasonreset season:<new name> confirm:RESET SEASON` (admin) copies everything
the bot saved during the season into `archive/<time>-<old season>/`, then empties it:

  - deaths.json            the death log (day/week counts and the death card's
                           registry number, BT-DC-<date>-<n>, which starts at 1 again)
  - players.db             known players (so everyone's first join is "new" again)
  - whitelist_requests.csv the whitelist requests (header kept)
  - rank_links.json        every Discord-to-PZ link (RankSync.unlink_all, which also
                           takes the rank roles the links gave)

With `whitelist_role:True` the whitelist role is also taken from everyone who has it.
Server accounts, Aegis stats and the BT export live on the game server and are
not touched (a server wipe resets those). Bot settings (feature toggles, restart
and mod-update state, the 24-hour player graph) are not season data and stay.

`season_state.json` records the current season and the archived ones. After a
reset `fresh` is set, and player_tracker no longer seeds known players from the
server, so last season's players are not counted as returning.
"""
from __future__ import annotations

import csv
import datetime
import json
import re
import shutil
import sqlite3
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

import death_store
import player_tracker
import roles
from checks import admin_only

_BOT_DIR = Path(__file__).parent
STATE_FILE = _BOT_DIR / "season_state.json"
ARCHIVE_DIR = _BOT_DIR / "archive"
CONFIRM = "RESET SEASON"


# ---- state -------------------------------------------------------------------

def load_state(path: Path = STATE_FILE) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def fresh_start(path: Path = STATE_FILE) -> bool:
    """True after a season reset: known players must not be seeded from the server."""
    return bool(load_state(path).get("fresh"))


def slug(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (label or "").lower()).strip("-")
    return s[:40] or "season"


def archive_name(old_label: str, now: datetime.datetime) -> str:
    return f"{now:%Y%m%d-%H%M%S}-{slug(old_label or 'previous')}"


# ---- archive / reset (file work only, so it can be tested) -------------------

def _db_rows(db: Path) -> int:
    try:
        conn = sqlite3.connect(db)
        try:
            return conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


def _death_rows(path: Path) -> int:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    return len(data.get("events", []) if isinstance(data, dict) else data)


def _csv_rows(path: Path) -> int:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return 0
    return max(0, sum(1 for line in lines if line.strip()) - 1)


def archive_files(dest: Path, deaths: Path, players_db: Path, whitelist_csv: Path) -> dict:
    """Copy the season's files into `dest` (created). -> {file name: rows archived}.
    Raises OSError / sqlite3.Error when a copy fails, so nothing gets reset."""
    dest.mkdir(parents=True, exist_ok=False)
    out = {}
    if deaths.is_file():
        shutil.copy2(deaths, dest / deaths.name)
        out[deaths.name] = _death_rows(deaths)
    if players_db.is_file():
        src = sqlite3.connect(players_db)
        dst = sqlite3.connect(dest / players_db.name)
        try:
            src.backup(dst)  # a consistent copy even if a write is in flight
        finally:
            dst.close()
            src.close()
        out[players_db.name] = _db_rows(dest / players_db.name)
    if whitelist_csv.is_file():
        shutil.copy2(whitelist_csv, dest / whitelist_csv.name)
        out[whitelist_csv.name] = _csv_rows(whitelist_csv)
    return out


def reset_files(deaths: Path, players_db: Path, whitelist_csv: Path, csv_headers) -> None:
    """Empty the season's files in place (each keeps its format)."""
    deaths.write_text(json.dumps({"events": []}), encoding="utf-8")
    if players_db.is_file():
        conn = sqlite3.connect(players_db)
        try:
            with conn:
                conn.execute("DELETE FROM players")
        except sqlite3.OperationalError:
            pass  # no table yet: nothing to empty
        finally:
            conn.close()
    if whitelist_csv.is_file():
        with whitelist_csv.open("w", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=list(csv_headers)).writeheader()


def write_state(label: str, archive: str, now: datetime.datetime, previous: dict,
                path: Path = STATE_FILE) -> dict:
    """The new season is `label`; the one that ended goes into `history`."""
    history = list(previous.get("history") or [])
    history.append({"season": previous.get("season") or "", "started": previous.get("started") or "",
                    "ended": now.isoformat(timespec="seconds"), "archive": archive})
    state = {"season": label, "started": now.isoformat(timespec="seconds"), "fresh": True,
             "history": history}
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


# ---- cog ---------------------------------------------------------------------

class SeasonCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        state = load_state()
        print(f"[Season] current={state.get('season') or 'unnamed'} "
              f"since={state.get('started') or '-'} fresh={bool(state.get('fresh'))}")

    def _whitelist_csv(self) -> Path:
        cog = self.bot.get_cog("WhitelistCog")
        if cog is not None:
            return cog._csv_path
        path = Path(getattr(self.bot.config, "WHITELIST_CSV_PATH", "") or "whitelist_requests.csv")
        return path if path.is_absolute() else _BOT_DIR / path

    async def _take_whitelist_role(self, guild) -> str:
        role_id = int(getattr(self.bot.config, "WHITELIST_ROLE_ID", 0) or 0)
        role = guild.get_role(role_id) if guild is not None and role_id else None
        if role is None:
            return "Whitelist role: not configured or not found, nothing taken."
        taken = 0
        for member in list(role.members):
            result, _line = await roles.set_member_role(guild, member.id, role_id, False, "Season reset")
            taken += result == "done"
        return f"Whitelist role taken from **{taken}** member(s)."

    async def reset(self, label: str, by: str, guild=None, take_role: bool = False) -> tuple[str, dict, list]:
        """Archive and empty the season's data. -> (archive name, rows archived, notes)."""
        import whitelist  # for its CSV columns
        now = datetime.datetime.now(death_store._TZ)
        previous = load_state()
        name = archive_name(previous.get("season", ""), now)
        dest = ARCHIVE_DIR / name

        wl = self.bot.get_cog("WhitelistCog")
        lock = getattr(wl, "_submit_lock", None)
        if lock is not None:
            await lock.acquire()  # no whitelist request is saved mid-reset
        try:
            counts = archive_files(dest, death_store._STORE_PATH, player_tracker.DB_PATH,
                                   self._whitelist_csv())
            reset_files(death_store._STORE_PATH, player_tracker.DB_PATH, self._whitelist_csv(),
                        whitelist._CSV_HEADERS)
        finally:
            if lock is not None:
                lock.release()

        notes = []
        rank = self.bot.get_cog("RankSync")
        if rank is not None:
            counts["rank_links.json"], _ = await rank.unlink_all(backup=dest / "rank_links.json")
        else:
            notes.append("Links: RankSync is not loaded, the links were left as they are.")
        tracker = self.bot.get_cog("PlayerTrackerCog")
        if tracker is not None:
            tracker._seeded = True  # don't re-seed last season's players this run
        if take_role:
            notes.append(await self._take_whitelist_role(guild))

        (dest / "season.json").write_text(json.dumps({
            "season": previous.get("season") or "", "started": previous.get("started") or "",
            "ended": now.isoformat(timespec="seconds"), "next_season": label,
            "reset_by": by, "rows": counts}, indent=2), encoding="utf-8")
        write_state(label, name, now, previous)
        print(f"[Season] New season {label!r} by {by}; archived {counts} to archive/{name}")
        return name, counts, notes

    @app_commands.command(name="seasonreset",
                          description="New season: archive and clear the bot's season data (deaths, players, whitelist, links).")
    @app_commands.describe(season="Name of the new season (for example Season 4)",
                           confirm="Type RESET SEASON to confirm",
                           whitelist_role="Also take the whitelist role from everyone (default: no)")
    @admin_only()
    async def cmd_seasonreset(self, interaction: discord.Interaction, season: str, confirm: str,
                              whitelist_role: bool = False):
        await interaction.response.defer(ephemeral=True)
        label = " ".join(season.split())[:60]
        if " ".join(confirm.split()).upper() != CONFIRM or not label:
            await interaction.followup.send(
                f"Nothing changed. To start a new season, name it in `season` and type "
                f"`{CONFIRM}` in `confirm`.", ephemeral=True)
            return
        try:
            name, counts, notes = await self.reset(label, str(interaction.user), interaction.guild,
                                                   whitelist_role)
        except (OSError, sqlite3.Error) as e:
            await interaction.followup.send(
                f"❌ Archiving failed, nothing was reset: `{e}`", ephemeral=True)
            print(f"[Season] Reset failed: {e}")
            return
        rows = "\n".join(f"• `{f}`: {n}" for f, n in counts.items()) or "• (no data yet)"
        desc = (f"**{label}** has started. Archived to `archive/{name}` in the bot folder:\n{rows}\n\n"
                "Death registry numbers start at 1 again, every player's first join is new, "
                "and whitelist requests and links start empty.")
        if notes:
            desc += "\n\n" + "\n".join(notes)
        await interaction.followup.send(embed=discord.Embed(
            title="\U0001f5d3️ New Season", description=desc, colour=discord.Colour.orange()),
            ephemeral=True)
        print(f"[Season] /seasonreset by {interaction.user}: {label!r}, archive/{name}")

    @app_commands.command(name="seasoninfo", description="The current season and the archived ones.")
    @admin_only()
    async def cmd_seasoninfo(self, interaction: discord.Interaction):
        state = load_state()
        lines = [f"**Current:** {state.get('season') or 'unnamed'}"
                 + (f" (since {state['started'][:10]})" if state.get("started") else "")]
        for h in reversed((state.get("history") or [])[-10:]):
            lines.append(f"• {h.get('season') or 'unnamed'}: ended {str(h.get('ended'))[:10]}, "
                         f"`archive/{h.get('archive')}`")
        await interaction.response.send_message(embed=discord.Embed(
            title="\U0001f5d3️ Seasons", description="\n".join(lines),
            colour=discord.Colour.blurple()), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SeasonCog(bot))
    print("[Season] Extension loaded.")
