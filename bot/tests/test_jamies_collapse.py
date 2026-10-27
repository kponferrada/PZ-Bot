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


def test_newer_posts_get_their_own_kinds():
    # headings from 1.10.42 (TC_Discord, TC_Run, TC_Server, TC_Extras)
    cases = {
        "## JAMIE'S COLLAPSE: PVE EVENT IN 15 MIN": "reminder",
        "## JAMIE'S COLLAPSE: EVENT NIGHT IN 60 MIN": "reminder",
        "## BLOOD RUN IN 15 MIN": "reminder",
        "## JAMIE'S COLLAPSE: EVENT ABORTED": "called_off",
        "### KINGSMOUTH RUN: CANCELLED": "called_off",
        "## JAMIE'S COLLAPSE: SEASON CHAMPIONS": "champion",
        "## LEADERBOARDS": "board",
        "### KINGSMOUTH RUN: THEY'RE GOING IN": "start",
        "### KINGSMOUTH RUN: EXTRACTION REVEALED": "live",
        "### KINGSMOUTH RUN: MOST WANTED": "live",
        "### OVERTIME": "live",
        "### THE STORM CLOSES IN": "live",
        "### THE SECOND COLLAPSE": "live",      # a collapse is headed by its own name
        "## SOMETHING NEW": "other",
    }
    for heading, kind in cases.items():
        assert jc.render({"time": 1, "content": heading + "\nbody"})["kind"] == kind, heading


def test_blood_run_posts_without_a_heading_get_a_title():
    reg = jc.render({"time": 1, "content": "**Kingsmouth Run**: registration is open, deploying in 10 min."})
    assert reg["kind"] == "registration" and reg["title"].endswith("KINGSMOUTH RUN: REGISTRATION OPEN")
    assert reg["description"].startswith("**Kingsmouth Run**: registration is open")
    over = jc.render({"time": 1, "content": "**Kingsmouth Run** is over.\nGot out (2 of 5): Ana, bob"})
    assert over["kind"] == "win" and over["title"].endswith("KINGSMOUTH RUN IS OVER")
    assert over["description"] == "Got out (2 of 5): Ana, bob"
    champ = jc.render({"time": 1, "content": "**Blood Run Champion, Oct 2026**: Ana!\n**Ana: 40 pts**"})
    assert champ["kind"] == "champion" and champ["title"].endswith("BLOOD RUN CHAMPION, OCT 2026")
    assert champ["description"].startswith("Ana!")


LINKS = {"ana": 111, "bob_pz": 222, "xo": 333}


def test_linked_players_are_mentioned_in_place_of_their_names():
    post = jc.render({"time": 1, "content": (
        "## JAMIE'S COLLAPSE: RED WINS\n**Friday Brawl** (Teams)\n**Last team standing: ANA & Bob_PZ & carl**\n"
        "Most zombies killed: bob_pz 40, Anabel 3, ana 2\nSeason so far (Oct): 1. ana 30, 2. bob_pz 20")}, LINKS)
    lines = post["description"].split("\n")
    assert lines[1] == "**Last team standing: <@111> & <@222> & carl**"
    assert lines[2] == "Most zombies killed: <@222> 40, Anabel 3, <@111> 2"   # whole names only
    assert lines[3] == "Season so far (Oct): 1. ana 30, 2. bob_pz 20"        # not this event's attendees
    assert post["mentions"] == [111, 222]


def test_no_mentions_on_leaderboards_short_names_or_without_links():
    board = jc.render({"time": 1, "content": "## LEADERBOARDS\n1. ana: 30 pts"}, LINKS)
    assert board["mentions"] == [] and "<@" not in board["description"]
    short = jc.render({"time": 1, "content": "### FLARE\n**xo** is trying to get out early."}, LINKS)
    assert short["mentions"] == []
    plain = jc.render({"time": 1, "content": "### FLARE\n**ana** is trying to get out early."})
    assert plain["mentions"] == [] and "**ana**" in plain["description"]


def test_blood_run_deploy_pings_every_linked_attendee():
    post = jc.render({"time": 1, "content": "### KINGSMOUTH RUN: THEY'RE GOING IN\n3 players into Kingsmouth: ana, bob_pz, carl"},
                     LINKS)
    assert post["kind"] == "start"
    assert post["description"].endswith(": <@111>, <@222>, carl") and post["mentions"] == [111, 222]


def test_sent_card_pings_only_the_linked_attendees():
    cog, channel = _cog()
    cog.bot.features = types.SimpleNamespace(is_enabled=lambda key: key in ("collapse", "collapse_mentions"))
    cog.bot.get_cog = lambda name: types.SimpleNamespace(discord_ids_by_pz_username=lambda: dict(LINKS))
    asyncio.run(cog._handle_line(_line("## JAMIE'S COLLAPSE: SURVIVED!\n**Night** (PvE)\n2 made it to the end: ana, carl").strip()))
    mentions = channel.sent[0]["allowed_mentions"]
    assert not mentions.everyone and not mentions.roles
    assert [u.id for u in mentions.users] == [111]
    assert "<@111>" in json.dumps(channel.sent[0]["view"].to_components())


def test_pings_off_with_the_toggle():
    cog, channel = _cog()   # only "collapse" is on
    cog.bot.get_cog = lambda name: types.SimpleNamespace(discord_ids_by_pz_username=lambda: dict(LINKS))
    asyncio.run(cog._handle_line(_line("## JAMIE'S COLLAPSE: SURVIVED!\n2 made it: ana, carl").strip()))
    assert not channel.sent[0]["allowed_mentions"].users
    assert "<@111>" not in json.dumps(channel.sent[0]["view"].to_components())
