"""whitelist.py — whitelist request form (button → modal → CSV → approval → players.db).

Flow:
  1. An admin runs `/whitelistsetup`, which posts a persistent "Apply for
     Whitelist" button into the whitelist channel (WHITELIST_CHANNEL_ID).
  2. A user clicks the button → a modal opens asking for username, password,
     SteamID and whether they want character lore.
  3. On submit the request is appended to a CSV on the bot host and forwarded to
     the approval channel (WHITELIST_APPROVAL_CHANNEL_ID) with Approve/Deny
     buttons.
  4. Approve → adds the user over RCON (`adduser` + `addSteamID` so the account
     is bound to a single SteamID), records the RCON command in `Code`, the admin
     in `Whitelisted By`, flags `isWhitelisted`, and DMs a welcome message.
     Deny → asks the admin for a reason, records it in `Notes`, and DMs the
     requester the denial reason.

CSV columns (see config.env.example → WHITELIST_CSV_PATH):
  Timestamp | Username | Password | Discord Username | SteamID | Code |
  withCharacterLore | isWhitelisted | Whitelisted By | Notes | request_id
  (`request_id` is an internal key, kept as the trailing column, used only to
  match a request back to its approval buttons.)

Config (see config.env.example):
  WHITELIST_CHANNEL_ID          — channel that holds the request button.
  WHITELIST_APPROVAL_CHANNEL_ID — channel that receives new requests.
  WHITELIST_CSV_PATH            — where requests are stored (default whitelist_requests.csv).
"""
from __future__ import annotations

import csv
import datetime
import uuid
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

_DEFAULT_CSV = "whitelist_requests.csv"

_CSV_HEADERS = ["Timestamp", "Username", "Password", "Discord Username", "SteamID",
                "Code", "withCharacterLore", "isWhitelisted", "Whitelisted By", "Notes",
                "request_id"]

_APPLY_CUSTOM_ID = "whitelist:apply"


def _normalize_yesno(value: str) -> str:
    """Normalize a yes/no answer to 'true'/'false'; anything else is kept as-is."""
    v = value.strip().lower()
    if v in ("yes", "y", "true", "1", "with lore"):
        return "true"
    if v in ("no", "n", "false", "0", "without lore"):
        return "false"
    return value.strip()


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
    character_lore = discord.ui.TextInput(
        label="Character Lore",
        placeholder="yes/no — do you want your character in the server's lore?",
        required=True,
        max_length=8,
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
            character_lore=_normalize_yesno(self.character_lore.value),
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


class DenyReasonModal(discord.ui.Modal, title="Deny Whitelist Request"):
    """Asks the approving admin why a request is being denied."""

    reason = discord.ui.TextInput(
        label="Reason for denial",
        style=discord.TextStyle.paragraph,
        placeholder="Why is this request being denied?",
        required=True,
        max_length=1024,
    )

    def __init__(self, cog: "WhitelistCog", view: "WhitelistApprovalView", message: discord.Message):
        super().__init__()
        self.cog = cog
        self.view = view
        self.message = message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.complete_denial(
            interaction, self.view, self.message, self.reason.value.strip())


class WhitelistApprovalView(discord.ui.View):
    """Approve/Deny buttons attached to each approval message (not persistent)."""

    def __init__(self, cog: "WhitelistCog", request_id: str,
                 username: str, password: str, steam_id: str,
                 character_lore: str, submitter: discord.User, timestamp: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.request_id = request_id
        self.username = username
        self.password = password
        self.steam_id = steam_id
        self.character_lore = character_lore
        self.submitter = submitter
        self.timestamp = timestamp

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.green, emoji="\u2705")
    async def approve(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.approve_request(interaction, self)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.red, emoji="\u274c")
    async def deny(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not self.cog._is_admin(interaction):
            await interaction.response.send_message(
                "\u274c You don't have permission to deny requests.", ephemeral=True)
            return
        await interaction.response.send_modal(
            DenyReasonModal(self.cog, self, interaction.message))


class WhitelistCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._channel_id = int(getattr(bot.config, "WHITELIST_CHANNEL_ID", 0) or 0)
        self._approval_channel_id = int(getattr(bot.config, "WHITELIST_APPROVAL_CHANNEL_ID", 0) or 0)

        csv_path = getattr(bot.config, "WHITELIST_CSV_PATH", "") or _DEFAULT_CSV
        self._csv_path = Path(csv_path)
        if not self._csv_path.is_absolute():
            self._csv_path = Path(__file__).parent / self._csv_path

        self._migrate_csv()

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

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            return False
        role = discord.utils.get(interaction.guild.roles, name=self.bot.config.DEFAULT_ROLE)
        return role is not None and role in interaction.user.roles

    # ---- CSV -------------------------------------------------------------

    def _migrate_csv(self) -> None:
        """Migrate a pre-rework CSV to the current column format (in place)."""
        if not self._csv_path.is_file():
            return
        try:
            if self._csv_path.stat().st_size == 0:
                return
        except OSError:
            return
        with self._csv_path.open("r", newline="", encoding="utf-8-sig") as fh:
            header = fh.readline().strip().lower()
        if "withcharacterlore" in header:
            return  # already in the current format
        rows = self._read_csv()  # keys are the old headers
        migrated = []
        for row in rows:
            status = (row.get("status") or "").strip().lower()
            migrated.append({
                "Timestamp": row.get("timestamp", ""),
                "Username": row.get("username", ""),
                "Password": row.get("password", ""),
                "Discord Username": row.get("discord_username", ""),
                "SteamID": row.get("steam_id", ""),
                "Code": "",
                "withCharacterLore": "",
                "isWhitelisted": "true" if status == "approved" else "false",
                "Whitelisted By": "",
                "Notes": row.get("reason", ""),
                "request_id": row.get("request_id", ""),
            })
        self._write_csv(migrated)
        print(f"[Whitelist] Migrated CSV {self._csv_path} to the new column format "
              f"({len(migrated)} row(s)).")

    def _read_csv(self) -> list:
        if not self._csv_path.is_file():
            return []
        try:
            with self._csv_path.open("r", newline="", encoding="utf-8") as fh:
                return list(csv.DictReader(fh))
        except OSError as e:
            print(f"[Whitelist] Failed to read CSV {self._csv_path}: {e}")
            return []

    def _write_csv(self, rows: list) -> None:
        self._csv_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._csv_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=_CSV_HEADERS)
                writer.writeheader()
                writer.writerows(rows)
        except OSError as e:
            print(f"[Whitelist] Failed to write CSV {self._csv_path}: {e}")

    def _append_csv(self, row: dict) -> None:
        self._csv_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            needs_header = (not self._csv_path.is_file()
                            or self._csv_path.stat().st_size == 0)
        except OSError:
            needs_header = True
        try:
            with self._csv_path.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=_CSV_HEADERS)
                if needs_header:
                    writer.writeheader()
                writer.writerow(row)
        except OSError as e:
            print(f"[Whitelist] Failed to write CSV {self._csv_path}: {e}")

    def _update_csv_row(self, request_id: str, updates: dict) -> None:
        """Update one CSV row (matched by its internal `request_id`)."""
        rows = self._read_csv()
        for row in rows:
            if row.get("request_id") == request_id:
                for key, value in updates.items():
                    row[key] = value
                break
        self._write_csv(rows)

    # ---- approval message --------------------------------------------------

    def _build_approval_embed(self, username: str, password: str, steam_id: str,
                              character_lore: str, user: discord.User,
                              timestamp: str) -> discord.Embed:
        embed = discord.Embed(
            title="\U0001f4dd New Whitelist Request",
            description=f"Submitted by **{user.mention}** (`{user.name}`).",
            colour=discord.Colour.green(),
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.add_field(name="Username", value=f"`{username}`", inline=False)
        embed.add_field(name="Password", value=f"`{password}`", inline=False)
        embed.add_field(name="SteamID", value=f"`{steam_id}`", inline=False)
        embed.add_field(name="Character Lore", value=character_lore, inline=False)
        embed.add_field(name="Submitted at", value=timestamp, inline=False)
        embed.set_footer(text="Approve to add to the server whitelist, or Deny with a reason.")
        return embed

    async def _finalize_approval(self, message, view: WhitelistApprovalView,
                                 status: str, admin: discord.User, reason: str = "") -> None:
        """Edit the approval message to show the decision and drop the buttons."""
        if message is None:
            return
        embed = self._build_approval_embed(
            view.username, view.password, view.steam_id, view.character_lore,
            view.submitter, view.timestamp)
        if status == "approved":
            embed.add_field(name="Status", value=f"\u2705 Approved by {admin.mention}", inline=False)
            embed.colour = discord.Colour.green()
        elif status == "denied":
            value = f"\u274c Denied by {admin.mention}"
            if reason:
                value += f" — {reason}"
            embed.add_field(name="Status", value=value, inline=False)
            embed.colour = discord.Colour.red()
        try:
            await message.edit(embed=embed, view=None)
        except discord.HTTPException as e:
            print(f"[Whitelist] Failed to update approval message: {e}")

    # ---- requester DMs ------------------------------------------------------

    async def _dm_submitter(self, submitter: discord.User, embed: discord.Embed) -> None:
        """DM the requester; log (don't raise) if their DMs can't be reached."""
        try:
            await submitter.send(embed=embed)
        except discord.HTTPException as e:
            print(f"[Whitelist] Could not DM {submitter} (id={submitter.id}): {e}")

    async def _send_approval_dm(self, view: WhitelistApprovalView) -> None:
        """Welcome the requester now that they've been whitelisted."""
        submitter = view.submitter
        embed = discord.Embed(
            title="\u2705 Whitelist Approved",
            description=(
                f"Hey {submitter.display_name}, your whitelist request for "
                f"**PZ Tambayan** has been **approved**! \U0001f389\n\n"
                f"Welcome to the barangay! Your account is now on the server whitelist.\n\n"
                f"- **Username:** `{view.username}`\n"
                f"- **SteamID:** `{view.steam_id}`\n\n"
                f"You can now join the server. See you in the apocalypse! \U0001f9df"
            ),
            colour=discord.Colour.green(),
        )
        await self._dm_submitter(submitter, embed)

    async def _send_denial_dm(self, view: WhitelistApprovalView, reason: str) -> None:
        """Tell the requester their request was denied, and why."""
        submitter = view.submitter
        embed = discord.Embed(
            title="\u274c Whitelist Denied",
            description=(
                f"Hey {submitter.display_name}, your whitelist request for "
                f"**PZ Tambayan** was **denied**.\n\n"
                f"**Reason:** {reason}\n\n"
                f"If you believe this was a mistake, please reach out to an admin."
            ),
            colour=discord.Colour.red(),
        )
        await self._dm_submitter(submitter, embed)

    # ---- RCON whitelist -----------------------------------------------------

    async def _add_whitelist_user_rcon(self, username: str, password: str, steam_id: str) -> tuple[str, str]:
        """Add a whitelist account + bind its SteamID over RCON.

        Uses the server's own `adduser` command (which hashes the password
        correctly server-side) and `addSteamID` to lock the account to a single
        SteamID. Returns `(error, rcon_command)` — `error` is "" on success,
        `rcon_command` is the command line(s) actually sent (stored in `Code`).
        """
        rcon = self.bot.rcon
        u = username.replace('"', "").strip()
        p = password.replace('"', "").strip()
        s = steam_id.replace('"', "").strip()

        if not rcon.is_server_online():
            return "server is offline (RCON unreachable)", ""

        cmds = []
        cmd1 = f'adduser "{u}" "{p}"'
        cmds.append(cmd1)
        resp = await rcon.send_command(cmd1)
        if resp is not None:
            low = resp.lower()
            if "already" in low or "exist" in low:
                return f"'{u}' may already be whitelisted: {resp.strip()}", "; ".join(cmds)
            print(f"[Whitelist] adduser resp: {resp!r}")
        elif not rcon.is_server_online():
            # None usually means an empty success response, but if the server
            # dropped mid-command, surface that as a real failure.
            return "RCON `adduser` failed (connection lost)", "; ".join(cmds)

        if s:
            cmd2 = f'addSteamID "{s}"'
            cmds.append(cmd2)
            resp2 = await rcon.send_command(cmd2)
            if resp2 is not None:
                print(f"[Whitelist] addSteamID resp: {resp2!r}")
            elif not rcon.is_server_online():
                return "account added, but `addSteamID` failed (connection lost)", "; ".join(cmds)

        return "", "; ".join(cmds)

    # ---- request handling ----------------------------------------------------

    async def handle_request(self, interaction: discord.Interaction,
                             username: str, password: str, steam_id: str,
                             character_lore: str = "false") -> None:
        """Persist the request to CSV and forward it to the approval channel."""
        now = datetime.datetime.now(datetime.timezone.utc)
        timestamp = now.strftime("%Y-%m-%d %H:%M:%S UTC")
        user = interaction.user
        request_id = uuid.uuid4().hex

        self._append_csv({
            "Timestamp": timestamp,
            "Username": username,
            "Password": password,
            "Discord Username": str(user),
            "SteamID": steam_id,
            "Code": "",
            "withCharacterLore": character_lore,
            "isWhitelisted": "false",
            "Whitelisted By": "",
            "Notes": "",
            "request_id": request_id,
        })

        channel = self._approval_channel()
        if channel is None:
            print("[Whitelist] Approval channel not configured/found; request saved to CSV only.")
        else:
            view = WhitelistApprovalView(self, request_id, username, password, steam_id,
                                         character_lore, user, timestamp)
            try:
                await channel.send(
                    embed=self._build_approval_embed(username, password, steam_id,
                                                     character_lore, user, timestamp),
                    view=view,
                )
            except discord.HTTPException as e:
                print(f"[Whitelist] Failed to send approval message: {e}")

        await interaction.response.send_message(
            "\u2705 Your whitelist request has been submitted for review.",
            ephemeral=True,
        )
        print(f"[Whitelist] Request from {user} (SteamID {steam_id}) saved.")

    # ---- approval actions ----------------------------------------------------

    async def approve_request(self, interaction: discord.Interaction,
                              view: WhitelistApprovalView) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "\u274c You don't have permission to approve requests.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        err, code = await self._add_whitelist_user_rcon(view.username, view.password, view.steam_id)
        if err:
            await interaction.followup.send(f"\u274c Approval failed: {err}", ephemeral=True)
            return
        self._update_csv_row(view.request_id, {
            "Code": code,
            "isWhitelisted": "true",
            "Whitelisted By": str(interaction.user),
        })
        await self._finalize_approval(interaction.message, view, "approved", interaction.user)
        await self._send_approval_dm(view)
        await interaction.followup.send(
            f"\u2705 Approved **{view.username}** and added to the whitelist.", ephemeral=True)
        print(f"[Whitelist] Approved {view.username} (SteamID {view.steam_id})")

    async def complete_denial(self, interaction: discord.Interaction,
                              view: WhitelistApprovalView, message, reason: str) -> None:
        self._update_csv_row(view.request_id, {
            "isWhitelisted": "false",
            "Notes": reason,
        })
        await self._finalize_approval(message, view, "denied", interaction.user, reason)
        await self._send_denial_dm(view, reason)
        await interaction.response.send_message(
            f"\u274c Denied. Reason recorded in the CSV.", ephemeral=True)
        print(f"[Whitelist] Denied request {view.request_id}: {reason}")

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
