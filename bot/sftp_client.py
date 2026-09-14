"""sftp_client.py — reconnecting async SFTP client for the PZ Tambayan bot.

This is the single abstraction that lets the bot run on a VPS separate from the
game server. Every server-file access in the bot goes through this module: reads
and writes that were previously local `Path`/`open` calls now happen over SFTP.

Requires: asyncssh (see requirements.txt).

Security note: host-key checking is disabled (known_hosts=None) for simplicity.
For production, pin the server's host key by passing known_hosts=<path> to
asyncssh.connect, or verify the fingerprint once and store it.
"""

from __future__ import annotations

import asyncio
import fnmatch

import asyncssh


class SftpError(Exception):
    """Raised when an SFTP operation fails."""


class SftpClient:
    def __init__(
        self,
        host: str,
        port: int = 22,
        username: str | None = None,
        password: str | None = None,
        key_path: str | None = None,
    ):
        self.host = host
        self.port = int(port)
        self.username = username
        self.password = password
        self.key_path = key_path
        self._conn = None
        self._sftp = None
        self._lock = asyncio.Lock()

    # ---- connection management ------------------------------------------------

    async def connect(self) -> None:
        if self._conn is not None and not self._conn.is_closed():
            return
        kwargs = dict(
            host=self.host,
            port=self.port,
            username=self.username,
            known_hosts=None,   # see security note above
            connect_timeout=15,  # fail fast with a clear error if the host is unreachable
        )
        if self.key_path:
            kwargs["client_keys"] = [self.key_path]
        elif self.password:
            kwargs["password"] = self.password
        self._conn = await asyncssh.connect(**kwargs)
        self._sftp = await self._conn.start_sftp_client()

    async def _ensure(self) -> None:
        async with self._lock:
            try:
                await self.connect()
            except Exception as exc:
                self._conn = None
                self._sftp = None
                raise SftpError(
                    f"SFTP connect failed to {self.host}:{self.port} — {exc}. "
                    "Check SFTP_HOST/SFTP_PORT and that outbound port 22 is reachable from this machine."
                ) from exc

    async def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
                self._sftp = None

    # ---- primitives -----------------------------------------------------------

    async def read_text(self, path: str, encoding: str = "utf-8") -> str:
        await self._ensure()
        try:
            async with self._sftp.open(path, "rb") as f:
                data = await f.read()
            return data.decode(encoding, errors="replace")
        except Exception as exc:
            raise SftpError(f"read_text({path}): {exc}") from exc

    async def read_bytes(self, path: str) -> bytes:
        """Read `path` raw (no decoding) — for binary files like players.db."""
        await self._ensure()
        try:
            async with self._sftp.open(path, "rb") as f:
                return await f.read()
        except Exception as exc:
            raise SftpError(f"read_bytes({path}): {exc}") from exc

    async def stat(self, path: str) -> tuple[int, int] | None:
        """Return (size, mtime) for `path`, or None if the file does not exist."""
        await self._ensure()
        try:
            st = await self._sftp.stat(path)
            return (st.size, st.mtime)
        except (asyncssh.SFTPNoSuchFile, asyncssh.SFTPNoSuchPath, FileNotFoundError, OSError):
            return None
        except Exception as exc:
            raise SftpError(f"stat({path}): {exc}") from exc

    async def exists(self, path: str) -> bool:
        return (await self.stat(path)) is not None

    async def list_dir(self, path: str) -> list[str]:
        """Return the base names in `path`."""
        await self._ensure()
        try:
            entries = await self._sftp.listdir(path)
        except Exception as exc:
            raise SftpError(f"list_dir({path}): {exc}") from exc
        names = []
        for entry in entries:
            # asyncssh returns SFTPName objects; be tolerant of plain strings too.
            names.append(getattr(entry, "filename", str(entry)))
        return names

    async def newest_matching(self, directory: str, pattern: str) -> str | None:
        """Return the full path of the newest file in `directory` whose name matches
        `pattern` (shell-style glob), or None if none exist."""
        try:
            names = await self.list_dir(directory)
        except SftpError:
            return None
        matches = [n for n in names if fnmatch.fnmatch(n, pattern)]
        if not matches:
            return None
        best = None
        best_mtime = -1.0
        for name in matches:
            path = f"{directory.rstrip('/')}/{name}"
            st = await self.stat(path)
            if st is not None and st[1] > best_mtime:
                best = path
                best_mtime = st[1]
        return best

    async def tail(self, path: str, offset: int) -> tuple[str, int]:
        """Read `path` from byte `offset` to EOF. Return (text, new_offset)."""
        await self._ensure()
        try:
            async with self._sftp.open(path, "rb") as f:
                await f.seek(offset)
                data = await f.read()
            return data.decode("utf-8", errors="replace"), offset + len(data)
        except Exception as exc:
            raise SftpError(f"tail({path}, {offset}): {exc}") from exc

    async def write_text(self, path: str, content: str, encoding: str = "utf-8") -> bool:
        """Write `content` to `path` directly.

        The tmp+rename "atomic" approach was dropped: this host's SFTP server
        rejects FXP_RENAME ("Failure"). These files are tiny, so a direct write
        is effectively atomic and matches upstream Jeeves, which wrote directly.
        """
        await self._ensure()
        data = content.encode(encoding)
        try:
            async with self._sftp.open(path, "wb") as f:
                await f.write(data)
            return True
        except Exception as exc:
            raise SftpError(f"write_text({path}): {exc}") from exc

    async def write_bytes(self, path: str, data: bytes) -> bool:
        """Write raw `data` to `path` directly (for binary files like players.db)."""
        await self._ensure()
        try:
            async with self._sftp.open(path, "wb") as f:
                await f.write(data)
            return True
        except Exception as exc:
            raise SftpError(f"write_bytes({path}): {exc}") from exc


# ---- module-level singleton ---------------------------------------------------

_client: SftpClient | None = None


def init(
    host: str,
    port: int = 22,
    username: str | None = None,
    password: str | None = None,
    key_path: str | None = None,
) -> SftpClient:
    global _client
    _client = SftpClient(host, port, username, password, key_path)
    return _client


def get() -> SftpClient:
    if _client is None:
        raise SftpError("sftp_client not initialized — call sftp_client.init() first")
    return _client
