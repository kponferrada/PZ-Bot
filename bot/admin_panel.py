"""admin_panel.py — button control panel for admins, grouped by function.

`/adminpanel` posts one card per group (Restarts, Server, Players & Ranks,
Whitelist, Events, Bot) into ADMIN_PANEL_CHANNEL_ID (or the channel it is run
in). Each button runs an existing slash command:

  - no parameters → it runs at once (a few ask "Are you sure?" first);
  - parameters → a form opens, built from the command's own parameters
    (text boxes, a dropdown for choices and yes/no, a member picker for
    members), and submitting it runs the command.

The panel calls the command's callback directly, so the slash command stays the
single implementation; a new parameter shows up in the form by itself. Calling
the callback skips the command's own checks, so every button checks the admin
role (DEFAULT_ROLE) first. Panel buttons are persistent (custom_id
`adminpanel:<key>`) and keep working after a bot restart.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import discord
from discord import app_commands
from discord.ext import commands

import cards
from checks import admin_only

_PREFIX = "adminpanel:"
_PARAM_LABEL_MAX = 45
_PARAM_DESC_MAX = 100
_MODAL_MAX_INPUTS = 5
_NO_DESCRIPTION = ("…", "...", "")


@dataclass(frozen=True)
class Action:
    key: str                      # custom_id suffix, unique
    label: str
    command: str                  # slash command name
    emoji: str = ""
    style: discord.ButtonStyle = discord.ButtonStyle.secondary
    fixed: dict = field(default_factory=dict)   # parameters the button sets (not asked)
    confirm: str = ""             # ask "Are you sure?" with this text first


@dataclass(frozen=True)
class Group:
    title: str
    description: str
    colour: int
    actions: tuple


_RED = discord.ButtonStyle.danger
_BLUE = discord.ButtonStyle.primary
_GREEN = discord.ButtonStyle.success

GROUPS = (
    Group("\U0001f504 Restarts", "Restart, defer or cancel restarts and run maintenance. "
          "A mod-update restart can only be deferred; a scheduled one can be cancelled.", 0xE67E22, (
        Action("restartstatus", "Status", "restartstatus", "\U0001f4cb"),
        Action("restart", "Restart", "restart", "\U0001f504", _BLUE,
               confirm="Restart the server with the full countdown (kick at T-1 min)?"),
        Action("restartnow", "Restart now", "restartnow", "⚡", _RED,
               confirm="Restart now? Players are kicked in 30 seconds."),
        Action("deferrestart", "Defer", "deferrestart", "⏸️"),
        Action("cancelrestart", "Cancel / skip next", "cancelrestart", "❌", _RED,
               fixed={"restore": False}),
        Action("restoreschedule", "Restore schedule", "cancelrestart", "↩️",
               fixed={"restore": True}),
        Action("forcemodupdate", "Force mod update", "forcemodupdate", "\U0001f527",
               confirm="Run a mod-update restart now?"),
        Action("maintenance", "Maintenance", "maintenance", "\U0001f6e0️", fixed={"end": False}),
        Action("maintenanceend", "End maintenance", "maintenance", "✅", _GREEN,
               fixed={"end": True, "reason": "", "minutes": 60}),
    )),
    Group("\U0001f5a5️ Server", "Check the server, talk to players, kick or stop it.", 0x3498DB, (
        Action("online", "Online?", "online", "\U0001f7e2"),
        Action("players", "Players", "players", "\U0001f465"),
        Action("playerlist", "Player list", "playerlist", "\U0001f4d2"),
        Action("modlist", "Mod list", "modlist", "\U0001f9e9"),
        Action("msg", "Broadcast", "msg", "\U0001f4e2", _BLUE),
        Action("announce", "Banner", "announce", "\U0001f5bc️"),
        Action("playsound", "Play sound", "playsound", "\U0001f50a"),
        Action("teleport", "Teleport", "teleport", "\U0001f300"),
        Action("kickall", "Kick all", "kickall", "\U0001f462", _RED, fixed={"cancel": False}),
        Action("kickallcancel", "Cancel kick", "kickall", "↩️", fixed={"cancel": True, "reason": ""}),
        Action("stop", "Stop server", "stop", "\U0001f6d1", _RED,
               confirm="Save and SHUT DOWN the server? It has to be started again from the host panel."),
    )),
    Group("\U0001f465 Players & Ranks", "Access levels, ranks and Discord ↔ PZ links.", 0x9B59B6, (
        Action("setaccesslevel", "Access level", "setaccesslevel", "\U0001f6e1️"),
        Action("setrank", "Set rank", "setrank", "\U0001f3c5"),
        Action("syncranks", "Sync ranks", "syncranks", "\U0001f501"),
        Action("linkname", "Link", "linkname", "\U0001f517", _BLUE),
        Action("unlinkname", "Unlink", "unlinkname", "⛓️", _RED),
        Action("listlinks", "List links", "listlinks", "\U0001f4dc"),
    )),
    Group("\U0001f4dd Whitelist", "Manage server accounts and whitelist requests.", 0x2ECC71, (
        Action("whitelistadd", "Add", "whitelistadd", "➕", _GREEN),
        Action("whitelistmodify", "Modify", "whitelistmodify", "✏️", _BLUE),
        Action("whitelistremove", "Remove", "whitelistremove", "\U0001f5d1️", _RED),
        Action("whitelistcancel", "Cancel request", "whitelistcancel", "\U0001f6ab"),
        Action("whitelistlist", "List", "whitelistlist", "\U0001f4cb"),
        Action("whitelistsetup", "Post apply panel", "whitelistsetup", "\U0001f4cc"),
    )),
    Group("\U0001f319 Events", "Siege Night, airdrops and supply events.", 0x1ABC9C, (
        Action("siegestatus", "Siege status", "siegestatus", "\U0001f319"),
        Action("siegestart", "Start siege", "siegestart", "⚔️", _RED,
               confirm="Start a siege night now?"),
        Action("siegestop", "Stop siege", "siegestop", "\U0001f6d1",
               confirm="Stop the active siege night?"),
        Action("siegeschedule", "Schedule siege", "siegeschedule", "\U0001f4c5"),
        Action("airdrop", "Airdrop", "airdrop", "\U0001fa82", _BLUE),
        Action("airdropstatus", "Airdrop status", "airdropstatus", "\U0001f4e6"),
        Action("supplyevent", "Supply event", "supplyevent", "\U0001f69a", _BLUE,
               confirm="Start a supply event now?"),
        Action("supplyeventstatus", "Supply status", "supplyeventstatus", "\U0001f4ca"),
    )),
    Group("\U0001f514 Bot", "Turn bot notifications on or off.", 0x95A5A6, (
        Action("features", "Features", "features", "\U0001f4cb"),
        Action("enable", "Enable", "enable", "\U0001f7e2", _GREEN),
        Action("disable", "Disable", "disable", "\U0001f534", _RED),
    )),
)

ACTIONS = {a.key: a for g in GROUPS for a in g.actions}


# ---- forms ------------------------------------------------------------------

def asked_params(command: app_commands.Command, action: Action) -> list:
    """The command's parameters the form asks for (the button fixes the rest)."""
    return [p for p in command.parameters if p.name not in action.fixed]


def parse_value(kind: discord.AppCommandOptionType, raw: str):
    """A text answer as the parameter's type. Raises ValueError with a message."""
    raw = (raw or "").strip()
    if kind is discord.AppCommandOptionType.integer:
        try:
            return int(raw)
        except ValueError:
            raise ValueError(f"`{raw}` is not a whole number.") from None
    if kind is discord.AppCommandOptionType.number:
        try:
            return float(raw)
        except ValueError:
            raise ValueError(f"`{raw}` is not a number.") from None
    return raw


def _text(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _param_label(p) -> str:
    return _text(p.display_name.replace("_", " ").capitalize() + ("" if p.required else " (optional)"),
                 _PARAM_LABEL_MAX)


def _param_input(p):
    """The modal component for one parameter."""
    kind = p.type
    if p.choices:
        return discord.ui.Select(required=p.required, options=[
            discord.SelectOption(label=_text(str(c.name), 100), value=str(c.value)) for c in p.choices[:25]])
    if kind is discord.AppCommandOptionType.boolean:
        return discord.ui.Select(required=p.required, options=[
            discord.SelectOption(label="Yes", value="true"), discord.SelectOption(label="No", value="false")])
    if kind is discord.AppCommandOptionType.user:
        return discord.ui.UserSelect(required=p.required)
    placeholder = None
    if not p.required and p.default not in (None, "", discord.utils.MISSING):
        placeholder = _text(f"Default: {p.default}", 100)
    elif p.min_value is not None or p.max_value is not None:
        placeholder = f"{p.min_value if p.min_value is not None else ''}–{p.max_value if p.max_value is not None else ''}"
    long = p.name in ("message", "reason")
    return discord.ui.TextInput(required=p.required, placeholder=placeholder,
                                style=discord.TextStyle.paragraph if long else discord.TextStyle.short,
                                max_length=1000 if long else 200)


def _wants_choice(command: app_commands.Command, name: str) -> bool:
    """The callback takes an app_commands.Choice (not the bare value) for `name`."""
    param = getattr(command, "_params", {}).get(name)
    return bool(param is not None and param.is_choice_annotation())


class CommandForm(discord.ui.Modal):
    """The form for one panel button: one input per asked parameter."""

    def __init__(self, panel: "AdminPanel", action: Action, command: app_commands.Command, params: list):
        super().__init__(title=_text(action.label, 45))
        self.panel = panel
        self.action = action
        self.command = command
        self.inputs = []
        for p in params:
            component = _param_input(p)
            desc = (p.description or "").strip()
            self.add_item(discord.ui.Label(
                text=_param_label(p), component=component,
                description=None if desc in _NO_DESCRIPTION else _text(desc, _PARAM_DESC_MAX)))
            self.inputs.append((p, component))

    def values(self) -> dict:
        """kwargs for the callback; unanswered optional parameters are left out."""
        kwargs = {}
        for p, component in self.inputs:
            if isinstance(component, discord.ui.TextInput):
                if not component.value.strip():
                    continue
                kwargs[p.name] = parse_value(p.type, component.value)
                continue
            if not component.values:
                continue
            picked = component.values[0]
            if isinstance(component, discord.ui.UserSelect):
                kwargs[p.name] = picked
            elif p.choices:
                choice = next(c for c in p.choices if str(c.value) == picked)
                kwargs[p.name] = choice if _wants_choice(self.command, p.name) else choice.value
            else:
                kwargs[p.name] = picked == "true"
        return kwargs

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            kwargs = self.values()
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await self.panel.run(interaction, self.action, self.command, kwargs)


class ConfirmView(discord.ui.LayoutView):
    """"Are you sure?" card for a risky button (only the admin who clicked can answer)."""

    def __init__(self, panel: "AdminPanel", action: Action, command: app_commands.Command, user_id: int):
        super().__init__(timeout=60)
        self.panel, self.action, self.command, self.user_id = panel, action, command, user_id
        yes = discord.ui.Button(label=f"Yes, {action.label.lower()}", style=_RED)
        yes.callback = self.yes
        no = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        no.callback = self.no
        self.add_item(cards.card(title=f"{action.emoji} {action.label}".strip(), description=action.confirm,
                                 colour=discord.Colour.red(), rows=[[yes, no]]))

    async def yes(self, interaction: discord.Interaction) -> None:
        self.stop()
        await self.panel.run(interaction, self.action, self.command, {})

    async def no(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(view=cards.CardView(cards.card(
            description="Cancelled.", colour=discord.Colour.dark_grey())))


# ---- panel --------------------------------------------------------------------

class GroupView(discord.ui.LayoutView):
    """One group's card: title, what it does, and its buttons (5 per row)."""

    def __init__(self, panel: "AdminPanel", group: Group):
        super().__init__(timeout=None)
        buttons = []
        for action in group.actions:
            button = discord.ui.Button(label=action.label, emoji=action.emoji or None, style=action.style,
                                       custom_id=_PREFIX + action.key)
            button.callback = panel.make_callback(action)
            buttons.append(button)
        rows = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
        self.add_item(cards.card(title=group.title, description=group.description,
                                 colour=group.colour, rows=rows))


class AdminPanel(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._channel_id = int(getattr(bot.config, "ADMIN_PANEL_CHANNEL_ID", 0) or 0)
        self.views = [GroupView(self, g) for g in GROUPS]
        for view in self.views:
            bot.add_view(view)
        print(f"[AdminPanel] {len(ACTIONS)} buttons in {len(GROUPS)} groups; "
              f"channel={self._channel_id or 'where /adminpanel runs'}")

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            return False
        role = discord.utils.get(interaction.guild.roles, name=self.bot.config.DEFAULT_ROLE)
        return role is not None and role in getattr(interaction.user, "roles", ())

    def make_callback(self, action: Action):
        async def callback(interaction: discord.Interaction) -> None:
            await self.on_click(interaction, action)
        return callback

    async def on_click(self, interaction: discord.Interaction, action: Action) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                f"❌ You need the **{self.bot.config.DEFAULT_ROLE}** role to use the admin panel.",
                ephemeral=True)
            return
        command = self.bot.tree.get_command(action.command)
        if not isinstance(command, app_commands.Command):
            await interaction.response.send_message(
                f"⚠️ `/{action.command}` isn't available (its module didn't load).", ephemeral=True)
            return
        params = asked_params(command, action)
        if params:
            await interaction.response.send_modal(CommandForm(self, action, command, params[:_MODAL_MAX_INPUTS]))
        elif action.confirm:
            await interaction.response.send_message(
                view=ConfirmView(self, action, command, interaction.user.id), ephemeral=True)
        else:
            await self.run(interaction, action, command, {})

    async def run(self, interaction: discord.Interaction, action: Action,
                  command: app_commands.Command, kwargs: dict) -> None:
        """Run the slash command's callback with this interaction."""
        kwargs = {**action.fixed, **kwargs}
        print(f"[AdminPanel] {interaction.user} -> /{command.name} {kwargs or ''}")
        try:
            if command.binding is not None:
                await command.callback(command.binding, interaction, **kwargs)
            else:
                await command.callback(interaction, **kwargs)
        except Exception as e:
            print(f"[AdminPanel] /{command.name} failed: {type(e).__name__}: {e}")
            text = f"⚠️ `/{command.name}` failed: {e}"
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(text, ephemeral=True)
                else:
                    await interaction.response.send_message(text, ephemeral=True)
            except discord.HTTPException:
                pass

    @app_commands.command(name="adminpanel",
                          description="Post the admin control panel (buttons for every admin command).")
    @admin_only()
    async def cmd_admin_panel(self, interaction: discord.Interaction) -> None:
        channel = self.bot.get_channel(self._channel_id) if self._channel_id else interaction.channel
        if channel is None:
            await interaction.response.send_message(
                "⚠️ ADMIN_PANEL_CHANNEL_ID points at a channel the bot can't see.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        removed = await self._remove_old_panel(channel)
        for view in self.views:
            await channel.send(view=view)
        await interaction.followup.send(
            f"✅ Admin panel posted in {channel.mention}"
            + (f" (replaced {removed} old card{'s' if removed != 1 else ''})." if removed else "."),
            ephemeral=True)

    async def _remove_old_panel(self, channel) -> int:
        """Delete this bot's earlier panel cards in the channel (last 100 messages)."""
        removed = 0
        try:
            async for message in channel.history(limit=100):
                if message.author.id != self.bot.user.id or not message.flags.components_v2:
                    continue
                ids = [getattr(c, "custom_id", None) for c in _walk(message.components)]
                if any(i and i.startswith(_PREFIX) for i in ids):
                    await message.delete()
                    removed += 1
        except discord.HTTPException as e:
            print(f"[AdminPanel] could not clear the old panel: {e}")
        return removed


def _walk(components):
    """Every component in a received message, any depth."""
    for c in components or ():
        yield c
        yield from _walk(getattr(c, "children", None) or getattr(c, "components", None))
        accessory = getattr(c, "accessory", None)
        if accessory is not None:
            yield accessory


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminPanel(bot))
    print("[AdminPanel] Extension loaded.")
