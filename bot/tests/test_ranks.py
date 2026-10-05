"""The rank tables shown to players all come from ranks.RANKS and agree.

Run from bot/:  python -m pytest tests
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chat_relay  # noqa: E402
import rank_sync  # noqa: E402
import ranks  # noqa: E402

# Server SandboxVars JeevesIntegration.RankColor_<n> -> JeevesRanks.COLOR_PALETTE
# name, as set on the live server. Blaze (5) is palette 7 = Cyan.
LIVE_COLOURS = {1: "Green", 2: "Blue", 3: "Violet", 4: "Yellow", 5: "Cyan", 6: "Red"}

# Colour name -> the ANSI foreground code chat_relay should use for it.
ANSI_FOR = {"Green": "32", "Blue": "34", "Violet": "35", "Yellow": "33",
            "Cyan": "36", "Red": "31"}


def test_colours_match_the_live_server():
    for n, colour in LIVE_COLOURS.items():
        assert ranks.RANKS[n].colour == colour


def test_blaze_is_cyan_with_its_own_emoji():
    blaze = ranks.RANKS[5]
    assert (blaze.name, blaze.colour, blaze.ansi) == ("Blaze", "Cyan", "1;36")
    emojis = [r.emoji for r in ranks.RANKS.values()]
    assert len(set(emojis)) == len(emojis)


def test_chat_relay_ansi_matches_colour_names():
    assert chat_relay.ANSI_COLORS[0] is None
    for n, r in ranks.RANKS.items():
        if n:
            assert chat_relay.ANSI_COLORS[n].endswith(ANSI_FOR[r.colour])


def test_rank_sync_tables_agree():
    for n, r in ranks.RANKS.items():
        assert rank_sync.RANK_DISPLAY[n] == f"{r.emoji} {r.name} ({r.colour})"
    assert {name: n for name, n in rank_sync.ROLE_TO_RANK.items()} == {
        r.name: n for n, r in ranks.RANKS.items() if n}
    choices = rank_sync.RankSync.cmd_setrank._params["rank"].choices
    assert [(c.name, c.value) for c in choices] == [
        (ranks.choice_label(n), n) for n in ranks.RANKS]
    assert choices[5].name == "5 - Blaze (cyan)"
