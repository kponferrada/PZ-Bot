"""whitelist: one pending request per requester.
While a requester's request waits for staff, the Apply button tells them so
instead of opening the form, and a form sent anyway is refused (not saved).
Run from bot/:  python -m pytest tests
"""

import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import whitelist  # noqa: E402
from whitelist import find_pending_request, is_pending, pending_message  # noqa: E402


def _row(**kw):
    row = {"Timestamp": "2026-10-17 01:00:00 UTC", "Username": "Juan", "Discord Username": "juan",
           "isWhitelisted": "false", "Notes": "", "request_id": "r1", "discord_id": "42"}
    row.update(kw)
    return row


def test_pending_means_not_approved_and_not_decided():
    assert is_pending(_row())
    assert not is_pending(_row(isWhitelisted="true"))
    assert not is_pending(_row(Notes="Denied: wrong SteamID"))
    assert not is_pending(_row(Notes="Deleted: left the server"))


def test_find_pending_request_by_discord_id():
    rows = [_row(request_id="old", Notes="Denied"), _row(request_id="new")]
    assert find_pending_request(rows, "42")["request_id"] == "new"
    assert find_pending_request(rows, "7") is None
    # a denied or approved request doesn't block a new one
    assert find_pending_request([_row(Notes="Denied")], "42") is None
    assert find_pending_request([_row(isWhitelisted="true")], "42") is None


def test_rows_without_discord_id_fall_back_to_the_username():
    rows = [_row(discord_id="", **{"Discord Username": "pedro"})]
    assert find_pending_request(rows, "99", "pedro") is not None
    assert find_pending_request(rows, "99", "someone") is None
    # a row WITH an id is never matched by name alone
    assert find_pending_request([_row()], "99", "juan") is None


def test_message_says_it_is_still_pending():
    msg = pending_message(_row())
    assert "still pending approval" in msg
    assert "2026-10-17 01:00:00 UTC" in msg and "`Juan`" in msg
    assert "can't send another request" in msg


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


def _cog(tmp_path):
    """A WhitelistCog without Discord: its CSV in tmp_path, no approval channel."""
    cog = whitelist.WhitelistCog.__new__(whitelist.WhitelistCog)
    cog._csv_path = tmp_path / "requests.csv"
    cog._approval_channel_id = 0
    cog._submit_lock = asyncio.Lock()
    cog.bot = types.SimpleNamespace(features=types.SimpleNamespace(is_enabled=lambda _n: True),
                                    get_channel=lambda _i: None)
    return cog


def _submit(cog, user):
    inter = types.SimpleNamespace(user=user, response=_Response())
    asyncio.run(cog.handle_request(inter, username="Juan", password="pw",
                                   steam_id="76561198000000001", character_lore="false"))
    return inter.response


def test_second_request_refused_while_first_is_pending(tmp_path):
    cog = _cog(tmp_path)
    juan = _User(42, "juan")
    first = _submit(cog, juan)
    assert "submitted for review" in first.sent[0][0]
    second = _submit(cog, juan)
    assert "still pending approval" in second.sent[0][0] and second.sent[0][1] is True
    assert len(cog._read_csv()) == 1, "the second request must not be saved"
    # someone else can still apply
    _submit(cog, _User(7, "pedro"))
    assert len(cog._read_csv()) == 2


def test_after_a_decision_a_new_request_is_allowed(tmp_path):
    cog = _cog(tmp_path)
    juan = _User(42, "juan")
    _submit(cog, juan)
    rid = cog._read_csv()[0]["request_id"]
    cog._update_csv_row(rid, {"Notes": "Denied: wrong SteamID"})
    again = _submit(cog, juan)
    assert "submitted for review" in again.sent[0][0]
    assert len(cog._read_csv()) == 2


def test_two_forms_sent_at_once_save_one(tmp_path):
    cog = _cog(tmp_path)
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


def test_apply_button_shows_the_note_instead_of_the_form(tmp_path):
    cog = _cog(tmp_path)
    juan = _User(42, "juan")
    view = types.SimpleNamespace(cog=cog)
    click = types.SimpleNamespace(user=juan, response=_Response())
    apply_fn = getattr(whitelist.WhitelistView.apply_button, "callback", whitelist.WhitelistView.apply_button)
    asyncio.run(apply_fn(view, click, None))
    assert len(click.response.modals) == 1 and not click.response.sent   # nothing pending: the form
    _submit(cog, juan)
    click2 = types.SimpleNamespace(user=juan, response=_Response())
    asyncio.run(apply_fn(view, click2, None))
    assert not click2.response.modals
    assert "still pending approval" in click2.response.sent[0][0]
