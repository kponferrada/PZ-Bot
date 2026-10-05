"""Tests for checks.admin_only (fake interaction, no Discord connection)."""

import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from discord import app_commands  # noqa: E402

import checks  # noqa: E402


def _predicate():
    @checks.admin_only()
    async def cmd(interaction):
        pass
    return cmd.__discord_app_commands_checks__[0]


def _interaction(role_names, guild_roles=("Admin", "Fuel"), configured="Admin"):
    roles = {n: SimpleNamespace(name=n) for n in guild_roles}
    return SimpleNamespace(
        client=SimpleNamespace(config=SimpleNamespace(DEFAULT_ROLE=configured)),
        guild=SimpleNamespace(roles=list(roles.values())),
        user=SimpleNamespace(roles=[roles[n] for n in role_names if n in roles]),
    )


def test_admin_passes():
    assert asyncio.run(_predicate()(_interaction(["Admin"]))) is True


def test_non_admin_raises_missing_role_with_configured_name():
    with pytest.raises(app_commands.MissingRole) as exc:
        asyncio.run(_predicate()(_interaction(["Fuel"], configured="Admin")))
    assert exc.value.missing_role == "Admin"


def test_missing_guild_role_or_dm_is_denied():
    with pytest.raises(app_commands.MissingRole):
        asyncio.run(_predicate()(_interaction(["Admin"], guild_roles=("Fuel",))))
    dm = _interaction(["Admin"])
    dm.guild = None
    with pytest.raises(app_commands.MissingRole):
        asyncio.run(_predicate()(dm))
