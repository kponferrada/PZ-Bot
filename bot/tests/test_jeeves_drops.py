"""Jeeve's Drops and Siege Night notices: who gets pinged.

A personal drop pings only the player it landed near, when they are linked
(/linkme); supply drop events and Siege Night ping EVENT_PING_ROLE_ID (else
AIRDROP_ROLE_ID / SIEGE_ROLE_ID).
Run from bot/:  python -m pytest tests
"""

import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jeeves_drops as jd  # noqa: E402

LINKS = {"ana": 111, "bob_pz": 222}


def test_drop_mention_matches_the_linked_username_any_case():
    assert jd.drop_mention("Ana", LINKS) == 111
    assert jd.drop_mention(" BOB_PZ ", LINKS) == 222
    assert jd.drop_mention("carl", LINKS) is None
    assert jd.drop_mention("", LINKS) is None and jd.drop_mention("ana", {}) is None


class _Channel:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)


def _cog(monkeypatch, status, links=LINKS, event_role=0, airdrop_role=55):
    channel, roled = _Channel(), []

    async def send_to_channel(ch, role_id, embed):
        roled.append((role_id, embed.title))

    bot = types.SimpleNamespace(
        features=types.SimpleNamespace(is_enabled=lambda key: key == "airdrops"),
        get_airdrop_channel=lambda: channel,
        get_cog=lambda name: types.SimpleNamespace(discord_ids_by_pz_username=lambda: dict(links)) if links else None,
        config=types.SimpleNamespace(AIRDROP_ROLE_ID=airdrop_role, EVENT_PING_ROLE_ID=event_role),
        send_to_channel=send_to_channel)
    cog = jd.JeevesDropsCog(bot)

    async def read():
        return status
    monkeypatch.setattr(jd.lua_bridge, "read_drops_status", read)
    monkeypatch.setattr(jd.lua_bridge, "read_supply_event_status", read)
    return cog, channel, roled


def _dropped(target):
    return {"phase": "dropped", "dropId": 1, "timestamp": 5, "targetPlayer": target,
            "itemCount": 9, "crateLabel": "Medical"}


def test_personal_drop_pings_its_linked_player_only(monkeypatch):
    cog, channel, roled = _cog(monkeypatch, _dropped("Ana"))
    asyncio.run(cog.drops_status_poller.coro(cog))
    assert roled == [] and len(channel.sent) == 1
    sent = channel.sent[0]
    assert sent["content"] == "<@111>"
    pings = sent["allowed_mentions"]
    assert [u.id for u in pings.users] == [111] and not pings.roles and not pings.everyone
    assert "**Ana**" in sent["embed"].description


def test_personal_drop_for_an_unlinked_player_pings_nobody(monkeypatch):
    for links in (LINKS, {}):
        cog, channel, roled = _cog(monkeypatch, _dropped("carl"), links=links)
        asyncio.run(cog.drops_status_poller.coro(cog))
        sent = channel.sent[0]
        assert roled == [] and "content" not in sent
        pings = sent["allowed_mentions"]
        assert not pings.users and not pings.roles and not pings.everyone


def test_supply_events_ping_the_event_role(monkeypatch):
    active = {"phase": "active", "eventId": 3, "timestamp": 9, "name": "Muldraugh"}
    cog, _, roled = _cog(monkeypatch, active, event_role=77)
    asyncio.run(cog.supply_event_poller.coro(cog))
    assert roled == [(77, "\U0001f6a8 SUPPLY DROP EVENT")]
    for phase in ("materialized", "ended"):
        cog, _, roled = _cog(monkeypatch, {"phase": phase, "eventId": 3, "timestamp": 9}, event_role=77)
        asyncio.run(cog.supply_event_poller.coro(cog))
        assert [r for r, _ in roled] == [77], phase
    cog, _, roled = _cog(monkeypatch, active)          # no event role set: the airdrop role
    asyncio.run(cog.supply_event_poller.coro(cog))
    assert [r for r, _ in roled] == [55]


def test_siege_night_pings_the_event_role():
    import siege_night
    cog = siege_night.SiegeNightCog.__new__(siege_night.SiegeNightCog)
    cog.bot = types.SimpleNamespace(config=types.SimpleNamespace(EVENT_PING_ROLE_ID=77, SIEGE_ROLE_ID=66))
    assert cog._ping_role() == 77
    cog.bot.config.EVENT_PING_ROLE_ID = 0                  # not set: the old siege role
    assert cog._ping_role() == 66
