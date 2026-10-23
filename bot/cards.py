"""cards.py — Components V2 cards: the text and its buttons in one box.

A classic message puts the buttons under the embed. A card (a V2 `Container`)
holds the title, fields, footer and the button rows together, with the
embed's colour as the accent bar. `from_embed` turns an existing embed into a
card so the embed builders stay as they are.

V2 messages can't carry `content` or `embeds`, so send or edit them with
`view=` only (`edit_card` clears the old content and embeds). Text across one
message is limited to 4000 characters and 40 components.
"""
from __future__ import annotations

import discord

_TEXT_LIMIT = 4000


def _rows(rows) -> list:
    """Each entry is an ActionRow or a list of buttons/selects (max 5 per row)."""
    out = []
    for row in rows or ():
        if isinstance(row, discord.ui.ActionRow):
            out.append(row)
        elif row:
            out.append(discord.ui.ActionRow(*row))
    return out


def _field_blocks(fields) -> list:
    """Text blocks for (name, value[, inline]) fields: one block per field,
    except that a run of inline fields shares one block, one field per line."""
    blocks, run = [], []
    for f in fields:
        name, value = f[0], f[1]
        if len(f) > 2 and f[2]:
            run.append(f"**{name}** · {' '.join(str(value).split())}" if name else str(value))
            continue
        if run:
            blocks.append("\n".join(run))
            run = []
        blocks.append(f"**{name}**\n{value}" if name else str(value))
    if run:
        blocks.append("\n".join(run))
    return blocks


def card(*, title: str = "", description: str = "", fields=(), footer: str = "",
         colour=None, thumbnail: str = "", image: str = "", rows=()) -> discord.ui.Container:
    """A Container: an optional banner `image`, title + description (beside an
    optional thumbnail), the fields, a footer line, then the button rows.
    `fields` are (name, value) or (name, value, inline) tuples."""
    head = "\n".join(p for p in (f"### {title}" if title else "", description or "") if p)
    items = []
    if image:
        items.append(discord.ui.MediaGallery(discord.MediaGalleryItem(image)))
    if head and thumbnail:
        items.append(discord.ui.Section(head[:_TEXT_LIMIT], accessory=discord.ui.Thumbnail(thumbnail)))
    elif head:
        items.append(discord.ui.TextDisplay(head[:_TEXT_LIMIT]))
    if fields:
        items.append(discord.ui.Separator())
        for text in _field_blocks(fields):
            items.append(discord.ui.TextDisplay(text[:_TEXT_LIMIT]))
    if footer:
        items.append(discord.ui.TextDisplay(f"-# {footer}"))
    action_rows = _rows(rows)
    if action_rows and items:
        items.append(discord.ui.Separator())
    items.extend(action_rows)
    return discord.ui.Container(*items, accent_colour=colour)


def from_embed(embed: discord.Embed, rows=()) -> discord.ui.Container:
    """The embed as a card, with `rows` of buttons inside it. An embed with an
    author and no title uses the author as the title and its icon as the
    thumbnail; the embed image becomes the banner."""
    footer = embed.footer.text if embed.footer and embed.footer.text else ""
    if embed.timestamp:
        stamp = f"<t:{int(embed.timestamp.timestamp())}:f>"
        footer = f"{footer} • {stamp}" if footer else stamp
    thumb = embed.thumbnail.url if embed.thumbnail and embed.thumbnail.url else ""
    title = embed.title or ""
    if not title and embed.author and embed.author.name:
        title = embed.author.name
        thumb = thumb or embed.author.icon_url or ""
    image = embed.image.url if embed.image and embed.image.url else ""
    return card(title=title, description=embed.description or "",
                fields=[(f.name, f.value, f.inline) for f in embed.fields], footer=footer,
                colour=embed.colour, thumbnail=thumb or "", image=image or "", rows=rows)


class CardView(discord.ui.LayoutView):
    """A LayoutView holding cards. `timeout=None` + custom_ids = persistent."""

    def __init__(self, *cards, timeout: float | None = None):
        super().__init__(timeout=timeout)
        for c in cards:
            self.add_item(c)


def buttons(view: discord.ui.LayoutView) -> list:
    """Every button in a layout (any depth), in order."""
    return [i for i in view.walk_children() if isinstance(i, discord.ui.Button)]


async def edit_card(message: discord.Message, view: discord.ui.LayoutView, log: str = "Cards") -> discord.Message | None:
    """Turn `message` into this card. A message posted before cards existed
    (content/embeds + classic buttons) is converted in place; if Discord
    refuses that, the card is posted fresh and the old message removed."""
    try:
        return await message.edit(content=None, embeds=[], view=view)
    except discord.HTTPException as e:
        if message.flags.components_v2:
            print(f"[{log}] Failed to update card: {e}")
            return None
        print(f"[{log}] Could not convert an old message to a card ({e}); reposting it.")
    try:
        new = await message.channel.send(view=view)
    except discord.HTTPException as e:
        print(f"[{log}] Failed to repost card: {e}")
        return None
    try:
        await message.delete()
    except discord.HTTPException:
        pass
    return new
