"""status_notice.py — a one-line "server down / back up" notice for extra channels.

The full up/down banners go to SERVER_NOTIFICATION_CHANNEL_ID. Other channels
(general chat, a players' channel) get only this short line, so players see
at a glance when the server goes down and when it is back (user, 2026-10-26).

Channels: STATUS_NOTICE_CHANNEL_IDS (comma-separated; empty = off), with an
optional role to mention, STATUS_NOTICE_ROLE_ID. Toggle: the `status_notices`
feature. The monitor in main.py sends one "down" per outage, after the same
debounce the banners use (so brief RCON blips post nothing), and an "up" only
after a "down" was posted.
"""
from __future__ import annotations

import time

import discord


def channel_ids(raw: str) -> list:
    """Channel ids from "123, 456" (anything that isn't a number is skipped)."""
    out = []
    for part in str(raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit() and int(part) not in out:
            out.append(int(part))
    return out


def notice_text(up: bool, reason: str = "", now: float | None = None) -> str:
    """ "🔴 **Server is down** · restarting · <time>" / "🟢 **Server is back up** · <time>".
    The time is a Discord timestamp, shown in each reader's own timezone."""
    ts = int(time.time() if now is None else now)
    if up:
        return f"\U0001f7e2 **Server is back up** · <t:{ts}:t>"
    return f"\U0001f534 **Server is down**{f' · {reason}' if reason else ''} · <t:{ts}:t>"


async def send(bot, ids: list, text: str, role_id: int = 0) -> int:
    """Post `text` to each channel; returns how many got it. Failures are logged."""
    content = f"<@&{role_id}> {text}" if role_id else text
    mentions = discord.AllowedMentions(roles=[discord.Object(id=role_id)] if role_id else False)
    sent = 0
    for cid in ids:
        channel = bot.get_channel(cid)
        try:
            if channel is None:
                channel = await bot.fetch_channel(cid)
            await channel.send(content, allowed_mentions=mentions)
            sent += 1
        except discord.HTTPException as e:
            print(f"[StatusNotice] Could not post to channel {cid}: {e}")
    return sent
