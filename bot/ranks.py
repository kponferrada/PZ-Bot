"""ranks.py — the one rank table (name, chat colour, emoji, ANSI code).

Ranks 1-6 are the in-game chat name colours that Jeeve's Integration applies
(`JeevesIntegration.RankColor_<n>` in the server's SandboxVars, an index into
`JeevesRanks.COLOR_PALETTE`). Everything that shows a rank — /myrank,
/setrank, /linkme, /listlinks, the chat relay's ANSI colours — reads this
table, so the colour a player is told always matches the one they have.

There is no cyan square emoji, so Cyan uses the light blue heart, which keeps
it distinct from Spark's blue square.
"""

from typing import Dict, NamedTuple, Optional


class Rank(NamedTuple):
    name: str
    colour: str           # chat name colour in game
    emoji: str
    ansi: Optional[str]   # Discord ```ansi colour code, None = default text


RANKS: Dict[int, Rank] = {
    0: Rank("Default", "No color", "⬜", None),        # ⬜
    1: Rank("Fuel", "Green", "\U0001f7e9", "1;32"),        # 🟩
    2: Rank("Spark", "Blue", "\U0001f7e6", "1;34"),        # 🟦
    3: Rank("Cinder", "Violet", "\U0001f7ea", "1;35"),     # 🟪
    4: Rank("Flame", "Yellow", "\U0001f7e8", "1;33"),      # 🟨
    5: Rank("Blaze", "Cyan", "\U0001fa75", "1;36"),        # 🩵
    6: Rank("Inferno", "Red", "\U0001f7e5", "1;31"),       # 🟥
}


def display(rank: int) -> str:
    """"🩵 Blaze (Cyan)" — the label used in embeds."""
    r = RANKS.get(rank)
    return f"{r.emoji} {r.name} ({r.colour})" if r else str(rank)


def choice_label(rank: int) -> str:
    """"5 - Blaze (cyan)" — the /setrank choice label."""
    r = RANKS[rank]
    colour = "no color" if rank == 0 else r.colour.lower()
    return f"{rank} - {r.name} ({colour})"
