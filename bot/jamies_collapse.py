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
as a card in the collapse channel: the heading as the title, the rest as the
body, coloured by kind. Players named in a post who are linked with /linkme
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
    (r"VOTE", "vote", "\U0001f5f3️", 0x3498DB),
    (r"CHAMPION", "champion", "\U0001f451", 0xF1C40F),
    (r"LEADERBOARD", "board", "\U0001f4ca", 0x3498DB),
    (r"HAS STARTED|THEY'RE GOING IN", "start", "\U0001f6a8", 0xE74C3C),
    (r"THE HORDE WINS", "loss", "\U0001f9df", 0x992D22),
    (r"NO WINNER", "loss", "\U0001f480", 0x992D22),
    (r"SURVIVED| WINS|IS OVER", "win", "\U0001f3c6", 0xF1C40F),
    (r"REGISTRATION", "registration", "\U0001f4dd", 0x3498DB),
    (r"EXTRACTION", "live", "\U0001f681", 0xD35400),
    (r"JACKPOT", "live", "\U0001f48e", 0xD35400),
    (r"MOST WANTED", "live", "\U0001f3af", 0xD35400),
    (r"FLARE", "live", "\U0001f525", 0xD35400),
    (r"OVERTIME", "live", "⏱️", 0xD35400),
    (r"STORM", "live", "\U0001f32a️", 0xD35400),
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
    """The card for one post: title, description, colour, kind, footer and the
    Discord ids it pings (`links`: lower-cased PZ username -> Discord id)."""
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
    mentions = []
    if links and kind not in _NO_MENTION_KINDS:
        body, mentions = mention_names(body, links)
    if len(body) > _BODY_LIMIT:
        body = body[:_BODY_LIMIT - 1].rstrip() + "…"
        mentions = [did for did in mentions if f"<@{did}>" in body]
    footer = "Jamie's Collapse"
    if entry.get("time"):
        footer += f" • <t:{entry['time']}:f>"
    return {"title": f"{emoji} {heading or PREFIX}", "description": body, "colour": colour,
            "kind": kind, "footer": footer, "mentions": mentions}


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
        card = cards.card(title=post["title"], description=post["description"],
                          footer=post["footer"], colour=discord.Colour(post["colour"]))
        try:
            pings = discord.AllowedMentions(everyone=False, roles=False,
                                            users=[discord.Object(id=did) for did in post["mentions"]])
            await channel.send(view=cards.CardView(card), allowed_mentions=pings)
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
