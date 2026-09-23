"""features.py — runtime feature toggles for the bot's notification functions.

Cogs consult ``bot.features.is_enabled(<key>)`` before sending a notification so
an admin can turn a noisy/misbehaving function off at runtime with ``/disable``
and back on with ``/enable``.

State persists across restarts in a JSON file (``feature_state.json`` next to this
module by default; override with the ``FEATURE_STATE_PATH`` env var).
"""

import json
import os
from pathlib import Path

# Feature key -> human-readable name shown in /features and the /enable //disable
# dropdown. Keys are the stable identifiers (stored in feature_state.json).
FEATURES = {
    "join_leave": "Join & leave notifications",
    "deaths": "Death notifications",
    "siege_night": "Siege night notifications",
    "airdrops": "Airdrop & supply notifications",
    "restarts": "Restart notifications",
    "scheduled_restarts": "Scheduled restarts",
    "mod_updates": "Workshop mod update checker",
    "server_up_down": "Server up/down notifications",
    "chat_relay": "In-game \u2194 Discord chat relay",
    "status_dashboard": "Status dashboard (auto-updating panel)",
    "whitelist": "Whitelist application notifications",
}

# Old key names (pre-rename) -> current key. Used to migrate an existing
# feature_state.json so a disabled feature stays disabled across the rename.
_LEGACY_KEY_MAP = {
    "siege": "siege_night",
    "airdrop": "airdrops",
    "restart": "restarts",
    "mod_check": "mod_updates",
    "server_status": "server_up_down",
}


def _state_path() -> Path:
    env = os.getenv("FEATURE_STATE_PATH", "").strip()
    if env:
        return Path(env)
    return Path(__file__).parent / "feature_state.json"


class FeatureFlags:
    """A set of disabled feature keys, persisted to a JSON file."""

    def __init__(self, path: str | Path | None = None):
        self._path = Path(path) if path else _state_path()
        self._disabled: set = self._load()

    # ---- persistence ---------------------------------------------------------

    def _load(self) -> set:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            raw = data.get("disabled", [])
            # Migrate any legacy key names, then keep only known feature keys.
            migrated = {_LEGACY_KEY_MAP.get(k, k) for k in raw}
            return {k for k in migrated if k in FEATURES}
        except (OSError, ValueError, TypeError):
            # Missing or unreadable file -> start with everything enabled.
            return set()

    def _save(self) -> None:
        try:
            payload = {"disabled": sorted(self._disabled)}
            self._path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as e:
            print(f"[Features] Failed to save state to {self._path}: {e}")

    # ---- public API ----------------------------------------------------------

    def is_enabled(self, feature: str) -> bool:
        return feature not in self._disabled

    def is_disabled(self, feature: str) -> bool:
        return feature in self._disabled

    def disable(self, feature: str) -> None:
        if feature in FEATURES and feature not in self._disabled:
            self._disabled.add(feature)
            self._save()

    def enable(self, feature: str) -> None:
        if feature in FEATURES and feature in self._disabled:
            self._disabled.discard(feature)
            self._save()
