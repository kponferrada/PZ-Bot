"""features.py — runtime feature toggles for notification features.

Cogs consult ``bot.features.is_enabled(<key>)`` before sending a notification so
an admin can turn a noisy/misbehaving feature off at runtime with ``/disable`` and
back on with ``/enable``.

State persists across restarts in a JSON file (``feature_state.json`` next to this
module by default; override with the ``FEATURE_STATE_PATH`` env var).
"""

import json
import os
from pathlib import Path

FEATURES = {
    "join_leave": "Player join/leave notifications",
    "deaths": "Player death notifications",
    "siege": "Siege night notifications (Discord + in-game)",
    "airdrop": "Air drop & supply event notifications (Discord + in-game)",
    "restart": "Restart & mod-update notifications (Discord + in-game save/kick)",
    "mod_check": "Bot-driven workshop mod update checker",
    "server_status": "Server up/down notifications",
    "chat_relay": "In-game \u2194 Discord chat relay",
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
            # Keep only known feature keys (ignore stale/unknown entries).
            return {k for k in raw if k in FEATURES}
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
