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
