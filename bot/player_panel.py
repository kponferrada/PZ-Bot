"""player_panel.py — buttons for the player commands, for everyone.

`/playerpanel` (admins) posts the panel card into PLAYER_PANEL_CHANNEL_ID (or
the channel it is run in). Four of the buttons (STATUS_KEYS) also sit under the
server status card (server_status.py asks for `status_rows`).

Each button runs the player slash command of the same name (command_panel.py
has the engine); commands with parameters open a form. Every reply is private
(ephemeral), so clicks don't post into the status or panel channel. Calling the
callback skips the command's cooldown (/linkme has one); the commands
themselves refuse repeats (/linkme refuses a second link).
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from checks import admin_only
from command_panel import BLUE, GREEN, Action, Group, PanelCog, button_rows

PREFIX = "playerpanel:"

GROUP = Group("\U0001f3ae Player Panel", "Your rank, stats and the leaderboards. Answers are only shown to you.",
              0x5865F2, (
                  Action("myrank", "My rank", "myrank", "\U0001f3c5", BLUE),
                  Action("onlineplayers", "Online players", "onlineplayers", "\U0001f465"),
                  Action("linkme", "Link account", "linkme", "\U0001f517"),
                  Action("stats", "Player stats", "stats", "\U0001f4ca"),
                  Action("leaderboard", "Leaderboard", "leaderboard", "\U0001f3c6"),
                  Action("rpleaderboard", "RP leaderboard", "rpleaderboard", "\U0001f4c8"),
                  Action("repboard", "Rep board", "repboard", "\U0001f396️"),
                  Action("whitelist", "Apply for whitelist", "whitelist", "\U0001f4dd", GREEN),
              ))
GROUPS = (GROUP,)
# Under the status card (user, 2026-10-23): no Rep board, Link account or Apply for whitelist.
STATUS_KEYS = ("myrank", "onlineplayers", "stats", "leaderboard", "rpleaderboard")
STATUS_GROUP = Group(GROUP.title, GROUP.description, GROUP.colour,
                     tuple(a for a in GROUP.actions if a.key in STATUS_KEYS))
ACTIONS = {a.key: a for a in GROUP.actions}


class PlayerPanel(PanelCog):
    panel_prefix = PREFIX
    panel_groups = GROUPS
    private = True
    log_name = "PlayerPanel"

    def __init__(self, bot: commands.Bot):
        super().__init__(bot)
        self._channel_id = int(getattr(bot.config, "PLAYER_PANEL_CHANNEL_ID", 0) or 0)

    async def allowed(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message("Use the panel in the server.", ephemeral=True)
            return False
        return True

    def status_rows(self) -> list:
        """The buttons under the status card (fresh instances each update): the
        look-up ones only; linking and whitelist stay on the player panel."""
        return button_rows(self, STATUS_GROUP)

    @app_commands.command(name="playerpanel",
                          description="Post the player panel (buttons for the player commands).")
    @admin_only()
    async def cmd_player_panel(self, interaction: discord.Interaction) -> None:
        channel = self.bot.get_channel(self._channel_id) if self._channel_id else interaction.channel
        await self.post(interaction, channel)


async def setup(bot: commands.Bot):
    await bot.add_cog(PlayerPanel(bot))
    print("[PlayerPanel] Extension loaded.")
