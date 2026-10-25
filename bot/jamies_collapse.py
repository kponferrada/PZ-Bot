"""jamies_collapse.py — event announcements from the Jamie's Collapse mod.

Jamie's Collapse (Workshop 3821310744; WIP 3821629861) appends one JSON line
per Discord post to `Lua/JamiesCollapse_discord.log` (TC_Server.lua
`S.discord`, the same format as Jamie's Lucky Draw), unless its admin panel's
Discord switch is off or the run is a test:

    {"time":1792900000,"content":"## JAMIE'S COLLAPSE: SURVIVED!\\n**Night Run** (PvE)\\n..."}

`content` is Discord markdown whose first line is a `## JAMIE'S COLLAPSE...`
heading: event start, results (a team wins, SURVIVED!, THE HORDE WINS, NO
WINNER), event night scheduled / on / called off, votes, season champion.

This cog tails that file over SFTP (like `jamies_fortune`) and posts each line
as a card in the collapse channel: the heading as the title, the rest as the
body, coloured by kind. The text comes from players (event, team and player
names), so nothing in it can ping anyone.
"""

import json
from collections import deque
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands, tasks

import cards
import lua_bridge
import sftp_client

PREFIX = "JAMIE'S COLLAPSE"

# (heading contains, kind, emoji, colour), first match wins: "THE HORDE WINS"
# must be a loss before " WINS" is a win.
_KINDS = (
    ("CALLED OFF", "called_off", "\U0001f6ab", 0x95A5A6),
    ("EVENT NIGHT", "night", "\U0001f319", 0xE67E22),
    ("VOTE", "vote", "\U0001f5f3️", 0x3498DB),
    ("SEASON CHAMPION", "champion", "\U0001f451", 0xF1C40F),
    ("HAS STARTED", "start", "\U0001f6a8", 0xE74C3C),
    ("THE HORDE WINS", "loss", "\U0001f9df", 0x992D22),
    ("NO WINNER", "loss", "\U0001f480", 0x992D22),
    ("SURVIVED", "win", "\U0001f3c6", 0xF1C40F),
    (" WINS", "win", "\U0001f3c6", 0xF1C40F),
)
_OTHER = ("other", "\U0001f4e3", 0x7F8C8D)

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


def kind_of(heading: str) -> tuple:
    """(kind, emoji, colour) for a post's heading."""
    upper = heading.upper()
    for needle, kind, emoji, colour in _KINDS:
        if needle in upper:
            return kind, emoji, colour
    return _OTHER


def render(entry: dict) -> dict:
    """The card for one post: title, description, colour, kind and footer."""
    lines = entry["content"].replace("\r", "").strip().split("\n")
    heading = ""
    if lines and lines[0].startswith("#"):
        heading = lines.pop(0).lstrip("#").strip()
    kind, emoji, colour = kind_of(heading)
    body = "\n".join(lines).strip()
    if len(body) > _BODY_LIMIT:
        body = body[:_BODY_LIMIT - 1].rstrip() + "…"
    footer = "Jamie's Collapse"
    if entry.get("time"):
        footer += f" • <t:{entry['time']}:f>"
    return {"title": f"{emoji} {heading or PREFIX}", "description": body, "colour": colour,
            "kind": kind, "footer": footer}


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

    async def _handle_line(self, line: str) -> None:
        entry = parse_line(line)
        if entry is None:
            return
        key = (entry["time"], entry["content"])
        if key in self._seen:
            return
        self._seen.append(key)

        post = render(entry)
        if not self.bot.features.is_enabled("collapse"):
            print(f"[JamiesCollapse] {post['title']} (notifications disabled)")
            return

        channel = self.bot.get_collapse_channel()
        if not channel:
            return
        card = cards.card(title=post["title"], description=post["description"],
                          footer=post["footer"], colour=discord.Colour(post["colour"]))
        try:
            await channel.send(view=cards.CardView(card), allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"[JamiesCollapse] Failed to send announcement: {e}")
            return
        when = datetime.fromtimestamp(entry["time"], timezone.utc).isoformat() if entry["time"] else "?"
        print(f"[JamiesCollapse] Posted {post['kind']}: {post['title']} ({when})")

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
