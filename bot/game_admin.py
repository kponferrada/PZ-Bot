"""game_admin.py — in-game admin tasks from Discord.

Two paths, both admin-only (`admin_only`, DEFAULT_ROLE):

  BLib2 Admin's Discord bridge (blib2-admin `Discord.lua`, `lua_bridge.discord_command`):
    /giveitem /givexp /givetokens  through BLib2's reward payout; `offline` holds an offline
                                   player's share until their next login
    /heal /unstuck /whisper /finditem /pending /whereis /gameplayers
  Every bridge command carries the Discord name (`by`), is checked by BLib2 (arguments, rate,
  game state), answered in the status file and kept in BLib2's staff log as "Bot: <name>".

  RCON — the game's own admin commands, run exactly as an admin typing them in game:
    /godmode /invisible /noclip /tpto /kick /ban /unban /horde /weather /chopper /addvehicle
    /save /gamecmd (any other admin command; `quit` refused, use /stop)
  /unstuck asks BLib2 for a free square near the player, then moves them there with RCON
  `teleportto` (the game's own teleport).

Every button on the admin panel (`admin_panel.py`, "In game" groups) runs these commands.
"""
from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import lua_bridge
from checks import admin_only

PERKS = ("Aiming", "Axe", "Blunt", "Carpentry", "Cooking", "Doctor", "Electricity", "Farming", "Fishing",
         "Fitness", "Lightfoot", "LongBlade", "Maintenance", "Mechanics", "MetalWelding", "Nimble",
         "PlantScavenging", "Reloading", "SmallBlade", "SmallBlunt", "Sneak", "Spear", "Sprinting",
         "Strength", "Tailoring", "Trapping", "Woodwork")

_REFUSED_RCON = ("quit",)


def quote(value: str) -> str:
    """A value for an RCON command line, in double quotes (quotes and newlines removed)."""
    return '"' + str(value).replace('"', "").replace("\n", " ").replace("\r", " ").strip() + '"'


def rcon_line(command: str, *args: str) -> str:
    return " ".join([command, *args])


def gamecmd_refusal(line: str) -> str | None:
    """Why /gamecmd won't run `line` (None = it runs)."""
    word = line.strip().lstrip("/").split(" ", 1)[0].lower()
    if not word:
        return "Empty command."
    if word in _REFUSED_RCON:
        return "Use /stop to shut the server down (it saves first)."
    return None


def outcome_text(outcome: dict) -> tuple[str, bool | None]:
    """(text, ok) for a bridge answer; ok None = no answer in time."""
    if not outcome.get("sent"):
        return "Could not write the command file (SFTP).", False
    r = outcome.get("result")
    if not r:
        return ("No answer from the game in time: the server may be down or restarting, "
                "or BLib2 Admin's Discord bridge is off (setting DiscordBridge)."), None
    return str(r.get("message") or r.get("code") or ""), bool(r.get("ok"))


def players_from(status: dict | None) -> list[dict]:
    return [p for p in lua_bridge.keyed((status or {}).get("players"), "p") if isinstance(p, dict)]


def find_player(status: dict | None, name: str) -> dict | None:
    want = (name or "").strip().lower()
    for p in players_from(status):
        if str(p.get("name", "")).lower() == want:
            return p
    return None


def player_line(p: dict) -> str:
    flags = []
    if p.get("staff"):
        flags.append(str(p.get("access") or "staff"))
    if p.get("dead"):
        flags.append("dead")
    if p.get("vehicle"):
        flags.append("in a vehicle")
    extra = f" ({', '.join(flags)})" if flags else ""
    return (f"**{p.get('name')}** — {p.get('x')},{p.get('y')},{p.get('z')} · "
            f"{p.get('hours', 0)} h survived · {p.get('kills', 0)} kills{extra}")


class GameAdmin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---- helpers ----------------------------------------------------------------

    @staticmethod
    def _colour(ok: bool | None) -> discord.Colour:
        return discord.Colour.green() if ok else (discord.Colour.red() if ok is False else discord.Colour.orange())

    async def _reply(self, interaction: discord.Interaction, title: str, text: str, ok: bool | None) -> None:
        await interaction.followup.send(
            embed=discord.Embed(title=title, description=text[:4000], colour=self._colour(ok)),
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def _bridge(self, interaction: discord.Interaction, title: str, command: str, **kwargs) -> dict:
        outcome = await lua_bridge.discord_command(command, by=interaction.user.display_name,
                                                   by_id=str(interaction.user.id), **kwargs)
        text, ok = outcome_text(outcome)
        await self._reply(interaction, title, text, ok)
        return outcome

    async def _rcon(self, interaction: discord.Interaction, title: str, line: str) -> None:
        response = await self.bot.rcon.send_command(line)
        err = getattr(self.bot.rcon, "last_error", None)
        if err:
            await self._reply(interaction, title, f"`{line}`\nRCON failed: {err}", False)
            return
        await self._reply(interaction, title, f"`{line}`\n{(response or 'Done.').strip()}", True)
        print(f"[GameAdmin] {interaction.user.display_name} ran: {line}")

    async def _player_names(self, current: str) -> list[app_commands.Choice[str]]:
        try:
            status = await lua_bridge.read_discord_status()
        except Exception:
            status = None
        names = [str(p.get("name")) for p in players_from(status) if p.get("name")]
        cur = (current or "").lower()
        return [app_commands.Choice(name=n, value=n) for n in names if cur in n.lower()][:25]

    async def _player_ac(self, interaction: discord.Interaction, current: str):
        return await self._player_names(current)

    # ---- BLib2 bridge -------------------------------------------------------------

    @app_commands.command(name="giveitem", description="Give a player items (held until next login if offline).")
    @app_commands.describe(player="PZ username", item="Item id, e.g. Base.Axe (try /finditem)",
                           count="How many (1-100)", offline="Deliver at their next login if they're offline")
    @admin_only()
    async def cmd_giveitem(self, interaction: discord.Interaction, player: str, item: str,
                           count: app_commands.Range[int, 1, 100] = 1, offline: bool = False) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._bridge(interaction, f"Give {count} x {item} to {player}", "giveitem",
                           player=player, item=item, count=count, offline=offline or None)

    @app_commands.command(name="givexp", description="Give a player skill XP (held until next login if offline).")
    @app_commands.describe(player="PZ username", perk="Skill", amount="XP (1-100000)",
                           offline="Deliver at their next login if they're offline")
    @admin_only()
    async def cmd_givexp(self, interaction: discord.Interaction, player: str, perk: str,
                         amount: app_commands.Range[int, 1, 100000], offline: bool = False) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._bridge(interaction, f"Give {amount} {perk} XP to {player}", "givexp",
                           player=player, perk=perk, amount=amount, offline=offline or None)

    @cmd_givexp.autocomplete("perk")
    async def _perk_ac(self, interaction: discord.Interaction, current: str):
        cur = (current or "").lower()
        return [app_commands.Choice(name=p, value=p) for p in PERKS if cur in p.lower()][:25]

    @app_commands.command(name="givetokens", description="Give a player wallet tokens (needs a wallet mod).")
    @app_commands.describe(player="PZ username", amount="Tokens (1-10000)",
                           offline="Deliver at their next login if they're offline")
    @admin_only()
    async def cmd_givetokens(self, interaction: discord.Interaction, player: str,
                             amount: app_commands.Range[int, 1, 10000], offline: bool = False) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._bridge(interaction, f"Give {amount} token(s) to {player}", "givetokens",
                           player=player, amount=amount, offline=offline or None)

    @app_commands.command(name="heal", description="Fully heal an online player.")
    @app_commands.describe(player="PZ username")
    @admin_only()
    async def cmd_heal(self, interaction: discord.Interaction, player: str) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._bridge(interaction, f"Heal {player}", "heal", player=player)

    @app_commands.command(name="unstuck", description="Move a stuck player to the nearest free square.")
    @app_commands.describe(player="PZ username")
    @admin_only()
    async def cmd_unstuck(self, interaction: discord.Interaction, player: str) -> None:
        await interaction.response.defer(ephemeral=True)
        outcome = await lua_bridge.discord_command("unstuck", by=interaction.user.display_name,
                                                   by_id=str(interaction.user.id), player=player)
        text, ok = outcome_text(outcome)
        spot = lua_bridge.unstuck_spot(text) if ok else None
        if not spot:
            await self._reply(interaction, f"Unstuck {player}", text, ok)
            return
        x, y, z = spot
        await self._rcon(interaction, f"Unstuck {player} → {x},{y},{z}",
                         rcon_line("teleportto", quote(player), f"{x},{y},{z}"))

    @app_commands.command(name="whisper", description="Show a message to one online player only.")
    @app_commands.describe(player="PZ username", message="What they see in their chat")
    @admin_only()
    async def cmd_whisper(self, interaction: discord.Interaction, player: str, message: str) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._bridge(interaction, f"Whisper to {player}", "whisper", player=player, message=message)

    @app_commands.command(name="finditem", description="Search the server's item ids (for /giveitem).")
    @app_commands.describe(query="Part of an item id or name, e.g. axe")
    @admin_only()
    async def cmd_finditem(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer(ephemeral=True)
        outcome = await lua_bridge.discord_command("finditem", by=interaction.user.display_name,
                                                   by_id=str(interaction.user.id), query=query)
        text, ok = outcome_text(outcome)
        rows = lua_bridge.keyed(((outcome.get("status") or {}).get("lookup")), "i") if ok else []
        if rows:
            text = "\n".join(f"`{r.get('id')}` — {r.get('name')}" for r in rows if isinstance(r, dict))
        await self._reply(interaction, f"Items matching '{query}'", text or "No matches.", ok)

    @app_commands.command(name="pending", description="What still waits for a player (items, XP, tokens).")
    @app_commands.describe(player="PZ username")
    @admin_only()
    async def cmd_pending(self, interaction: discord.Interaction, player: str) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._bridge(interaction, f"Waiting for {player}", "pending", player=player)

    @app_commands.command(name="whereis", description="Where an online player is, from the game.")
    @app_commands.describe(player="PZ username")
    @admin_only()
    async def cmd_whereis(self, interaction: discord.Interaction, player: str) -> None:
        await interaction.response.defer(ephemeral=True)
        p = find_player(await lua_bridge.read_discord_status(), player)
        if p is None:
            await self._reply(interaction, f"Where is {player}?", "Not online (or BLib2's status file is missing).", False)
            return
        await self._reply(interaction, f"Where is {player}?", player_line(p), True)

    @app_commands.command(name="gameplayers", description="Online players with positions, from the game.")
    @admin_only()
    async def cmd_gameplayers(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        rows = players_from(await lua_bridge.read_discord_status())
        text = "\n".join(player_line(p) for p in rows) or "Nobody online (or BLib2's status file is missing)."
        await self._reply(interaction, f"In game now ({len(rows)})", text, True)

    # ---- RCON: the game's own admin commands -------------------------------------

    @app_commands.command(name="godmode", description="Turn god mode on or off for a player.")
    @app_commands.describe(player="PZ username", on="On or off")
    @admin_only()
    async def cmd_godmode(self, interaction: discord.Interaction, player: str, on: bool = True) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._rcon(interaction, f"God mode {'on' if on else 'off'}: {player}",
                         rcon_line("godmodeplayer", quote(player), "-true" if on else "-false"))

    @app_commands.command(name="invisible", description="Make a player invisible to zombies, or not.")
    @app_commands.describe(player="PZ username", on="On or off")
    @admin_only()
    async def cmd_invisible(self, interaction: discord.Interaction, player: str, on: bool = True) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._rcon(interaction, f"Invisible {'on' if on else 'off'}: {player}",
                         rcon_line("invisibleplayer", quote(player), "-true" if on else "-false"))

    @app_commands.command(name="noclip", description="Let a player walk through walls, or not.")
    @app_commands.describe(player="PZ username", on="On or off")
    @admin_only()
    async def cmd_noclip(self, interaction: discord.Interaction, player: str, on: bool = True) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._rcon(interaction, f"No-clip {'on' if on else 'off'}: {player}",
                         rcon_line("noclip", quote(player), "-true" if on else "-false"))

    @app_commands.command(name="tpto", description="Teleport a player to coordinates.")
    @app_commands.describe(player="PZ username", x="X", y="Y", z="Floor (0 = ground)")
    @admin_only()
    async def cmd_tpto(self, interaction: discord.Interaction, player: str, x: int, y: int,
                       z: app_commands.Range[int, -32, 32] = 0) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._rcon(interaction, f"Teleport {player} → {x},{y},{z}",
                         rcon_line("teleportto", quote(player), f"{x},{y},{z}"))

    @app_commands.command(name="kick", description="Kick a player from the server.")
    @app_commands.describe(player="PZ username", reason="Shown to the player")
    @admin_only()
    async def cmd_kick(self, interaction: discord.Interaction, player: str, reason: str = "") -> None:
        await interaction.response.defer(ephemeral=True)
        line = rcon_line("kickuser", quote(player), "-r", quote(reason)) if reason.strip() else rcon_line("kickuser", quote(player))
        await self._rcon(interaction, f"Kick {player}", line)

    @app_commands.command(name="ban", description="Ban a player (optionally their IP too).")
    @app_commands.describe(player="PZ username", reason="Shown to the player", ip="Also ban their IP")
    @admin_only()
    async def cmd_ban(self, interaction: discord.Interaction, player: str, reason: str = "", ip: bool = False) -> None:
        await interaction.response.defer(ephemeral=True)
        if ip:
            line = rcon_line("banuser", quote(player), "-ip")
        elif reason.strip():
            line = rcon_line("banuser", quote(player), "-r", quote(reason))
        else:
            line = rcon_line("banuser", quote(player))
        await self._rcon(interaction, f"Ban {player}", line)

    @app_commands.command(name="unban", description="Lift a player's ban.")
    @app_commands.describe(player="PZ username")
    @admin_only()
    async def cmd_unban(self, interaction: discord.Interaction, player: str) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._rcon(interaction, f"Unban {player}", rcon_line("unbanuser", quote(player)))

    @app_commands.command(name="horde", description="Spawn a zombie horde near a player.")
    @app_commands.describe(count="Zombies (1-500)", player="PZ username (blank: a random player)")
    @admin_only()
    async def cmd_horde(self, interaction: discord.Interaction, count: app_commands.Range[int, 1, 500],
                        player: str = "") -> None:
        await interaction.response.defer(ephemeral=True)
        line = rcon_line("createhorde", str(count), quote(player)) if player.strip() else rcon_line("createhorde", str(count))
        await self._rcon(interaction, f"Horde of {count}", line)

    @app_commands.command(name="weather", description="Start rain or a storm, or stop the weather.")
    @app_commands.describe(action="What to do", intensity="Rain / storm strength or hours (optional)")
    @app_commands.choices(action=[
        app_commands.Choice(name="Start rain", value="startrain"),
        app_commands.Choice(name="Stop rain", value="stoprain"),
        app_commands.Choice(name="Start storm", value="startstorm"),
        app_commands.Choice(name="Stop all weather", value="stopweather"),
    ])
    @admin_only()
    async def cmd_weather(self, interaction: discord.Interaction, action: app_commands.Choice[str],
                          intensity: Optional[app_commands.Range[int, 1, 100]] = None) -> None:
        await interaction.response.defer(ephemeral=True)
        line = action.value
        if intensity and action.value in ("startrain", "startstorm"):
            line = rcon_line(action.value, str(intensity))
        await self._rcon(interaction, action.name, line)

    @app_commands.command(name="chopper", description="Send the helicopter event over a player.")
    @app_commands.describe(player="PZ username (blank: random)")
    @admin_only()
    async def cmd_chopper(self, interaction: discord.Interaction, player: str = "") -> None:
        await interaction.response.defer(ephemeral=True)
        safe = player.replace('"', "").strip()
        await self._rcon(interaction, "Helicopter", rcon_line("chopper", safe) if safe else "chopper")

    @app_commands.command(name="addvehicle", description="Spawn a vehicle next to a player.")
    @app_commands.describe(vehicle="Vehicle script, e.g. Base.PickUpTruck", player="PZ username")
    @admin_only()
    async def cmd_addvehicle(self, interaction: discord.Interaction, vehicle: str, player: str) -> None:
        await interaction.response.defer(ephemeral=True)
        script = "".join(c for c in vehicle if c.isalnum() or c in "._-")
        await self._rcon(interaction, f"Vehicle for {player}", rcon_line("addvehicle", script, quote(player)))

    @app_commands.command(name="save", description="Save the world now.")
    @admin_only()
    async def cmd_save(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._rcon(interaction, "Save", "save")

    @app_commands.command(name="gamecmd", description="Run any in-game admin command (as typed in game, no /).")
    @app_commands.describe(command='e.g. additem "Bob" Base.Axe 1   ·   addxp "Bob" Carpentry=100')
    @admin_only()
    async def cmd_gamecmd(self, interaction: discord.Interaction, command: str) -> None:
        await interaction.response.defer(ephemeral=True)
        line = command.strip().lstrip("/")
        why = gamecmd_refusal(line)
        if why:
            await self._reply(interaction, "Not run", why, False)
            return
        await self._rcon(interaction, "Game command", line)

    # player-name autocomplete on every command that takes one
    for _cmd in (cmd_giveitem, cmd_givexp, cmd_givetokens, cmd_heal, cmd_unstuck, cmd_whisper, cmd_pending,
                 cmd_whereis, cmd_godmode, cmd_invisible, cmd_noclip, cmd_tpto, cmd_kick, cmd_addvehicle):
        _cmd.autocomplete("player")(_player_ac)
    del _cmd


async def setup(bot: commands.Bot):
    await bot.add_cog(GameAdmin(bot))
    print("[GameAdmin] Extension loaded.")
