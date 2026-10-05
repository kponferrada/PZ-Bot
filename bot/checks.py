"""checks.py — shared slash-command permission checks.

Lives outside main.py so cogs can import it. Importing `main` from a cog would
execute main.py a second time (the running script is `__main__`, not `main`),
building a second Config and bot instance.
"""

import discord
from discord import app_commands


def require_role(role_name: str):
    """App-command check: the invoking member must have the role named `role_name`."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False
        role = discord.utils.get(interaction.guild.roles, name=role_name)
        if role is None or role not in interaction.user.roles:
            raise app_commands.MissingRole(role_name)
        return True
    return app_commands.check(predicate)


def admin_only():
    """App-command check: the member must have the admin role (DEFAULT_ROLE).

    Reads the role name from the bot's config when the command runs, so cogs
    can use it as a plain decorator. A failure raises MissingRole, which the
    global handler in main.py answers with a "Permission Denied" embed.
    """
    async def predicate(interaction: discord.Interaction) -> bool:
        role_name = interaction.client.config.DEFAULT_ROLE
        if not interaction.guild:
            raise app_commands.MissingRole(role_name)
        role = discord.utils.get(interaction.guild.roles, name=role_name)
        if role is None or role not in interaction.user.roles:
            raise app_commands.MissingRole(role_name)
        return True
    return app_commands.check(predicate)
