"""
siege_night.py — Discord bot cog for "Siege Night" (Workshop 3669589584).

Provides /siegestatus, /siegestart, /siegestop, and /siegeschedule slash
commands that write commands via the unified lua_bridge module, which the
`siege-night-bridge` companion mod reads.

Also runs a background task that polls siege_night_status.txt written by the
companion mod, and sends Discord notifications at three key siege phases:
  1) "warning" — Siege night approaching (announced the day of, during daylight)
  2) "active"  — Siege night has begun (zombies spawning)
  3) "ended"   — Siege night has concluded

Deduplication ensures each phase is only announced once per siege, even across
bot restarts.

All commands that call lua_bridge defer the interaction first, since
write_command may sleep up to 3s waiting for the mod to consume the previous
command file. Discord interactions expire after 3s, so we must acknowledge
immediately and use followup.send for the actual reply.
"""

import asyncio

import discord
from discord import app_commands
from discord.ext import commands, tasks

import lua_bridge
from game_calendar import siege_date_string


class SiegeNightCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Dedup sets (survive across bot restarts via seeding in before_loop).
        self._announced_warning_days: set[int] = set()   # eventDay
        self._announced_active_sieges: set[int] = set()  # siegeCount
        self._announced_ended_sieges: set[int] = set()   # totalSiegesCompleted
        self._last_poller_key = None

    async def cog_load(self):
        self.siege_status_poller.start()

    async def cog_unload(self):
        self.siege_status_poller.cancel()

    # ── helpers ──────────────────────────────────────────────────────────

    def _check_role(self, interaction: discord.Interaction) -> bool:
        role = discord.utils.get(
            interaction.guild.roles, name=self.bot.config.DEFAULT_ROLE
        )
        return role is not None and role in interaction.user.roles

    # ── background poller ───────────────────────────────────────────────

    @tasks.loop(seconds=15)
    async def siege_status_poller(self):
        try:
            status = await lua_bridge.read_siege_status()
            if not status:
                return

            phase = status.get("phase") or "idle"
            event_day = status.get("eventDay", 0)
            siege_count = status.get("siegeCount", 0)
            completed = status.get("totalSiegesCompleted", 0)

            poller_key = (phase, event_day, completed, siege_count)
            if poller_key != self._last_poller_key:
                print(f"[SiegeNight] Poller: phase={phase}, day={event_day}, "
                      f"siege={siege_count}, completed={completed}")
                self._last_poller_key = poller_key

            channel = self.bot.get_siege_channel()

            if phase == "warning":
                if event_day and event_day not in self._announced_warning_days:
                    self._announced_warning_days.add(event_day)
                    world = await lua_bridge.read_world_status()
                    date_str = siege_date_string(world, status) or ""
                    desc = f"A siege night is scheduled for **Day {event_day}**"
                    if date_str:
                        desc += f" ({date_str})"
                    desc += ("\n\nUse the daylight hours to prepare \u2014 fortify your "
                             "base, stock up on supplies, and gather your group.")
                    embed = discord.Embed(
                        title="\U0001f319 Siege Night Is Tonight!",
                        description=desc,
                        colour=discord.Colour.dark_red(),
                    )
                    if self.bot.features.is_enabled("siege"):
                        await self.bot.send_to_channel(channel, self.bot.config.SIEGE_ROLE_ID, embed)
                    print(f"[SiegeNight] Siege tonight announced: day {event_day}")

            elif phase == "active":
                if siege_count and siege_count not in self._announced_active_sieges:
                    self._announced_active_sieges.add(siege_count)
                    target = status.get("targetZombies", "?")
                    players = status.get("playerCount", "?")
                    # In-game red-alert (servermsg + alert sound) — fires once per siege.
                    await self.bot.rcon.broadcast(
                        "SIEGE NIGHT HAS BEGUN! Zombies are attacking. Hold the line!"
                    )
                    embed = discord.Embed(
                        title="\U0001f9df Siege Night Has Begun!",
                        description=(
                            f"**{target}** zombies are "
                            f"descending on **{players}** survivor(s).\n\n"
                            "Hold the line."
                        ),
                        colour=discord.Colour.red(),
                    )
                    if self.bot.features.is_enabled("siege"):
                        await self.bot.send_to_channel(channel, self.bot.config.SIEGE_ROLE_ID, embed)
                    print(f"[SiegeNight] Notification sent: active, siege={siege_count}")

            elif phase in ("idle", "dawn"):
                # "Ended" fires once when the completed count increments past
                # what we've already announced.
                if completed and completed not in self._announced_ended_sieges:
                    self._announced_ended_sieges.add(completed)
                    next_day = status.get("nextSiegeDay")
                    desc = "The siege has been repelled. The night is quiet once more."
                    if next_day:
                        world = await lua_bridge.read_world_status()
                        date_str = siege_date_string(world, status)
                        desc += f"\n\nNext siege night: **Day {next_day}**"
                        if date_str:
                            desc += f" ({date_str})"
                    embed = discord.Embed(
                        title="\u2705 Siege Night Has Ended",
                        description=desc,
                        colour=discord.Colour.green(),
                    )
                    if self.bot.features.is_enabled("siege"):
                        await self.bot.send_to_channel(channel, self.bot.config.SIEGE_ROLE_ID, embed)
                    print(f"[SiegeNight] Notification sent: ended, completed={completed}")

        except Exception as e:
            print(f"[SiegeNight] Siege status poller error: {e}")

    @siege_status_poller.before_loop
    async def before_siege_poller(self):
        await self.bot.wait_until_ready()
        # Seed dedup sets with the current status so stale notifications from
        # before the bot started are suppressed.
        try:
            status = await lua_bridge.read_siege_status()
            if status:
                phase = status.get("phase") or "idle"
                event_day = status.get("eventDay", 0)
                siege_count = status.get("siegeCount", 0)
                completed = status.get("totalSiegesCompleted", 0)
                if phase == "warning" and event_day:
                    self._announced_warning_days.add(event_day)
                if phase == "active" and siege_count:
                    self._announced_active_sieges.add(siege_count)
                if completed:
                    self._announced_ended_sieges.add(completed)
                print(f"[SiegeNight] Seeded dedup: phase={phase}, day={event_day}, "
                      f"siege={siege_count}, completed={completed}")
        except Exception:
            pass

    # ── /siegestatus ────────────────────────────────────────────────────

    @app_commands.command(name="siegestatus", description="Show the current Siege Night status.")
    async def cmd_siegestatus(self, interaction: discord.Interaction) -> None:
        if not self._check_role(interaction):
            await interaction.response.send_message(embed=discord.Embed(
                title="Permission Denied",
                description=f"You need the **{self.bot.config.DEFAULT_ROLE}** role to use this command.",
                colour=discord.Colour.red(),
            ), ephemeral=True)
            return

        await interaction.response.defer()

        status = await lua_bridge.read_siege_status()
        if not status:
            await interaction.followup.send(embed=discord.Embed(
                title="\U0001f4cb Siege Status",
                description="No siege status available. Is the Siege Night bridge mod installed?",
                colour=discord.Colour.orange(),
            ))
            return

        phase = status.get("phase") or "idle"
        event_day = status.get("eventDay", "?")
        next_day = status.get("nextSiegeDay", 0)
        completed = status.get("totalSiegesCompleted", 0)
        siege_count = status.get("siegeCount", 0)

        world = await lua_bridge.read_world_status()
        date_str = siege_date_string(world, status)

        if phase == "active":
            state = "\U0001f534 **ACTIVE** — Siege in progress"
            target = status.get("targetZombies", 0)
            kills = status.get("killsThisSiege", 0)
            spawned = status.get("spawnedThisSiege", 0)
            state += f"\n\U0001f9df Spawned: **{spawned}** | Kills: **{kills}** | Target: **{target}**"
        elif phase == "warning":
            state = "\U0001f7e1 **Warning Active** — Siege night approaching tonight"
        elif phase == "dawn":
            state = "\U0001f7e0 **Dawn** — Siege wrapping up"
        else:
            state = "\U0001f7e2 **Idle** — No active siege"

        if next_day:
            next_line = f"\U0001f319 Next Siege Night: **Day {next_day}**"
            if date_str:
                next_line += f" ({date_str})"
        else:
            next_line = "\U0001f319 Next Siege Night: **Not scheduled**"

        lines = [
            state,
            "",
            f"\U0001f4c5 Current Day: **{event_day}**",
            next_line,
            f"\U0001f4ca Sieges Completed: **{completed}**",
        ]

        await interaction.followup.send(embed=discord.Embed(
            title="\U0001f4cb Siege Night Status",
            description="\n".join(lines),
            colour=discord.Colour.red() if phase == "active" else discord.Colour.blue(),
        ))

    # ── /siegestart ─────────────────────────────────────────────────────

    @app_commands.command(name="siegestart", description="Force a siege night to start immediately.")
    async def cmd_siegestart(self, interaction: discord.Interaction) -> None:
        if not self._check_role(interaction):
            await interaction.response.send_message(embed=discord.Embed(
                title="Permission Denied",
                description=f"You need the **{self.bot.config.DEFAULT_ROLE}** role to use this command.",
                colour=discord.Colour.red(),
            ), ephemeral=True)
            return

        await interaction.response.defer()

        success = await lua_bridge.siege_start()
        if success:
            await interaction.followup.send(embed=discord.Embed(
                title="\U0001f6e1\ufe0f Siege Night Started!",
                description=(
                    f"Triggered by **{interaction.user.display_name}**\n\n"
                    "A siege night has been forced. Zombies will begin "
                    "spawning against the players **now**."
                ),
                colour=discord.Colour.red(),
            ))
        else:
            await interaction.followup.send(embed=discord.Embed(
                title="Failed to schedule siege",
                description="Could not write the command file. Check lua_bridge initialization.",
                colour=discord.Colour.red(),
            ), ephemeral=True)

    # ── /siegestop ──────────────────────────────────────────────────────

    @app_commands.command(name="siegestop", description="Stop the currently active siege night.")
    async def cmd_siegestop(self, interaction: discord.Interaction) -> None:
        if not self._check_role(interaction):
            await interaction.response.send_message(embed=discord.Embed(
                title="Permission Denied",
                description=f"You need the **{self.bot.config.DEFAULT_ROLE}** role to use this command.",
                colour=discord.Colour.red(),
            ), ephemeral=True)
            return

        await interaction.response.defer()

        success = await lua_bridge.siege_stop()
        if success:
            await interaction.followup.send(embed=discord.Embed(
                title="\U0001f6d1 Siege Stopped",
                description=f"Stop requested by **{interaction.user.display_name}**",
                colour=discord.Colour.green(),
            ))
        else:
            await interaction.followup.send(embed=discord.Embed(
                title="Failed to stop siege",
                description="Could not write the command file.",
                colour=discord.Colour.red(),
            ), ephemeral=True)

    # ── /siegeschedule ──────────────────────────────────────────────────

    @app_commands.command(name="siegeschedule", description="Change the next siege night to a specific world day.")
    @app_commands.describe(day="The world day number to schedule the next siege on")
    async def cmd_siegeschedule(self, interaction: discord.Interaction, day: int) -> None:
        if not self._check_role(interaction):
            await interaction.response.send_message(embed=discord.Embed(
                title="Permission Denied",
                description=f"You need the **{self.bot.config.DEFAULT_ROLE}** role to use this command.",
                colour=discord.Colour.red(),
            ), ephemeral=True)
            return

        if day < 1:
            await interaction.response.send_message(embed=discord.Embed(
                title="Invalid Day",
                description="Day must be 1 or greater.",
                colour=discord.Colour.red(),
            ), ephemeral=True)
            return

        await interaction.response.defer()

        success = await lua_bridge.siege_schedule(day)
        if success:
            await interaction.followup.send(embed=discord.Embed(
                title="\U0001f4c5 Siege Day Changed",
                description=(
                    f"Changed by **{interaction.user.display_name}**\n\n"
                    f"Next siege night rescheduled to **day {day}**."
                ),
                colour=discord.Colour.blue(),
            ))
        else:
            await interaction.followup.send(embed=discord.Embed(
                title="Failed to change siege day",
                description="Could not write the command file. Check lua_bridge initialization.",
                colour=discord.Colour.red(),
            ), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SiegeNightCog(bot))
    print("[SiegeNight] Bot cog loaded")
