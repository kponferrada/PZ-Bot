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
    assert "JAMIE'S COLLAPSE HAS STARTED" in text and "-# PLAYERS\\n### 4" in text


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


# --- The cards (the user's mockups, 2026-10-30) ------------------------------

def _texts(post) -> list:
    return [item[1] for item in post["layout"] if item[0] == "text"]


def _components(post) -> dict:
    return jc.card(post).to_component_dict()


def test_critical_collapse_is_the_final_stand():
    post = jc.render({"time": 1792900000, "content":
                      "### CRITICAL COLLAPSE\nArena 4 of 4: **Final Arena**. 2 players still in."})
    assert post["kind"] == "collapse" and post["colour"] == jc._RED
    head, players, zone, warning = _texts(post)
    assert head == ("### ⚡ CRITICAL COLLAPSE\n-# JAMIE'S COLLAPSE · ARENA 4 OF 4\n**Final Arena**\n"
                    "The final collapsed zone is now active. Only the last survivors remain.")
    assert players == "-# PLAYERS STILL IN\n### 2"
    assert zone.startswith("-# ZONE STATUS\n### ") and zone.endswith("CRITICAL")
    assert warning.endswith("**This is the final stand.**\nStay alive. There is no next arena.")
    card = _components(post)
    assert card["accent_color"] == jc._RED
    assert card["components"][-1]["content"] == "-# Jamie's Collapse • <t:1792900000:f>"
    # text, line, two tiles, line, the warning, the footer
    assert [c["type"] for c in card["components"]] == [10, 14, 10, 10, 14, 10, 10]


def test_second_collapse_counts_down_to_the_next():
    post = jc.render({"time": 1000, "content":
                      "### SECOND COLLAPSE\nArena 3 of 4: **Mall**. 2 players (2 teams) still in.\nNext collapse in 06:59."})
    assert post["kind"] == "collapse" and post["colour"] == jc._ORANGE
    head, players, nxt = _texts(post)
    assert "-# JAMIE'S COLLAPSE · ARENA 3 OF 4" in head and head.endswith("The second collapsed zone is now active.")
    assert players == "-# PLAYERS REMAINING\n### 2 players · 2 teams"
    assert nxt.startswith("-# NEXT COLLAPSE\n### ") and "06:59" in nxt and nxt.endswith("<t:1419:R>")
    pvp = jc.render({"time": None, "content":
                     "### FIRST COLLAPSE\nArena 2 of 4: **Yard**. 1 player still in.\n12:00 left on the clock."})
    assert "-# TIME LEFT" in _texts(pvp)[-1] and "<t:" not in _texts(pvp)[-1]


def test_vote_options_are_coloured_buttons():
    post = jc.render({"time": 1, "content": "## JAMIE'S COLLAPSE: VOTE\n**What do you want to play this weekend?**\n"
                                            "Blood Toll | Battle Royale\nVote in game on your JPhone: Collapse app > Stats."})
    assert post["colour"] == 0x3498DB and post["title"] == "\U0001f4ca JAMIE'S COLLAPSE: VOTE"
    assert _texts(post)[0].endswith("-# PvP · COMMUNITY POLL\n**What do you want to play this weekend?**")
    assert post["footer"] == "Vote in-game: JPhone → Collapse → Stats"
    row = [c for c in _components(post)["components"] if c["type"] == 1][0]
    buttons = [(b["label"], b["style"], b["disabled"], b["emoji"]["name"]) for b in row["components"]]
    assert buttons == [("Blood Toll", 4, True, "\U0001f480"), ("Battle Royale", 1, True, "⚔️")]
    mixed = jc.render({"time": 1, "content": "## JAMIE'S COLLAPSE: VOTE\n**Next?**\nPvE survival | Blood Toll: Mall"})
    assert "-# COMMUNITY POLL" in _texts(mixed)[0]


def test_survived_results_card():
    post = jc.render({"time": 1, "content": (
        "## JAMIE'S COLLAPSE: SURVIVED!\n**Jamie's Collapse** (PvE survival)\n**2 made it to the end: Ardyan, NEJiii**\n"
        "Prize is handed out by the event staff.\nMost zombies killed: NEJiii 244, Ardyan 231\n"
        "**MVP: NEJiii (244 zombies, survived 30:00)**\nSeason so far (Oct): 1. NEJiii 30\n\n"
        "Next event: **Fri 20:00 (UTC+8)**. Sign up on JPhone > Collapse.")}, {"ardyan": 1, "nejiii": 2})
    head, survivors, board, mvp, small, nxt = _texts(post)
    assert head.endswith("SURVIVED!\n-# PvE SURVIVAL EVENT\n**2 players made it to the end!**")
    assert survivors == "-# SURVIVORS\n<@1> · <@2>"
    assert board == "**KILL LEADERBOARD**\n\U0001f947 <@2> · **244 kills**\n\U0001f948 <@1> · **231 kills**"
    assert mvp == "\U0001f451 **MVP · <@2>**\n244 kills · 30:00 survived"
    assert small == "-# Prize distribution handled by event staff.\n-# Season so far (Oct): 1. NEJiii 30"
    assert nxt.startswith("Next event:")
    assert post["mentions"] == [1, 2]
    _components(post)


def test_team_royale_and_blood_run_results():
    team = jc.render({"time": 1, "content": "## JAMIE'S COLLAPSE: Red WINS\n**Brawl** (Blood Toll)\n"
                                            "**Last team standing: ana & carl**\n**Prize: $500, $250 each**\n"
                                            "Most players eliminated: ana 3\nMost zombies killed: carl 40\n"
                                            "**MVP: ana (3 kills, 40 hits, 2 zombies, survived 20:00)**"}, {"ana": 7})
    head, winners, elim, zombies, mvp, prize = _texts(team)
    assert head.endswith("-# BRAWL · BLOOD TOLL EVENT\n**Red is the last team standing!**")
    assert winners == "-# WINNERS\n<@7> · `carl`"          # unlinked players as pills
    assert elim.startswith("**ELIMINATIONS**") and "**3 eliminated**" in elim
    assert zombies.startswith("**ZOMBIE KILLS**")
    assert mvp.endswith("3 kills · 40 hits · 2 zombies · 20:00 survived")
    assert prize == "\U0001f4b0 **Prize:** $500, $250 each"
    royale = jc.render({"time": 1, "content": "## JAMIE'S COLLAPSE: Blue WINS THE ROYALE\n**BR** (Battle Royale)\n"
                                              "**1st  Blue  (ana, bob)  5 kills**\n2nd  Red  (carl)  1 kill"})
    texts = _texts(royale)
    assert texts[0].endswith("**Blue wins the Royale!**")
    assert texts[1] == ("**PLACINGS**\n\U0001f947 **Blue** (ana, bob) · **5 kills**\n"
                        "\U0001f948 **Red** (carl) · **1 kill**")
    horde = jc.render({"time": 1, "content": "## JAMIE'S COLLAPSE: THE HORDE WINS\n**Night** (PvE survival)\nNobody survived."})
    assert _texts(horde)[0].endswith("-# NIGHT · PvE SURVIVAL EVENT\n**Nobody survived.**")
    run = jc.render({"time": 1, "content": "**Kingsmouth Run** is over.\nGot out (2 of 5): Ana, bob\nMost kills: bob (3)"})
    head, extracted, highlights = _texts(run)
    assert "-# JAMIE'S COLLAPSE · BLOOD RUN · RESULTS\n**2 of 5 got out!**" in head
    assert extracted == "-# EXTRACTED\n`Ana` · `bob`" and highlights == "Most kills: bob (3)"


def test_other_kinds_share_the_style():
    start = jc.render({"time": 1, "content": "## JAMIE'S COLLAPSE HAS STARTED\n**Friday Brawl** (Blood Toll), 6 players\n"
                                             "**THE COLLAPSE HAS BEGUN.**\nStay inside the arena."})
    assert _texts(start) == ["### \U0001f6a8 JAMIE'S COLLAPSE HAS STARTED\n-# FRIDAY BRAWL · BLOOD TOLL · LIVE NOW\n"
                             "**THE COLLAPSE HAS BEGUN.**\nStay inside the arena.", "-# PLAYERS\n### 6"]
    remind = jc.render({"time": 1000, "content": "## BLOOD RUN IN 15 MIN\n**Kingsmouth Run**: registration opens at **20:00**."})
    assert _texts(remind)[0].startswith("### ⏰ BLOOD RUN IN 15 MIN\n-# JAMIE'S COLLAPSE · COMING UP\n")
    assert _texts(remind)[1] == "-# STARTS\n### <t:1900:R>"
    board = jc.render({"time": 1, "content": "## LEADERBOARDS\n**Blood Toll, Oct 2026** Champion gets 5 reputation.\n"
                                             "1. ana: 30 pts (2 wins)\n4. bob: 1 pts\n\n**Blood Run, Oct 2026**\nNobody on it yet."},
                      LINKS)
    assert _texts(board)[1] == ("**BLOOD TOLL, OCT 2026**\n-# Champion gets 5 reputation.\n"
                                "\U0001f947 ana · **30 pts** · 2 wins\n`4.` bob · **1 pts**")
    assert _texts(board)[2] == "**BLOOD RUN, OCT 2026**\nNobody on it yet." and board["mentions"] == []
    champs = jc.render({"time": 1, "content": "## JAMIE'S COLLAPSE: SEASON CHAMPIONS\nOct 2026\n"
                                              "**Blood Toll:** ana (30 pts), 2nd bob, 3rd carl\nEach champion gets 5 reputation."})
    head, rows, notes = _texts(champs)
    assert head.endswith("-# OCT 2026")
    assert rows == "\U0001f451 **Blood Toll** · ana · **30 pts**\n-# 2nd bob · 3rd carl"
    over = jc.render({"time": 1, "content": "### OVERTIME\n5 more minutes in the final arena. 3 players still in."})
    assert over["colour"] == jc._RED and _texts(over)[-1] == "-# PLAYERS STILL IN\n### 3"
    off = jc.render({"time": 1, "content": "## JAMIE'S COLLAPSE: EVENT NIGHT CALLED OFF\nNot enough players."})
    assert _texts(off) == ["### \U0001f6ab JAMIE'S COLLAPSE: EVENT NIGHT CALLED OFF\n-# CALLED OFF\nNot enough players."]
    for post in (start, remind, board, champs, over, off):
        assert _components(post)["components"][-1]["content"].startswith("-# Jamie's Collapse")


def test_every_card_fits_discords_limits():
    long = jc.render({"time": 1, "content": "## JAMIE'S COLLAPSE: SURVIVED!\n**N** (PvE survival)\n" + "x" * 5000})
    card = _components(long)
    assert sum(len(c.get("content", "")) for c in card["components"]) <= 4000 and len(card["components"]) <= 40
    many = jc.render({"time": 1, "content": "## JAMIE'S COLLAPSE: VOTE\n**?**\n" + " | ".join(f"Opt {i}" for i in range(30))})
    rows = [c for c in _components(many)["components"] if c["type"] == 1]
    assert [len(r["components"]) for r in rows] == [5] * 5
