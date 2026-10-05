"""/help's [admin] markers match the real permission checks on cog commands.

main.py (the bot script) and whitelist.py (checks inside the handlers) are not
importable/inspectable here; every other cog's commands are.
Run from bot/:  python -m pytest tests
"""

import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from discord import app_commands  # noqa: E402

import help as help_cog  # noqa: E402

_COGS = ("death_log", "rank_sync", "chat_relay", "siege_night", "jeeves_drops",
         "jamies_fortune", "jeeves_modmanager", "server_status", "restart_watch",
         "feature_controls", "stats", "weekly_rp", "cleanup", "help", "player_tracker")


def _is_admin_check(check) -> bool:
    return getattr(check, "__qualname__", "").startswith(("admin_only.", "require_role."))


def _cog_commands():
    out = {}
    for mod_name in _COGS:
        mod = importlib.import_module(mod_name)
        for obj in vars(mod).values():
            if not isinstance(obj, type):
                continue
            for attr in vars(obj).values():
                if isinstance(attr, app_commands.Command):
                    out[attr.name] = any(_is_admin_check(c) for c in attr.checks)
    return out


def _help_markers():
    markers = {}
    for rows in help_cog._COMMANDS.values():
        for usage, desc in rows:
            name = usage.split()[0].lstrip("/")
            markers[name] = "[admin]" in desc
    return markers


def test_help_admin_markers_match_checks():
    commands = _cog_commands()
    markers = _help_markers()
    assert commands, "no cog commands found"
    mismatched = {n: (markers[n], is_admin) for n, is_admin in commands.items()
                  if n in markers and n != "help" and markers[n] != is_admin}
    assert mismatched == {}, f"help marker vs real check (marker, admin): {mismatched}"


def test_every_cog_command_is_in_help():
    missing = sorted(set(_cog_commands()) - set(_help_markers()) - {"help"})
    assert missing == []
