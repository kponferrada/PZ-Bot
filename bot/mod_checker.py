"""mod_checker.py — Steam Workshop update checker (migrated from Jeeves's ModChecker).

Compares each subscribed Workshop item's `time_updated` against a saved baseline
to detect updates, so the bot can restart the server to apply them.

Source order (matching Jeeves):
  1. Keyless ``ISteamRemoteStorage/GetPublishedFileDetails`` — public items only;
     it answers ``result != 1`` with no data for unlisted items.
  2. Keyed ``IPublishedFileService/GetDetails`` — CAN see unlisted items, but
     needs a ``STEAM_API_KEY``. This is a live query, so an unlisted mod is
     detected on the same cadence as a public one.
"""

import os
import json
from pathlib import Path

import aiohttp

import server_config

_KEYLESS_URL = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
_KEYED_URL = "https://api.steampowered.com/IPublishedFileService/GetDetails/v1/"


class ModChecker:
    def __init__(self, bot):
        self.bot = bot
        self._key = (getattr(bot.config, "STEAM_API_KEY", "")
                     or os.getenv("STEAM_API_KEY", "") or "").strip()
        state_path = getattr(bot.config, "MOD_UPDATE_STATE_PATH", "") or "mod_update_state.json"
        self._state_path = Path(state_path)
        if not self._state_path.is_absolute():
            self._state_path = Path(__file__).parent / self._state_path

    # ---- workshop ids --------------------------------------------------------

    async def get_workshop_ids(self) -> list:
        return await server_config.read_workshop_items(self.bot)

    # ---- Steam API sources ---------------------------------------------------

    async def _fetch_keyless(self, ids: list) -> dict:
        """Keyless lookup. Returns {id: {time, title, source}} for items it can
        see; unlisted items (result != 1) are left out entirely."""
        if not ids:
            return {}
        data = {"itemcount": str(len(ids)), "format": "json"}
        for i, item_id in enumerate(ids):
            data[f"publishedfileids[{i}]"] = item_id
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(_KEYLESS_URL, data=data,
                                        timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    payload = await resp.json()
        except Exception as e:
            print(f"[ModCheck] Steam API error: {e}")
            return {}
        state = {}
        for item in payload.get("response", {}).get("publishedfiledetails", []):
            item_id = str(item.get("publishedfileid"))
            if item.get("result") != 1:
                print(f"[ModCheck] Steam API cannot see {item_id} "
                      f"(result={item.get('result')}); will try keyed API.")
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
        """
        if not ids or not self._key:
            return {}
        params = {"key": self._key}
        for i, item_id in enumerate(ids):
            params[f"publishedfileids[{i}]"] = item_id
        params["includetags"] = "false"
        params["includeadditionalpreviews"] = "false"
        params["includechildren"] = "false"
        out = {}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(_KEYED_URL, params=params,
                                       timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        print(f"[ModCheck] keyed API refused lookup "
                              f"(HTTP {resp.status}); skipping unlisted ids.")
                        return {}
                    payload = await resp.json()
        except Exception as e:
            print(f"[ModCheck] keyed API error ({type(e).__name__}); "
                  f"skipping unlisted ids.")
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
        if out:
            print(f"[ModCheck] keyed API resolved {len(out)} unlisted id(s): "
                  f"{', '.join(sorted(out))}")
        return out

    async def _fetch_workshop_state(self, ids: list) -> dict:
        state = await self._fetch_keyless(ids)
        missing = [i for i in ids if i not in state]
        if missing:
            state.update(await self._fetch_keyed(missing))
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
            return []
        previous = self._load_state()
        current = await self._fetch_workshop_state(ids)
        if not current:
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

        self._save_state(current)
        return updated

    async def seed_state(self) -> None:
        """Snapshot current timestamps as the baseline (called on server start)."""
        ids = await self.get_workshop_ids()
        if not ids:
            return
        state = await self._fetch_workshop_state(ids)
        if state:
            self._save_state(state)
            print(f"[ModCheck] seeded {len(state)} mod(s).")
