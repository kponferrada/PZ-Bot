"""roles.py — give a member a configured Discord role, or take it away.

The whitelist uses it (WHITELIST_ROLE_ID): the role is given when a request is
approved and taken away when the account is deleted. Any other feature that
hands out a role can call `set_member_role` too.

Discord only lets the bot change a role when the bot has **Manage Roles** and
its own highest role sits above that role. When it can't, the change is
skipped and the line returned for the admin says why.
"""
from __future__ import annotations

import discord

GIVEN, TAKEN, ALREADY, OFF, NO_ROLE, NO_MEMBER, FORBIDDEN, FAILED = (
    "given", "taken", "already", "off", "no_role", "no_member", "forbidden", "failed")


async def _member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.HTTPException:  # NotFound: they left the server
        return None


async def set_member_role(guild: discord.Guild | None, user_id, role_id: int, give: bool,
                          reason: str) -> tuple[str, str]:
    """Give (`give=True`) or take away role `role_id` from the member `user_id`.

    Returns (result, a line for the admin). `result` is one of GIVEN, TAKEN,
    ALREADY (nothing to do), OFF (no role configured: the line is ""),
    NO_ROLE (the role doesn't exist), NO_MEMBER (no such member in the
    server), FORBIDDEN (missing Manage Roles, or the role is above the bot's)
    or FAILED."""
    if not role_id:
        return OFF, ""
    if guild is None:
        return NO_MEMBER, "⚠️ Role not changed: not in a server."
    role = guild.get_role(int(role_id))
    if role is None:
        return NO_ROLE, f"⚠️ Role not changed: role `{role_id}` doesn't exist in this server."
    if not str(user_id or "").isdigit():
        return NO_MEMBER, f"⚠️ {role.mention} not changed: no Discord account on record."
    member = await _member(guild, int(user_id))
    if member is None:
        return NO_MEMBER, f"⚠️ {role.mention} not changed: <@{user_id}> isn't in the server."
    if (role in member.roles) == give:
        state = "already has" if give else "doesn't have"
        return ALREADY, f"\U0001f3ad {member.mention} {state} {role.mention}."
    try:
        if give:
            await member.add_roles(role, reason=reason)
        else:
            await member.remove_roles(role, reason=reason)
    except discord.Forbidden:
        return FORBIDDEN, (f"⚠️ {role.mention} not changed: the bot needs **Manage Roles** "
                           "and its own role must be above that role.")
    except discord.HTTPException as e:
        print(f"[Roles] {'add' if give else 'remove'} {role.name} for {member} failed: {e}")
        return FAILED, f"⚠️ {role.mention} not changed (Discord error)."
    print(f"[Roles] {'Gave' if give else 'Took'} {role.name} {'to' if give else 'from'} {member}: {reason}")
    if give:
        return GIVEN, f"\U0001f3ad Gave {member.mention} {role.mention}."
    return TAKEN, f"\U0001f3ad Took {role.mention} from {member.mention}."
