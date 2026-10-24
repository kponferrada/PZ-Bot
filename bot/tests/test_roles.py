"""Whitelist role: who gets it, and set_member_role's answers (fake guild).

Run from bot/:  python -m pytest tests
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord  # noqa: E402

import roles  # noqa: E402
import whitelist  # noqa: E402


def test_whitelisted_requesters_and_lookup():
    rows = [
        {"Username": "Juan", "isWhitelisted": "true", "discord_id": "111"},
        {"Username": "Ana", "isWhitelisted": "false", "discord_id": "222", "Notes": "deleted"},
        {"Username": "Juan2", "isWhitelisted": "true", "discord_id": "111"},   # same person: once
        {"Username": "Old", "isWhitelisted": "true", "discord_id": ""},        # no id on record
        {"Username": "ana", "isWhitelisted": "true", "discord_id": "333"},
    ]
    assert whitelist.whitelisted_requesters(rows) == ["111", "333"]
    assert whitelist.requester_for_username(rows, "ANA") == "333"           # latest request wins
    assert whitelist.requester_for_username(rows, "Old") == ""
    assert whitelist.requester_for_username(rows, "nobody") == ""


class _Role:
    def __init__(self, rid):
        self.id, self.name, self.mention = rid, "Whitelisted", f"<@&{rid}>"


class _Member:
    def __init__(self, uid, role_list, forbid=False):
        self.id, self.mention, self.roles, self.forbid = uid, f"<@{uid}>", role_list, forbid

    async def add_roles(self, role, reason=None):
        if self.forbid:
            raise discord.Forbidden(type("R", (), {"status": 403, "reason": "Forbidden"})(), "no")
        self.roles.append(role)

    async def remove_roles(self, role, reason=None):
        self.roles.remove(role)


class _Guild:
    def __init__(self, role, members):
        self.role, self.members = role, members

    def get_role(self, rid):
        return self.role if self.role and rid == self.role.id else None

    def get_member(self, uid):
        return self.members.get(uid)

    async def fetch_member(self, uid):
        raise discord.NotFound(type("R", (), {"status": 404, "reason": "Not Found"})(), "gone")


def test_set_member_role():
    role = _Role(9)
    juan, locked = _Member(1, []), _Member(2, [], forbid=True)
    guild = _Guild(role, {1: juan, 2: locked})

    def run(*args):
        return asyncio.run(roles.set_member_role(*args))[0]

    assert run(guild, "1", 0, True, "x") == roles.OFF                   # no role configured
    assert run(guild, "1", 9, True, "x") == roles.GIVEN and role in juan.roles
    assert run(guild, "1", 9, True, "x") == roles.ALREADY
    assert run(guild, "1", 9, False, "x") == roles.TAKEN and role not in juan.roles
    assert run(guild, "1", 9, False, "x") == roles.ALREADY
    assert run(guild, "3", 9, True, "x") == roles.NO_MEMBER             # left the server
    assert run(guild, "", 9, True, "x") == roles.NO_MEMBER              # no Discord id on record
    assert run(guild, "1", 8, True, "x") == roles.NO_ROLE
    assert run(guild, "2", 9, True, "x") == roles.FORBIDDEN
