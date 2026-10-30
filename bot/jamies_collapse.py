"""jamies_collapse.py — event announcements from the Jamie's Collapse mod.

Jamie's Collapse (Workshop 3821310744; WIP 3821629861) appends one JSON line
per Discord post to `Lua/JamiesCollapse_discord.log` (TC_Server.lua
`S.discord`, the same format as Jamie's Lucky Draw), unless its admin panel's
Discord switch is off or the run is a test:

    {"time":1792900000,"content":"## JAMIE'S COLLAPSE: SURVIVED!\\n**Night Run** (PvE)\\n..."}

`content` is Discord markdown (read against 1.10.42):
  `## JAMIE'S COLLAPSE...`  event start, results (a team wins, SURVIVED!, THE
                            HORDE WINS, NO WINNER, EVENT ABORTED), event night
                            scheduled / on / called off, reminders (... IN 15
                            MIN), votes, season champions
  `## BLOOD RUN IN 15 MIN`, `## LEADERBOARDS`
  `### <NAME>: ...`         live moments: a collapse, overtime, the storm, and
                            the Blood Run's deploy, extraction, jackpot, most
                            wanted, flare, cancelled
  no heading                Blood Run registration open, results ("**Run** is
                            over."), Blood Run champion

This cog tails that file over SFTP (like `jamies_fortune`) and posts each line
as a Components V2 card in the collapse channel, coloured by kind (layouts
from the user's mockups, 2026-10-30): the heading as the title, a small
subtitle, a bold lead line, stat tiles (a grey label over a large value,
stacked: V2 has no columns), separator lines, the footer. A collapse is
"arena n of m" with the players still in and the next collapse (red "final
stand" in the last arena); results get survivors, medal kill boards and the
MVP; vote options are coloured disabled buttons. Players named in a post who are linked with /linkme
(rank_links.json) are pinged in place of their name (toggle
`collapse_mentions`); the mod writes usernames (`TC.nameOf` = getUsername).
Only those users can be pinged: the rest of the text (event names, notes) is
typed in game and pings nobody.
"""

import json
import re
from collections import deque
from datetime import datetime, timezone
from typing import Dict, Optional

import discord
from discord.ext import commands, tasks

import cards
import lua_bridge
import sftp_client

PREFIX = "JAMIE'S COLLAPSE"

# (pattern on the upper-cased heading, kind, emoji, colour), first match wins:
# reminders before "EVENT NIGHT", "THE HORDE WINS" a loss before " WINS" a win.
_KINDS = tuple((re.compile(p), kind, emoji, colour) for p, kind, emoji, colour in (
    (r"\bIN \d+ MIN\b", "reminder", "⏰", 0xE67E22),
    (r"CALLED OFF|ABORTED|CANCELLED", "called_off", "\U0001f6ab", 0x95A5A6),
    (r"EVENT NIGHT", "night", "\U0001f319", 0xE67E22),
    (r"VOTE", "vote", "📊", 0x3498DB),
    (r"CHAMPION", "champion", "\U0001f451", 0xF1C40F),
    (r"LEADERBOARD", "board", "🏅", 0x3498DB),
    (r"HAS STARTED|THEY'RE GOING IN", "start", "\U0001f6a8", 0xE74C3C),
    (r"THE HORDE WINS", "loss", "\U0001f9df", 0x992D22),
    (r"NO WINNER", "loss", "\U0001f480", 0x992D22),
    (r"SURVIVED| WINS|IS OVER", "win", "\U0001f3c6", 0xF1C40F),
    (r"REGISTRATION", "registration", "\U0001f4dd", 0x3498DB),
    (r"EXTRACTION", "live", "\U0001f681", 0xD35400),
    (r"JACKPOT", "live", "\U0001f48e", 0xD35400),
    (r"MOST WANTED", "live", "\U0001f3af", 0xD35400),
    (r"FLARE", "live", "\U0001f525", 0xD35400),
    (r"OVERTIME", "live", "⏱️", 0xE74C3C),
    (r"STORM", "live", "🌪️", 0xE74C3C),
))
_OTHER = ("other", "\U0001f4e3", 0x7F8C8D)
# A `###` heading is a live moment (TC_Discord `matchLive` / `runLive`): a
# collapse is headed by its own name, so anything unknown there is live too.
_LIVE = ("live", "⚡", 0xD35400)

# Posts without a heading (Blood Run, TC_Run.lua): (first line, title, take
# the matched part off the body).
_HEADLESS = (
    (re.compile(r"^\*\*(.+?)\*\*: registration is open"), lambda m: f"{m.group(1)}: REGISTRATION OPEN", False),
    (re.compile(r"^\*\*(.+?)\*\* is over\.\s*"), lambda m: f"{m.group(1)} IS OVER", True),
    (re.compile(r"^\*\*(.+? Champion\b.*?)\*\*:\s*"), lambda m: m.group(1), True),
)

# Kinds whose names are not the event's attendees: nobody is pinged.
_NO_MENTION_KINDS = {"board"}
# Lines that rank the whole season, not this event: left as plain names.
_NO_MENTION_LINES = ("Season so far",)
# Shorter linked names are too likely to be part of ordinary text.
_MIN_NAME = 3

# A card's text block holds 4000 characters; leave room for the title.
_BODY_LIMIT = 3800


def parse_line(line: str) -> Optional[dict]:
    """{"time": int | None, "content": str} for one log line, or None if it isn't one."""
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    if not isinstance(entry, dict):
        return None
    content = entry.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    when = entry.get("time")
    return {"time": int(when) if isinstance(when, (int, float)) else None, "content": content}


def kind_of(heading: str, live: bool = False) -> tuple:
    """(kind, emoji, colour) for a post's heading; `live` for a `###` heading."""
    upper = heading.upper()
    for pattern, kind, emoji, colour in _KINDS:
        if pattern.search(upper):
            return kind, emoji, colour
    return _LIVE if live else _OTHER


def mention_names(body: str, links: Dict[str, int]) -> tuple:
    """Each linked username in `body` (whole word, any case) -> `<@id>`.
    `links` maps lower-cased PZ username -> Discord id. -> (text, [ids])."""
    names = sorted((n for n in links if len(n) >= _MIN_NAME), key=len, reverse=True)
    if not names or not body:
        return body, []
    pattern = re.compile(r"(?<![\w])(" + "|".join(re.escape(n) for n in names) + r")(?![\w])", re.IGNORECASE)
    ids = []

    def swap(m):
        did = links[m.group(1).lower()]
        if did not in ids:
            ids.append(did)
        return f"<@{did}>"

    out = []
    for line in body.split("\n"):
        out.append(line if line.startswith(_NO_MENTION_LINES) else pattern.sub(swap, line))
    return "\n".join(out), ids


def render(entry: dict, links: Optional[Dict[str, int]] = None) -> dict:
    """The card for one post: title, description (the body, names pinged),
    colour, kind, footer, the Discord ids it pings (`links`: lower-cased PZ
    username -> Discord id) and `layout`, the card's parts for `card`."""
    lines = entry["content"].replace("\r", "").strip().split("\n")
    heading, live = "", False
    if lines and lines[0].startswith("#"):
        first = lines.pop(0)
        live = first.startswith("###")
        heading = first.lstrip("#").strip()
    elif lines:
        for pattern, title, take in _HEADLESS:
            m = pattern.match(lines[0])
            if m:
                heading = title(m).upper()
                if take:
                    lines[0] = lines[0][m.end():]
                break
    kind, emoji, colour = kind_of(heading, live)
    body = "\n".join(lines).strip()
    if live and _ARENA.match(body):
        kind, emoji = "collapse", "⚡"
    mentions = []
    if links and kind not in _NO_MENTION_KINDS:
        body, mentions = mention_names(body, links)
    if len(body) > _BODY_LIMIT:
        body = body[:_BODY_LIMIT - 1].rstrip() + "…"
        mentions = [did for did in mentions if f"<@{did}>" in body]
    footer = "Jamie's Collapse"
    if entry.get("time"):
        footer += f" • <t:{entry['time']}:f>"
    post = {"title": f"{emoji} {heading or PREFIX}", "description": body, "colour": colour, "kind": kind,
            "footer": footer, "mentions": mentions}
    build = _LAYOUTS.get(kind, _layout_plain)
    post["layout"] = build(post, heading, body.split("\n") if body else [], entry.get("time"))
    return post


# --- Card layouts -----------------------------------------------------------
# A V2 card has no columns or tile backgrounds, so a "tile" is a small grey
# label over a large value, stacked (like the status card), and the parts are
# split by separator lines. Vote options are disabled buttons: the only thing
# in V2 that sits side by side with its own colour.
# Layout items: ("text", markdown), ("gap", visible line), ("buttons", [(label, emoji, style)]).

_ORANGE, _RED = 0xF39C12, 0xE74C3C
_MEDALS = ("\U0001f947", "\U0001f948", "\U0001f949")
_SUBTITLES = {"start": "LIVE NOW", "reminder": "COMING UP", "night": "EVENT NIGHT", "called_off": "CALLED OFF",
              "vote": "COMMUNITY POLL", "champion": "SEASON CHAMPIONS", "board": "STANDINGS",
              "registration": "BLOOD RUN", "live": "LIVE", "win": "RESULTS", "loss": "RESULTS"}

# "**Night Run** (PvE survival)" and, on a start, ", 4 players".
_EVENT = re.compile(r"^\*\*(.+?)\*\* \(([^)]+)\)(?:, (\d+) players?)?$")
# A collapse (TC_Discord matchLive): "Arena 3 of 4: **Name**. 2 players (2 teams) still in."
_ARENA = re.compile(r"^Arena (\d+) of (\d+): \*\*(.*?)\*\*\.\s*(.*)")
_STILL = re.compile(r"\s*(\d+) players? (?:\((\d+) teams?\) )?still in\.")
_NEXT = re.compile(r"^Next collapse in ([\d:]+)\.$")
_LEFT = re.compile(r"^([\d:]+) left on the clock\.$")
_VOTE_HINT = re.compile(r"^Vote in game")
# Vote option -> (button style, emoji), by the mode before any ": preset".
_VOTE_MODES = {"blood toll": ("danger", "\U0001f480"), "battle royale": ("primary", "⚔️"),
               "pve survival": ("success", "\U0001f9df")}
_PVP_MODES = {"blood toll", "battle royale"}

# Results (TC_Server S.finish, TC_Extras MVP lines, TC_Run results), bold already taken off.
_MADE = re.compile(r"^(\d+) made it to the end: (.+)$")
_TEAM = re.compile(r"^Last team standing: (.+)$")
_PLACE = re.compile(r"^(\d+)(?:st|nd|rd|th)\s+(.+?)\s+\((.*)\)\s+(\d+) kills?$")
_GOT_OUT = re.compile(r"^Got out \((\d+) of (\d+)\): (.+)$")
_MOST = re.compile(r"^Most (players eliminated|zombies killed): (.+)$")
_MVP = re.compile(r"^MVP: (.+?) \((.+)\)$")
_PRIZE = re.compile(r"^Prize(?: is handed out by the event staff\.|: (.+))$")
_HIGHLIGHTS = ("Most damage:", "Hung on the longest:", "Bounty hunter:", "Bounty:", "Biggest haul:", "Most kills:",
               "TRAYDOR:", "Dog tags carried out:", "Pot:")
_SMALL = ("(test run", "Season so far", "It doesn't count", "Each champion gets")
_NEXT_EVENT = ("Next event:", "Blood Run:")
# Leaderboards (TC_Discord boardsText) and season champions (TC_Extras).
_BOARD_ROW = re.compile(r"^(\d+)\. (.+?): (\d+ pts)(?: \((.*)\))?$")
_CHAMP_ROW = re.compile(r"^\*\*(.+?):\*\* (.+?) \((\d+ pts)\)(?:, (.*))?$")


def _upper(text: str) -> str:
    """Upper case, but PvE / PvP keep their case ("PvE survival" -> "PvE SURVIVAL")."""
    return re.sub(r"\bPV([EP])\b", r"Pv\1", text.upper())


def _plain(line: str) -> str:
    """The line without the bold the mod puts round a whole highlighted line."""
    if len(line) > 4 and line.startswith("**") and line.endswith("**") and line.count("**") == 2:
        return line[2:-2]
    return line


def _subtitle(post: dict, *parts) -> str:
    """JAMIE'S COLLAPSE · parts, without repeating the name the title already has."""
    parts = [p for p in parts if p]
    if PREFIX not in post["title"].upper():
        parts.insert(0, PREFIX)
    return " · ".join(parts)


def _event_parts(m) -> list:
    """Subtitle parts for an event line: its name (unless it's the default) and mode."""
    name = [] if m.group(1).strip().upper() == PREFIX else [_upper(m.group(1))]
    return name + [_upper(m.group(2))]


def _head(post: dict, subtitle: str = "", lead: str = "", body=()) -> tuple:
    """Title, small grey subtitle, bold lead line, then the body."""
    parts = [f"### {post['title']}"]
    if subtitle:
        parts.append(f"-# {subtitle}")
    if lead:
        parts.append(lead if lead.startswith("**") else f"**{lead}**")
    parts.append("\n".join(body).strip())
    return ("text", "\n".join(p for p in parts if p))


def _tile(label: str, value: str, note: str = "") -> tuple:
    """A stat tile: small grey label over a large value."""
    return ("text", f"-# {label}\n### {value}" + (f"\n-# {note}" if note else ""))


def _seconds(clock: str) -> int:
    total = 0
    for part in clock.split(":"):
        total = total * 60 + int(part or 0)
    return total


def _who(name: str) -> str:
    """A pinged player as is, anyone else as a code pill (looks like a mention)."""
    name = name.strip()
    return name if name.startswith("<@") else f"`{name}`"


def _layout_plain(post: dict, heading: str, lines: list, when) -> list:
    """Title, subtitle, the body as written."""
    sub = [_SUBTITLES.get(post["kind"], "")]
    m = _EVENT.match(lines[0]) if lines else None
    if m:
        sub = _event_parts(m) + sub
        lines = lines[1:]
    return [_head(post, _subtitle(post, *sub), body=lines)]


def _layout_start(post: dict, heading: str, lines: list, when) -> list:
    """Event and mode as the subtitle, the intro, the players going in."""
    m = _EVENT.match(lines[0]) if lines else None
    if not m:
        return _layout_plain(post, heading, lines, when)
    rest = lines[1:]
    lead = rest.pop(0) if rest else ""
    out = [_head(post, _subtitle(post, *_event_parts(m), "LIVE NOW"), lead, rest)]
    if m.group(3):
        out += [("gap", True), _tile("PLAYERS", m.group(3))]
    return out


def _layout_reminder(post: dict, heading: str, lines: list, when) -> list:
    """The body, then when it starts (a live Discord countdown)."""
    out = _layout_plain(post, heading, lines, when)
    mins = re.search(r"\bIN (\d+) MIN\b", heading.upper())
    if mins:
        value = f"<t:{when + int(mins.group(1)) * 60}:R>" if when else f"in {mins.group(1)} min"
        out += [("gap", True), _tile("STARTS", value)]
    return out


def _layout_collapse(post: dict, heading: str, lines: list, when) -> list:
    """A collapse (mockups: SECOND / CRITICAL COLLAPSE): arena n of m, the
    players still in, the next collapse or, in the last arena, the final stand."""
    m = _ARENA.match(lines[0])
    stage, total, name, rest = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
    final = stage >= total
    post["colour"] = _RED if final else _ORANGE
    word = re.match(r"^(\w+) COLLAPSE$", heading.upper())
    if final:
        blurb = "The final collapsed zone is now active. Only the last survivors remain."
    elif word:
        blurb = f"The {word.group(1).lower()} collapsed zone is now active."
    else:
        blurb = f"Arena {stage} of {total} is now active."
    still = _STILL.search(rest)
    body = [blurb, _STILL.sub("", rest).strip()]
    tiles = []
    if still:
        n, teams = int(still.group(1)), still.group(2)
        value = str(n) if final else f"{n} player{'' if n == 1 else 's'}"
        if teams:
            value += f" · {teams} team{'' if teams == '1' else 's'}"
        tiles.append(_tile("PLAYERS STILL IN" if final else "PLAYERS REMAINING", value))
    if final:
        tiles.append(_tile("ZONE STATUS", "\U0001f534 CRITICAL"))
    for line in lines[1:]:
        hit = _NEXT.match(line) or _LEFT.match(line)
        if hit:
            note = f"<t:{when + _seconds(hit.group(1))}:R>" if when else ""
            label = "NEXT COLLAPSE" if hit.re is _NEXT else "TIME LEFT"
            tiles.append(_tile(label, f"⏳ {hit.group(1)}", note))
        else:
            body.append(line)
    out = [_head(post, _subtitle(post, f"ARENA {stage} OF {total}"), name, body)]
    if tiles:
        out += [("gap", True)] + tiles
    if final:
        out += [("gap", True), ("text", "⚠️ **This is the final stand.**\nStay alive. There is no next arena.")]
    return out


def _layout_live(post: dict, heading: str, lines: list, when) -> list:
    """Overtime, the storm, Blood Run moments: the body, the players still in."""
    still = _STILL.search(lines[0]) if lines else None
    if still:
        lines = [_STILL.sub("", lines[0]).strip()] + lines[1:]
    out = [_head(post, _subtitle(post, "LIVE"), body=lines)]
    if still:
        value = still.group(1) + (f" · {still.group(2)} teams" if still.group(2) else "")
        out += [("gap", True), _tile("PLAYERS STILL IN", value)]
    return out


def _layout_vote(post: dict, heading: str, lines: list, when) -> list:
    """The question, then each option as a coloured (disabled) button."""
    if len(lines) < 2:
        return _layout_plain(post, heading, lines, when)
    question, options = _plain(lines[0]), [o.strip() for o in lines[1].split(" | ") if o.strip()]
    notes = []
    for line in lines[2:]:
        if _VOTE_HINT.match(line):
            post["footer"] = "Vote in-game: JPhone → Collapse → Stats"
        elif line:
            notes.append(line)
    modes = {o.split(":")[0].strip().lower() for o in options}
    sub = "PvP · COMMUNITY POLL" if modes and modes <= _PVP_MODES else (
        "PvE · COMMUNITY POLL" if modes == {"pve survival"} else "COMMUNITY POLL")
    buttons = []
    for o in options:
        style, emoji = _VOTE_MODES.get(o.split(":")[0].strip().lower(), ("secondary", None))
        buttons.append((o, emoji, style))
    return [_head(post, _subtitle(post, sub), question, notes), ("gap", True), ("buttons", buttons)]


def _mvp_why(why: str) -> str:
    """"244 zombies, survived 30:00" -> "244 kills · 30:00 survived" (zombie
    kills are the kills when nobody killed a player)."""
    parts = why.split(", ")
    players = any(re.match(r"^\d+ kills?$", p) for p in parts)
    out = []
    for p in parts:
        z, s = re.match(r"^(\d+) zombies?$", p), re.match(r"^survived (.+)$", p)
        if z and not players:
            p = f"{z.group(1)} kill{'' if z.group(1) == '1' else 's'}"
        elif s:
            p = f"{s.group(1)} survived"
        out.append(p)
    return " · ".join(out)


def _board_rows(text: str, players: bool) -> list:
    """"bob 40, ana 2" -> medal rows, the count in bold."""
    rows = []
    for i, part in enumerate(text.split(", ")):
        pm = re.match(r"^(.*\S)\s+(\d+)$", part)
        name, n = (pm.group(1), pm.group(2)) if pm else (part, "")
        unit = "eliminated" if players else f"kill{'' if n == '1' else 's'}"
        rows.append(f"{_MEDALS[i] if i < 3 else '•'} {name}" + (f" · **{n} {unit}**" if n else ""))
    return rows


def _layout_results(post: dict, heading: str, lines: list, when) -> list:
    """Who made it, the kill boards, the MVP, the prize (mockup: SURVIVED!)."""
    sub = [_SUBTITLES.get(post["kind"], "")]
    m = _EVENT.match(lines[0]) if lines else None
    if m:
        sub = _event_parts(m)
        sub[-1] += " EVENT"
        lines = lines[1:]
    elif "IS OVER" in heading.upper():
        sub = ["BLOOD RUN", "RESULTS"]
    lead, group, boards, mvp, highlights, notes, small, nexts = "", None, [], [], [], [], [], []
    for raw in lines:
        line = _plain(raw.strip())
        if not line:
            continue
        made, team, place, got, most, best, prize = (p.match(line) for p in (
            _MADE, _TEAM, _PLACE, _GOT_OUT, _MOST, _MVP, _PRIZE))
        if made:
            n = int(made.group(1))
            lead = f"{n} player{'' if n == 1 else 's'} made it to the end!"
            group = ("SURVIVORS", made.group(2).split(", "))
        elif team:
            name = re.search(r": (.+?) WINS$", heading)
            lead = f"{name.group(1) if name else 'A team'} is the last team standing!"
            group = ("WINNERS", team.group(1).split(" & "))
        elif place:
            if not boards or boards[-1][0] != "PLACINGS":
                boards.append(("PLACINGS", []))
                lead = lead or f"{place.group(2)} wins the Royale!"
            i = len(boards[-1][1])
            medal = _MEDALS[i] if i < 3 else f"`{place.group(1)}.`"
            boards[-1][1].append(f"{medal} **{place.group(2)}** ({place.group(3)}) · **{place.group(4)} kill{'' if place.group(4) == '1' else 's'}**")
        elif got:
            lead = f"{got.group(1)} of {got.group(2)} got out!"
            group = ("EXTRACTED", got.group(3).split(", "))
        elif most:
            players = most.group(1) == "players eliminated"
            boards.append(("ELIMINATIONS" if players else "KILL LEADERBOARD", _board_rows(most.group(2), players)))
        elif best:
            mvp = [f"\U0001f451 **MVP · {best.group(1)}**", _mvp_why(best.group(2))]
        elif prize:
            small.append(f"\U0001f4b0 **Prize:** {prize.group(1)}" if prize.group(1)
                         else "-# Prize distribution handled by event staff.")
        elif line.startswith(_HIGHLIGHTS):
            highlights.append(line)
        elif line.startswith(_SMALL):
            small.append(f"-# {line}")
        elif line.startswith(_NEXT_EVENT):
            nexts.append(line)
        else:
            notes.append(line)
    if not lead and notes:
        lead = notes.pop(0)
    if any(label == "ELIMINATIONS" for label, _ in boards):
        boards = [("ZOMBIE KILLS" if label == "KILL LEADERBOARD" else label, rows) for label, rows in boards]
    out = [_head(post, _subtitle(post, *sub), lead, notes)]
    if group:
        out += [("gap", True), ("text", f"-# {group[0]}\n" + " · ".join(_who(n) for n in group[1] if n.strip()))]
    for i, (label, rows) in enumerate(boards):
        out += [("gap", i == 0), ("text", f"**{label}**\n" + "\n".join(rows))]
    if mvp or highlights:
        out += [("gap", True), ("text", "\n".join(mvp + highlights))]
    if small:
        out += [("gap", not (mvp or highlights)), ("text", "\n".join(small))]
    if nexts:
        out += [("gap", True), ("text", "\n".join(nexts))]
    return out


def _layout_board(post: dict, heading: str, lines: list, when) -> list:
    """Each board (split by blank lines) under its name, medals on the top three."""
    groups, cur = [], []
    for line in lines + [""]:
        if line.strip():
            cur.append(line)
        elif cur:
            groups.append(cur)
            cur = []
    out = [_head(post, _subtitle(post, "STANDINGS"))]
    for group in groups:
        rows = []
        for line in group:
            row = _BOARD_ROW.match(line)
            head = re.match(r"^\*\*(.+?)\*\*(.*)$", line)
            if row:
                i = int(row.group(1))
                medal = _MEDALS[i - 1] if i <= 3 else f"`{i}.`"
                rows.append(f"{medal} {row.group(2)} · **{row.group(3)}**" + (f" · {row.group(4)}" if row.group(4) else ""))
            elif head and not rows:
                extra = head.group(2).strip()
                rows.append(f"**{_upper(head.group(1))}**" + (f"\n-# {extra}" if extra else ""))
            else:
                rows.append(line)
        out += [("gap", True), ("text", "\n".join(rows))]
    return out


def _layout_champion(post: dict, heading: str, lines: list, when) -> list:
    """Season champions: one crowned line per board, 2nd and 3rd under it."""
    if not any(_CHAMP_ROW.match(line) for line in lines):
        return _layout_plain(post, heading, lines, when)
    label = "" if _CHAMP_ROW.match(lines[0]) else lines[0]
    rows, notes = [], []
    for line in lines[1 if label else 0:]:
        row = _CHAMP_ROW.match(line)
        if row:
            rows.append(f"\U0001f451 **{row.group(1)}** · {row.group(2)} · **{row.group(3)}**" +
                        (f"\n-# {row.group(4).replace(', ', ' · ')}" if row.group(4) else ""))
        elif line.strip():
            notes.append(f"-# {line}" if line.startswith(_SMALL) else line)
    out = [_head(post, _subtitle(post, _upper(label) if label else "SEASON CHAMPIONS")), ("gap", True),
           ("text", "\n".join(rows))]
    if notes:
        out.append(("text", "\n".join(notes)))
    return out


_LAYOUTS = {"start": _layout_start, "reminder": _layout_reminder, "collapse": _layout_collapse,
            "live": _layout_live, "vote": _layout_vote, "win": _layout_results, "loss": _layout_results,
            "board": _layout_board, "champion": _layout_champion}


def card(post: dict) -> discord.ui.Container:
    """The post's layout as a V2 card, the footer last, the kind's colour as the accent bar."""
    items, gap = [], None
    for item in post["layout"] + [("text", f"-# {post['footer']}")]:
        if item[0] == "gap":
            gap = bool(gap) or item[1]  # gaps in a row make one, a line if any is
            continue
        if item[0] == "text" and not item[1].strip():
            continue
        if gap is not None and items:
            items.append(discord.ui.Separator(visible=gap))
        gap = None
        if item[0] == "text":
            items.append(discord.ui.TextDisplay(item[1][:cards._TEXT_LIMIT]))
        elif item[0] == "buttons":
            buttons = [discord.ui.Button(label=label[:80], emoji=emoji, disabled=True,
                                         style=getattr(discord.ButtonStyle, style))
                       for label, emoji, style in item[1][:25]]
            for i in range(0, len(buttons), 5):
                items.append(discord.ui.ActionRow(*buttons[i:i + 5]))
    return discord.ui.Container(*items, accent_colour=discord.Colour(post["colour"]))


class JamiesCollapseCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._lua_dir = getattr(bot.config, "SFTP_LUA_DIR", None)
        self._file: Optional[str] = None
        self._pos = 0
        self._buffer = ""
        self._saw_absent = False  # True once we've observed the file missing
        # Recently posted lines (time + content): guards against re-posting when
        # the file is rewritten and we re-read it from the start.
        self._seen: deque = deque(maxlen=200)
        self._active = bool(self._lua_dir)
        if not self._active:
            print("[JamiesCollapse] Disabled (SFTP_LUA_DIR not set).")
        else:
            self._tail.start()
            print(f"[JamiesCollapse] Watching {lua_bridge.JAMIES_COLLAPSE_FILE}")

    def cog_unload(self):
        if self._active:
            self._tail.cancel()

    def _log_path(self) -> str:
        return f"{self._lua_dir.rstrip('/')}/{lua_bridge.JAMIES_COLLAPSE_FILE}"

    def _links(self) -> Dict[str, int]:
        """Lower-cased PZ username -> Discord id for every /linkme link, or {}
        when pings are off or RankSync isn't loaded."""
        if not self.bot.features.is_enabled("collapse_mentions"):
            return {}
        rank_cog = self.bot.get_cog("RankSync")
        return rank_cog.discord_ids_by_pz_username() if rank_cog else {}

    async def _handle_line(self, line: str) -> None:
        entry = parse_line(line)
        if entry is None:
            return
        key = (entry["time"], entry["content"])
        if key in self._seen:
            return
        self._seen.append(key)

        post = render(entry, self._links())
        if not self.bot.features.is_enabled("collapse"):
            print(f"[JamiesCollapse] {post['title']} (notifications disabled)")
            return

        channel = self.bot.get_collapse_channel()
        if not channel:
            return
        try:
            pings = discord.AllowedMentions(everyone=False, roles=False,
                                            users=[discord.Object(id=did) for did in post["mentions"]])
            view = cards.CardView(card(post))
            await channel.send(view=view, allowed_mentions=pings)
            view.stop()  # the vote buttons are disabled: nothing to listen for
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"[JamiesCollapse] Failed to send announcement: {e}")
            return
        when = datetime.fromtimestamp(entry["time"], timezone.utc).isoformat() if entry["time"] else "?"
        pinged = f", pinged {len(post['mentions'])}" if post["mentions"] else ""
        print(f"[JamiesCollapse] Posted {post['kind']}: {post['title']} ({when}{pinged})")

    @tasks.loop(seconds=2.0)
    async def _tail(self):
        if not self._active:
            return
        try:
            sftp = sftp_client.get()
            path = self._log_path()
            st = await sftp.stat(path)
            if st is None:
                # Mod not installed / nothing announced yet.
                self._file = None
                self._pos = 0
                self._buffer = ""
                self._saw_absent = True
                return

            size = st[0]
            if self._file is None:
                # File just appeared. If we had seen it missing (created by the
                # first post), read from the start so that post isn't dropped;
                # on startup with an existing log, skip history.
                self._pos = 0 if self._saw_absent else size
                self._file = path
                self._buffer = ""
            elif size < self._pos:
                # File truncated (cleared by hand / rotated): start over.
                self._pos = 0
                self._buffer = ""

            if size == self._pos:
                return  # nothing new; skip opening the file

            text, self._pos = await sftp.tail(path, self._pos)
            if not text:
                return

            self._buffer += text
            lines = self._buffer.split("\n")
            self._buffer = lines[-1]  # keep any trailing (incomplete) line
            for line in lines[:-1]:
                line = line.strip()
                if line:
                    await self._handle_line(line)

        except sftp_client.SftpError:
            pass
        except Exception as e:
            print(f"[JamiesCollapse] tail error: {e}")

    @_tail.before_loop
    async def _before_tail(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(JamiesCollapseCog(bot))
    print("[JamiesCollapse] Extension loaded.")
