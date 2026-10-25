"""Jamie's Collapse announcements: parsing the mod's log lines and the cards.

The lines below are built the way Jamie's Collapse writes them (TC_Server.lua
`S.discord`: `{"time":<s>,"content":"<jsonStr(text)>"}` + CRLF), with the
headings its code produces (start, results, event night, vote, champion).
Run from bot/:  python -m pytest tests
"""

import asyncio
import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jamies_collapse as jc  # noqa: E402


def _line(text: str, when: int = 1792900000) -> str:
    # S.jsonStr: \ and " escaped, newlines as \n, CR dropped; then CRLF.
    s = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "")
    return '{"time":' + str(when) + ',"content":"' + s + '"}\r\n'


def test_parse_line_reads_the_mods_json_and_skips_anything_else():
    entry = jc.parse_line(_line('## JAMIE\'S COLLAPSE: SURVIVED!\n**Night "Run"** (PvE)').strip())
    assert entry == {"time": 1792900000, "content": '## JAMIE\'S COLLAPSE: SURVIVED!\n**Night "Run"** (PvE)'}
    for bad in ("", "not json", "[1, 2]", '{"time": 1}', '{"time": 1, "content": "  "}', '{"content": 5}'):
        assert jc.parse_line(bad) is None, bad
    assert jc.parse_line('{"content": "x"}') == {"time": None, "content": "x"}


def test_kinds_follow_the_mods_headings():
    cases = {
        "JAMIE'S COLLAPSE HAS STARTED": "start",
        "JAMIE'S COLLAPSE: RED TEAM WINS": "win",
        "JAMIE'S COLLAPSE: BLUE WINS THE ROYALE": "win",
        "JAMIE'S COLLAPSE: SURVIVED!": "win",
        "JAMIE'S COLLAPSE: THE HORDE WINS": "loss",
        "JAMIE'S COLLAPSE: NO WINNER": "loss",
        "JAMIE'S COLLAPSE: EVENT NIGHT": "night",
        "JAMIE'S COLLAPSE: EVENT NIGHT IS ON": "night",
        "JAMIE'S COLLAPSE: EVENT NIGHT CALLED OFF": "called_off",
        "JAMIE'S COLLAPSE: VOTE": "vote",
        "JAMIE'S COLLAPSE: SEASON CHAMPION": "champion",
        "JAMIE'S COLLAPSE: SOMETHING NEW": "other",
    }
    for heading, kind in cases.items():
        assert jc.kind_of(heading)[0] == kind, heading


def test_render_splits_heading_and_body():
    post = jc.render(jc.parse_line(_line(
        "## JAMIE'S COLLAPSE: RED TEAM WINS\n**Friday Brawl** (Teams)\n**MVP: Peña**\nMost zombies killed: bob 40\n\n"
        "Next event: Wed 08:00 (UTC+8)")))
    assert post["kind"] == "win"
    assert post["title"].endswith("JAMIE'S COLLAPSE: RED TEAM WINS")
    assert post["description"].startswith("**Friday Brawl** (Teams)\n**MVP: Peña**")
    assert post["description"].endswith("Next event: Wed 08:00 (UTC+8)")
    assert post["footer"] == "Jamie's Collapse \u2022 <t:1792900000:f>"


def test_render_without_heading_or_time_and_long_bodies():
    post = jc.render({"time": None, "content": "just text"})
    assert post["kind"] == "other" and post["description"] == "just text"
    assert post["footer"] == "Jamie's Collapse"
    long = jc.render({"time": 1, "content": "## JAMIE'S COLLAPSE: VOTE\n" + "x" * 5000})
    assert len(long["description"]) == jc._BODY_LIMIT and long["description"].endswith("\u2026")


class _Channel:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)


def _cog(enabled=True):
    channel = _Channel()
    cog = jc.JamiesCollapseCog.__new__(jc.JamiesCollapseCog)
    cog.bot = types.SimpleNamespace(features=types.SimpleNamespace(is_enabled=lambda key: enabled and key == "collapse"),
                                    get_collapse_channel=lambda: channel)
    cog._seen = jc.deque(maxlen=200)
    return cog, channel


def test_each_post_is_sent_once_as_a_card_that_pings_nobody():
    cog, channel = _cog()
    line = _line("## JAMIE'S COLLAPSE HAS STARTED\n**@everyone night** (PvP), 4 players").strip()
    asyncio.run(cog._handle_line(line))
    asyncio.run(cog._handle_line(line))          # re-read after a rewrite: not again
    asyncio.run(cog._handle_line("garbage"))
    assert len(channel.sent) == 1
    sent = channel.sent[0]
    mentions = sent["allowed_mentions"]
    assert not mentions.everyone and not mentions.users and not mentions.roles
    text = json.dumps(sent["view"].to_components())
    assert "JAMIE'S COLLAPSE HAS STARTED" in text and "4 players" in text


def test_nothing_sent_while_the_toggle_is_off():
    cog, channel = _cog(enabled=False)
    asyncio.run(cog._handle_line(_line("## JAMIE'S COLLAPSE: VOTE\n**Next map?**").strip()))
    assert channel.sent == []
