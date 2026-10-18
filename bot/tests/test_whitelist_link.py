"""whitelist: no new request while the requester's Discord account is linked.
Approval links the requester (RankSync, as /linkme would). While linked, the
Apply button says so instead of opening the form and a form sent anyway is
refused (not saved); an admin's /unlinkname lets them apply again. Old CSV rows
(a request deleted outside the card, a pending one) never block anyone.
Run from bot/:  python -m pytest tests
"""

import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import whitelist  # noqa: E402
from whitelist import is_pending, linked_message  # noqa: E402


class _RankSync:
    """RankSync's link lookup: Discord id (str) -> PZ username."""
    def __init__(self, links=None):
        self.links = dict(links or {})

    def pz_username_for_discord_id(self, discord_id):
        return self.links.get(str(discord_id))


class _Response:
    def __init__(self):
        self.sent = []
        self.modals = []

    async def send_message(self, text, ephemeral=False):
        self.sent.append((text, ephemeral))

    async def send_modal(self, modal):
        self.modals.append(modal)


class _User:
    def __init__(self, uid, name):
        self.id, self._name = uid, name

    def __str__(self):
        return self._name


def _cog(tmp_path, rank=None):
    """A WhitelistCog without Discord: its CSV in tmp_path, no approval channel."""
    cog = whitelist.WhitelistCog.__new__(whitelist.WhitelistCog)
    cog._csv_path = tmp_path / "requests.csv"
    cog._approval_channel_id = 0
    cog._submit_lock = asyncio.Lock()
    cogs = {"RankSync": rank} if rank is not None else {}
    cog.bot = types.SimpleNamespace(features=types.SimpleNamespace(is_enabled=lambda _n: True),
                                    get_channel=lambda _i: None, get_cog=cogs.get)
    return cog


def _submit(cog, user):
    inter = types.SimpleNamespace(user=user, response=_Response())
    asyncio.run(cog.handle_request(inter, username="Juan", password="pw",
                                   steam_id="76561198000000001", character_lore="false"))
    return inter.response


def _click(cog, user):
    view = types.SimpleNamespace(cog=cog)
    click = types.SimpleNamespace(user=user, response=_Response())
    apply_fn = getattr(whitelist.WhitelistView.apply_button, "callback", whitelist.WhitelistView.apply_button)
    asyncio.run(apply_fn(view, click, None))
    return click.response


def test_linked_requester_is_told_and_gets_no_form(tmp_path):
    cog = _cog(tmp_path, _RankSync({"42": "JuanPZ"}))
    r = _click(cog, _User(42, "juan"))
    assert not r.modals
    text, ephemeral = r.sent[0]
    assert "already linked" in text and "**JuanPZ**" in text and "/unlinkname" in text and ephemeral


def test_linked_requester_form_sent_anyway_is_refused(tmp_path):
    cog = _cog(tmp_path, _RankSync({"42": "JuanPZ"}))
    r = _submit(cog, _User(42, "juan"))
    assert "already linked" in r.sent[0][0]
    assert cog._read_csv() == [], "a refused request must not be saved"


def test_unlinked_requester_gets_the_form_and_can_submit(tmp_path):
    rank = _RankSync({"7": "PedroPZ"})
    cog = _cog(tmp_path, rank)
    juan = _User(42, "juan")
    r = _click(cog, juan)
    assert len(r.modals) == 1 and not r.sent
    assert "submitted for review" in _submit(cog, juan).sent[0][0]
    assert len(cog._read_csv()) == 1


def test_a_stale_pending_row_no_longer_blocks(tmp_path):
    """The reported case: a request deleted outside the card stays 'pending' in the CSV."""
    cog = _cog(tmp_path, _RankSync())
    juan = _User(42, "juan")
    _submit(cog, juan)
    assert is_pending(cog._read_csv()[0])                     # still looks pending
    assert "submitted for review" in _submit(cog, juan).sent[0][0]
    assert len(_click(cog, juan).modals) == 1


def test_admin_unlink_lets_them_apply_again(tmp_path):
    rank = _RankSync({"42": "JuanPZ"})
    cog = _cog(tmp_path, rank)
    juan = _User(42, "juan")
    assert "already linked" in _submit(cog, juan).sent[0][0]
    del rank.links["42"]                                        # /unlinkname
    assert "submitted for review" in _submit(cog, juan).sent[0][0]


def test_without_rank_sync_nobody_counts_as_linked(tmp_path):
    cog = _cog(tmp_path, None)
    assert cog.linked_username_for(_User(42, "juan")) is None
    assert "submitted for review" in _submit(cog, _User(42, "juan")).sent[0][0]


def test_lookup_errors_dont_block(tmp_path):
    class Broken:
        def pz_username_for_discord_id(self, _i):
            raise RuntimeError("links file unreadable")
    cog = _cog(tmp_path, Broken())
    assert cog.linked_username_for(_User(42, "juan")) is None


def test_pending_rule_still_used_for_the_cards():
    row = {"isWhitelisted": "false", "Notes": ""}
    assert is_pending(row)
    assert not is_pending(dict(row, isWhitelisted="true"))
    assert not is_pending(dict(row, Notes="Denied: wrong SteamID"))
    assert "**X**" in linked_message("X")
