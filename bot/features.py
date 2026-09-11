"""features.py — runtime feature toggles for notification features.

Cogs consult ``bot.features.is_enabled(<key>)`` before sending a notification so
an admin can turn a noisy/misbehaving feature off at runtime with ``/disable`` and
back on with ``/enable``. Features are enabled unless explicitly disabled (state is
in-memory only and resets on bot restart).
"""

FEATURES = {
    "join_leave": "Player join/leave notifications",
    "deaths": "Player death notifications",
    "horde": "Horde night notifications (Discord + in-game)",
    "airdrop": "Air drop & supply event notifications (Discord + in-game)",
    "restart": "Restart & mod-update notifications (Discord + in-game save/kick)",
    "server_status": "Server up/down notifications",
    "chat_relay": "In-game \u2194 Discord chat relay",
}


class FeatureFlags:
    """A set of disabled feature keys. A feature is enabled unless present here."""

    def __init__(self):
        self._disabled: set = set()

    def is_enabled(self, feature: str) -> bool:
        return feature not in self._disabled

    def is_disabled(self, feature: str) -> bool:
        return feature in self._disabled

    def disable(self, feature: str) -> None:
        self._disabled.add(feature)

    def enable(self, feature: str) -> None:
        self._disabled.discard(feature)
