"""mod_checker.py — Steam Workshop update checker (migrated from Jeeves's ModChecker).

Compares each subscribed Workshop item's update timestamp against a saved
baseline to detect updates, so the bot can restart the server to apply them.

Source order (matching Jeeves):
  1. Keyless ``ISteamRemoteStorage/GetPublishedFileDetails`` — public items only;
     it answers ``result != 1`` with no data for unlisted items.
  2. Keyed ``IPublishedFileService/GetDetails`` — CAN see unlisted items, but
     needs a ``STEAM_API_KEY``. Live query: an unlisted mod is detected on the
     same cadence as a public one.
  3. ``steamapps/workshop/appworkshop_108600.acf`` (read over SFTP) — Steam's own
     local manifest. Needs no key and visibility does not restrict it, but it
     LAGS: ``latest_timeupdated`` only advances when the game server boots and
     fetches the update, so on its own it can never announce an update before it
     has already been applied. It is the no-key fallback, not the primary source.
"""

import os
import re
import json
import asyncio
from pathlib import Path

import aiohttp

import server_config
import sftp_client

_KEYLESS_URL = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
_KEYED_URL = "https://api.steampowered.com/IPublishedFileService/GetDetails/v1/"

_APPID = "108600"

# Steam's Web API is flaky with large batches — one request carrying 200+ ids
# routinely times out (or returns an empty response), which made the checker
# silently conclude "everything current". Chunk into small batches and retry
# each on transient failure.
_API_CHUNK_SIZE = 50           # Workshop ids per request
_API_RETRIES = 3               # attempts per chunk
_API_RETRY_BASE_DELAY = 2.0    # seconds, doubled per retry
_API_INTER_CHUNK_DELAY = 1.0   # seconds between chunk requests

# One flat "<id> { ... }" block. Non-greedy and brace-free inside, so it matches
# each leaf block rather than swallowing the enclosing section.
_BLOCK = r'"%s"\s*\{([^{}]*)\}'
_KV = re.compile(r'"([A-Za-z_]+)"\s+"([^"]*)"')


def _chunk(ids: list, size: int) -> list:
    """Split a flat id list into sub-lists of at most `size` items."""
    return [ids[i:i + size] for i in range(0, len(ids), size)]


class ModChecker:
    def __init__(self, bot):
        self.bot = bot
        self._key = (getattr(bot.config, "STEAM_API_KEY", "")
                     or os.getenv("STEAM_API_KEY", "") or "").strip()
        state_path = getattr(bot.config, "MOD_UPDATE_STATE_PATH", "") or "mod_update_state.json"
        self._state_path = Path(state_path)
        if not self._state_path.is_absolute():
            self._state_path = Path(__file__).parent / self._state_path

        # The ACF manifest sits two levels up from .../steamapps/workshop/content/108600.
        self._manifest_path = self._derive_manifest_path(bot)

    def _derive_manifest_path(self, bot) -> str | None:
        mods_dir = (getattr(bot.config, "SFTP_MODS_DIR", "")
                    or os.getenv("SFTP_MODS_DIR", "") or "").strip()
        if not mods_dir:
            return None
        workshop_dir = "/".join(mods_dir.rstrip("/").split("/")[:-2])
        return f"{workshop_dir}/appworkshop_{_APPID}.acf"

    # ---- workshop ids --------------------------------------------------------

    async def get_workshop_ids(self) -> list:
        return await server_config.read_workshop_items(self.bot)

    # ---- Steam API sources ---------------------------------------------------

    async def _fetch_keyless(self, ids: list) -> dict:
        """Keyless lookup. Returns {id: {time, title, source}} for items it can
        see; unlisted items (result != 1) are left out entirely.

        Chunked into small batches and retried, because a single request carrying
        hundreds of ids times out (or returns an empty response).
        """
        if not ids:
            return {}
        state = {}
        for batch in _chunk(ids, _API_CHUNK_SIZE):
            state.update(await self._fetch_keyless_batch(batch))
            await asyncio.sleep(_API_INTER_CHUNK_DELAY)
        return state

    async def _fetch_keyless_batch(self, ids: list) -> dict:
        data = {"itemcount": str(len(ids)), "format": "json"}
        for i, item_id in enumerate(ids):
            data[f"publishedfileids[{i}]"] = item_id
        payload = None
        for attempt in range(_API_RETRIES):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(_KEYLESS_URL, data=data,
                                            timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        payload = await resp.json()
                break
            except Exception as e:
                if attempt < _API_RETRIES - 1:
                    await asyncio.sleep(_API_RETRY_BASE_DELAY * (2 ** attempt))
                    continue
                print(f"[ModCheck] Steam API error after {_API_RETRIES} tries: "
                      f"{type(e).__name__}: {e}")
                return {}
        if payload is None:
            return {}
        state = {}
        for item in payload.get("response", {}).get("publishedfiledetails", []):
            item_id = str(item.get("publishedfileid"))
            if item.get("result") != 1:
                print(f"[ModCheck] Steam API cannot see {item_id} "
                      f"(result={item.get('result')}); will try keyed API / manifest.")
                continue
            state[item_id] = {
                "time": item.get("time_updated", 0),
                "title": item.get("title", "Unknown Mod"),
                "source": "api",
            }
        return state

    async def _fetch_keyed(self, ids: list) -> dict:
        """Keyed lookup for unlisted items. Requires STEAM_API_KEY. GET-only.

        The key travels in the query string, so NOTHING in this method may print
        a URL, request, or response body — an exception's string form can leak
        the key into logs and from there Discord. Status codes, exception CLASS
        names, and ids only. Keep it that way.

        Chunked + retried: a single GET with hundreds of ids builds a huge query
        string that Steam times out on.
        """
        if not ids or not self._key:
            return {}
        out = {}
        for batch in _chunk(ids, _API_CHUNK_SIZE):
            out.update(await self._fetch_keyed_batch(batch))
            await asyncio.sleep(_API_INTER_CHUNK_DELAY)
        if out:
            print(f"[ModCheck] keyed API resolved {len(out)} unlisted id(s): "
                  f"{', '.join(sorted(out))}")
        return out

    async def _fetch_keyed_batch(self, ids: list) -> dict:
        params = {"key": self._key}
        for i, item_id in enumerate(ids):
            params[f"publishedfileids[{i}]"] = item_id
        params["includetags"] = "false"
        params["includeadditionalpreviews"] = "false"
        params["includechildren"] = "false"
        out = {}
        payload = None
        for attempt in range(_API_RETRIES):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(_KEYED_URL, params=params,
                                           timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        if resp.status != 200:
                            print(f"[ModCheck] keyed API refused lookup "
                                  f"(HTTP {resp.status}); skipping unlisted ids.")
                            return {}
                        payload = await resp.json()
                break
            except Exception:
                if attempt < _API_RETRIES - 1:
                    await asyncio.sleep(_API_RETRY_BASE_DELAY * (2 ** attempt))
                    continue
                print(f"[ModCheck] keyed API error after {_API_RETRIES} tries; "
                      f"skipping unlisted ids.")
                return {}
        if payload is None:
            return {}
        for item in payload.get("response", {}).get("publishedfiledetails", []):
            item_id = str(item.get("publishedfileid"))
            if item.get("result") != 1:
                continue
            when = item.get("time_updated") or item.get("time_created") or 0
            try:
                when = int(when)
            except (TypeError, ValueError):
                when = 0
            if when <= 0:
                continue
            out[item_id] = {
                "time": when,
                "title": item.get("title") or f"Workshop item {item_id}",
                "source": "api-keyed",
            }
        return out

    async def _fetch_state_from_manifest(self, ids: list) -> dict:
        """No-key fallback: read ``latest_timeupdated`` from Steam's local
        ``appworkshop_108600.acf`` over SFTP. Unlisted items are as readable as
        public ones, but the value lags — it only advances when the server boots
        and fetches the update, so this cannot announce an update before it has
        already been applied."""
        if not ids or not self._manifest_path:
            return {}
        sftp = sftp_client.get()
        try:
            if not await sftp.exists(self._manifest_path):
                print(f"[ModCheck] manifest not found: {self._manifest_path}")
                return {}
            text = await sftp.read_text(self._manifest_path)
        except sftp_client.SftpError as e:
            print(f"[ModCheck] manifest read failed: {e}")
            return {}

        out = {}
        for item_id in ids:
            best = None
            for body in re.findall(_BLOCK % re.escape(item_id), text):
                block = {k: v for k, v in _KV.findall(body)}
                for key in ("latest_timeupdated", "timeupdated"):
                    raw = block.get(key)
                    if raw and raw.isdigit():
                        value = int(raw)
                        if best is None or value > best:
                            best = value
                        break
            if best is not None:
                out[item_id] = {
                    "time": best,
                    "title": await self._read_item_title_sftp(item_id),
                    "source": "manifest",
                }
        if out:
            print(f"[ModCheck] manifest supplied {len(out)} id(s) the API could not: "
                  f"{', '.join(sorted(out))}")
        return out

    async def _read_item_title_sftp(self, item_id: str) -> str:
        """Best-effort human name for an item, from the mod.info it shipped."""
        mods_dir = (getattr(self.bot.config, "SFTP_MODS_DIR", "")
                    or os.getenv("SFTP_MODS_DIR", "") or "").strip()
        if not mods_dir:
            return f"Workshop item {item_id}"
        sftp = sftp_client.get()
        base = f"{mods_dir.rstrip('/')}/{item_id}/mods"
        try:
            if not await sftp.exists(base):
                return f"Workshop item {item_id}"
            for mod_dir in sorted(await sftp.list_dir(base)):
                info = f"{base}/{mod_dir}/mod.info"
                if not await sftp.exists(info):
                    continue
                for line in (await sftp.read_text(info)).splitlines():
                    line = line.strip()
                    if line.lower().startswith("name="):
                        name = line.split("=", 1)[1].strip()
                        if name:
                            return name
        except sftp_client.SftpError:
            pass
        return f"Workshop item {item_id}"

    async def _fetch_workshop_state(self, ids: list) -> dict:
        state = await self._fetch_keyless(ids)
        missing = [i for i in ids if i not in state]
        if missing:
            state.update(await self._fetch_keyed(missing))
        missing = [i for i in ids if i not in state]
        if missing:
            state.update(await self._fetch_state_from_manifest(missing))
        still_missing = [i for i in ids if i not in state]
        if still_missing:
            print(f"[ModCheck] !! no update source for {len(still_missing)} "
                  f"id(s): {', '.join(still_missing)}")
        return state

    # ---- baseline state ------------------------------------------------------

    def _load_state(self) -> dict:
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self, state: dict) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except OSError as e:
            print(f"[ModCheck] failed to save state: {e}")

    # ---- public API ----------------------------------------------------------

    async def check_for_updates(self) -> list:
        """Compare current timestamps against the saved baseline. Returns the
        titles of any mod that has been updated since the last check."""
        ids = await self.get_workshop_ids()
        if not ids:
            print("[ModCheck] No Workshop IDs found in server ini — nothing to poll.")
            return []
        previous = self._load_state()
        current = await self._fetch_workshop_state(ids)
        if not current:
            print("[ModCheck] Failed to fetch mod data from Steam (no usable source).")
            return []

        updated = []
        for mod_id, info in current.items():
            was = previous.get(mod_id)
            if not was:
                continue  # newly tracked: adopt silently, don't announce
            base = was.get("time", 0)
            if base <= 0:
                continue  # no usable baseline
            if info["time"] > base:
                updated.append(info.get("title") or mod_id)

        if not updated:
            # Advance the baseline only when nothing is pending. If we saved here
            # with a pending update, a deferral would silently "consume" it and the
            # next poll would never re-detect it.
            self._save_state(current)
            print(f"[ModCheck] ✅ {len(current)} mod(s) current.")
        else:
            print(f"[ModCheck] 🚨 {len(updated)} update(s) pending: {', '.join(updated)}")
        return updated

    async def seed_state(self) -> bool:
        """Snapshot current timestamps as the baseline (called on server start /
        after a restart). Returns True if the baseline was written."""
        ids = await self.get_workshop_ids()
        if not ids:
            return False
        state = await self._fetch_workshop_state(ids)
        if state:
            self._save_state(state)
            print(f"[ModCheck] Seeded {len(state)} mod(s).")
            return True
        return False
