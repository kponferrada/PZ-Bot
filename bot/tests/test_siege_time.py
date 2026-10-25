"""Real-world time of the next siege, the game-clock speed it is based on,
reading SiegeStartHour from the sandbox file, and the flat status icons.

Run from bot/:  python -m pytest tests
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import game_calendar as gc  # noqa: E402
import server_config  # noqa: E402
import server_status  # noqa: E402
import status_icons  # noqa: E402

WORLD = {"year": 1995, "month": 3, "day": 28, "hour": 22, "minutes": 39}   # Apr 29, 10:39 PM
SIEGE = {"phase": "idle", "eventDay": 659, "nextSiegeDay": 665}


def _world_at(game_min):
    # A world dict whose clock reads `game_min` minutes after WORLD.
    base = gc.game_minutes(WORLD) + game_min
    d = gc.date.fromordinal(base // 1440)
    return {"year": d.year, "month": d.month - 1, "day": d.day - 1,
            "hour": base % 1440 // 60, "minutes": base % 60}


def test_clock_rate_from_samples():
    clock = gc.GameClockRate()
    assert clock.rate() is None
    for i in range(0, 301, 30):          # 24 game minutes per real minute (a 1-hour day)
        clock.record(1000 + i, _world_at(i * 24 // 60))
    assert abs(clock.rate() - 0.4) < 0.01


def test_clock_rate_resets_on_restart_and_pause():
    clock = gc.GameClockRate()
    for i in range(0, 301, 30):
        clock.record(1000 + i, _world_at(i // 2))
    clock.record(1400, _world_at(-60))   # went backwards (world reloaded)
    assert clock.rate() is None and len(clock.samples) == 1
    clock.record(1800, _world_at(-60))   # stuck for over 5 minutes: paused
    assert len(clock.samples) == 1


def test_siege_real_time():
    now = 1_000_000.0
    # 6 days ahead at 20:00, now 22:39 -> 6*1440 + 1200 - 1359 = 8481 game minutes.
    at = gc.siege_real_time(WORLD, SIEGE, 20, 0.4, now)
    assert at == now + 8481 / 0.4
    assert gc.siege_real_time(WORLD, {**SIEGE, "phase": "active"}, 20, 0.4, now) is None
    assert gc.siege_real_time(WORLD, SIEGE, 20, None, now) is None          # speed not known yet
    assert gc.siege_real_time(WORLD, {**SIEGE, "nextSiegeDay": 659}, 20, 0.4, now) is None  # past today's start


def test_siege_line_in_the_embed():
    now = 1_792_700_000
    embed = server_status.build_embed(True, {**WORLD, "playerCount": 1}, SIEGE, False, restart=None,
                                      siege_at=now + 3600)
    siege = embed.fields[-1].value
    assert f"<t:{now + 3600}:t>" in siege and f"<t:{now + 3600}:R>" in siege
    far = server_status.siege_when_line(now + 3 * 86400, now)
    assert far.startswith("<t:") and ":f>" in far
    assert server_status.siege_when_line(None, now) == ""


def test_sandbox_siege_start_hour():
    text = """SandboxVars = {
    DayLength = 3,
    SiegeNight = {
        Enabled = true,
        SiegeStartHour = 21,
        FrequencyDays = 5,
    },
}"""
    assert server_config.sandbox_number(text, "SiegeNight", "SiegeStartHour") == 21
    assert server_config.sandbox_number(text, "SiegeNight", "Missing") is None
    assert server_config.sandbox_number("SandboxVars = {}", "SiegeNight", "SiegeStartHour") is None


def test_icons_draw_and_fall_back():
    names = set()
    for key in status_icons.KEYS:
        img = status_icons.draw(key)
        assert img.size == (128, 128) and img.getbbox() is not None
        name = status_icons.emoji_name(key)
        assert 2 <= len(name) <= 32 and name.startswith("pzs_")   # Discord emoji name limit
        names.add(name)
        assert status_icons.icon(key)                               # a plain emoji until synced
    assert len(names) == len(status_icons.KEYS)
    assert status_icons.weather_key("Heavy Rain") == "rain" and status_icons.weather_key("Clear") == "clear"


def _status_cog(monkeypatch, rates):
    async def start_hour(_bot):
        return 20
    monkeypatch.setattr(server_config, "read_siege_start_hour", start_hour)
    cog = server_status.ServerStatusCog.__new__(server_status.ServerStatusCog)
    cog.bot, cog._siege_at, cog._last_players, cog._clock = None, None, None, gc.GameClockRate()
    rates = iter(rates)
    cog._clock.best = lambda: next(rates)
    return cog


def test_siege_time_is_worked_out_once(monkeypatch):
    import asyncio
    cog = _status_cog(monkeypatch, [0.4, 0.5, 0.6, 0.7, 0.8])
    on = {**WORLD, "playerCount": 1}

    async def run():
        first = await cog._siege_time(True, on, SIEGE)
        again = await cog._siege_time(True, {**on, "minutes": 40}, SIEGE)       # next update: kept
        moved = await cog._siege_time(True, on, {**SIEGE, "nextSiegeDay": 666})  # another siege
        down = await cog._siege_time(False, on, SIEGE)                          # restart: dropped
        back = await cog._siege_time(True, on, SIEGE)                           # back: worked out again
        return first, again, moved, down, back

    first, again, moved, down, back = asyncio.run(run())
    assert first == again                       # the clock-speed estimate moved, the shown time didn't
    assert moved != first and down is None and back not in (None, first)


def test_siege_time_redone_when_players_return(monkeypatch):
    import asyncio
    cog = _status_cog(monkeypatch, [0.4, 0.8])
    on, empty = {**WORLD, "playerCount": 1}, {**WORLD, "playerCount": 0}

    async def run():
        first = await cog._siege_time(True, on, SIEGE)
        while_empty = await cog._siege_time(True, empty, SIEGE)    # empty: kept, not redone
        rejoined = await cog._siege_time(True, on, SIEGE)          # 0 -> 1: redone
        kept = await cog._siege_time(True, on, SIEGE)
        return first, while_empty, rejoined, kept

    first, while_empty, rejoined, kept = asyncio.run(run())
    assert while_empty == first and rejoined != first and kept == rejoined


def test_nothing_worked_out_while_empty(monkeypatch):
    import asyncio
    cog = _status_cog(monkeypatch, [0.4])
    assert asyncio.run(cog._siege_time(True, {**WORLD, "playerCount": 0}, SIEGE)) is None


def test_live_players_falls_back_to_rcon_when_the_file_is_stale():
    import asyncio
    import types
    sent = []

    class Rcon:
        async def send_command(self, cmd):
            sent.append(cmd)
            return "Players connected (0):"

        def parse_players(self, response):
            return set(), 0

    cog = server_status.ServerStatusCog.__new__(server_status.ServerStatusCog)
    cog.bot = types.SimpleNamespace(rcon=Rcon(), state=types.SimpleNamespace())
    stuck = {**WORLD, "playerCount": 1, "players": "admin"}
    fresh = asyncio.run(cog._live_players(stuck, True))
    assert fresh["playerCount"] == 1 and not sent                 # fresh file: trusted, no RCON
    now = asyncio.run(cog._live_players(stuck, False))
    assert now["playerCount"] == 0 and now["players"] == "" and sent == ["players"]
    assert cog.bot.state.player_count == 0 and cog.bot.state.players_online is False


def test_clock_keeps_its_last_speed():
    clock = gc.GameClockRate()
    for i in range(0, 301, 30):
        clock.record(1000 + i, _world_at(i * 24 // 60))
    assert abs(clock.best() - 0.4) < 0.01
    clock.samples = []                                           # restart / pause
    assert clock.rate() is None and abs(clock.best() - 0.4) < 0.01
