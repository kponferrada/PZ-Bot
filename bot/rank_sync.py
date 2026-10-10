"""
Rank Sync Extension (SFTP).

Monitors Discord role changes and syncs them to Project Zomboid by writing a
Lua data file over SFTP that the Jeeves Integration server mod reads.

The bot writes: {SFTP_LUA_DIR}/jeeves_ranks.lua
The server mod reads it via getFileReader("jeeves_ranks.lua").

After writing, uses the Lua bridge to signal the mod to reload and push ranks
to all connected clients.

Rank source (RANK_SOURCE in config.env):
    bt_ladder (default) — ranks come from the Barangay Tales weekly reputation
        ladder (bt_progression.ladder_ranks), the same fire ranks BT shows on
        its leaderboard (Notebook.fireTier): the weekly title holders (last
        week's places 1-5 who still hold the title) -> Inferno, Blaze, Flame,
        Cinder, Spark; RP earned this week -> Fuel.
        Refreshed every 5 minutes. With RANK_LADDER_ROLES=true, linked members
        also get the matching Discord role.
    roles — ranks come from the Discord roles of linked members:

Discord Roles -> In-Game Ranks:
    Fuel    -> Rank 1 (green)
    Spark   -> Rank 2 (blue)
    Cinder  -> Rank 3 (violet)
    Flame   -> Rank 4 (yellow)
    Blaze   -> Rank 5 (cyan)
    Inferno -> Rank 6 (red)
"""

import json
import asyncio
import discord
from discord import app_commands
from discord.ext import commands, tasks
from typing import Optional, Dict

import bt_progression
from checks import admin_only
import ranks
import lua_bridge
import sftp_client


ROLE_TO_RANK = {
    "Fuel": 1,
    "Spark": 2,
    "Cinder": 3,
    "Flame": 4,
    "Blaze": 5,
    "Inferno": 6,
}

RANK_DISPLAY = {n: ranks.display(n) for n in ranks.RANKS}

LINK_FILE = __import__("pathlib").Path(__file__).parent / "rank_links.json"
RANKS_FILENAME = "jeeves_ranks.lua"


def link_check(links: Dict[str, str], discord_id, pz_username: str) -> str:
    """Can `discord_id` be linked to `pz_username`? (whitelist approval)

    "ok"       — neither side is linked yet.
    "same"     — already linked to exactly this username.
    "has_link" — the Discord user is linked to another username (one each).
    "taken"    — another Discord user holds this username (case-insensitive).
    """
    key = str(discord_id)
    current = links.get(key)
    if current is not None:
        return "same" if current.lower() == pz_username.lower() else "has_link"
    if any(name.lower() == pz_username.lower() for did, name in links.items() if did != key):
        return "taken"
    return "ok"


def get_rank_from_roles(member: discord.Member) -> int:
    """Determine the highest rank from a member's Discord roles."""
    highest = 0
    for role in member.roles:
        rank = ROLE_TO_RANK.get(role.name, 0)
        if rank > highest:
            highest = rank
    return highest


_LINKS_PAGE_SIZE = 20  # links per page in /listlinks
_LADDER_ROLE_REASON = "Barangay Tales reputation ladder"


class LinksPaginator(discord.ui.View):
    """Previous/Next pagination for the /listlinks embed (author-only)."""

    def __init__(self, pages: list, author_id: int):
        super().__init__(timeout=180)
        self.pages = pages
        self.author_id = author_id
        self.current = 0

        self.prev_button = discord.ui.Button(label="\u25c0", style=discord.ButtonStyle.secondary)
        self.prev_button.callback = self._on_prev
        self.add_item(self.prev_button)

        self.counter_button = discord.ui.Button(label="1 / 1", style=discord.ButtonStyle.secondary, disabled=True)
        self.add_item(self.counter_button)

        self.next_button = discord.ui.Button(label="\u25b6", style=discord.ButtonStyle.secondary)
        self.next_button.callback = self._on_next
        self.add_item(self.next_button)

        self._refresh()

    def _refresh(self) -> None:
        self.prev_button.disabled = self.current == 0
        self.next_button.disabled = self.current >= len(self.pages) - 1
        self.counter_button.label = f"{self.current + 1} / {len(self.pages)}"

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "\u274c This isn't your menu.", ephemeral=True)
            return False
        return True

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        if not await self._guard(interaction):
            return
        self.current = max(0, self.current - 1)
        self._refresh()
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        if not await self._guard(interaction):
            return
        self.current = min(len(self.pages) - 1, self.current + 1)
        self._refresh()
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)


class RankSync(commands.Cog):
    """Syncs Discord roles to in-game PZ ranks via a Lua data file over SFTP."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._links: Dict[str, str] = self._load_links()
        self._ranks: Dict[str, int] = {}   # pz_username -> rank
        self._ranks_file_path: Optional[str] = None
        self._ranks_file_written = False  # True once this run has written the file
        self._ladder_mode = getattr(bot.config, "RANK_SOURCE", "bt_ladder") != "roles"
        self._ladder_roles = self._ladder_mode and bool(getattr(bot.config, "RANK_LADDER_ROLES", False))
        self._ladder: Dict[str, int] = {}      # BT player (PZ username) -> rank
        self._ladder_loaded = False
        self._roles_forbidden = False
        if self._ladder_mode:
            self._ladder_sync.start()
            print("[RankSync] Rank source: Barangay Tales reputation ladder"
                  + (" (+ Discord roles)" if self._ladder_roles else ""))
        else:
            self._startup_sync.start()
            print("[RankSync] Rank source: Discord roles")

    def cog_unload(self):
        self._startup_sync.cancel()
        self._ladder_sync.cancel()

    # ---- Rank lookup (ladder or roles) ---------------------------------------

    def _ladder_rank(self, pz_username: str) -> int:
        if pz_username in self._ladder:
            return self._ladder[pz_username]
        low = pz_username.lower()
        for name, rank in self._ladder.items():
            if name.lower() == low:
                return rank
        return 0

    def _rank_for(self, member: Optional[discord.Member], pz_username: str) -> int:
        """A linked player's rank from the configured source."""
        if self._ladder_mode:
            return self._ladder_rank(pz_username)
        return get_rank_from_roles(member) if member else 0

    # ---- Lua ranks file path (remote) --------------------------------------

    def _get_ranks_file_path(self) -> Optional[str]:
        if self._ranks_file_path:
            return self._ranks_file_path
        lua_dir = getattr(self.bot.config, "SFTP_LUA_DIR", None) or lua_bridge.get_lua_dir()
        if not lua_dir:
            print("[RankSync] WARNING: SFTP_LUA_DIR not set, cannot locate Zomboid/Lua/")
            return None
        self._ranks_file_path = f"{lua_dir.rstrip('/')}/{RANKS_FILENAME}"
        print(f"[RankSync] Rank file path: {self._ranks_file_path}")
        return self._ranks_file_path

    # ---- Write ranks to Lua file over SFTP ---------------------------------

    async def _write_ranks_file(self) -> bool:
        path = self._get_ranks_file_path()
        if not path:
            print("[RankSync] ERROR: No valid path for rank file!")
            return False
        try:
            lines = [
                "-- Auto-generated by the PZ Tambayan bot. Do not edit manually.",
                "return {",
            ]
            for username, rank in sorted(self._ranks.items()):
                if rank > 0:
                    safe_name = username.replace('\\', '\\\\').replace('"', '\\"')
                    lines.append(f'  ["{safe_name}"] = {rank},')
            lines.append("}")
            content = "\n".join(lines)
            sftp = sftp_client.get()
            await sftp.write_text(path, content)
            self._ranks_file_written = True
            return True
        except Exception as e:
            print(f"[RankSync] Error writing rank file: {e}")
            import traceback
            traceback.print_exc()
            return False

    async def _push_ranks_to_server(self) -> None:
        try:
            await lua_bridge.rank_push()
        except Exception as e:
            print(f"[RankSync] Lua bridge push error: {e}")

    async def _update_rank(self, username: str, rank: int) -> bool:
        """Update a player's rank in memory and write to file (skipped when
        nothing changed — every join calls this)."""
        if self._ranks.get(username, 0) == rank and self._ranks_file_written:
            return True
        if rank > 0:
            self._ranks[username] = rank
        elif username in self._ranks:
            del self._ranks[username]
        return await self._write_ranks_file()

    async def _update_rank_and_push(self, username: str, rank: int) -> bool:
        success = await self._update_rank(username, rank)
        if success:
            await self._push_ranks_to_server()
        return success

    # ---- Build full rank table from Discord ---------------------------------

    def _build_all_ranks(self) -> Dict[str, int]:
        if self._ladder_mode:
            # Every ladder player gets a rank, linked to Discord or not.
            return {name: rank for name, rank in self._ladder.items() if rank > 0}
        guild = self.bot.get_guild(self.bot.config.GUILD_ID)
        if not guild:
            return {}
        ranks = {}
        for discord_id, pz_username in self._links.items():
            member = guild.get_member(int(discord_id))
            if not member:
                continue
            rank = get_rank_from_roles(member)
            if rank > 0:
                ranks[pz_username] = rank
        return ranks

    # ---- Startup sync -------------------------------------------------------

    @tasks.loop(count=1)
    async def _startup_sync(self):
        await asyncio.sleep(10)
        self._ranks = self._build_all_ranks()
        success = await self._write_ranks_file()
        count = len([r for r in self._ranks.values() if r > 0])
        if success:
            print(f"[RankSync] Startup: wrote {count} rank(s) to file.")
        else:
            print("[RankSync] Startup: failed to write rank file.")

    @_startup_sync.before_loop
    async def _before_startup_sync(self):
        await self.bot.wait_until_ready()

    # ---- Barangay Tales reputation ladder ------------------------------------

    async def _refresh_ladder(self) -> bool:
        ranks = await bt_progression.get_ladder_ranks(self.bot, force=True)
        if ranks is None:
            return False
        self._ladder = ranks
        return True

    async def _apply_ladder(self, force_write: bool = False) -> Optional[int]:
        """Rebuild ranks from the ladder; write + push if they changed.

        Returns the number of ranked players, or None if the ladder or the
        rank file couldn't be read/written.
        """
        if not await self._refresh_ladder():
            return None
        new = self._build_all_ranks()
        if force_write or new != self._ranks or not self._ladder_loaded:
            self._ranks = new
            if not await self._write_ranks_file():
                return None
            await self._push_ranks_to_server()
            print(f"[RankSync] Ladder: wrote {len(new)} rank(s).")
        self._ladder_loaded = True
        if self._ladder_roles:
            await self._sync_all_roles()
        return len(new)

    @tasks.loop(minutes=5)
    async def _ladder_sync(self):
        try:
            if await self._apply_ladder() is None:
                print("[RankSync] Ladder: progression export unavailable, ranks unchanged.")
        except Exception as e:
            print(f"[RankSync] Ladder sync error: {e}")

    @_ladder_sync.before_loop
    async def _before_ladder_sync(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(10)

    # ---- Discord rank roles (ladder mode, RANK_LADDER_ROLES) -----------------

    def _rank_roles(self, guild: discord.Guild) -> Dict[int, discord.Role]:
        names = getattr(self.bot.config, "RANKS", None) or {r: n for n, r in ROLE_TO_RANK.items()}
        roles = {}
        for rank, name in names.items():
            role = discord.utils.get(guild.roles, name=name)
            if role:
                roles[rank] = role
        return roles

    async def _sync_member_roles(self, member: discord.Member, rank: int,
                                 rank_roles: Optional[Dict[int, discord.Role]] = None) -> None:
        """Give `member` the role for `rank` and remove the other rank roles."""
        if not self._ladder_roles or self._roles_forbidden:
            return
        rank_roles = rank_roles if rank_roles is not None else self._rank_roles(member.guild)
        want = rank_roles.get(rank)
        remove = [r for r in member.roles if r in rank_roles.values() and r != want]
        add = want is not None and want not in member.roles
        if not remove and not add:
            return
        try:
            if remove:
                await member.remove_roles(*remove, reason=_LADDER_ROLE_REASON)
            if add:
                await member.add_roles(want, reason=_LADDER_ROLE_REASON)
        except discord.Forbidden:
            # Missing Manage Roles or the rank roles sit above the bot's role.
            self._roles_forbidden = True
            print("[RankSync] Ladder: no permission to manage rank roles; role sync off until restart.")
            return
        except discord.HTTPException as e:
            print(f"[RankSync] Ladder: role update failed for {member}: {e}")
            return
        print(f"[RankSync] Ladder role: {member} -> {want.name if want else 'none'}")

    async def _sync_all_roles(self) -> None:
        guild = self.bot.get_guild(self.bot.config.GUILD_ID)
        if not guild:
            return
        rank_roles = self._rank_roles(guild)
        for discord_id, pz_username in list(self._links.items()):
            member = guild.get_member(int(discord_id))
            if member:
                await self._sync_member_roles(member, self._ladder_rank(pz_username), rank_roles)

    # ---- Persistence for Discord <-> PZ username links ----------------------

    def _load_links(self) -> Dict[str, str]:
        if LINK_FILE.exists():
            try:
                with open(LINK_FILE, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return {}

    def _save_links(self) -> None:
        try:
            with open(LINK_FILE, 'w') as f:
                json.dump(self._links, f, indent=2)
        except IOError as e:
            print(f"[RankSync] Could not save links: {e}")

    def _get_pz_username(self, member: discord.Member) -> Optional[str]:
        return self._links.get(str(member.id))

    # ---- Public helpers -----------------------------------------------------

    @property
    def ranks_from_ladder(self) -> bool:
        """True when ranks follow the Barangay Tales ladder (RANK_SOURCE=bt_ladder)."""
        return self._ladder_mode

    def rank_for_discord_id(self, discord_id: int) -> Optional[int]:
        """Ladder rank of a linked Discord user, or None if they aren't linked."""
        pz_username = self._links.get(str(discord_id))
        if pz_username is None:
            return None
        return self._rank_for(None, pz_username)

    def pz_username_for_discord_id(self, discord_id) -> Optional[str]:
        """The PZ username a Discord user is linked to, if any."""
        return self._links.get(str(discord_id))

    def discord_id_for_pz_username(self, pz_username: str) -> Optional[int]:
        """The Discord user ID linked to a PZ username, if any."""
        for did, pzname in self._links.items():
            if pzname.lower() == pz_username.lower():
                return int(did)
        return None

    async def link_account(self, discord_id: int, pz_username: str) -> tuple[str, Optional[str]]:
        """Link a Discord user to a PZ username unless either side is already
        linked (see `link_check`). Returns (result, the user's current link).
        Used by whitelist approval; /linkme and /linkname keep their own rules."""
        pz_username = (pz_username or "").strip()
        if not pz_username:
            return "invalid", None
        key = str(discord_id)
        result = link_check(self._links, key, pz_username)
        if result != "ok":
            return result, self._links.get(key)
        self._links[key] = pz_username
        self._save_links()
        guild = self.bot.get_guild(self.bot.config.GUILD_ID)
        member = guild.get_member(int(discord_id)) if guild else None
        rank = self._rank_for(member, pz_username)
        try:
            if not self._ladder_mode:  # ladder ranks don't depend on links
                await self._update_rank_and_push(pz_username, rank)
            if member is not None:
                await self._sync_member_roles(member, rank)
        except Exception as e:  # the link is saved; ranks catch up on the next sync
            print(f"[RankSync] Linked {discord_id} -> {pz_username}, rank sync failed: {e}")
        print(f"[RankSync] Linked {discord_id} -> {pz_username} (whitelist approval)")
        return "ok", pz_username

    async def rename_link(self, old_username: str, new_username: str) -> bool:
        """Point whoever is linked to `old_username` at `new_username` (a
        whitelist rename). Returns True if a link moved."""
        moved = False
        for did, name in list(self._links.items()):
            if name.lower() == (old_username or "").lower():
                self._links[did] = new_username
                moved = True
        if not moved:
            return False
        self._save_links()
        if not self._ladder_mode and old_username in self._ranks:
            self._ranks[new_username] = self._ranks.pop(old_username)
            await self._write_ranks_file()
            await self._push_ranks_to_server()
        print(f"[RankSync] Link moved {old_username} -> {new_username} (whitelist rename)")
        return True

    def get_rank_for_pz_username(self, pz_username: str) -> Optional[int]:
        if self._ladder_mode:
            return self._ladder_rank(pz_username) if self._ladder_loaded else None
        discord_id = self.discord_id_for_pz_username(pz_username)
        if not discord_id:
            return None
        guild = self.bot.get_guild(self.bot.config.GUILD_ID)
        if not guild:
            return None
        member = guild.get_member(discord_id)
        if not member:
            return None
        return get_rank_from_roles(member)

    async def sync_by_pz_username(self, pz_username: str) -> bool:
        """Sync a specific player's rank to the file (no push). Called on join."""
        rank = self.get_rank_for_pz_username(pz_username)
        if rank is None:
            return False
        return await self._update_rank(pz_username, rank)

    # ---- Auto-sync on role change ------------------------------------------

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if self._ladder_mode:
            return  # ranks follow the ladder, not roles
        if before.roles == after.roles:
            return
        old_rank = get_rank_from_roles(before)
        new_rank = get_rank_from_roles(after)
        if old_rank == new_rank:
            return
        pz_username = self._get_pz_username(after)
        if not pz_username:
            return
        print(f"[RankSync] {after.display_name} ({pz_username}): {old_rank} -> {new_rank}")
        await self._update_rank_and_push(pz_username, new_rank)

    async def _on_unlinked(self, user, old_name: str) -> None:
        """Undo what a link gave: role-based rank, or the ladder rank role."""
        if self._ladder_mode:
            # The in-game rank follows the ladder, link or not; only the
            # Discord role came from the link.
            if isinstance(user, discord.Member):
                await self._sync_member_roles(user, 0)
            return
        if old_name in self._ranks:
            del self._ranks[old_name]
            await self._write_ranks_file()
            await self._push_ranks_to_server()

    # ====================================================================
    # PUBLIC COMMANDS (no role requirement, 60s cooldown)
    # ====================================================================

    @app_commands.command(name="linkme", description="Link your Discord account to your PZ username.")
    @app_commands.describe(username="Your Project Zomboid username (case-sensitive)")
    @app_commands.checks.cooldown(1, 60.0)
    async def cmd_linkme(self, interaction: discord.Interaction, username: str):
        await interaction.response.defer(ephemeral=True)
        key = str(interaction.user.id)

        if key in self._links:
            current_name = self._links[key]
            rank = self._rank_for(interaction.user, current_name)
            display = RANK_DISPLAY.get(rank, str(rank))
            embed = discord.Embed(
                title="\u26a0\ufe0f Already Linked",
                description=(
                    f"Your account is already linked to **{current_name}**.\n"
                    f"**Current Rank:** {display}\n\n"
                    "Each Discord account is limited to one linked character.\n"
                    "Run `/unlinkme` first to change it."
                ),
                colour=discord.Colour.orange(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        self._links[key] = username
        self._save_links()
        rank = self._rank_for(interaction.user, username)
        if not self._ladder_mode:  # ladder ranks don't depend on links
            await self._update_rank_and_push(username, rank)
        if isinstance(interaction.user, discord.Member):
            await self._sync_member_roles(interaction.user, rank)
        display = RANK_DISPLAY.get(rank, str(rank))
        await interaction.followup.send(embed=discord.Embed(
            title="\U0001f517 Account Linked",
            description=f"**PZ Username:** {username}\n**Current Rank:** {display}",
            colour=discord.Colour.blue(),
        ), ephemeral=True)

    @app_commands.command(name="unlinkme", description="Remove your own Discord-to-PZ username link.")
    @app_commands.checks.cooldown(1, 60.0)
    async def cmd_unlinkme(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        key = str(interaction.user.id)

        if key not in self._links:
            await interaction.followup.send(embed=discord.Embed(
                title="Not Linked",
                description="You don't have a PZ username linked. Use `/linkme` first.",
                colour=discord.Colour.greyple(),
            ), ephemeral=True)
            return

        old_name = self._links.pop(key)
        self._save_links()
        await self._on_unlinked(interaction.user, old_name)

        await interaction.followup.send(embed=discord.Embed(
            title="\U0001f517 Account Unlinked",
            description=f"Removed link to **{old_name}**.",
            colour=discord.Colour.orange(),
        ), ephemeral=True)

    # ====================================================================
    # ADMIN COMMANDS (require DEFAULT_ROLE)
    # ====================================================================

    @app_commands.command(name="setrank", description="Set a player's in-game rank (chat name color).")
    @app_commands.describe(username="The player's PZ username (case-sensitive)", rank="Rank 0-6")
    @app_commands.choices(rank=[
        app_commands.Choice(name=ranks.choice_label(n), value=n) for n in ranks.RANKS
    ])
    @admin_only()
    async def cmd_setrank(self, interaction: discord.Interaction, username: str,
                          rank: app_commands.Choice[int]):
        await interaction.response.defer(ephemeral=True)
        success = await self._update_rank_and_push(username, rank.value)
        display = RANK_DISPLAY.get(rank.value, str(rank.value))
        if success:
            note = ("\n*Ranks follow the Barangay Tales reputation ladder; the next "
                    "ladder sync (every 5 min) replaces this.*" if self._ladder_mode else "")
            embed = discord.Embed(title="\U0001f3c5 Rank Updated",
                                  description=f"**{username}** \u2192 {display}{note}",
                                  colour=discord.Colour.green())
        else:
            embed = discord.Embed(title="\u274c Rank Update Failed",
                                  description="Could not write rank file over SFTP.",
                                  colour=discord.Colour.red())
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="syncranks", description="Rebuild the rank file from all linked members and push.")
    @admin_only()
    async def cmd_syncranks(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if self._ladder_mode:
            count = await self._apply_ladder(force_write=True)
            if count is None:
                embed = discord.Embed(title="\u274c Sync Failed",
                                      description="Could not read the Barangay Tales progression "
                                                  "export or write the rank file over SFTP.",
                                      colour=discord.Colour.red())
            else:
                roles = " Discord rank roles updated." if self._ladder_roles else ""
                embed = discord.Embed(title="\U0001f504 Rank Sync Complete",
                                      description=f"**{count}** rank(s) from the Barangay Tales "
                                                  f"reputation ladder written and pushed.{roles}",
                                      colour=discord.Colour.green())
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        self._ranks = self._build_all_ranks()
        success = await self._write_ranks_file()
        count = len([r for r in self._ranks.values() if r > 0])
        if success:
            await self._push_ranks_to_server()
            embed = discord.Embed(title="\U0001f504 Rank Sync Complete",
                                  description=f"**{count}** rank(s) written and pushed to server.",
                                  colour=discord.Colour.green())
        else:
            embed = discord.Embed(title="\u274c Sync Failed",
                                  description="Could not write rank file over SFTP.",
                                  colour=discord.Colour.red())
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="linkname", description="Link a Discord user to their PZ username.")
    @app_commands.describe(member="The Discord user", username="Their PZ username (case-sensitive)")
    @admin_only()
    async def cmd_linkname(self, interaction: discord.Interaction, member: discord.Member, username: str):
        await interaction.response.defer(ephemeral=True)
        self._links[str(member.id)] = username
        self._save_links()
        rank = self._rank_for(member, username)
        if not self._ladder_mode:  # ladder ranks don't depend on links
            await self._update_rank_and_push(username, rank)
        await self._sync_member_roles(member, rank)
        display = RANK_DISPLAY.get(rank, str(rank))
        await interaction.followup.send(embed=discord.Embed(
            title="\U0001f517 Player Linked",
            description=f"**Discord:** {member.mention}\n**PZ Username:** {username}\n**Current Rank:** {display}",
            colour=discord.Colour.blue(),
        ), ephemeral=True)

    @app_commands.command(name="unlinkname", description="Remove the Discord-to-PZ username link for a user.")
    @app_commands.describe(member="The Discord user to unlink")
    @admin_only()
    async def cmd_unlinkname(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)
        key = str(member.id)
        if key in self._links:
            old_name = self._links.pop(key)
            self._save_links()
            await self._on_unlinked(member, old_name)
            embed = discord.Embed(title="\U0001f517 Player Unlinked",
                                  description=f"Removed link: {member.mention} \u2194 **{old_name}**",
                                  colour=discord.Colour.orange())
        else:
            embed = discord.Embed(title="Not Linked",
                                  description=f"{member.mention} has no PZ username linked.",
                                  colour=discord.Colour.greyple())
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="listlinks", description="List all Discord-to-PZ username links.")
    @admin_only()
    async def cmd_listlinks(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not self._links:
            await interaction.followup.send(embed=discord.Embed(
                title="No Links",
                description="No Discord-to-PZ username links exist yet.",
                colour=discord.Colour.greyple(),
            ), ephemeral=True)
            return

        guild = self.bot.get_guild(self.bot.config.GUILD_ID)
        rows = []
        for discord_id, pz_username in self._links.items():
            member = guild.get_member(int(discord_id)) if guild else None
            rank = self._rank_for(member, pz_username)
            rows.append((pz_username.lower(), discord_id, pz_username, member, rank))
        rows.sort(key=lambda r: r[0])

        lines = []
        for _, discord_id, pz_username, member, rank in rows:
            discord_name = member.mention if member else f"`{discord_id}` (left server)"
            display = RANK_DISPLAY.get(rank, str(rank))
            lines.append(f"{discord_name} \u2192 **{pz_username}** \u2014 {display}")

        # Paginate (in case the link list grows beyond a single embed).
        pages = []
        for i in range(0, len(lines), _LINKS_PAGE_SIZE):
            chunk = lines[i:i + _LINKS_PAGE_SIZE]
            pages.append(discord.Embed(
                title=f"\U0001f517 Discord \u2194 PZ Links ({len(rows)})",
                description="\n".join(chunk),
                colour=discord.Colour.blue(),
            ))

        if len(pages) == 1:
            await interaction.followup.send(embed=pages[0], ephemeral=True)
        else:
            view = LinksPaginator(pages, interaction.user.id)
            await interaction.followup.send(embed=pages[0], view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(RankSync(bot))
    print("[RankSync] Extension loaded.")
