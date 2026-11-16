"""BLib2 Admin's Discord bridge (blib2-admin Discord.lua) from the bot's side:
the status sample parses, answers match on id + timestamp, the ranks file and
command lines are what the game reads, and the bridge picks BLib2 or Jeeve's.

The fixture is a real BLib2 status file (blib-v2 tools/dump_discord_status.lua).
Run from bot/:  python -m pytest tests
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import game_admin  # noqa: E402
import lua_bridge  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "blib2_discord_status.txt")


def _status() -> dict:
    with open(FIXTURE, encoding="utf-8") as fh:
        return lua_bridge._parse_lua_nested(fh.read())


def test_status_sample_parses_into_jeeves_world_keys():
    st = _status()
    world = st["world"]
    for key in ("hour", "minutes", "day", "month", "year", "elapsedDays", "isNight", "temperature", "windSpeed",
                "rainIntensity", "fogIntensity", "snowIntensity", "cloudiness", "weather", "playerCount",
                "players", "hoursForDay", "timestamp"):
        assert key in world, key
    assert world["playerCount"] == 2
    assert st["bridge"]["channel"] == "discord"


def test_players_and_lookup_lists():
    st = _status()
    players = game_admin.players_from(st)
    assert [p["name"] for p in players] == ["Peña", "bob"]
    bob = game_admin.find_player(st, "BOB")
    assert (bob["x"], bob["y"], bob["z"]) == (8100, 11600, 1)
    assert "8100,11600,1" in game_admin.player_line(bob)
    rows = lua_bridge.keyed(st["lookup"], "i")
    assert [r["id"] for r in rows] == ["Base.Nails", "Base.NailsBox"]


def test_answers_match_id_and_timestamp():
    st = _status()
    r = lua_bridge.find_siege_result(st, 1, 1700000001)
    assert r["command"] == "giveitem" and r["ok"] is True
    assert lua_bridge.find_siege_result(st, 1, 1699999999) is None   # same id, older bot run
    text, ok = game_admin.outcome_text({"sent": True, "result": r})
    assert ok is True and "5 x Base.Nails" in text
    assert game_admin.outcome_text({"sent": True, "result": None})[1] is None
    assert game_admin.outcome_text({"sent": False})[1] is False


def test_unstuck_spot():
    assert lua_bridge.unstuck_spot("safe spot 101,-5,0 for Ann") == (101, -5, 0)
    assert lua_bridge.unstuck_spot("no free square within 12 of Ann") is None


def test_ranks_file_matches_blib2s_reader():
    text = lua_bridge.ranks_file_text({"Ann": (3, (255, 0, 51)), "bad|name": (1, (0, 0, 0)), "Bob": (6, (0, 255, 0))})
    lines = text.strip().split("\n")
    assert lines[0].startswith("#")
    assert lines[1:] == ["Ann\t3\t1.000,0.000,0.200", "Bob\t6\t0.000,1.000,0.000"]


def test_jeeves_sounds_map_onto_blib2s():
    assert [lua_bridge.blib2_sound(n) for n in range(1, 12)] == [1, 2, 3, 4, 5, 6, 1, 2, 3, 4, 5]


def test_command_file_is_protocol_2_with_escapes():
    text = lua_bridge._build_lua_table("giveitem", 3, timestamp=1700000000, protocol=2, by="Keym", byId="1",
                                       player="Peña", item="Base.Axe", count=2, offline=True)
    assert 'player = "Pe\\195\\177a",' in text
    assert "offline = true," in text and "protocol = 2," in text


def test_rcon_lines_quote_names():
    assert game_admin.rcon_line("godmodeplayer", game_admin.quote('Bo"b'), "-true") == 'godmodeplayer "Bob" -true'
    assert game_admin.rcon_line("teleportto", game_admin.quote("Ann"), "1,2,0") == 'teleportto "Ann" 1,2,0'
    assert game_admin.gamecmd_refusal("quit") is not None
    assert game_admin.gamecmd_refusal("/QUIT now") is not None
    assert game_admin.gamecmd_refusal('additem "Bob" Base.Axe 1') is None


def test_mode_pins_and_auto(monkeypatch):
    class _Cfg:
        SFTP_LUA_DIR = "/srv/Lua"
        GAME_BRIDGE = "jeeves"

    class _Bot:
        config = _Cfg()

    lua_bridge.init(_Bot())
    assert lua_bridge.mode() == "jeeves" and asyncio.run(lua_bridge.uses_blib2()) is False
    _Cfg.GAME_BRIDGE = "blib2"
    lua_bridge.init(_Bot())
    assert asyncio.run(lua_bridge.uses_blib2()) is True
    _Cfg.GAME_BRIDGE = "nonsense"
    lua_bridge.init(_Bot())
    assert lua_bridge.mode() == "auto"

    class _Sftp:
        def __init__(self, st):
            self.st = st

        async def stat(self, path):
            assert path.endswith("blib2_discord_status.txt")
            return self.st

    import time
    for st, want in (((900, time.time() - 5), True), ((900, time.time() - 3600), False), (None, False)):
        lua_bridge._blib2_seen = None
        lua_bridge._mtime_seen.clear()
        monkeypatch.setattr(lua_bridge.sftp_client, "get", lambda s=st: _Sftp(s))
        assert asyncio.run(lua_bridge.uses_blib2()) is want, st
    lua_bridge._blib2_seen = None
