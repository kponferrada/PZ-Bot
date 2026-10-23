"""Button panels (admin and player): every button points at a real command,
forms fit Discord's limits, the cards fit in one message, and the player
panel's replies are private.

Run from bot/:  python -m pytest tests
"""

import asyncio
import importlib
import os
import re
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord  # noqa: E402
from discord import app_commands  # noqa: E402
import pytest  # noqa: E402

import admin_panel  # noqa: E402
import cards  # noqa: E402
import command_panel  # noqa: E402
import player_panel  # noqa: E402

_COG_MODULES = ("rank_sync", "siege_night", "jeeves_drops", "jeeves_modmanager", "restart_watch",
                "feature_controls", "whitelist", "stats", "weekly_rp", "admin_panel", "player_panel")
_MAIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
_PANELS = ((admin_panel.PREFIX, admin_panel.GROUPS), (player_panel.PREFIX, player_panel.GROUPS))
_ALL_ACTIONS = [a for _, groups in _PANELS for g in groups for a in g.actions]


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
    missing = sorted({a.command for a in _ALL_ACTIONS} - known)
    assert missing == []


def test_button_keys_are_unique_and_short():
    for prefix, groups in _PANELS:
        keys = [a.key for g in groups for a in g.actions]
        assert len(keys) == len(set(keys))
        assert all(len(prefix + k) <= 100 for k in keys)
    assert admin_panel.PREFIX != player_panel.PREFIX
    assert all(len(a.label) <= 80 for a in _ALL_ACTIONS)


@pytest.mark.parametrize("action", _ALL_ACTIONS, ids=lambda a: a.key)
def test_forms_fit_discord_limits(action):
    command = _cog_commands().get(action.command)
    if command is None:
        return  # a main.py command: checked by name only
    names = {p.name for p in command.parameters}
    assert set(action.fixed) <= names, f"fixed parameters not on /{command.name}"
    params = command_panel.asked_params(command, action)
    assert len(params) <= command_panel._MODAL_MAX_INPUTS

    async def build():
        return command_panel.CommandForm(None, action, command, params)

    form = asyncio.run(build())
    for p, component in form.inputs:
        assert len(command_panel.param_label(p)) <= 45
        if isinstance(component, discord.ui.Select):
            assert 1 <= len(component.options) <= 25


def test_parameterless_risky_buttons_confirm():
    for key in ("stop", "restartnow", "restart", "forcemodupdate"):
        assert admin_panel.ACTIONS[key].confirm


def test_player_panel_has_no_admin_commands():
    cmds = _cog_commands()
    for action in player_panel.ACTIONS.values():
        command = cmds.get(action.command)
        if command is not None:
            checks = [getattr(c, "__qualname__", "") for c in command.checks]
            assert not any(q.startswith(("admin_only.", "require_role.")) for q in checks), action.key


def test_parse_value():
    t = discord.AppCommandOptionType
    assert command_panel.parse_value(t.integer, " 15 ") == 15
    assert command_panel.parse_value(t.string, " hi ") == "hi"
    with pytest.raises(ValueError):
        command_panel.parse_value(t.integer, "ten")


def test_group_cards_fit_one_message():
    async def build():
        return [(prefix, command_panel.GroupView(_Panel(prefix), g)) for prefix, groups in _PANELS for g in groups]

    for prefix, view in asyncio.run(build()):
        assert view.is_persistent()
        assert sum(1 for _ in view.walk_children()) <= 40   # Discord's per-message component cap
        assert all(b.custom_id.startswith(prefix) for b in cards.buttons(view))


def test_choice_parameters_get_choice_objects():
    # /setrank takes an app_commands.Choice[int]; /deferrestart a bare int.
    cmds = _cog_commands()
    assert command_panel.wants_choice(cmds["setrank"], "rank")
    assert not command_panel.wants_choice(cmds["deferrestart"], "minutes")


def test_private_interaction_forces_ephemeral():
    calls = []

    class _Resp:
        async def send_message(self, *a, **kw):
            calls.append(("send", kw))

        async def defer(self, **kw):
            calls.append(("defer", kw))

        def is_done(self):
            return False

    class _Followup:
        async def send(self, *a, **kw):
            calls.append(("followup", kw))

    real = types.SimpleNamespace(response=_Resp(), followup=_Followup(), user="juan")
    wrapped = command_panel.PrivateInteraction(real)

    async def use():
        await wrapped.response.defer()
        await wrapped.response.send_message("hi")
        await wrapped.followup.send("x", ephemeral=False)

    asyncio.run(use())
    assert wrapped.user == "juan" and not wrapped.response.is_done()
    assert calls == [("defer", {"ephemeral": True, "thinking": True}),
                     ("send", {"ephemeral": True}), ("followup", {"ephemeral": True})]


def test_status_style_embed_becomes_a_card_with_buttons():
    async def build():
        embed = discord.Embed(colour=0x00E676, description="ONLINE")
        embed.set_author(name="PZ TAMBAYAN", icon_url="https://example.com/i.png")
        embed.set_image(url="https://example.com/b.png")
        embed.add_field(name="Age", value="Day 3", inline=True)
        embed.add_field(name="Date", value="Jul 9", inline=True)
        embed.add_field(name="Players", value="• Juan", inline=False)
        panel = _Panel(player_panel.PREFIX)
        rows = command_panel.button_rows(panel, player_panel.GROUP)
        return cards.CardView(cards.from_embed(embed, rows=rows))

    view = asyncio.run(build())
    texts = [getattr(i, "content", "") for i in view.walk_children()]
    assert any(t.startswith("### PZ TAMBAYAN") for t in texts)
    assert "**Age** · Day 3\n**Date** · Jul 9" in texts          # inline fields share one block
    assert any(isinstance(i, discord.ui.MediaGallery) for i in view.walk_children())
    assert [b.custom_id for b in cards.buttons(view)] == [
        player_panel.PREFIX + a.key for a in player_panel.GROUP.actions]
    assert sum(1 for _ in view.walk_children()) <= 40


class _Panel:
    def __init__(self, prefix):
        self.panel_prefix = prefix

    def make_callback(self, _action):
        async def cb(_interaction):
            return None
        return cb
