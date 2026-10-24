"""admin_panel.py — button control panel for admins, grouped by function.

`/adminpanel` posts one card per group (Restarts, Server, Players & Ranks,
Whitelist, Events, Bot) into ADMIN_PANEL_CHANNEL_ID (or the channel it is run
in). Each button runs the admin slash command of the same name; commands with
parameters open a form (command_panel.py has the engine). Calling the callback
skips the command's own checks, so every click checks the admin role
(DEFAULT_ROLE) first. Buttons are persistent (custom_id `adminpanel:<key>`).
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from checks import admin_only
from command_panel import BLUE, GREEN, RED, Action, Group, PanelCog

PREFIX = "adminpanel:"

GROUPS = (
    Group("\U0001f504 Restarts", "Restart, defer or cancel restarts and run maintenance. "
          "A mod-update restart can only be deferred; a scheduled one can be cancelled.", 0xE67E22, (
        Action("restartstatus", "Status", "restartstatus", "\U0001f4cb"),
        Action("restart", "Restart", "restart", "\U0001f504", BLUE,
               confirm="Restart the server with the full countdown (kick at T-1 min)?"),
        Action("restartnow", "Restart now", "restartnow", "⚡", RED,
               confirm="Restart now? Players are kicked in 30 seconds."),
        Action("deferrestart", "Defer", "deferrestart", "⏸️"),
        Action("cancelrestart", "Cancel / skip next", "cancelrestart", "❌", RED,
               fixed={"restore": False}),
        Action("restoreschedule", "Restore schedule", "cancelrestart", "↩️",
               fixed={"restore": True}),
        Action("forcemodupdate", "Force mod update", "forcemodupdate", "\U0001f527",
               confirm="Run a mod-update restart now?"),
        Action("maintenance", "Maintenance", "maintenance", "\U0001f6e0️", fixed={"end": False}),
        Action("maintenanceend", "End maintenance", "maintenance", "✅", GREEN,
               fixed={"end": True, "reason": "", "minutes": 60}),
    )),
    Group("\U0001f5a5️ Server", "Check the server, talk to players, kick or stop it.", 0x3498DB, (
        Action("online", "Online?", "online", "\U0001f7e2"),
        Action("players", "Players", "players", "\U0001f465"),
        Action("playerlist", "Player list", "playerlist", "\U0001f4d2"),
        Action("modlist", "Mod list", "modlist", "\U0001f9e9"),
        Action("msg", "Broadcast", "msg", "\U0001f4e2", BLUE),
        Action("announce", "Banner", "announce", "\U0001f5bc️"),
        Action("playsound", "Play sound", "playsound", "\U0001f50a"),
        Action("teleport", "Teleport", "teleport", "\U0001f300"),
        Action("kickall", "Kick all", "kickall", "\U0001f462", RED, fixed={"cancel": False}),
        Action("kickallcancel", "Cancel kick", "kickall", "↩️", fixed={"cancel": True, "reason": ""}),
        Action("stop", "Stop server", "stop", "\U0001f6d1", RED,
               confirm="Save and SHUT DOWN the server? It has to be started again from the host panel."),
    )),
    Group("\U0001f465 Players & Ranks", "Access levels, ranks and Discord ↔ PZ links.", 0x9B59B6, (
        Action("setaccesslevel", "Access level", "setaccesslevel", "\U0001f6e1️"),
        Action("setrank", "Set rank", "setrank", "\U0001f3c5"),
        Action("syncranks", "Sync ranks", "syncranks", "\U0001f501"),
        Action("linkname", "Link", "linkname", "\U0001f517", BLUE),
        Action("unlinkname", "Unlink", "unlinkname", "⛓️", RED),
        Action("listlinks", "List links", "listlinks", "\U0001f4dc"),
    )),
    Group("\U0001f4dd Whitelist", "Manage server accounts and whitelist requests.", 0x2ECC71, (
        Action("whitelistadd", "Add", "whitelistadd", "➕", GREEN),
        Action("whitelistmodify", "Modify", "whitelistmodify", "✏️", BLUE),
        Action("whitelistremove", "Remove", "whitelistremove", "\U0001f5d1️", RED),
        Action("whitelistcancel", "Cancel request", "whitelistcancel", "\U0001f6ab"),
        Action("whitelistlist", "List", "whitelistlist", "\U0001f4cb"),
        Action("whitelistrolesync", "Sync roles", "whitelistrolesync", "\U0001f3ad"),
        Action("whitelistsetup", "Post apply panel", "whitelistsetup", "\U0001f4cc"),
    )),
    Group("\U0001f319 Events", "Siege Night, airdrops and supply events.", 0x1ABC9C, (
        Action("siegestatus", "Siege status", "siegestatus", "\U0001f319"),
        Action("siegestart", "Start siege", "siegestart", "⚔️", RED,
               confirm="Start a siege night now?"),
        Action("siegestop", "Stop siege", "siegestop", "\U0001f6d1",
               confirm="Stop the active siege night?"),
        Action("siegeschedule", "Schedule siege", "siegeschedule", "\U0001f4c5"),
        Action("airdrop", "Airdrop", "airdrop", "\U0001fa82", BLUE),
        Action("airdropstatus", "Airdrop status", "airdropstatus", "\U0001f4e6"),
        Action("supplyevent", "Supply event", "supplyevent", "\U0001f69a", BLUE,
               confirm="Start a supply event now?"),
        Action("supplyeventstatus", "Supply status", "supplyeventstatus", "\U0001f4ca"),
    )),
    Group("\U0001f514 Bot", "Turn bot notifications on or off.", 0x95A5A6, (
        Action("features", "Features", "features", "\U0001f4cb"),
        Action("enable", "Enable", "enable", "\U0001f7e2", GREEN),
        Action("disable", "Disable", "disable", "\U0001f534", RED),
    )),
)

ACTIONS = {a.key: a for g in GROUPS for a in g.actions}


class AdminPanel(PanelCog):
    panel_prefix = PREFIX
    panel_groups = GROUPS
    log_name = "AdminPanel"

    def __init__(self, bot: commands.Bot):
        super().__init__(bot)
        self._channel_id = int(getattr(bot.config, "ADMIN_PANEL_CHANNEL_ID", 0) or 0)
        print(f"[AdminPanel] {len(ACTIONS)} buttons in {len(GROUPS)} groups; "
              f"channel={self._channel_id or 'where /adminpanel runs'}")

    async def allowed(self, interaction: discord.Interaction) -> bool:
        role = None
        if interaction.guild is not None:
            role = discord.utils.get(interaction.guild.roles, name=self.bot.config.DEFAULT_ROLE)
        if role is not None and role in getattr(interaction.user, "roles", ()):
            return True
        await interaction.response.send_message(
            f"❌ You need the **{self.bot.config.DEFAULT_ROLE}** role to use the admin panel.",
            ephemeral=True)
        return False

    @app_commands.command(name="adminpanel",
                          description="Post the admin control panel (buttons for every admin command).")
    @admin_only()
    async def cmd_admin_panel(self, interaction: discord.Interaction) -> None:
        channel = self.bot.get_channel(self._channel_id) if self._channel_id else interaction.channel
        await self.post(interaction, channel)


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminPanel(bot))
    print("[AdminPanel] Extension loaded.")
