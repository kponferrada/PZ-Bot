"""command_panel.py — button panels that run slash commands (shared engine).

A panel is a set of groups; each group is a card with buttons inside it. Each
button runs an existing slash command:

  - no parameters → it runs at once (an Action with `confirm` asks first);
  - parameters → a form opens, built from the command's own parameters
    (text boxes, a dropdown for choices and yes/no, a member picker for
    members), and submitting it runs the command.

The panel calls the command's callback directly, so the slash command stays the
single implementation; a new parameter shows up in the form by itself. Calling
the callback skips the command's own checks and cooldowns, so a panel decides
who may click (`PanelCog.allowed`). A `private` panel forces every reply to be
ephemeral, so public commands don't post into the panel's channel.

Buttons are persistent (custom_id `<prefix><key>`) and keep working after a
bot restart. Used by admin_panel.py and player_panel.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import discord
from discord import app_commands
from discord.ext import commands

import cards

_PARAM_LABEL_MAX = 45
_PARAM_DESC_MAX = 100
_MODAL_MAX_INPUTS = 5
_NO_DESCRIPTION = ("…", "...", "")

RED = discord.ButtonStyle.danger
BLUE = discord.ButtonStyle.primary
GREEN = discord.ButtonStyle.success


@dataclass(frozen=True)
class Action:
    key: str                      # custom_id suffix, unique within the panel
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


def param_label(p) -> str:
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


def wants_choice(command: app_commands.Command, name: str) -> bool:
    """The callback takes an app_commands.Choice (not the bare value) for `name`."""
    param = getattr(command, "_params", {}).get(name)
    return bool(param is not None and param.is_choice_annotation())


class CommandForm(discord.ui.Modal):
    """The form for one panel button: one input per asked parameter."""

    def __init__(self, panel: "PanelCog", action: Action, command: app_commands.Command, params: list):
        super().__init__(title=_text(action.label, 45))
        self.panel = panel
        self.action = action
        self.command = command
        self.inputs = []
        for p in params:
            component = _param_input(p)
            desc = (p.description or "").strip()
            self.add_item(discord.ui.Label(
                text=param_label(p), component=component,
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
                kwargs[p.name] = choice if wants_choice(self.command, p.name) else choice.value
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
    """"Are you sure?" card for a risky button."""

    def __init__(self, panel: "PanelCog", action: Action, command: app_commands.Command):
        super().__init__(timeout=60)
        self.panel, self.action, self.command = panel, action, command
        yes = discord.ui.Button(label=f"Yes, {action.label.lower()}", style=RED)
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


# ---- private replies ----------------------------------------------------------

class _PrivateResponse:
    """interaction.response, but every reply is ephemeral."""

    def __init__(self, response):
        self._response = response

    def __getattr__(self, name):
        return getattr(self._response, name)

    async def send_message(self, *args, **kwargs):
        kwargs["ephemeral"] = True
        return await self._response.send_message(*args, **kwargs)

    async def defer(self, **_kwargs):
        # thinking=True: on a button a plain defer would edit the panel message.
        return await self._response.defer(ephemeral=True, thinking=True)


class _PrivateFollowup:
    def __init__(self, followup):
        self._followup = followup

    def __getattr__(self, name):
        return getattr(self._followup, name)

    async def send(self, *args, **kwargs):
        kwargs["ephemeral"] = True
        return await self._followup.send(*args, **kwargs)


class PrivateInteraction:
    """Wraps an interaction so a command's public replies only reach the clicker."""

    def __init__(self, interaction: discord.Interaction):
        self._interaction = interaction
        self.response = _PrivateResponse(interaction.response)
        self.followup = _PrivateFollowup(interaction.followup)

    def __getattr__(self, name):
        return getattr(self._interaction, name)


# ---- panel --------------------------------------------------------------------

def button_rows(panel: "PanelCog", group: Group) -> list:
    """A group's buttons (fresh instances, wired to `panel`), 5 per row."""
    buttons = []
    for action in group.actions:
        button = discord.ui.Button(label=action.label, emoji=action.emoji or None, style=action.style,
                                   custom_id=panel.panel_prefix + action.key)
        button.callback = panel.make_callback(action)
        buttons.append(button)
    return [buttons[i:i + 5] for i in range(0, len(buttons), 5)]


class GroupView(discord.ui.LayoutView):
    """One group's card: title, what it does, and its buttons."""

    def __init__(self, panel: "PanelCog", group: Group):
        super().__init__(timeout=None)
        self.add_item(cards.card(title=group.title, description=group.description,
                                 colour=group.colour, rows=button_rows(panel, group)))


class PanelCog(commands.Cog):
    """Base for a panel cog: set `panel_prefix`, `panel_groups`, `private` and `allowed`."""

    panel_prefix = ""
    panel_groups: tuple = ()
    private = False      # force every reply to be ephemeral
    log_name = "Panel"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.views = [GroupView(self, g) for g in self.panel_groups]
        for view in self.views:
            bot.add_view(view)

    async def allowed(self, interaction: discord.Interaction) -> bool:
        """Answer the interaction yourself and return False to refuse a click."""
        return True

    def make_callback(self, action: Action):
        async def callback(interaction: discord.Interaction) -> None:
            await self.on_click(interaction, action)
        return callback

    async def on_click(self, interaction: discord.Interaction, action: Action) -> None:
        if not await self.allowed(interaction):
            return
        command = self.bot.tree.get_command(action.command)
        if not isinstance(command, app_commands.Command):
            await interaction.response.send_message(
                f"⚠️ `/{action.command}` isn't available right now.", ephemeral=True)
            return
        params = asked_params(command, action)
        if params:
            await interaction.response.send_modal(CommandForm(self, action, command, params[:_MODAL_MAX_INPUTS]))
        elif action.confirm:
            await interaction.response.send_message(view=ConfirmView(self, action, command), ephemeral=True)
        else:
            await self.run(interaction, action, command, {})

    async def run(self, interaction: discord.Interaction, action: Action,
                  command: app_commands.Command, kwargs: dict) -> None:
        """Run the slash command's callback with this interaction."""
        kwargs = {**action.fixed, **kwargs}
        print(f"[{self.log_name}] {interaction.user} -> /{command.name} {kwargs or ''}")
        target = PrivateInteraction(interaction) if self.private else interaction
        try:
            if command.binding is not None:
                await command.callback(command.binding, target, **kwargs)
            else:
                await command.callback(target, **kwargs)
        except Exception as e:
            print(f"[{self.log_name}] /{command.name} failed: {type(e).__name__}: {e}")
            text = f"⚠️ `/{command.name}` failed: {e}"
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(text, ephemeral=True)
                else:
                    await interaction.response.send_message(text, ephemeral=True)
            except discord.HTTPException:
                pass

    async def post(self, interaction: discord.Interaction, channel) -> None:
        """Post every group card into `channel`, replacing this panel's earlier cards."""
        if channel is None:
            await interaction.response.send_message(
                "⚠️ The panel channel is not set or the bot can't see it.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        removed = await self._remove_old_cards(channel)
        for view in self.views:
            await channel.send(view=view)
        await interaction.followup.send(
            f"✅ Posted in {channel.mention}"
            + (f" (replaced {removed} old card{'s' if removed != 1 else ''})." if removed else "."),
            ephemeral=True)

    async def _remove_old_cards(self, channel) -> int:
        """Delete this bot's earlier cards for this panel in the channel (last 100
        messages): V2 cards titled with one of the panel's group titles. Other
        cards carrying the same buttons (the status card) have other titles."""
        removed = 0
        try:
            async for message in channel.history(limit=100):
                if message.author.id != self.bot.user.id or not message.flags.components_v2:
                    continue
                if _is_panel_card(message, self):
                    await message.delete()
                    removed += 1
        except discord.HTTPException as e:
            print(f"[{self.log_name}] could not clear the old panel: {e}")
        return removed


def _is_panel_card(message: discord.Message, panel: PanelCog) -> bool:
    """The message's text starts with one of the panel's group titles."""
    texts = [getattr(c, "content", "") or "" for c in walk_components(message.components)]
    titles = [f"### {g.title}" for g in panel.panel_groups]
    return any(t.startswith(title) for t in texts for title in titles)


def walk_components(components):
    """Every component in a received message, any depth."""
    for c in components or ():
        yield c
        yield from walk_components(getattr(c, "children", None) or getattr(c, "components", None))
        accessory = getattr(c, "accessory", None)
        if accessory is not None:
            yield accessory
