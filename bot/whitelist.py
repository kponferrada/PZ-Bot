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
  5. Delete → (after approval) removes the account over RCON (`removeuser` +
     `removeSteamID`), records the reason in `Notes`, and updates the form.

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


class WhitelistDeleteView(discord.ui.View):
    """Persistent "Delete Account" button shown after a request is approved."""

    def __init__(self, cog: "WhitelistCog", request_id: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.request_id = request_id

        delete = discord.ui.Button(
            label="Delete Account",
            style=discord.ButtonStyle.red,
            emoji="\U0001f5d1\ufe0f",  # 🗑️
            custom_id=f"whitelist:delete:{request_id}",
        )
        delete.callback = self.delete
        self.add_item(delete)

    async def delete(self, interaction: discord.Interaction) -> None:
        await self.cog.delete_request(interaction, self.request_id)


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
        """Re-register persistent views (Approve/Deny for pending, Delete for approved)."""
        pending = 0
        approved = 0
        for row in self._read_csv():
            rid = row.get("request_id", "")
            if not rid:
                continue
            whitelisted = (row.get("isWhitelisted", "false") or "false").strip().lower() == "true"
            notes = (row.get("Notes", "") or "").strip()
            if whitelisted:
                self.bot.add_view(WhitelistDeleteView(self, rid))
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
            # Keep a persistent "Delete Account" button so the account can be
            # removed later.
            view = WhitelistDeleteView(self, request.get("request_id", ""))
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

        if not rcon.is_server_online():
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
            else:
                # PZ `adduser` has no output on success, so any other non-empty
                # response is a failure — surface the exact message.
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

        if not rcon.is_server_online():
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

        submitter = await self._fetch_submitter(request.get("discord_id", ""))
        if submitter is not None:
            await self._send_approval_dm(submitter, username, request.get("SteamID", ""))

        await interaction.followup.send(
            f"\u2705 Approved **{username}** and added to the whitelist.", ephemeral=True)
        print(f"[Whitelist] Approved {username} (SteamID {request.get('SteamID', '')})")

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
