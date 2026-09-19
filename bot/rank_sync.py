"""
Rank Sync Extension (SFTP).

Monitors Discord role changes and syncs them to Project Zomboid by writing a
Lua data file over SFTP that the Jeeves Integration server mod reads.

The bot writes: {SFTP_LUA_DIR}/jeeves_ranks.lua
The server mod reads it via getFileReader("jeeves_ranks.lua").

After writing, uses the Lua bridge to signal the mod to reload and push ranks
to all connected clients.

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

RANK_DISPLAY = {
    0: "\u2b1c Default",
    1: "\U0001f7e9 Fuel (Green)",
    2: "\U0001f7e6 Spark (Blue)",
    3: "\U0001f7ea Cinder (Violet)",
    4: "\U0001f7e8 Flame (Yellow)",
    5: "\U0001f7e6 Blaze (Cyan)",
    6: "\U0001f7e5 Inferno (Red)",
}

LINK_FILE = __import__("pathlib").Path(__file__).parent / "rank_links.json"
RANKS_FILENAME = "jeeves_ranks.lua"


def get_rank_from_roles(member: discord.Member) -> int:
    """Determine the highest rank from a member's Discord roles."""
    highest = 0
    for role in member.roles:
        rank = ROLE_TO_RANK.get(role.name, 0)
        if rank > highest:
            highest = rank
    return highest


_LINKS_PAGE_SIZE = 20  # links per page in /listlinks


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
        self._startup_sync.start()

    def cog_unload(self):
        self._startup_sync.cancel()

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
        """Update a player's rank in memory and write to file."""
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

    def get_rank_for_pz_username(self, pz_username: str) -> Optional[int]:
        discord_id = None
        for did, pzname in self._links.items():
            if pzname.lower() == pz_username.lower():
                discord_id = did
                break
        if not discord_id:
            return None
        guild = self.bot.get_guild(self.bot.config.GUILD_ID)
        if not guild:
            return None
        member = guild.get_member(int(discord_id))
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
            rank = get_rank_from_roles(interaction.user)
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
        rank = get_rank_from_roles(interaction.user)
        await self._update_rank_and_push(username, rank)
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
        if old_name in self._ranks:
            del self._ranks[old_name]
            await self._write_ranks_file()
            await self._push_ranks_to_server()

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
        app_commands.Choice(name="0 - Default (no color)", value=0),
        app_commands.Choice(name="1 - Fuel (green)", value=1),
        app_commands.Choice(name="2 - Spark (blue)", value=2),
        app_commands.Choice(name="3 - Cinder (violet)", value=3),
        app_commands.Choice(name="4 - Flame (yellow)", value=4),
        app_commands.Choice(name="5 - Blaze (cyan)", value=5),
        app_commands.Choice(name="6 - Inferno (red)", value=6),
    ])
    async def cmd_setrank(self, interaction: discord.Interaction, username: str,
                          rank: app_commands.Choice[int]):
        await interaction.response.defer(ephemeral=True)
        success = await self._update_rank_and_push(username, rank.value)
        display = RANK_DISPLAY.get(rank.value, str(rank.value))
        if success:
            embed = discord.Embed(title="\U0001f3c5 Rank Updated",
                                  description=f"**{username}** \u2192 {display}",
                                  colour=discord.Colour.green())
        else:
            embed = discord.Embed(title="\u274c Rank Update Failed",
                                  description="Could not write rank file over SFTP.",
                                  colour=discord.Colour.red())
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="syncranks", description="Rebuild the rank file from all linked members and push.")
    async def cmd_syncranks(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
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
    async def cmd_linkname(self, interaction: discord.Interaction, member: discord.Member, username: str):
        await interaction.response.defer(ephemeral=True)
        self._links[str(member.id)] = username
        self._save_links()
        rank = get_rank_from_roles(member)
        await self._update_rank_and_push(username, rank)
        display = RANK_DISPLAY.get(rank, str(rank))
        await interaction.followup.send(embed=discord.Embed(
            title="\U0001f517 Player Linked",
            description=f"**Discord:** {member.mention}\n**PZ Username:** {username}\n**Current Rank:** {display}",
            colour=discord.Colour.blue(),
        ), ephemeral=True)

    @app_commands.command(name="unlinkname", description="Remove the Discord-to-PZ username link for a user.")
    @app_commands.describe(member="The Discord user to unlink")
    async def cmd_unlinkname(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)
        key = str(member.id)
        if key in self._links:
            old_name = self._links.pop(key)
            self._save_links()
            if old_name in self._ranks:
                del self._ranks[old_name]
                await self._write_ranks_file()
                await self._push_ranks_to_server()
            embed = discord.Embed(title="\U0001f517 Player Unlinked",
                                  description=f"Removed link: {member.mention} \u2194 **{old_name}**",
                                  colour=discord.Colour.orange())
        else:
            embed = discord.Embed(title="Not Linked",
                                  description=f"{member.mention} has no PZ username linked.",
                                  colour=discord.Colour.greyple())
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="listlinks", description="List all Discord-to-PZ username links.")
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
            rank = get_rank_from_roles(member) if member else 0
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
    from main import require_role, config

    cog = RankSync(bot)

    for cmd_name in ("cmd_setrank", "cmd_syncranks", "cmd_linkname", "cmd_unlinkname", "cmd_listlinks"):
        cmd = getattr(cog, cmd_name)
        setattr(cog, cmd_name, require_role(config.DEFAULT_ROLE)(cmd))

    await bot.add_cog(cog)
    print("[RankSync] Extension loaded.")
