"""
Chat Relay Extension (SFTP).

Bridges in-game PZ chat with a Discord channel.

Game -> Discord:
  - Tails the server chat log over SFTP
  - Strips <RGB:...> color tags from author names
  - Posts as plain text with ANSI color codes for ranked players

Discord -> Game:
  - Listens for messages in the relay channel
  - Forwards them to the PZ server via the Lua file bridge (SFTP)

Config (config.env):
  CHAT_RELAY_CHANNEL_ID=  (Discord channel ID for the relay)
  SFTP_LOGS_DIR=          (remote path to the PZ server Logs folder)
"""

import re
import os
import asyncio
import discord
from discord.ext import commands, tasks
from typing import Optional

import lua_bridge
import sftp_client

# Regex to strip PZ rich-text tags from author names
RGB_TAG_RE = re.compile(r'<RGB:[^>]+>')
SIZE_TAG_RE = re.compile(r'<SIZE:[^>]+>')

# Parse player chat lines from the PZ server chat log
# Format: [timestamp][info] Got message:ChatMessage{chat=General, author='StewBag', text='hello'}.
CHAT_LINE_RE = re.compile(
    r"\[.*?\]\[info\] Got message:ChatMessage\{chat=(\w+), author='([^']+)', text='(.*)'\}\."
)

# Chat types we relay
RELAY_CHAT_TYPES = {'General'}

# Discord ANSI color codes (used inside ```ansi blocks)
ANSI_COLORS = {
    0: None,        # Default - no color
    1: "1;32",      # Fuel - bold green
    2: "1;34",      # Spark - bold blue
    3: "1;35",      # Cinder - bold pink/violet
    4: "1;33",      # Flame - bold yellow
    5: "1;36",      # Blaze - bold cyan
    6: "1;31",      # Inferno - bold red
}


# In-game chat must never ping anyone on Discord.
_NO_MENTIONS = discord.AllowedMentions.none()


def strip_rgb_tags(text: str) -> str:
    """Remove all <RGB:...> and <SIZE:...> tags from a string."""
    text = RGB_TAG_RE.sub('', text)
    text = SIZE_TAG_RE.sub('', text)
    return text.strip()


class ChatRelay(commands.Cog):
    """Relays chat between PZ server and Discord."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._channel_id = int(os.getenv('CHAT_RELAY_CHANNEL_ID', '0'))
        self._log_dir = getattr(bot.config, 'SFTP_LOGS_DIR', None) or os.getenv('CHAT_LOG_PATH', '')
        self._file_pos = 0
        self._current_log = None
        self._buffer = ""  # trailing partial line from the last read
        self._active = False

        if not self._channel_id:
            print("[ChatRelay] WARNING: CHAT_RELAY_CHANNEL_ID not set. Relay disabled.")
            return

        self._active = True
        self._tail_chat_log.start()
        print(f"[ChatRelay] Relay active. Channel={self._channel_id} Logs={self._log_dir}")

    def cog_unload(self):
        if self._active:
            self._tail_chat_log.cancel()

    def _get_channel(self) -> Optional[discord.TextChannel]:
        return self.bot.get_channel(self._channel_id)

    def _get_rank_for_author(self, author: str) -> int:
        """Look up the rank for a PZ username from the RankSync cog."""
        rank_cog = self.bot.get_cog("RankSync")
        if not rank_cog:
            return 0
        for pz_name, rank in rank_cog._ranks.items():
            if pz_name.lower() == author.lower():
                return rank
        return 0

    def _format_message(self, chat_type: str, author: str, text: str) -> str:
        """Format a chat message for Discord with ANSI rank colors."""
        rank = self._get_rank_for_author(author)
        ansi_code = ANSI_COLORS.get(rank)
        if ansi_code:
            # Inside a code block only a ``` run can break out; defuse it.
            author, text = (t.replace("`", "\u02cb") for t in (author, text))
            return f"```ansi\n\u001b[{ansi_code}m[{author}]\u001b[0m: {text}\n```"
        author = discord.utils.escape_markdown(author)
        text = discord.utils.escape_markdown(text)
        return f"**[{author}]**: {text}"

    # ================================================================
    # Game -> Discord: tail the chat log over SFTP
    # ================================================================

    async def _find_latest_chat_log(self) -> Optional[str]:
        """Find the most recent chat log (PZ names them YYYY-MM-DD_HH-MM_chat.txt)."""
        if not self._log_dir:
            return None
        sftp = sftp_client.get()
        return await sftp.newest_matching(self._log_dir, '*chat*.txt')

    @tasks.loop(seconds=2.0)
    async def _tail_chat_log(self):
        if not self._active:
            return

        try:
            sftp = sftp_client.get()
            log_file = await self._find_latest_chat_log()
            if not log_file:
                return

            # Log rotation (a new file each server session): read the new file
            # from the start so the first messages after a restart aren't lost.
            # On the first file seen (startup), skip its history instead.
            if log_file != self._current_log:
                first = self._current_log is None
                self._current_log = log_file
                self._buffer = ""
                if first:
                    st = await sftp.stat(log_file)
                    self._file_pos = st[0] if st else 0
                    print(f"[ChatRelay] Now tailing: {log_file} (from {self._file_pos})")
                    return
                self._file_pos = 0
                print(f"[ChatRelay] Now tailing: {log_file}")

            try:
                new_text, self._file_pos = await sftp.tail(log_file, self._file_pos)
            except sftp_client.SftpError:
                return
            if not new_text:
                return
            # The game may be mid-line when we read; keep the partial line.
            self._buffer += new_text
            new_lines = self._buffer.split("\n")
            self._buffer = new_lines.pop()

            channel = self._get_channel()
            if not channel:
                return

            for line in new_lines:
                line = line.strip()

                match = CHAT_LINE_RE.search(line)
                if not match:
                    continue

                chat_type = match.group(1)
                raw_author = match.group(2)
                message_text = match.group(3)

                if chat_type not in RELAY_CHAT_TYPES:
                    continue

                clean_author = strip_rgb_tags(raw_author)

                if not message_text.strip():
                    continue

                msg = self._format_message(chat_type, clean_author, message_text)

                if self.bot.features.is_enabled("chat_relay"):
                    try:
                        await channel.send(msg, allowed_mentions=_NO_MENTIONS)
                    except discord.HTTPException as e:
                        print(f"[ChatRelay] Discord send error: {e}")

        except Exception as e:
            print(f"[ChatRelay] Tail error: {e}")

    @_tail_chat_log.before_loop
    async def _before_tail(self):
        await self.bot.wait_until_ready()

        # Seek to end of current log so we don't replay history. If SFTP is
        # down now, the loop does the same on the first file it finds.
        try:
            log_file = await self._find_latest_chat_log()
            if log_file:
                sftp = sftp_client.get()
                st = await sftp.stat(log_file)
                self._file_pos = st[0] if st else 0
                self._current_log = log_file
                print(f"[ChatRelay] Tailing {log_file} from position {self._file_pos}")
        except sftp_client.SftpError as e:
            print(f"[ChatRelay] Initial seek failed ({e}); will retry in the loop.")

    # ================================================================
    # Discord -> Game: listen for messages in the relay channel
    # ================================================================

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Forward Discord messages from the relay channel to the game."""
        if not self._active:
            return
        if message.author.bot:
            return
        if message.channel.id != self._channel_id:
            return
        if message.content.startswith('/') or message.content.startswith('!'):
            return
        if not message.content.strip():
            return

        if not self.bot.features.is_enabled("chat_relay"):
            return
        try:
            display_name = message.author.display_name
            # clean_content turns <@123>/<#456> into readable @name/#channel.
            await lua_bridge.chat_relay(display_name, message.clean_content)
        except Exception as e:
            print(f"[ChatRelay] Relay error: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(ChatRelay(bot))
    print("[ChatRelay] Extension loaded.")
