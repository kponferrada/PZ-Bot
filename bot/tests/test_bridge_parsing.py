"""Tests for the pure helpers behind the SFTP tailers and the Lua bridge.

Run from bot/:  python -m pytest tests
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aegis_stats  # noqa: E402
import chat_relay  # noqa: E402
import lua_bridge  # noqa: E402
from sftp_client import complete_utf8_length  # noqa: E402


def test_complete_utf8_length_holds_back_split_characters():
    enye = "ñ".encode()          # 2 bytes
    skull = "💀".encode()        # 4 bytes
    assert complete_utf8_length(b"abc") == 3
    assert complete_utf8_length(b"ab" + enye) == 4
    assert complete_utf8_length(b"ab" + enye[:1]) == 2
    for cut in range(1, 4):
        assert complete_utf8_length(b"x" + skull[:cut]) == 1
    assert complete_utf8_length(b"x" + skull) == 5
    assert complete_utf8_length(b"") == 0


def test_lua_string_byte_escapes_decode_as_utf8():
    # "Peña \"x\"" as the bridge writes it: non-ASCII as \ddd UTF-8 bytes.
    text = 'return { name = "Pe\\195\\177a \\"x\\"", n = 3, ok = true }'
    assert lua_bridge._parse_lua_nested(text) == {"name": 'Peña "x"', "n": 3, "ok": True}


def test_flat_status_parser_handles_escapes_and_lists():
    text = 'return {\n  playerCount = 2,\n  players = "Jos\\195\\169, bob",\n  isNight = false,\n}'
    assert lua_bridge._parse_lua_table(text) == {
        "playerCount": 2, "players": "José, bob", "isNight": False}


def test_mtime_age_is_skew_immune():
    path = "/test/skew.txt"
    lua_bridge._mtime_seen.pop(path, None)
    # Host clock is 1 hour ahead of ours: the naive age would be negative/zero.
    host_ahead = 3600.0
    first = lua_bridge._mtime_age(path, 1000.0 + host_ahead, now_mono=50.0, now_wall=1000.0)
    assert first == 0.0
    # Unchanged mtime: age grows with OUR clock, not the host's.
    assert lua_bridge._mtime_age(path, 1000.0 + host_ahead, now_mono=150.0, now_wall=1100.0) == 100.0
    # A new write resets it.
    assert lua_bridge._mtime_age(path, 1200.0 + host_ahead, now_mono=160.0, now_wall=1110.0) == 0.0


def test_aegis_find_user_ignores_case():
    data = {"Silvast": {}, "bob": {}}
    assert aegis_stats.find_user(data, "silvast") == "Silvast"
    assert aegis_stats.find_user(data, "bob") == "bob"
    assert aegis_stats.find_user(data, "nobody") is None


class _Relay:
    def __init__(self, rank):
        self._rank = rank

    def _get_rank_for_author(self, _author):
        return self._rank


def test_chat_relay_escapes_markdown_and_code_fences():
    fmt = chat_relay.ChatRelay._format_message
    plain = fmt(_Relay(0), "General", "x_y", "**hi** @everyone")
    assert plain.startswith("**[x\\_y]**: ")
    assert "\\*\\*hi\\*\\*" in plain
    ranked = fmt(_Relay(6), "General", "bob", "```\n@everyone")
    assert ranked.count("```") == 2  # only the wrapper fences survive


def test_newest_matching_uses_one_readdir():
    import asyncio
    from types import SimpleNamespace
    from sftp_client import SftpClient

    calls = []

    class FakeSftp:
        async def readdir(self, path):
            calls.append(path)
            return [
                SimpleNamespace(filename="a_user.txt", attrs=SimpleNamespace(mtime=10)),
                SimpleNamespace(filename="b_user.txt", attrs=SimpleNamespace(mtime=30)),
                SimpleNamespace(filename="c_chat.txt", attrs=SimpleNamespace(mtime=99)),
                SimpleNamespace(filename="d_user.txt", attrs=SimpleNamespace(mtime=None)),
            ]

    client = SftpClient("h")
    client._conn = SimpleNamespace(is_closed=lambda: False)
    client._sftp = FakeSftp()
    newest = asyncio.run(client.newest_matching("/logs/", "*_user.txt"))
    assert newest == "/logs/b_user.txt"
    assert calls == ["/logs/"]
