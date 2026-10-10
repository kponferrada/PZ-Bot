"""whitelist.account_status_fields: the Check Status reply (pure, no Discord/SFTP).
Run from bot/:  python -m pytest tests
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from whitelist import account_status_fields, _fmt_last_connection  # noqa: E402

REQ = {"Username": "Juan", "SteamID": "76561198000000001", "isWhitelisted": "true",
       "Whitelisted By": "admin#1", "Notes": "", "discord_id": "42"}


def _fields(**kw):
    args = dict(request=REQ, account=None, server_online=True, online_names=(),
                linked_name=None, username_holder=None)
    args.update(kw)
    return dict(account_status_fields(**args))


def test_approved_account_on_server_online_and_linked():
    f = _fields(account={"username": "Juan", "steamid": "76561198000000001", "role": 2,
                         "lastConnection": 1790000000000},
                online_names={"juan"}, linked_name="Juan", username_holder=42)
    assert f["Request"].startswith("✅ Approved by admin#1")
    assert "`Juan`" in f["Server whitelist"] and "Player (level 2)" in f["Server whitelist"]
    assert "(request has" not in f["Server whitelist"]
    assert "<t:1790000000:R>" in f["Server whitelist"]
    assert "In game now" in f["Online"]
    assert f["Discord link"] == "\U0001f517 Linked to **Juan**"


def test_missing_account_offline_and_link_problems():
    f = _fields(server_online=False, linked_name="Pedro", username_holder=7)
    assert "No account" in f["Server whitelist"]
    assert f["Online"] == "Server is offline"
    assert "linked to **Pedro**" in f["Discord link"] and "<@7>" in f["Discord link"]


def test_steamid_mismatch_admin_and_banned():
    f = _fields(account={"username": "Juan", "steamid": "76561198000000009", "role": 7, "banned": 1})
    assert "(request has `76561198000000001`)" in f["Server whitelist"]
    assert "Admin" in f["Server whitelist"] and "Banned" in f["Server whitelist"]


def test_pending_and_denied_requests():
    assert _fields(request={**REQ, "isWhitelisted": "false"})["Request"] == "⏳ Pending"
    assert "spam" in _fields(request={**REQ, "isWhitelisted": "false", "Notes": "spam"})["Request"]


def test_last_connection_formats():
    assert _fmt_last_connection(None) == "never"
    assert _fmt_last_connection("01-10-26 12:00:00") == "01-10-26 12:00:00"
    assert _fmt_last_connection(1790000000) == "<t:1790000000:R>"


def test_manage_view_and_modals_build():
    import asyncio
    import whitelist

    async def build():
        view = whitelist.WhitelistManageView(cog=None, request_id="abc")
        ids = [item.custom_id for item in view.children]
        rename = whitelist.RenameModal(None, "abc", None, "Juan", "pw")
        password = whitelist.PasswordModal(None, "abc", None)
        return ids, rename, password

    ids, rename, password = asyncio.run(build())
    assert ids == ["whitelist:rename:abc", "whitelist:password:abc",
                   "whitelist:status:abc", "whitelist:delete:abc"]   # delete id unchanged
    assert rename.new_username.default == "Juan" and rename.password.default == "pw"
    for modal in (rename, password):
        assert all(len(item.label) <= 45 for item in modal.children)
