"""whitelist.py — whitelist request form (button → modal → CSV → approval channel).

Flow:
  1. An admin runs `/whitelistsetup`, which posts a persistent "Apply for
     Whitelist" button into the whitelist channel (WHITELIST_CHANNEL_ID).
  2. A user clicks the button → a modal opens asking for username, password and
     SteamID.
  3. On submit the request is appended to a CSV on the bot host (the VPS) and
     forwarded to the approval channel (WHITELIST_APPROVAL_CHANNEL_ID).

Config (see config.env.example):
  WHITELIST_CHANNEL_ID          — channel that holds the request button.
  WHITELIST_APPROVAL_CHANNEL_ID — channel that receives new requests.
  WHITELIST_CSV_PATH            — where requests are stored (default whitelist_requests.csv).
"""
from __future__ import annotations

import csv
import datetime
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

_DEFAULT_CSV = "whitelist_requests.csv"
_CSV_HEADERS = ["timestamp", "discord_username", "discord_id",
                "username", "password", "steam_id"]

_APPLY_CUSTOM_ID = "whitelist:apply"


class WhitelistModal(discord.ui.Modal, title="Whitelist Request"):
    """The form a user fills in to request whitelist access."""

    username = discord.ui.TextInput(
        label="Username",
        placeholder="Your in-game username",
        required=True,
        max_length=64,
    )
    password = discord.ui.TextInput(
        label="Password",
        placeholder="Your account password",
        required=True,
        max_length=128,
    )
    steam_id = discord.ui.TextInput(
        label="SteamID",
        placeholder="Your SteamID (e.g. 7656119…)",
        required=True,
        max_length=32,
    )

    def __init__(self, cog: "WhitelistCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_request(
            interaction,
            username=self.username.value.strip(),
            password=self.password.value.strip(),
            steam_id=self.steam_id.value.strip(),
        )


class WhitelistView(discord.ui.View):
    """Persistent view holding the single "Apply for Whitelist" button."""

    def __init__(self, cog: "WhitelistCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Apply for Whitelist",
        style=discord.ButtonStyle.green,
        emoji="\U0001f4dd",  # 📝
        custom_id=_APPLY_CUSTOM_ID,
    )
    async def apply_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(WhitelistModal(self.cog))


class WhitelistCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._channel_id = int(getattr(bot.config, "WHITELIST_CHANNEL_ID", 0) or 0)
        self._approval_channel_id = int(getattr(bot.config, "WHITELIST_APPROVAL_CHANNEL_ID", 0) or 0)

        csv_path = getattr(bot.config, "WHITELIST_CSV_PATH", "") or _DEFAULT_CSV
        self._csv_path = Path(csv_path)
        if not self._csv_path.is_absolute():
            self._csv_path = Path(__file__).parent / self._csv_path

        # Persistent view — re-registered on every startup so the button keeps
        # working across restarts (the message itself persists in Discord).
        self.view = WhitelistView(self)
        bot.add_view(self.view)

        print(f"[Whitelist] channel={self._channel_id or 'unset'} "
              f"approval={self._approval_channel_id or 'unset'} "
              f"csv={self._csv_path}")

    # ---- helpers -------------------------------------------------------------

    def _channel(self):
        if self._channel_id:
            return self.bot.get_channel(self._channel_id)
        return None

    def _approval_channel(self):
        if self._approval_channel_id:
            return self.bot.get_channel(self._approval_channel_id)
        return None

    def _append_csv(self, row: dict) -> None:
        self._csv_path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self._csv_path.is_file()
        try:
            with self._csv_path.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=_CSV_HEADERS)
                if new_file:
                    writer.writeheader()
                writer.writerow(row)
        except OSError as e:
            print(f"[Whitelist] Failed to write CSV {self._csv_path}: {e}")

    def _build_approval_embed(self, username: str, password: str, steam_id: str,
                              user: discord.User, timestamp: str) -> discord.Embed:
        embed = discord.Embed(
            title="\U0001f4dd New Whitelist Request",
            description=f"Submitted by **{user.mention}** (`{user.name}`).",
            colour=discord.Colour.green(),
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.add_field(name="Username", value=f"`{username}`", inline=False)
        embed.add_field(name="Password", value=f"`{password}`", inline=False)
        embed.add_field(name="SteamID", value=f"`{steam_id}`", inline=False)
        embed.add_field(name="Submitted at", value=timestamp, inline=False)
        embed.set_footer(text="Review and add to the server whitelist.")
        return embed

    # ---- request handling ----------------------------------------------------

    async def handle_request(self, interaction: discord.Interaction,
                             username: str, password: str, steam_id: str) -> None:
        """Persist the request to CSV and forward it to the approval channel."""
        now = datetime.datetime.now(datetime.timezone.utc)
        timestamp = now.strftime("%Y-%m-%d %H:%M UTC")
        user = interaction.user

        row = {
            "timestamp": now.isoformat(),
            "discord_username": str(user),
            "discord_id": str(user.id),
            "username": username,
            "password": password,
            "steam_id": steam_id,
        }
        self._append_csv(row)

        channel = self._approval_channel()
        if channel is None:
            print("[Whitelist] Approval channel not configured/found; request saved to CSV only.")
        else:
            try:
                await channel.send(embed=self._build_approval_embed(
                    username, password, steam_id, user, timestamp))
            except discord.HTTPException as e:
                print(f"[Whitelist] Failed to send approval message: {e}")

        await interaction.response.send_message(
            "\u2705 Your whitelist request has been submitted for review.",
            ephemeral=True,
        )
        print(f"[Whitelist] Request from {user} (SteamID {steam_id}) saved.")

    # ---- commands ------------------------------------------------------------

    @app_commands.command(
        name="whitelistsetup",
        description="Post the whitelist request button into the whitelist channel.",
    )
    async def cmd_whitelist_setup(self, interaction: discord.Interaction) -> None:
        role = discord.utils.get(interaction.guild.roles, name=self.bot.config.DEFAULT_ROLE)
        if role is None or role not in interaction.user.roles:
            await interaction.response.send_message(embed=discord.Embed(
                title="Permission Denied",
                description=f"You need the **{self.bot.config.DEFAULT_ROLE}** role.",
                colour=discord.Colour.red(),
            ), ephemeral=True)
            return

        channel = self._channel()
        if channel is None:
            await interaction.response.send_message(
                "Whitelist channel is not configured (WHITELIST_CHANNEL_ID).",
                ephemeral=True,
            )
            return

        await channel.send(
            "\U0001f4dd **Whitelist Application**\n\n"
            "Click the button below to request for server whitelisting.\n"
            "You will be asked for a `username`, `password`, and `Steam ID`\n\n"
            "PZ Tambayan \u2022 Whitelist Request",
            view=self.view,
        )
        await interaction.response.send_message(
            "Whitelist button posted.",
            ephemeral=True,
        )

    @app_commands.command(
        name="whitelist",
        description="Open the whitelist request form.",
    )
    async def cmd_whitelist(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(WhitelistModal(self))


async def setup(bot: commands.Bot):
    await bot.add_cog(WhitelistCog(bot))
    print("[Whitelist] Extension loaded.")
