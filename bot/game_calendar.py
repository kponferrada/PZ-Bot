"""game_calendar.py — map Project Zomboid world-day numbers to calendar dates.

PZ tracks time as (year, month, day-of-month) plus a "world age" day counter.
The Siege Night bridge reports `eventDay` (current world day) and `nextSiegeDay`
(next siege night's world day) on the same world-age counter. Their *difference*
is the number of days until the next siege — anchoring that difference to the
world's current in-game date yields a real calendar date, independent of any
offset between the two counters.
"""

from datetime import date, timedelta

_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def siege_date_string(world: dict | None, siege: dict | None) -> str | None:
    """Return the in-game calendar date of the next siege night (e.g. "Nov 5, 2026"),
    or None if it can't be determined from the given world/siege data.

    `world` needs `year`, `month` (0-indexed) and `day` (0-indexed day-of-month);
    `siege` needs `eventDay` and `nextSiegeDay`.
    """
    if not world or not siege:
        return None
    next_day = siege.get("nextSiegeDay")
    event_day = siege.get("eventDay")
    if next_day is None or event_day is None:
        return None
    year = world.get("year")
    month = world.get("month")
    day = world.get("day")
    if year is None or month is None or day is None:
        return None
    try:
        current = date(int(year), int(month) + 1, int(day) + 1)
        target = current + timedelta(days=int(next_day) - int(event_day))
    except (ValueError, OverflowError, TypeError):
        return None
    mn = _MONTH_NAMES[target.month - 1][:3] if 1 <= target.month <= 12 else "?"
    return f"{mn} {target.day}, {target.year}"


# --- Real-world time of the next siege ------------------------------------------

def game_minutes(world: dict | None) -> int | None:
    """The in-game clock as minutes since 1 Jan of year 1 (for differences)."""
    if not world:
        return None
    try:
        d = date(int(world["year"]), int(world["month"]) + 1, int(world["day"]) + 1)
        return d.toordinal() * 1440 + int(world.get("hour", 0)) * 60 + int(world.get("minutes", 0))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


class GameClockRate:
    """How fast the in-game clock runs, measured from the world status samples.

    Keeps (real time, game minutes) samples from the last `window` seconds. A
    jump backwards or a stall (server restart, world paused) starts over, so
    the rate always describes the clock as it is running now."""

    def __init__(self, window: float = 900.0, min_span: float = 120.0):
        self.window, self.min_span = window, min_span
        self.samples: list = []

    def record(self, real: float, world: dict | None) -> None:
        game = game_minutes(world)
        if game is None:
            return
        if self.samples and (game < self.samples[-1][1] or game - self.samples[-1][1] > 24 * 60):
            self.samples = []
        if self.samples and game == self.samples[-1][1] and real - self.samples[-1][0] > 300:
            self.samples = []  # clock stopped for 5 min: paused, not slow
        self.samples.append((real, game))
        self.samples = [s for s in self.samples if s[0] >= real - self.window]

    def rate(self) -> float | None:
        """Game minutes per real second, or None until enough has been seen."""
        if len(self.samples) < 2:
            return None
        (r0, g0), (r1, g1) = self.samples[0], self.samples[-1]
        if r1 - r0 < self.min_span or g1 <= g0:
            return None
        return (g1 - g0) / (r1 - r0)


def siege_real_time(world: dict | None, siege: dict | None, start_hour: int,
                    rate: float | None, now: float) -> float | None:
    """Epoch seconds when the next siege should start in the real world, or None.

    The siege starts at `start_hour`:00 in-game on `nextSiegeDay` (counted on the
    same world-day counter as `eventDay`). Assumes the clock keeps its current
    speed (a server that pauses when empty will be later)."""
    if not rate or not world or not siege:
        return None
    if str(siege.get("phase", "")).lower() in ("active", "dawn"):
        return None
    try:
        days = int(siege["nextSiegeDay"]) - int(siege["eventDay"])
        now_min = int(world.get("hour", 0)) * 60 + int(world.get("minutes", 0))
    except (KeyError, TypeError, ValueError):
        return None
    until = days * 1440 + int(start_hour) * 60 - now_min
    if until < 0:
        return None
    return now + until / rate
