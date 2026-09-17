"""cleanup.py — periodic removal of bot-generated files that are safe to delete.

Scoped STRICTLY to the bot's own directory. Only removes files the bot can
regenerate or live without; never touches data (DBs, state, CSVs, links), source,
or config.

Targets:
  - Python bytecode: `__pycache__/` directories and `*.pyc`/`*.pyo` files.
  - Stale regenerated files: `status_card.png` and `dashboard_assets.json`,
    but only once they are older than the stale threshold (so the bot's actively
    regenerated caches are never raced).

Excluded (never descended into): `venv/`, `.venv/`, `.git/`, `assets/`,
`scripts/`, `deploy/`. Everything not in the safe set above is left alone.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path

from discord.ext import commands, tasks

_BOT_DIR = Path(__file__).resolve().parent

# Directories we never descend into (third-party env, VCS, static/source assets).
_EXCLUDED_DIRS = {".git", "venv", ".venv", "assets", "scripts", "deploy"}

# Regenerated-on-demand files that are safe to delete once they go stale.
_STALE_NAMES = {"status_card.png", "dashboard_assets.json"}


def _dir_size(path: Path) -> int:
    try:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    except OSError:
        return 0


def run_cleanup(stale_seconds: float) -> dict:
    """Remove safe-to-delete generated files under the bot directory.

    Returns a summary dict: {"removed_files", "removed_dirs", "freed_bytes"}.
    """
    removed_files = 0
    removed_dirs = 0
    freed_bytes = 0
    now = time.time()

    def _remove_file(p: Path) -> None:
        nonlocal removed_files, freed_bytes
        try:
            freed_bytes += p.stat().st_size
        except OSError:
            pass
        try:
            p.unlink(missing_ok=True)
            removed_files += 1
        except OSError as e:
            print(f"[Cleanup] Could not remove {p}: {e}")

    for root, dirs, files in os.walk(_BOT_DIR, topdown=True):
        root_path = Path(root)

        kept: list[str] = []
        for d in dirs:
            if d == "__pycache__":
                dp = root_path / d
                try:
                    freed_bytes += _dir_size(dp)
                    shutil.rmtree(dp, ignore_errors=True)
                    removed_dirs += 1
                except OSError as e:
                    print(f"[Cleanup] Could not remove {dp}: {e}")
                # do not descend into the (now removed) directory
            elif d not in _EXCLUDED_DIRS:
                kept.append(d)
        dirs[:] = kept

        for name in files:
            p = root_path / name
            if name.endswith((".pyc", ".pyo")):
                _remove_file(p)
            elif name in _STALE_NAMES:
                try:
                    if now - p.stat().st_mtime >= stale_seconds:
                        _remove_file(p)
                except OSError:
                    pass

    return {"removed_files": removed_files, "removed_dirs": removed_dirs,
            "freed_bytes": freed_bytes}


class CleanupCog(commands.Cog):
    """Periodically removes bot-generated files that are safe to delete."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        try:
            self.interval_hours = max(1, int(getattr(bot.config, "CLEANUP_INTERVAL_HOURS", 24) or 24))
        except (TypeError, ValueError):
            self.interval_hours = 24
        try:
            self.stale_hours = max(0, int(getattr(bot.config, "CLEANUP_STALE_HOURS", 24) or 24))
        except (TypeError, ValueError):
            self.stale_hours = 24
        self.cleanup_loop.start()

    def cog_unload(self) -> None:
        self.cleanup_loop.cancel()

    @tasks.loop(hours=24.0)
    async def cleanup_loop(self) -> None:
        await self._run()

    @cleanup_loop.before_loop
    async def _before_loop(self) -> None:
        await self.bot.wait_until_ready()
        self.cleanup_loop.change_interval(hours=self.interval_hours)
        await self._run()  # initial cleanup shortly after startup

    async def _run(self) -> None:
        stale_seconds = self.stale_hours * 3600.0
        summary = await asyncio.to_thread(run_cleanup, stale_seconds)
        if summary["removed_files"] or summary["removed_dirs"]:
            print(f"[Cleanup] removed {summary['removed_files']} file(s) and "
                  f"{summary['removed_dirs']} dir(s), freed {summary['freed_bytes']:,} bytes")


async def setup(bot: commands.Bot):
    await bot.add_cog(CleanupCog(bot))
    print("[Cleanup] Extension loaded.")
