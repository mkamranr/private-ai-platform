"""Quiet hours for notification channels (expansion E42).

A channel can name a daily window, for example 22:00 to 07:00, when it sends only
CRITICAL alerts. Anything else that comes due in the window is held, not dropped: its
delivery waits until the window ends and is sent then, in order, with no attempt counted.
A test send from the console is never held.

The window is read in the channel's time zone, given as ``UTC``, a fixed offset such as
``+04:00``, or an IANA name such as ``Asia/Dubai``. A name needs the server's time zone
data, which a slim container may not carry, so a name the server cannot resolve is refused
when the channel is saved (with a hint to use an offset) rather than failing at 3 a.m.

Pure: no database, no clock of its own.
"""

from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_OFFSET = re.compile(r"^([+-])(\d{2}):(\d{2})$")


class QuietHoursError(ValueError):
    """A window or time zone that cannot be used; the message says why."""


def parse_time(value: str) -> dt.time:
    match = _HHMM.match(value.strip())
    if not match:
        raise QuietHoursError(f"{value!r} is not a time of day as HH:MM (00:00 to 23:59).")
    return dt.time(int(match.group(1)), int(match.group(2)))


def parse_zone(value: str) -> dt.tzinfo:
    name = value.strip()
    if name.upper() in {"UTC", "Z", "GMT"}:
        return dt.UTC
    offset = _OFFSET.match(name)
    if offset:
        hours, minutes = int(offset.group(2)), int(offset.group(3))
        if hours > 14 or minutes > 59:
            raise QuietHoursError(f"{value!r} is not a UTC offset between -14:00 and +14:00.")
        delta = dt.timedelta(hours=hours, minutes=minutes)
        return dt.timezone(-delta if offset.group(1) == "-" else delta)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise QuietHoursError(
            f"This server has no time zone data for {value!r}. Use a UTC offset such as "
            "+04:00, or UTC."
        ) from exc


def validate(start: str | None, end: str | None, zone: str | None) -> None:
    """Both times or neither; distinct; and a zone the server understands."""
    if (start is None) != (end is None):
        raise QuietHoursError("Quiet hours need both a start and an end, or neither.")
    if start is None or end is None:
        return
    if parse_time(start) == parse_time(end):
        raise QuietHoursError("Quiet hours cannot start and end at the same time.")
    parse_zone(zone or "UTC")


def quiet_until(
    start: str | None, end: str | None, zone: str | None, now: dt.datetime
) -> dt.datetime | None:
    """When the quiet window containing ``now`` ends (UTC), or None outside one.

    A window whose end is earlier than its start runs past midnight (22:00 to 07:00).
    """
    if not start or not end:
        return None
    tz = parse_zone(zone or "UTC")
    first, last = parse_time(start), parse_time(end)
    local = now.astimezone(tz)
    today = local.date()

    def at(day: dt.date, when: dt.time) -> dt.datetime:
        return dt.datetime.combine(day, when, tzinfo=tz)

    if first < last:
        window = (at(today, first), at(today, last))
        inside = window[0] <= local < window[1]
        ends = window[1]
    elif local.time() >= first:
        inside, ends = True, at(today + dt.timedelta(days=1), last)
    elif local.time() < last:
        inside, ends = True, at(today, last)
    else:
        inside, ends = False, local
    return ends.astimezone(dt.UTC) if inside else None
