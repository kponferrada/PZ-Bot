"""Deleting a whitelisted account: the Discord link goes, the account is checked
gone from the server database, and the whitelist role is taken.

Run from bot/:  python -m pytest tests
"""

import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rank_sync  # noqa: E402
import whitelist  # noqa: E402


def _rank_cog(links):
    cog = rank_sync.RankSync.__new__(rank_sync.RankSync)
    cog._links = dict(links)
    cog.saved, cog.unlinked = 0, []
    cog.bot = types.SimpleNamespace(get_guild=lambda _id: None, config=types.SimpleNamespace(GUILD_ID=0))

    def save():
        cog.saved += 1

    async def on_unlinked(user, old):
        cog.unlinked.append((user.id, old))
    cog._save_links, cog._on_unlinked = save, on_unlinked
    return cog


def test_unlink_account_removes_every_link_to_the_name():
    cog = _rank_cog({"111": "Juan", "222": "juan", "333": "Ana"})
    assert cog.links_to("JUAN") == ["111", "222"]
    assert asyncio.run(cog.unlink_account("Juan")) == ["111", "222"]
    assert cog._links == {"333": "Ana"} and cog.saved == 1
    assert cog.unlinked == [(111, "Juan"), (222, "juan")]
    assert asyncio.run(cog.unlink_account("nobody")) == [] and cog.saved == 1   # nothing to save


def test_after_removal_unlinks_checks_and_takes_role(monkeypatch):
    rank = _rank_cog({"555": "Pedro"})
    wl = whitelist.WhitelistCog.__new__(whitelist.WhitelistCog)
    wl.bot = types.SimpleNamespace(get_cog=lambda name: rank if name == "RankSync" else None)
    lookups = iter([({"username": "Pedro"}, ""), (None, "")])   # still there, then gone

    async def server_account(_name):
        return next(lookups)

    async def no_sleep(_s):
        return None
    roles_taken = []

    async def set_role(guild, did, give, why):
        roles_taken.append((did, give))
        return "role line"
    wl._server_account, wl._set_role = server_account, set_role
    monkeypatch.setattr(whitelist.asyncio, "sleep", no_sleep)

    notes = asyncio.run(wl._after_removal(None, "Pedro", "", "deleted"))
    assert "gone from the server's account database" in notes
    assert "Unlinked <@555> from **Pedro**" in notes and rank._links == {}
    assert roles_taken == [("555", False)]   # no requester on record: the linked member


def test_confirm_removed_warns_when_still_there(monkeypatch):
    wl = whitelist.WhitelistCog.__new__(whitelist.WhitelistCog)

    async def server_account(_name):
        return {"username": "Pedro"}, ""

    async def no_sleep(_s):
        return None
    wl._server_account = server_account
    monkeypatch.setattr(whitelist.asyncio, "sleep", no_sleep)
    assert "still shows" in asyncio.run(wl._confirm_removed("Pedro"))
