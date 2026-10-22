"""Admin panel: every button points at a real command, forms fit Discord's limits,
and the panel cards fit in one message each.

Run from bot/:  python -m pytest tests
"""

import asyncio
import importlib
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord  # noqa: E402
from discord import app_commands  # noqa: E402
import pytest  # noqa: E402

import admin_panel  # noqa: E402
import cards  # noqa: E402

_COG_MODULES = ("rank_sync", "siege_night", "jeeves_drops", "jeeves_modmanager", "restart_watch",
                "feature_controls", "whitelist", "admin_panel")
_MAIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")


def _cog_commands() -> dict:
    out = {}
    for mod_name in _COG_MODULES:
        mod = importlib.import_module(mod_name)
        for obj in vars(mod).values():
            if isinstance(obj, type):
                for attr in vars(obj).values():
                    if isinstance(attr, app_commands.Command):
                        out[attr.name] = attr
    return out


def _main_commands() -> set:
    # main.py is the bot script and can't be imported here; read its command names.
    with open(_MAIN, encoding="utf-8") as fh:
        return set(re.findall(r'@bot\.tree\.command\(name="([^"]+)"', fh.read()))


def test_every_button_runs_a_real_command():
    known = set(_cog_commands()) | _main_commands()
    missing = sorted({a.command for a in admin_panel.ACTIONS.values()} - known)
    assert missing == []


def test_button_keys_are_unique_and_short():
    keys = [a.key for g in admin_panel.GROUPS for a in g.actions]
    assert len(keys) == len(set(keys))
    assert all(len(admin_panel._PREFIX + k) <= 100 for k in keys)
    assert all(len(a.label) <= 80 for a in admin_panel.ACTIONS.values())


@pytest.mark.parametrize("action", list(admin_panel.ACTIONS.values()), ids=lambda a: a.key)
def test_forms_fit_discord_limits(action):
    command = _cog_commands().get(action.command)
    if command is None:
        return  # a main.py command: checked by name only
    names = {p.name for p in command.parameters}
    assert set(action.fixed) <= names, f"fixed parameters not on /{command.name}"
    params = admin_panel.asked_params(command, action)
    assert len(params) <= admin_panel._MODAL_MAX_INPUTS

    async def build():
        form = admin_panel.CommandForm(None, action, command, params)
        return form

    form = asyncio.run(build())
    for p, component in form.inputs:
        assert len(admin_panel._param_label(p)) <= 45
        if isinstance(component, discord.ui.Select):
            assert 1 <= len(component.options) <= 25


def test_parameterless_risky_buttons_confirm():
    for key in ("stop", "restartnow", "restart", "forcemodupdate"):
        assert admin_panel.ACTIONS[key].confirm


def test_parse_value():
    t = discord.AppCommandOptionType
    assert admin_panel.parse_value(t.integer, " 15 ") == 15
    assert admin_panel.parse_value(t.string, " hi ") == "hi"
    with pytest.raises(ValueError):
        admin_panel.parse_value(t.integer, "ten")


def test_group_cards_fit_one_message():
    async def build():
        return [admin_panel.GroupView(_Panel(), g) for g in admin_panel.GROUPS]

    for view in asyncio.run(build()):
        assert view.is_persistent()
        assert sum(1 for _ in view.walk_children()) <= 40   # Discord's per-message component cap
        assert all(b.custom_id.startswith(admin_panel._PREFIX) for b in cards.buttons(view))


def test_choice_parameters_get_choice_objects():
    # /setrank takes an app_commands.Choice[int]; /deferrestart a bare int.
    cmds = _cog_commands()
    assert admin_panel._wants_choice(cmds["setrank"], "rank")
    assert not admin_panel._wants_choice(cmds["deferrestart"], "minutes")


class _Panel:
    def make_callback(self, _action):
        async def cb(_interaction):
            return None
        return cb
