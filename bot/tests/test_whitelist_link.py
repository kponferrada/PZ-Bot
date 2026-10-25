"""whitelist: no new request while the requester's Discord account is linked, or
while a request of theirs is still pending (never acted on by staff).
Approval links the requester (RankSync, as /linkme would). The Apply button and
/whitelist say why instead of opening the form, and a form sent anyway is
refused (not saved). An admin's /unlinkname or /whitelistcancel lets them apply
again. Rows staff acted on (approved, denied, deleted) never block.
Run from bot/:  python -m pytest tests
"""

import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import whitelist  # noqa: E402
from whitelist import is_pending, linked_message, pending_message  # noqa: E402


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
    apply_fn = getattr(whitelist._ApplyRow.apply_button, "callback", whitelist._ApplyRow.apply_button)
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


def test_a_pending_request_blocks_a_second_one(tmp_path):
    cog = _cog(tmp_path, _RankSync())
    juan = _User(42, "juan")
    assert "submitted for review" in _submit(cog, juan).sent[0][0]
    again = _submit(cog, juan)
    assert "still pending approval" in again.sent[0][0] and again.sent[0][1] is True
    assert len(cog._read_csv()) == 1, "the second request must not be saved"
    r = _click(cog, juan)
    assert not r.modals and "still pending approval" in r.sent[0][0]
    # someone else can still apply
    assert "submitted for review" in _submit(cog, _User(7, "pedro")).sent[0][0]


def test_rows_staff_acted_on_never_block(tmp_path):
    """The reported case: a deleted request must not read as pending - even a
    deleted account whose reason came out blank keeps its approval record."""
    cog = _cog(tmp_path, _RankSync())
    juan = _User(42, "juan")
    _submit(cog, juan)
    rid = cog._read_csv()[0]["request_id"]
    cog._update_csv_row(rid, {"isWhitelisted": "false", "Notes": "", "Code": "adduser ...",
                              "Whitelisted By": "admin#1"})     # approved once, then deleted
    assert not is_pending(cog._read_csv()[0])
    assert "submitted for review" in _submit(cog, juan).sent[0][0]
    rid2 = cog._read_csv()[1]["request_id"]
    cog._update_csv_row(rid2, {"Notes": "Denied: wrong SteamID"})
    assert "submitted for review" in _submit(cog, juan).sent[0][0]


def test_admin_cancel_clears_a_stuck_pending_request(tmp_path):
    cog = _cog(tmp_path, _RankSync())
    juan = _User(42, "juan")
    _submit(cog, juan)
    assert cog.cancel_pending(juan, "admin#1", "card was deleted") == 1
    assert cog._read_csv()[0]["Notes"] == "Cancelled by admin#1: card was deleted"
    assert cog.cancel_pending(juan, "admin#1") == 0
    assert "submitted for review" in _submit(cog, juan).sent[0][0]


def test_linked_wins_over_pending(tmp_path):
    rank = _RankSync()
    cog = _cog(tmp_path, rank)
    juan = _User(42, "juan")
    _submit(cog, juan)
    rank.links["42"] = "JuanPZ"
    assert "already linked" in cog.refusal_for(juan)


def test_two_forms_sent_at_once_save_one(tmp_path):
    cog = _cog(tmp_path, _RankSync())
    juan = _User(42, "juan")

    async def both():
        a = types.SimpleNamespace(user=juan, response=_Response())
        b = types.SimpleNamespace(user=juan, response=_Response())
        await asyncio.gather(
            cog.handle_request(a, username="Juan", password="pw", steam_id="76561198000000001"),
            cog.handle_request(b, username="Juan", password="pw", steam_id="76561198000000001"))
        return a.response.sent + b.response.sent

    replies = [t for t, _e in asyncio.run(both())]
    assert len(cog._read_csv()) == 1
    assert sum("submitted for review" in t for t in replies) == 1
    assert sum("still pending approval" in t for t in replies) == 1


def test_whitelist_command_checks_too(tmp_path):
    cog = _cog(tmp_path, _RankSync({"42": "JuanPZ"}))
    inter = types.SimpleNamespace(user=_User(42, "juan"), response=_Response())
    fn = getattr(whitelist.WhitelistCog.cmd_whitelist, "callback", whitelist.WhitelistCog.cmd_whitelist)
    asyncio.run(fn(cog, inter))
    assert not inter.response.modals and "already linked" in inter.response.sent[0][0]


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


def test_pending_rule():
    row = {"isWhitelisted": "false", "Notes": "", "Code": "", "Whitelisted By": ""}
    assert is_pending(row)
    assert not is_pending(dict(row, isWhitelisted="true"))
    assert not is_pending(dict(row, Notes="Denied: wrong SteamID"))
    assert not is_pending(dict(row, Notes="   ", Code="adduser x"))    # blank reason, approved once
    assert not is_pending(dict(row, **{"Whitelisted By": "admin#1"}))
    assert "**X**" in linked_message("X")
    msg = pending_message({"Timestamp": "2026-10-17 01:00:00 UTC", "Username": "Juan"})
    assert "still pending approval" in msg and "`Juan`" in msg


def test_cancel_clears_an_old_row_without_ids(tmp_path):
    """In Discord: an old request (2026-09-14, no request_id / discord_id) blocked the
    requester, yet /whitelistcancel said there was none - it only updated rows with an id."""
    import csv
    cog = _cog(tmp_path, _RankSync())
    with cog._csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=whitelist._CSV_HEADERS)
        w.writeheader()
        w.writerow({"Timestamp": "2026-09-14T07:47:07.478404+00:00", "Username": "silver",
                    "Discord Username": "silvast", "isWhitelisted": "false"})
    silvast = _User(99, "silvast")
    assert "still pending approval" in cog.refusal_for(silvast)
    assert cog.cancel_pending(silvast, "admin#1") == 1
    assert cog._read_csv()[0]["Notes"] == "Cancelled by admin#1"
    assert cog._read_csv()[0]["Username"] == "silver"           # the rest of the row kept
    assert cog.refusal_for(silvast) is None
    assert "submitted for review" in _submit(cog, silvast).sent[0][0]


def test_approval_message():
    text = whitelist.approval_message("Juan", "juan_pz", "76561198000000001")
    assert text.startswith("Hey Juan, your whitelist request for **PZ Tambayan** has been **approved**!")
    assert "Username: `juan_pz`\nSteamID: `76561198000000001`" in text
    for label in ("Rules", "Modlist", "Guides"):
        assert f"[{label}](https://discord.com/channels/1541725057938882600/" in text
    assert "Tomb's body mod" in text and "Desire" not in text
    assert len(text) < 4096                      # embed description limit

