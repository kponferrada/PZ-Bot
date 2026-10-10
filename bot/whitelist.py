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
     in `Whitelisted By`, flags `isWhitelisted`, links the requester's Discord
     account to the username (as `/linkme` would, through RankSync) and DMs a
     welcome message. The link is skipped if either side is already linked.
     Deny → asks the admin for a reason, records it in `Notes`, and DMs the
     requester the denial reason.
  5. After approval the card keeps four buttons:
     - Change Username → PZ has no rename command, so the bot creates the new
       account (`adduser`) and then removes the old one. PZ keeps characters
       per username, so the player starts without their old character. The
       Discord link (rank_links.json) moves to the new name.
     - Change Password → `removeuser` + `adduser` with the new password (the
       stored password is a hash; if re-adding fails the old one is restored).
     - Check Status → the account on the server whitelist (SteamID, access
       level, last connection), whether the player is online, and the link.
     - Delete Account → removes the account over RCON (`removeuser` +
       `removeSteamID`), records the reason in `Notes`, and updates the form.
     Name and password changes are refused while the player is online, update
     the CSV and the card, and DM the requester their new login.

The Approve/Deny buttons are *persistent* — their `custom_id` encodes the
request id, so they keep working across bot restarts (views are re-registered
from the CSV on startup).

CSV columns (see config.env.example → WHITELIST_CSV_PATH):
  Timestamp | Username | Password | Discord Username | SteamID | Code |
  withCharacterLore | isWhitelisted | Whitelisted By | Notes | request_id | discord_id
  (`request_id` and `discord_id` are internal trailing keys: `request_id` matches
  a request back to its approval buttons, `discord_id` lets the bot DM the
  requester after a restart.)

Config (see config.env.example):
  WHITELIST_CHANNEL_ID          — channel that holds the request button.
  WHITELIST_APPROVAL_CHANNEL_ID — channel that receives new requests.
  WHITELIST_CSV_PATH            — where requests are stored (default whitelist_requests.csv).
"""
from __future__ import annotations

import asyncio
import csv
import datetime
import sqlite3
import uuid
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

import sftp_client

_DEFAULT_CSV = "whitelist_requests.csv"

_CSV_HEADERS = ["Timestamp", "Username", "Password", "Discord Username", "SteamID",
                "Code", "withCharacterLore", "isWhitelisted", "Whitelisted By", "Notes",
                "request_id", "discord_id"]

_APPLY_CUSTOM_ID = "whitelist:apply"


def _normalize_yesno(value: str) -> str:
    """Normalize a yes/no answer to 'true'/'false'; anything else is kept as-is."""
    v = value.strip().lower()
    if v in ("yes", "y", "true", "1", "with lore"):
        return "true"
    if v in ("no", "n", "false", "0", "without lore"):
        return "false"
    return value.strip()


def _validate_steam_id(value: str) -> str | None:
    """Return an error message if `value` isn't a valid SteamID64, else None.

    A SteamID64 is a 17-digit decimal number. (The leading digits vary — not all
    accounts share the same prefix — so we only enforce length + digits.)
    """
    sid = (value or "").strip()
    if not sid:
        return "SteamID is required."
    if not sid.isdigit():
        return f"SteamID must contain only numbers — `{sid}` has other characters."
    if len(sid) != 17:
        return f"SteamID must be exactly 17 digits — `{sid}` has {len(sid)}."
    return None


def _fmt_last_connection(value) -> str:
    """PZ stores lastConnection as text ("dd-MM-yy HH:mm:ss") or epoch ms; show it as-is or as <t:>."""
    if value in (None, ""):
        return "never"
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return str(value)
    if n > 10**11:          # epoch milliseconds
        n //= 1000
    return f"<t:{n}:R>" if n > 0 else "never"


def account_status_fields(request: dict, account: dict | None, server_online: bool | None,
                          online_names, linked_name: str | None,
                          username_holder: int | None) -> list:
    """(name, value) rows for the Check Status reply. Pure: no Discord or SFTP.

    `account` is the server whitelist row (None if not found), `online_names`
    the players the bot last saw online, `linked_name` the PZ name the
    requester is linked to, `username_holder` the Discord id linked to this
    username (None if nobody).
    """
    username = request.get("Username", "")
    approved = (request.get("isWhitelisted", "") or "").strip().lower() == "true"
    notes = (request.get("Notes", "") or "").strip()
    if approved:
        card = f"\u2705 Approved by {request.get('Whitelisted By') or '?'}"
    elif notes:
        card = f"\u274c Denied / deleted — {notes}"
    else:
        card = "\u23f3 Pending"
    rows = [("Request", card)]

    if account is None:
        rows.append(("Server whitelist", "\u274c No account with this username on the server"))
    else:
        lines = [f"\u2705 `{account.get('username')}`"]
        sid = account.get("steamid") or ""
        want = (request.get("SteamID") or "").strip()
        if sid:
            lines.append(f"SteamID `{sid}`" + ("" if not want or sid == want else f" (request has `{want}`)"))
        else:
            lines.append("SteamID: not bound")
        role = account.get("role")
        if str(role).isdigit():
            lines.append("Access: " + ("Admin" if int(role) >= 7 else f"Player (level {role})"))
        if account.get("banned") not in (None, "", 0, "0", False, "false"):
            lines.append("\u26d4 **Banned**")
        if "lastConnection" in account:
            lines.append(f"Last connection: {_fmt_last_connection(account.get('lastConnection'))}")
        rows.append(("Server whitelist", "\n".join(lines)))

    names = {n.lower() for n in (online_names or ())}
    if server_online is False:
        rows.append(("Online", "Server is offline"))
    else:
        rows.append(("Online", "\U0001f7e2 In game now" if username.lower() in names else "\u26aa Not in game"))

    if linked_name and linked_name.lower() == username.lower():
        link = f"\U0001f517 Linked to **{linked_name}**"
    elif linked_name:
        link = f"\u26a0\ufe0f Requester is linked to **{linked_name}**, not this username"
    else:
        link = "Requester is not linked"
    if username_holder is not None and not (linked_name and linked_name.lower() == username.lower()):
        link += f"\n\u26a0\ufe0f **{username}** is linked to <@{username_holder}>"
    rows.append(("Discord link", link))
    return rows


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
        placeholder="Your 17-digit SteamID64",
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
        error = _validate_steam_id(self.steam_id.value)
        if error:
            await interaction.response.send_message(f"\u274c {error}", ephemeral=True)
            return
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

    def __init__(self, cog: "WhitelistCog", request_id: str, message: discord.Message):
        super().__init__()
        self.cog = cog
        self.request_id = request_id
        self.message = message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.complete_denial(
            interaction, self.request_id, self.message, self.reason.value.strip())


class WhitelistApprovalView(discord.ui.View):
    """Persistent Approve/Deny buttons for one request.

    The `custom_id`s encode the request id so the same buttons keep working
    after a bot restart (the view is re-registered from the CSV on startup).
    """

    def __init__(self, cog: "WhitelistCog", request_id: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.request_id = request_id

        approve = discord.ui.Button(
            label="Approve",
            style=discord.ButtonStyle.green,
            emoji="\u2705",
            custom_id=f"whitelist:approve:{request_id}",
        )
        approve.callback = self.approve
        self.add_item(approve)

        deny = discord.ui.Button(
            label="Deny",
            style=discord.ButtonStyle.red,
            emoji="\u274c",
            custom_id=f"whitelist:deny:{request_id}",
        )
        deny.callback = self.deny
        self.add_item(deny)

    async def approve(self, interaction: discord.Interaction) -> None:
        await self.cog.approve_request(interaction, self.request_id)

    async def deny(self, interaction: discord.Interaction) -> None:
        await self.cog.deny_request(interaction, self.request_id)


class WhitelistManageView(discord.ui.View):
    """Persistent buttons on an approved request's card.

    The Delete button keeps its old custom_id, so cards posted before the other
    buttons existed still work (they only show Delete until they're updated).
    """

    def __init__(self, cog: "WhitelistCog", request_id: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.request_id = request_id
        for label, style, emoji, action, callback in (
            ("Change Username", discord.ButtonStyle.blurple, "\u270f\ufe0f", "rename", self.rename),
            ("Change Password", discord.ButtonStyle.blurple, "\U0001f511", "password", self.password),
            ("Check Status", discord.ButtonStyle.grey, "\U0001f50e", "status", self.status),
            ("Delete Account", discord.ButtonStyle.red, "\U0001f5d1\ufe0f", "delete", self.delete),
        ):
            button = discord.ui.Button(label=label, style=style, emoji=emoji,
                                       custom_id=f"whitelist:{action}:{request_id}")
            button.callback = callback
            self.add_item(button)

    async def rename(self, interaction: discord.Interaction) -> None:
        await self.cog.open_rename(interaction, self.request_id)

    async def password(self, interaction: discord.Interaction) -> None:
        await self.cog.open_password(interaction, self.request_id)

    async def status(self, interaction: discord.Interaction) -> None:
        await self.cog.show_status(interaction, self.request_id)

    async def delete(self, interaction: discord.Interaction) -> None:
        await self.cog.delete_request(interaction, self.request_id)


class RenameModal(discord.ui.Modal, title="Change Username"):
    """New username for an approved account (re-created under the new name)."""

    new_username = discord.ui.TextInput(
        label="New username (old character is NOT kept)",
        placeholder="PZ saves characters per username",
        required=True,
        max_length=64,
    )
    password = discord.ui.TextInput(
        label="Password for the new account",
        required=True,
        max_length=128,
    )

    def __init__(self, cog: "WhitelistCog", request_id: str, message: discord.Message,
                 username: str, password: str):
        super().__init__()
        self.cog = cog
        self.request_id = request_id
        self.message = message
        self.new_username.default = username
        self.password.default = password or None

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.complete_rename(interaction, self.request_id, self.message,
                                       self.new_username.value.strip(), self.password.value.strip())


class PasswordModal(discord.ui.Modal, title="Change Password"):
    """New password for an approved account."""

    new_password = discord.ui.TextInput(
        label="New password",
        required=True,
        max_length=128,
    )

    def __init__(self, cog: "WhitelistCog", request_id: str, message: discord.Message):
        super().__init__()
        self.cog = cog
        self.request_id = request_id
        self.message = message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.complete_password(interaction, self.request_id, self.message,
                                         self.new_password.value.strip())


class DeleteReasonModal(discord.ui.Modal, title="Delete Whitelist Account"):
    """Asks the admin why the approved account is being deleted."""

    reason = discord.ui.TextInput(
        label="Reason for deletion",
        style=discord.TextStyle.paragraph,
        placeholder="Why is this account being deleted?",
        required=True,
        max_length=1024,
    )

    def __init__(self, cog: "WhitelistCog", request_id: str, message: discord.Message):
        super().__init__()
        self.cog = cog
        self.request_id = request_id
        self.message = message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.complete_deletion(
            interaction, self.request_id, self.message, self.reason.value.strip())


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

        # Persistent "Apply" view — re-registered on every startup so the button
        # keeps working across restarts (the message itself persists in Discord).
        self.view = WhitelistView(self)
        bot.add_view(self.view)

        # Re-register persistent views (Approve/Deny + Delete) so their buttons
        # survive a restart too.
        self._register_views()

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
        """Migrate an older CSV to the current column format (in place)."""
        if not self._csv_path.is_file():
            return
        try:
            if self._csv_path.stat().st_size == 0:
                return
        except OSError:
            return
        with self._csv_path.open("r", newline="", encoding="utf-8-sig") as fh:
            header = fh.readline().strip().lower()
        if "withcharacterlore" in header and "discord_id" in header:
            return  # already in the current format

        rows = self._read_csv()
        migrated = []
        for row in rows:
            # Case-insensitive field access (handles old lowercase + title-case).
            get = lambda *keys: next((row[k] for k in keys if k in row), "")
            status = get("status").strip().lower()
            migrated.append({
                "Timestamp": get("Timestamp", "timestamp"),
                "Username": get("Username", "username"),
                "Password": get("Password", "password"),
                "Discord Username": get("Discord Username", "discord_username"),
                "SteamID": get("SteamID", "steam_id"),
                "Code": get("Code"),
                "withCharacterLore": get("withCharacterLore"),
                "isWhitelisted": get("isWhitelisted") or ("true" if status == "approved" else "false"),
                "Whitelisted By": get("Whitelisted By"),
                "Notes": get("Notes", "reason"),
                "request_id": get("request_id"),
                "discord_id": get("discord_id"),
            })
        self._write_csv(migrated)
        print(f"[Whitelist] Migrated CSV {self._csv_path} to the current column format "
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

    def _lookup_request(self, request_id: str) -> dict | None:
        for row in self._read_csv():
            if row.get("request_id") == request_id:
                return row
        return None

    def _register_views(self) -> None:
        """Re-register persistent views (Approve/Deny for pending, management for approved)."""
        pending = 0
        approved = 0
        for row in self._read_csv():
            rid = row.get("request_id", "")
            if not rid:
                continue
            whitelisted = (row.get("isWhitelisted", "false") or "false").strip().lower() == "true"
            notes = (row.get("Notes", "") or "").strip()
            if whitelisted:
                self.bot.add_view(WhitelistManageView(self, rid))
                approved += 1
            elif not notes:
                self.bot.add_view(WhitelistApprovalView(self, rid))
                pending += 1
        if pending or approved:
            print(f"[Whitelist] Re-registered {pending} pending + {approved} approved view(s).")

    # ---- submitter fetch -----------------------------------------------------

    async def _fetch_submitter(self, discord_id: str) -> discord.User | None:
        if not discord_id:
            return None
        try:
            uid = int(discord_id)
        except (TypeError, ValueError):
            return None
        user = self.bot.get_user(uid)
        if user is None:
            try:
                user = await self.bot.fetch_user(uid)
            except discord.HTTPException as e:
                print(f"[Whitelist] Could not fetch submitter {discord_id}: {e}")
                return None
        return user

    # ---- approval message --------------------------------------------------

    def _request_embed(self, request: dict, submitter: discord.User | None) -> discord.Embed:
        discord_name = request.get("Discord Username", "")
        if submitter is not None:
            desc = f"Submitted by **{submitter.mention}** (`{submitter.name}`)."
        else:
            desc = f"Submitted by **{discord_name}**."
        embed = discord.Embed(
            title="\U0001f4dd New Whitelist Request",
            description=desc,
            colour=discord.Colour.green(),
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.add_field(name="Username", value=f"`{request.get('Username', '')}`", inline=False)
        embed.add_field(name="Password", value=f"`{request.get('Password', '')}`", inline=False)
        embed.add_field(name="SteamID", value=f"`{request.get('SteamID', '')}`", inline=False)
        embed.add_field(name="Character Lore", value=request.get("withCharacterLore", ""), inline=False)
        embed.add_field(name="Submitted at", value=request.get("Timestamp", ""), inline=False)
        embed.set_footer(text="Approve to add to the server whitelist, or Deny with a reason.")
        return embed

    async def _finalize_approval(self, message, request: dict, admin: discord.User,
                                 status: str, reason: str = "") -> None:
        """Edit the approval message to show the decision and update the buttons."""
        if message is None:
            return
        submitter = await self._fetch_submitter(request.get("discord_id", ""))
        embed = self._request_embed(request, submitter)
        view = None
        if status == "approved":
            embed.add_field(name="Status", value=f"\u2705 Approved by {admin.mention}", inline=False)
            embed.colour = discord.Colour.green()
            # Keep the management buttons (rename, password, status, delete).
            view = WhitelistManageView(self, request.get("request_id", ""))
        elif status == "denied":
            value = f"\u274c Denied by {admin.mention}"
            if reason:
                value += f" — {reason}"
            embed.add_field(name="Status", value=value, inline=False)
            embed.colour = discord.Colour.red()
        elif status == "deleted":
            value = f"\U0001f5d1\ufe0f Deleted by {admin.mention}"
            if reason:
                value += f" — {reason}"
            embed.add_field(name="Status", value=value, inline=False)
            embed.colour = discord.Colour.dark_grey()
        try:
            await message.edit(embed=embed, view=view)
        except discord.HTTPException as e:
            print(f"[Whitelist] Failed to update approval message: {e}")

    # ---- requester DMs ------------------------------------------------------

    async def _dm_submitter(self, submitter: discord.User, embed: discord.Embed) -> None:
        """DM the requester; log (don't raise) if their DMs can't be reached."""
        try:
            await submitter.send(embed=embed)
        except discord.HTTPException as e:
            print(f"[Whitelist] Could not DM {submitter} (id={submitter.id}): {e}")

    async def _send_approval_dm(self, submitter: discord.User, username: str, steam_id: str) -> None:
        """Welcome the requester now that they've been whitelisted."""
        embed = discord.Embed(
            title="\u2705 Whitelist Approved",
            description=(
                f"Hey {submitter.display_name}, your whitelist request for "
                f"**PZ Tambayan** has been **approved**! \U0001f389\n\n"
                f"Welcome to the barangay! Your account is now on the server whitelist.\n\n"
                f"- **Username:** `{username}`\n"
                f"- **SteamID:** `{steam_id}`\n\n"
                f"Before you proceed further, we would like to remind you to check below channels and read through them to familiarize yourself with the server rules and guidelines:\n"
                f"Rules: https://discord.com/channels/1541725057938882600/1541725891053363220 \n"
                f"In-Game Rules: https://discord.com/channels/1541725057938882600/1547307029297635328 \n"
                f"Modlist (Preferrably download from outside game before connecting): https://discord.com/channels/1541725057938882600/1541726944368926720 \n"
                f"Performance guide: https://discord.com/channels/1541725057938882600/1550188547787595837 \n"
                f"Server guides: https://discord.com/channels/1541725057938882600/1550134978216722494 \n"
                f"Please note that some mods may require you to load it locally first then restarting Project Zomboid before joining. One sample is Desire body mod: https://steamcommunity.com/sharedfiles/filedetails/?id=3766704869 \n\n"
                f"You can now join the server. See you in the apocalypse! \U0001f9df"
            ),
            colour=discord.Colour.green(),
        )
        await self._dm_submitter(submitter, embed)

    async def _send_denial_dm(self, submitter: discord.User, reason: str) -> None:
        """Tell the requester their request was denied, and why."""
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

        if not await asyncio.to_thread(rcon.is_server_online):
            detail = getattr(rcon, "last_error", "") or "connection failed"
            return f"server is offline / RCON unreachable — {detail}", ""

        cmds = []
        cmd1 = f'adduser "{u}" "{p}"'
        cmds.append(cmd1)
        resp = await rcon.send_command(cmd1)
        if resp is not None:
            low = resp.lower()
            if "already" in low or "exist" in low:
                # Account already exists (e.g. re-application) — don't abort;
                # still bind the SteamID below.
                print(f"[Whitelist] adduser: account already exists ({resp.strip()!r}); "
                      f"continuing to addSteamID")
            elif "created" in low or "added" in low:
                print(f"[Whitelist] adduser: {resp.strip()!r}")
            else:
                # Anything else is a genuine failure — surface the exact message.
                return f"`adduser` failed: {resp.strip()}", "; ".join(cmds)
        elif rcon.last_error:
            return f"`adduser` failed: {rcon.last_error}", "; ".join(cmds)

        if s:
            cmd2 = f'addSteamID "{s}"'
            cmds.append(cmd2)
            resp2 = await rcon.send_command(cmd2)
            if resp2 is not None:
                low2 = resp2.lower()
                if "already" in low2 or "exist" in low2:
                    print(f"[Whitelist] addSteamID: already whitelisted ({resp2.strip()!r})")
                elif "added" in low2 or "created" in low2:
                    print(f"[Whitelist] addSteamID: {resp2.strip()!r}")
                else:
                    return f"account added, but `addSteamID` failed: {resp2.strip()}", "; ".join(cmds)
            elif rcon.last_error:
                return f"account added, but `addSteamID` failed: {rcon.last_error}", "; ".join(cmds)

        return "", "; ".join(cmds)

    async def _remove_whitelist_user_rcon(self, username: str, steam_id: str) -> tuple[str, str]:
        """Remove a whitelist account + its SteamID over RCON.

        Returns `(error, rcon_command)` — `error` is "" on success.
        """
        rcon = self.bot.rcon
        u = username.replace('"', "").strip()
        s = steam_id.replace('"', "").strip()

        if not await asyncio.to_thread(rcon.is_server_online):
            detail = getattr(rcon, "last_error", "") or "connection failed"
            return f"server is offline / RCON unreachable — {detail}", ""

        cmds = []
        cmd1 = f'removeuser "{u}"'
        cmds.append(cmd1)
        resp = await rcon.send_command(cmd1)
        if resp is not None:
            print(f"[Whitelist] removeuser resp: {resp!r}")
        elif rcon.last_error:
            return f"`removeuser` failed: {rcon.last_error}", "; ".join(cmds)

        if s:
            cmd2 = f'removeSteamID "{s}"'
            cmds.append(cmd2)
            resp2 = await rcon.send_command(cmd2)
            if resp2 is not None:
                print(f"[Whitelist] removeSteamID resp: {resp2!r}")
            elif rcon.last_error:
                return f"account removed, but `removeSteamID` failed: {rcon.last_error}", "; ".join(cmds)

        return "", "; ".join(cmds)

    async def _read_server_whitelist(self) -> list:
        """Read the server's `whitelist` table (username/steamid/role) over SFTP.

        Returns a list of dicts `{username, steamid, role}`. `role` is the PZ
        access level (7 = admin, 2 = player).
        """
        sftp = sftp_client.get()
        db = getattr(self.bot.config, "SFTP_SERVER_DB", "") or "/server-data/db/pzserver.db"
        try:
            data = await sftp.read_bytes(db)
        except sftp_client.SftpError as e:
            print(f"[Whitelist] Cannot read whitelist DB {db}: {e}")
            return []
        rows: list = []
        try:
            conn = sqlite3.connect(":memory:")
            conn.deserialize(data)
            conn.row_factory = sqlite3.Row
            for r in conn.execute("SELECT username, steamid, role FROM whitelist"):
                rows.append({
                    "username": (r["username"] or "").strip(),
                    "steamid": (r["steamid"] or "").strip() if r["steamid"] is not None else "",
                    "role": r["role"] if r["role"] is not None else 2,
                })
            conn.close()
        except Exception as e:
            print(f"[Whitelist] Failed to parse whitelist table: {e}")
        return rows

    async def _server_account(self, username: str) -> tuple[dict | None, str]:
        """The server whitelist row for `username` (case-insensitive) with every
        column the table has (lastConnection, banned, ... vary by build).
        Returns (row or None, error)."""
        sftp = sftp_client.get()
        db = getattr(self.bot.config, "SFTP_SERVER_DB", "") or "/server-data/db/pzserver.db"
        try:
            data = await sftp.read_bytes(db)
        except sftp_client.SftpError as e:
            return None, f"can't read the server database: {e}"
        try:
            conn = sqlite3.connect(":memory:")
            conn.deserialize(data)
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM whitelist WHERE lower(username) = lower(?)",
                               ((username or "").strip(),)).fetchone()
            result = {k: row[k] for k in row.keys() if k.lower() not in ("password", "encryptedpwd")} \
                if row else None
            conn.close()
            return result, ""
        except Exception as e:
            return None, f"can't parse the server database: {e}"

    async def _lookup_user(self, username: str) -> dict | None:
        for row in await self._read_server_whitelist():
            if row["username"].lower() == (username or "").strip().lower():
                return row
        return None

    async def _modify_whitelist_user_rcon(self, username: str, steam_id: str,
                                          new_username: str = "", new_password: str = "",
                                          new_steam_id: str = "",
                                          old_password: str = "") -> tuple[str, str]:
        """Modify a whitelist account over RCON.

        `username` + `steam_id` identify the account; pass the field(s) to change
        as `new_username` / `new_password` / `new_steam_id` (empty = unchanged).
        A rename or password change is a remove-then-re-add (``adduser`` won't
        overwrite, and the stored password is a one-way hash). A rename to a
        different name adds the new account before removing the old one, so a
        failed `adduser` leaves the old account as it was. A password change on
        the same name must remove first; if `adduser` then fails and
        `old_password` is known, the old account is put back. Returns
        `(error, rcon_commands)`.
        """
        rcon = self.bot.rcon
        if not await asyncio.to_thread(rcon.is_server_online):
            detail = getattr(rcon, "last_error", "") or "connection failed"
            return f"server is offline / RCON unreachable — {detail}", ""

        old_u = username.replace('"', "").strip()
        old_s = (steam_id or "").replace('"', "").strip()
        new_u = (new_username or username).replace('"', "").strip()
        new_s = (new_steam_id or steam_id or "").replace('"', "").strip()
        new_p = (new_password or "").replace('"', "").strip()

        rename = bool(new_username) and new_u != old_u
        repass = bool(new_password)

        cmds: list = []
        if rename or repass:
            if rename and not new_p:
                return ("changing the username requires a new password — the stored "
                        "password is hashed and can't be reused", "")
            # Same name (password or letter-case change): remove first.
            remove_first = bool(old_u) and new_u.lower() == old_u.lower()
            if remove_first:
                cmds.append(f'removeuser "{old_u}"')
                await rcon.send_command(cmds[-1])
            cmds.append(f'adduser "{new_u}" "{new_p}"')
            resp = await rcon.send_command(cmds[-1])
            failure = ""
            if resp is not None:
                low = resp.lower()
                if not ("created" in low or "added" in low):
                    failure = resp.strip()
            elif rcon.last_error:
                failure = rcon.last_error
            if failure:
                if remove_first:
                    old_p = (old_password or "").replace('"', "").strip()
                    if old_p:
                        cmds.append(f'adduser "{old_u}" "<old password>"')
                        await rcon.send_command(f'adduser "{old_u}" "{old_p}"')
                        if old_s:
                            cmds.append(f'addSteamID "{old_s}"')
                            await rcon.send_command(cmds[-1])
                        failure += " (the old account was restored)"
                    else:
                        failure += f" (the old account `{old_u}` was removed; re-add it with /whitelistadd)"
                return f"`adduser` failed: {failure}", "; ".join(cmds)
            if old_u and not remove_first:
                cmds.append(f'removeuser "{old_u}"')
                await rcon.send_command(cmds[-1])

        # SteamID: after a rename/repass the re-created account is unbound, so
        # (re)bind it; for a pure SteamID change swap old -> new.
        if rename or repass:
            if new_s:
                cmds.append(f'addSteamID "{new_s}"')
                await rcon.send_command(cmds[-1])
        elif new_s and new_s != old_s:
            if old_s:
                cmds.append(f'removeSteamID "{old_s}"')
                await rcon.send_command(cmds[-1])
            cmds.append(f'addSteamID "{new_s}"')
            await rcon.send_command(cmds[-1])

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

        row = {
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
            "discord_id": str(user.id),
        }
        self._append_csv(row)

        channel = self._approval_channel()
        if channel is None:
            print("[Whitelist] Approval channel not configured/found; request saved to CSV only.")
        elif not self.bot.features.is_enabled("whitelist"):
            print("[Whitelist] Whitelist notifications disabled; request saved to CSV only.")
        else:
            view = WhitelistApprovalView(self, request_id)
            try:
                await channel.send(embed=self._request_embed(row, user), view=view)
            except discord.HTTPException as e:
                print(f"[Whitelist] Failed to send approval message: {e}")

        await interaction.response.send_message(
            "\u2705 Your whitelist request has been submitted for review.",
            ephemeral=True,
        )
        print(f"[Whitelist] Request from {user} (SteamID {steam_id}) saved.")

    # ---- approval actions ----------------------------------------------------

    async def approve_request(self, interaction: discord.Interaction, request_id: str) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "\u274c You don't have permission to approve requests.", ephemeral=True)
            return
        request = self._lookup_request(request_id)
        if request is None:
            await interaction.response.send_message(
                "\u274c Request not found (it may have been removed).", ephemeral=True)
            return
        if (request.get("isWhitelisted", "false") or "false").strip().lower() == "true":
            await interaction.response.send_message(
                "\u26a0\ufe0f This request was already approved.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        username = request.get("Username", "")
        err, code = await self._add_whitelist_user_rcon(
            username, request.get("Password", ""), request.get("SteamID", ""))
        if err:
            await interaction.followup.send(embed=discord.Embed(
                title="\u274c Approval Failed",
                description=f"Could not whitelist **{username}**:\n\n{err}",
                colour=discord.Colour.red(),
            ), ephemeral=True)
            return

        self._update_csv_row(request_id, {
            "Code": code,
            "isWhitelisted": "true",
            "Whitelisted By": str(interaction.user),
        })
        request = self._lookup_request(request_id)
        await self._finalize_approval(interaction.message, request, interaction.user, "approved")

        link_note = await self._link_requester(request.get("discord_id", ""), username)

        submitter = await self._fetch_submitter(request.get("discord_id", ""))
        if submitter is not None:
            await self._send_approval_dm(submitter, username, request.get("SteamID", ""))

        await interaction.followup.send(
            f"\u2705 Approved **{username}** and added to the whitelist.\n{link_note}", ephemeral=True)
        print(f"[Whitelist] Approved {username} (SteamID {request.get('SteamID', '')})")

    async def _link_requester(self, discord_id: str, username: str) -> str:
        """Link the requester to their approved username; returns a line for the admin."""
        rank_cog = self.bot.get_cog("RankSync")
        if rank_cog is None or not str(discord_id).isdigit():
            return "\u26a0\ufe0f Not linked: no requester Discord ID or rank sync not loaded."
        try:
            result, current = await rank_cog.link_account(int(discord_id), username)
        except Exception as e:
            print(f"[Whitelist] Auto-link failed for {username}: {e}")
            return "\u26a0\ufe0f Not linked (error). Use `/linkname` to link them."
        if result == "ok":
            return f"\U0001f517 Linked <@{discord_id}> to **{username}**."
        if result == "same":
            return f"\U0001f517 <@{discord_id}> was already linked to **{username}**."
        if result == "has_link":
            return (f"\u26a0\ufe0f Not linked: <@{discord_id}> is already linked to **{current}**. "
                    "Use `/linkname` to change it.")
        if result == "taken":
            return (f"\u26a0\ufe0f Not linked: **{username}** is already linked to another "
                    "Discord account (see `/listlinks`).")
        return "\u26a0\ufe0f Not linked: no username."

    async def deny_request(self, interaction: discord.Interaction, request_id: str) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "\u274c You don't have permission to deny requests.", ephemeral=True)
            return
        await interaction.response.send_modal(
            DenyReasonModal(self, request_id, interaction.message))

    async def complete_denial(self, interaction: discord.Interaction,
                              request_id: str, message, reason: str) -> None:
        request = self._lookup_request(request_id)
        if request is None:
            await interaction.response.send_message(
                "\u274c Request not found (it may have been removed).", ephemeral=True)
            return
        self._update_csv_row(request_id, {
            "isWhitelisted": "false",
            "Notes": reason,
        })
        request = self._lookup_request(request_id)
        await self._finalize_approval(message, request, interaction.user, "denied", reason)

        submitter = await self._fetch_submitter(request.get("discord_id", ""))
        if submitter is not None:
            await self._send_denial_dm(submitter, reason)

        await interaction.response.send_message(
            "\u274c Denied. Reason recorded in the CSV.", ephemeral=True)
        print(f"[Whitelist] Denied request {request_id}: {reason}")

    async def delete_request(self, interaction: discord.Interaction, request_id: str) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "\u274c You don't have permission to delete accounts.", ephemeral=True)
            return
        await interaction.response.send_modal(
            DeleteReasonModal(self, request_id, interaction.message))

    async def complete_deletion(self, interaction: discord.Interaction,
                                request_id: str, message, reason: str) -> None:
        request = self._lookup_request(request_id)
        if request is None:
            await interaction.response.send_message(
                "\u274c Request not found (it may have been removed).", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        username = request.get("Username", "")
        err, _code = await self._remove_whitelist_user_rcon(
            username, request.get("SteamID", ""))
        if err:
            await interaction.followup.send(embed=discord.Embed(
                title="\u274c Deletion Failed",
                description=f"Could not delete **{username}**:\n\n{err}",
                colour=discord.Colour.red(),
            ), ephemeral=True)
            return
        self._update_csv_row(request_id, {
            "isWhitelisted": "false",
            "Notes": reason,
        })
        request = self._lookup_request(request_id)
        await self._finalize_approval(message, request, interaction.user, "deleted", reason)
        await interaction.followup.send(
            f"\U0001f5d1\ufe0f Deleted **{username}**. Reason recorded in the CSV.",
            ephemeral=True)
        print(f"[Whitelist] Deleted account {username} (request {request_id}): {reason}")

    # ---- approved card: rename / password / status --------------------------

    def _approved_request(self, request_id: str) -> tuple[dict | None, str]:
        request = self._lookup_request(request_id)
        if request is None:
            return None, "\u274c Request not found (it may have been removed)."
        if (request.get("isWhitelisted", "") or "").strip().lower() != "true":
            return None, "\u274c This account isn't whitelisted any more."
        return request, ""

    def _is_in_game(self, username: str) -> bool:
        names = getattr(self.bot.state, "player_names", set()) or set()
        return username.lower() in {n.lower() for n in names}

    async def open_rename(self, interaction: discord.Interaction, request_id: str) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "\u274c You don't have permission to manage the whitelist.", ephemeral=True)
            return
        request, err = self._approved_request(request_id)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await interaction.response.send_modal(RenameModal(
            self, request_id, interaction.message,
            request.get("Username", ""), request.get("Password", "")))

    async def open_password(self, interaction: discord.Interaction, request_id: str) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "\u274c You don't have permission to manage the whitelist.", ephemeral=True)
            return
        _request, err = self._approved_request(request_id)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await interaction.response.send_modal(PasswordModal(self, request_id, interaction.message))

    async def complete_rename(self, interaction: discord.Interaction, request_id: str,
                              message, new_username: str, password: str) -> None:
        request, err = self._approved_request(request_id)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        old = request.get("Username", "")
        new_username = new_username.replace('"', "").strip()
        if not new_username or not password:
            await interaction.response.send_message("\u274c Username and password are required.", ephemeral=True)
            return
        if new_username == old:
            await interaction.response.send_message("\u2139\ufe0f That's already the username.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        if self._is_in_game(old):
            await interaction.followup.send(
                f"\u274c **{old}** is in game. Ask them to log out first.", ephemeral=True)
            return
        if new_username.lower() != old.lower():
            taken, db_err = await self._server_account(new_username)
            if taken is not None:
                await interaction.followup.send(
                    f"\u274c An account named **{taken.get('username')}** already exists on the server.",
                    ephemeral=True)
                return
            if db_err:
                print(f"[Whitelist] Rename {old} -> {new_username}: {db_err}; relying on adduser")
        err, _code = await self._modify_whitelist_user_rcon(
            old, request.get("SteamID", ""), new_username=new_username,
            new_password=password, old_password=request.get("Password", ""))
        if err:
            await interaction.followup.send(embed=discord.Embed(
                title="\u274c Rename Failed",
                description=f"Could not rename **{old}**:\n\n{err}",
                colour=discord.Colour.red(),
            ), ephemeral=True)
            return
        self._update_csv_row(request_id, {"Username": new_username, "Password": password})
        request = self._lookup_request(request_id)
        await self._refresh_card(message, request)
        link_note = ""
        rank_cog = self.bot.get_cog("RankSync")
        if rank_cog is not None and hasattr(rank_cog, "rename_link"):
            if await rank_cog.rename_link(old, new_username):
                link_note = "\n\U0001f517 Discord link moved to the new username."
        await self._dm_credentials(request, "username", new_username, password)
        await interaction.followup.send(
            f"\u270f\ufe0f Renamed **{old}** \u2192 **{new_username}**. "
            f"The old character stays with the old name.{link_note}", ephemeral=True)
        print(f"[Whitelist] Renamed {old} -> {new_username} by {interaction.user}")

    async def complete_password(self, interaction: discord.Interaction, request_id: str,
                                message, new_password: str) -> None:
        request, err = self._approved_request(request_id)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        if not new_password:
            await interaction.response.send_message("\u274c Password is required.", ephemeral=True)
            return
        username = request.get("Username", "")
        await interaction.response.defer(ephemeral=True)
        if self._is_in_game(username):
            await interaction.followup.send(
                f"\u274c **{username}** is in game. Ask them to log out first.", ephemeral=True)
            return
        err, _code = await self._modify_whitelist_user_rcon(
            username, request.get("SteamID", ""), new_password=new_password,
            old_password=request.get("Password", ""))
        if err:
            await interaction.followup.send(embed=discord.Embed(
                title="\u274c Password Change Failed",
                description=f"Could not change the password of **{username}**:\n\n{err}",
                colour=discord.Colour.red(),
            ), ephemeral=True)
            return
        self._update_csv_row(request_id, {"Password": new_password})
        request = self._lookup_request(request_id)
        await self._refresh_card(message, request)
        await self._dm_credentials(request, "password", username, new_password)
        await interaction.followup.send(f"\U0001f511 Changed the password of **{username}**.", ephemeral=True)
        print(f"[Whitelist] Password changed for {username} by {interaction.user}")

    async def show_status(self, interaction: discord.Interaction, request_id: str) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "\u274c You don't have permission to manage the whitelist.", ephemeral=True)
            return
        request = self._lookup_request(request_id)
        if request is None:
            await interaction.response.send_message(
                "\u274c Request not found (it may have been removed).", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        username = request.get("Username", "")
        account, db_err = await self._server_account(username)
        server_online = self.bot.state.recent_rcon_probe(60) \
            if hasattr(self.bot.state, "recent_rcon_probe") else None
        linked_name = holder = None
        rank_cog = self.bot.get_cog("RankSync")
        if rank_cog is not None:
            did = request.get("discord_id", "")
            linked_name = rank_cog.pz_username_for_discord_id(did) if did else None
            holder = rank_cog.discord_id_for_pz_username(username)
        embed = discord.Embed(title=f"\U0001f50e Account status — {username}",
                              colour=discord.Colour.blurple(),
                              timestamp=datetime.datetime.now(datetime.timezone.utc))
        for name, value in account_status_fields(request, account, server_online,
                                                 getattr(self.bot.state, "player_names", ()),
                                                 linked_name, holder):
            embed.add_field(name=name, value=value[:1024], inline=False)
        if db_err:
            embed.set_footer(text=f"Server whitelist unknown: {db_err}"[:2048])
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _refresh_card(self, message, request: dict) -> None:
        """Rewrite the Username / Password / SteamID fields on an approved card."""
        if message is None or not message.embeds:
            return
        embed = message.embeds[0].copy()
        values = {"Username": request.get("Username", ""), "Password": request.get("Password", ""),
                  "SteamID": request.get("SteamID", "")}
        for i, field in enumerate(embed.fields):
            if field.name in values:
                embed.set_field_at(i, name=field.name, value=f"`{values[field.name]}`",
                                   inline=field.inline)
        try:
            await message.edit(embed=embed, view=WhitelistManageView(self, request.get("request_id", "")))
        except discord.HTTPException as e:
            print(f"[Whitelist] Failed to update the card: {e}")

    async def _dm_credentials(self, request: dict, changed: str, username: str, password: str) -> None:
        submitter = await self._fetch_submitter(request.get("discord_id", ""))
        if submitter is None:
            return
        note = ("Your **username** was changed. Your old character stays with the old name, "
                "so you'll start a new one." if changed == "username"
                else "Your **password** was changed.")
        await self._dm_submitter(submitter, discord.Embed(
            title="\U0001f511 PZ Tambayan login updated",
            description=f"{note}\n\n- **Username:** `{username}`\n- **Password:** `{password}`",
            colour=discord.Colour.blurple(),
        ))

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

    # ---- manual whitelist management ----------------------------------------

    @app_commands.command(
        name="whitelistadd",
        description="Manually add a user to the whitelist (username + password + SteamID).",
    )
    @app_commands.describe(
        username="In-game username",
        password="Account password",
        steamid="17-digit SteamID64",
    )
    async def cmd_whitelist_add(self, interaction: discord.Interaction,
                                username: str, password: str, steamid: str) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "\u274c You don't have permission to manage the whitelist.", ephemeral=True)
            return
        error = _validate_steam_id(steamid)
        if error:
            await interaction.response.send_message(f"\u274c {error}", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        err, _code = await self._add_whitelist_user_rcon(username, password, steamid)
        if err:
            await interaction.followup.send(embed=discord.Embed(
                title="\u274c Add Failed",
                description=f"Could not whitelist **{username}**:\n\n{err}",
                colour=discord.Colour.red(),
            ), ephemeral=True)
            return
        await interaction.followup.send(
            f"\u2705 Added **{username}** (SteamID `{steamid}`) to the whitelist.", ephemeral=True)
        print(f"[Whitelist] Manually added {username} (SteamID {steamid}) by {interaction.user}")

    @app_commands.command(
        name="whitelistmodify",
        description="Modify a whitelisted user (password / username / SteamID).",
    )
    @app_commands.describe(
        username="Current username",
        steamid="Current SteamID",
        new_username="New username (leave empty to keep)",
        new_password="New password (leave empty to keep)",
        new_steamid="New SteamID (leave empty to keep)",
    )
    async def cmd_whitelist_modify(self, interaction: discord.Interaction,
                                   username: str, steamid: str,
                                   new_username: str = None,
                                   new_password: str = None,
                                   new_steamid: str = None) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "\u274c You don't have permission to manage the whitelist.", ephemeral=True)
            return
        if not (new_username or new_password or new_steamid):
            await interaction.response.send_message(
                "\u274c Provide at least one field to modify "
                "(`new_username`, `new_password`, or `new_steamid`).", ephemeral=True)
            return
        if new_steamid:
            error = _validate_steam_id(new_steamid)
            if error:
                await interaction.response.send_message(f"\u274c {error}", ephemeral=True)
                return
        await interaction.response.defer(ephemeral=True)
        err, _code = await self._modify_whitelist_user_rcon(
            username, steamid, new_username or "", new_password or "", new_steamid or "")
        if err:
            await interaction.followup.send(embed=discord.Embed(
                title="\u274c Modify Failed",
                description=f"Could not modify **{username}**:\n\n{err}",
                colour=discord.Colour.red(),
            ), ephemeral=True)
            return
        changed = []
        if new_username:
            changed.append(f"username \u2192 `{new_username}`")
        if new_password:
            changed.append("password")
        if new_steamid:
            changed.append(f"SteamID \u2192 `{new_steamid}`")
        await interaction.followup.send(
            f"\u2705 Modified **{username}** ({', '.join(changed)}).", ephemeral=True)
        print(f"[Whitelist] Modified {username} by {interaction.user}: {', '.join(changed)}")

    @app_commands.command(
        name="whitelistlist",
        description="List all whitelisted users.",
    )
    async def cmd_whitelist_list(self, interaction: discord.Interaction) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "\u274c You don't have permission to view the whitelist.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        users = await self._read_server_whitelist()
        if not users:
            await interaction.followup.send("No whitelisted users found.", ephemeral=True)
            return
        users = sorted(users, key=lambda u: u["username"].lower())
        lines = []
        for u in users:
            role = "Admin" if u.get("role", 2) >= 7 else "Player"
            sid = u.get("steamid", "") or "\u2014"
            lines.append(f"**{u['username']}** \u00b7 SteamID `{sid}` \u00b7 {role}")
        embeds = []
        for i in range(0, len(lines), 20):
            embeds.append(discord.Embed(
                title=f"\U0001f4dd Whitelisted Users ({len(users)})",
                description="\n".join(lines[i:i + 20]),
                colour=discord.Colour.blue(),
            ))
        await interaction.followup.send(embeds=embeds, ephemeral=True)

    @app_commands.command(
        name="whitelistremove",
        description="Remove a user from the whitelist by username.",
    )
    @app_commands.describe(
        username="Username to remove",
        reason="Reason for removal (optional)",
    )
    async def cmd_whitelist_remove(self, interaction: discord.Interaction,
                                   username: str, reason: str = None) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "\u274c You don't have permission to manage the whitelist.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        user = await self._lookup_user(username)
        if user is None:
            await interaction.followup.send(
                f"\u274c User **{username}** was not found in the whitelist.", ephemeral=True)
            return
        steamid = user.get("steamid", "")
        err, _code = await self._remove_whitelist_user_rcon(username, steamid)
        if err:
            await interaction.followup.send(embed=discord.Embed(
                title="\u274c Remove Failed",
                description=f"Could not remove **{username}**:\n\n{err}",
                colour=discord.Colour.red(),
            ), ephemeral=True)
            return
        msg = f"\U0001f5d1\ufe0f Removed **{username}** from the whitelist."
        if reason:
            msg += f"\nReason: {reason}"
        await interaction.followup.send(msg, ephemeral=True)
        print(f"[Whitelist] Removed {username} (SteamID {steamid or '-'}) "
              f"by {interaction.user}; reason: {reason or '-'}")


async def setup(bot: commands.Bot):
    await bot.add_cog(WhitelistCog(bot))
    print("[Whitelist] Extension loaded.")
