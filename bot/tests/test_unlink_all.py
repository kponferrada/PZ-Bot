"""/unlinkall (season reset): every link goes, a backup is written first, and
undoing what the links gave; the whitelist form no longer asks about lore.

Run from bot/:  python -m pytest tests
"""

import asyncio
import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rank_sync  # noqa: E402
import whitelist  # noqa: E402


def _cog(links, ladder, tmp_path, monkeypatch):
    monkeypatch.setattr(rank_sync, "LINK_FILE", tmp_path / "rank_links.json")
    cog = rank_sync.RankSync.__new__(rank_sync.RankSync)
    cog._links, cog._ladder_mode, cog._ranks = dict(links), ladder, {"Juan": 3, "Ana": 2, "Other": 1}
    cog.bot = types.SimpleNamespace(get_guild=lambda _id: None, config=types.SimpleNamespace(GUILD_ID=0))
    cog.calls = []

    def save():
        cog.calls.append("save")

    async def write():
        cog.calls.append("write")

    async def push():
        cog.calls.append("push")
    cog._save_links, cog._write_ranks_file, cog._push_ranks_to_server = save, write, push
    return cog


def test_unlink_all_backs_up_then_clears(tmp_path, monkeypatch):
    cog = _cog({"111": "Juan", "222": "Ana"}, False, tmp_path, monkeypatch)
    count, backup = asyncio.run(cog.unlink_all())
    assert count == 2 and cog._links == {}
    assert json.loads((tmp_path / backup).read_text(encoding="utf-8")) == {"111": "Juan", "222": "Ana"}
    assert backup.startswith("rank_links-backup-") and backup.endswith(".json")
    assert cog._ranks == {"Other": 1}                     # role mode: the linked ranks go
    assert cog.calls == ["save", "write", "push"]


def test_unlink_all_ladder_mode_and_nothing_to_do(tmp_path, monkeypatch):
    cog = _cog({"111": "Juan"}, True, tmp_path, monkeypatch)
    assert asyncio.run(cog.unlink_all())[0] == 1
    assert cog._ranks == {"Juan": 3, "Ana": 2, "Other": 1}   # ladder ranks don't come from links
    assert asyncio.run(cog.unlink_all()) == (0, "")
    assert cog.calls == ["save"]


def test_whitelist_form_has_no_lore_question():
    inputs = [n for n, v in vars(whitelist.WhitelistModal).items() if v.__class__.__name__ == "TextInput"]
    assert inputs == ["username", "password", "steam_id"]
