"""One-line up/down notices for extra channels.

Run from bot/:  python -m pytest tests
"""

import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord  # noqa: E402

import features  # noqa: E402
import status_notice as sn  # noqa: E402


def test_channel_ids():
    assert sn.channel_ids("123, 456;123 , x,") == [123, 456]
    assert sn.channel_ids("") == [] and sn.channel_ids(None) == []


def test_notice_text():
    assert sn.notice_text(False, "restarting", 1_792_700_000) == \
        "\U0001f534 **Server is down** · restarting · <t:1792700000:t>"
    assert sn.notice_text(False, "", 1_792_700_000) == "\U0001f534 **Server is down** · <t:1792700000:t>"
    assert sn.notice_text(True, now=1_792_700_000) == "\U0001f7e2 **Server is back up** · <t:1792700000:t>"
    assert "status_notices" in features.FEATURES


def test_send_skips_failed_channels():
    posted = []

    class Channel:
        def __init__(self, cid):
            self.cid = cid

        async def send(self, content, allowed_mentions=None):
            posted.append((self.cid, content))

    async def fetch_channel(cid):
        raise discord.NotFound(type("R", (), {"status": 404, "reason": "Not Found"})(), "gone")

    bot = types.SimpleNamespace(get_channel=lambda cid: Channel(cid) if cid == 1 else None,
                                fetch_channel=fetch_channel)
    assert asyncio.run(sn.send(bot, [1, 2], "hi", role_id=9)) == 1
    assert posted == [(1, "<@&9> hi")]
