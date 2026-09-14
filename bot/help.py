"""help.py — `/help` command listing all bot commands grouped by purpose."""

import discord
from discord import app_commands
from discord.ext import commands

# Command guide, grouped by category. `[admin]` marks role-gated commands.
_COMMANDS: dict[str, list[tuple[str, str]]] = {
    "🖥️ Server Status & Info": [
        ("/hello", "Health check — the bot replies if it's alive. [admin]"),
        ("/online", "Check whether the game server is online. [admin]"),
        ("/players", "List players currently connected. [admin]"),
        ("/playerlist", "Everyone who has ever joined (+ sessions & deaths). [admin]"),
        ("/modlist", "Show configured mods / Workshop items."),
        ("/features", "List notification features and their on/off state. [admin]"),
    ],
    "🔧 Server Control": [
        ("/stop", "Graceful shutdown (RCON save + quit). [admin]"),
        ("/restart", "Force a server restart (in-game announcement + kick in 1 min). [admin]"),
        ("/msg <message>", "Broadcast a red-alert message to all players in-game. [admin]"),
        ("/announce <kind>", "Post a server banner (up / restarting / down / modupdate). [admin]"),
        ("/playsound <sound> [message]", "Trigger a Jeeves Alerts sound on all players. [admin]"),
        ("/teleport <player1> <player2>", "Teleport player1 to player2's location. [admin]"),
        ("/forcemodupdate", "Force a mod-update restart now. [admin]"),
    ],
    "👥 Players & Ranks": [
        ("/setaccesslevel <player> <level>", "Set a player's server access level. [admin]"),
        ("/setrank <username> <rank>", "Set a player's in-game rank (chat name colour). [admin]"),
        ("/syncranks", "Rebuild the rank file from all linked members. [admin]"),
        ("/linkname <member> <username>", "Link a Discord user to their PZ username. [admin]"),
        ("/unlinkname <member>", "Remove a user's Discord-to-PZ link. [admin]"),
        ("/linkme <username>", "Link your Discord account to your PZ username."),
        ("/unlinkme", "Remove your own Discord-to-PZ link."),
        ("/myrank", "Show your in-game rank and chat colour."),
    ],
    "📝 Whitelist": [
        ("/whitelistsetup", "Post the whitelist application button into the whitelist channel. [admin]"),
        ("/whitelist", "Open the whitelist request form."),
    ],
    "🌙 Siege Night (Siege Night mod)": [
        ("/siegestatus", "Show current Siege Night status. [admin]"),
        ("/siegestart", "Force a siege night to start immediately. [admin]"),
        ("/siegestop", "Stop the active siege. [admin]"),
        ("/siegeschedule <day>", "Change the next siege night day. [admin]"),
    ],
    "📦 Airdrops & Events (Jeeve's Drops)": [
        ("/airdrop [player] [crate]", "Trigger an airdrop."),
        ("/airdropstatus", "Show airdrop status."),
        ("/supplyevent", "Trigger a supply event."),
        ("/supplyeventstatus", "Show supply event status."),
    ],
    "🔔 Notifications": [
        ("/enable <feature>", "Enable a notification feature. [admin]"),
        ("/disable <feature>", "Disable a notification feature. [admin]"),
    ],
}


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="List all bot commands and their purposes (admins only).")
    async def cmd_help(self, interaction: discord.Interaction) -> None:
        role = discord.utils.get(interaction.guild.roles, name=self.bot.config.DEFAULT_ROLE)
        if role is None or role not in interaction.user.roles:
            await interaction.response.send_message(
                "\u274c You need the **admin** role to use this command.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="📖 PZ Tambayan Bot — Command Guide",
            description=(
                "Everything the bot can do, grouped by purpose. Commands marked "
                "`[admin]` require the admin role."
            ),
            colour=discord.Colour.blurple(),
        )
        for category, cmds in _COMMANDS.items():
            value = "\n".join(f"`{name}` — {desc}" for name, desc in cmds)
            embed.add_field(name=category, value=value, inline=False)
        embed.set_footer(text="Use /features to see which notification features are currently enabled.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))
    print("[Help] Extension loaded.")
