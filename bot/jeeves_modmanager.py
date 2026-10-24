"""
jeeves_modmanager.py — /modlist only (remote).

Shows the server's Workshop items as "Mod Name - <Workshop page link>", one per
line, in the server config's order (user, 2026-10-25), plus a summary of the
Mods= and Map= lines and the same list as a text file. It reads the server
`.ini` over SFTP (via the shared `server_config` helper); the names come from
the Steam Workshop through `mod_checker.ModChecker` (public API, then the keyed
API, then the server's Workshop manifest + mod.info for unlisted items).

The text file can be pasted into the workspace Mod Builder
(`tools/modbuilder`) to make a Steam collection of the list.

Removed from the original Jeeves mod manager:
  - /modadd, /modremove  -> require SteamCMD (same-server)
  - /modreorder          -> requires recursive workshop-folder reads (mod_sorter)

Mod management on a managed host happens in the panel; this command is a
read-only view of what is configured.
"""

import io

import discord
from discord import app_commands
from discord.ext import commands

import server_config
from checks import admin_only
from mod_checker import ModChecker

WORKSHOP_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id={}"
_PAGE = 4000  # characters per embed page (Discord allows 4096)


def workshop_url(item_id: str) -> str:
    return WORKSHOP_URL.format(item_id)


def _name(titles: dict, item_id: str) -> str:
    title = " ".join(str(titles.get(item_id) or "").split())
    return title or f"Workshop item {item_id}"


def link_lines(ids: list, titles: dict) -> list:
    """ "Mod Name - <link>" per item, for Discord (<> stops link previews)."""
    return [f"{discord.utils.escape_markdown(_name(titles, i))} - <{workshop_url(i)}>" for i in ids]


def file_lines(ids: list, titles: dict) -> list:
    """The same list as plain text: "Mod Name - link"."""
    return [f"{_name(titles, i)} - {workshop_url(i)}" for i in ids]


def pages(lines: list, limit: int = _PAGE) -> list:
    """Join lines into pages of at most `limit` characters, never splitting a line."""
    out, cur = [], ""
    for line in lines:
        line = line[:limit]
        if cur and len(cur) + 1 + len(line) > limit:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


class JeevesModManagerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @staticmethod
    def _split_items(items) -> list:
        """Flatten a mod/workshop list, splitting on ';' and ',' (hosts vary)."""
        out = []
        for item in items:
            for part in str(item).replace(",", ";").split(";"):
                p = part.strip().lstrip("\\")
                if p:
                    out.append(p)
        return out

    async def _titles(self, ids: list) -> dict:
        """Workshop titles by id (missing ones are left out)."""
        try:
            state = await ModChecker(self.bot)._fetch_workshop_state(ids)
        except Exception as e:
            print(f"[ModManager] Workshop titles failed: {e}")
            return {}
        return {i: v.get("title", "") for i, v in state.items()}

    @app_commands.command(
        name="modlist",
        description="List the server's Workshop mods with their names and Workshop pages.",
    )
    @admin_only()
    async def cmd_modlist(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

        text = await server_config.read_ini(self.bot)
        if text is None:
            await interaction.followup.send(embed=discord.Embed(
                title="📋 Mod List",
                description="Could not read the server INI over SFTP.\n"
                            "Check `SFTP_SERVER_INI` in config.env.",
                colour=discord.Colour.red(),
            ))
            return

        mods = self._split_items(server_config.ini_value(text, "Mods"))
        workshop = list(dict.fromkeys(self._split_items(server_config.ini_value(text, "WorkshopItems"))))
        maps = [m for m in server_config.ini_value(text, "Map") if m.strip()]

        if not mods and not workshop and not maps:
            await interaction.followup.send(embed=discord.Embed(
                title="📋 Server Mod List", description="No mods configured.",
                colour=discord.Colour.purple()))
            return

        titles = await self._titles(workshop) if workshop else {}

        summary = [f"**{len(workshop)}** Workshop items · **{len(mods)}** mods enabled · **{len(maps)}** maps"]
        if maps:
            summary.append("**Maps:** " + ", ".join(f"`{m}`" for m in maps))
        unnamed = [i for i in workshop if not titles.get(i)]
        if unnamed:
            summary.append(f"-# {len(unnamed)} item(s) had no name on the Workshop (unlisted or removed).")
        summary.append("-# The attached file has the same list; paste it into the Mod Builder "
                       "to make a Steam collection.")

        lines = []
        if workshop:
            lines.append(f"WORKSHOP ITEMS ({len(workshop)})")
            lines.extend(file_lines(workshop, titles))
        if mods:
            lines.append(f"\nMods={';'.join(mods)}")
        if maps:
            lines.append(f"Map={';'.join(maps)}")
        lines.append(f"WorkshopItems={';'.join(workshop)}")
        file = discord.File(io.BytesIO("\n".join(lines).encode("utf-8")), filename="server-modlist.txt")

        await interaction.followup.send(embed=discord.Embed(
            title="📋 Server Mod List", description="\n".join(summary), colour=discord.Colour.purple(),
        ), file=file)

        # One embed per message keeps each under Discord's 6000-character message limit.
        body = pages(link_lines(workshop, titles))
        for n, page in enumerate(body, 1):
            embed = discord.Embed(description=page, colour=discord.Colour.purple())
            if len(body) > 1:
                embed.set_footer(text=f"Page {n} of {len(body)}")
            await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(JeevesModManagerCog(bot))
    print("[ModManager] /modlist loaded.")
