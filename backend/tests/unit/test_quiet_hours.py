"""Quiet-hours windows (E42), without a database."""

from __future__ import annotations

import datetime as dt

import pytest

from app.services.quiet_hours import QuietHoursError, parse_zone, quiet_until, validate


def utc(h: int, m: int = 0, day: int = 17) -> dt.datetime:
    return dt.datetime(2026, 11, day, h, m, tzinfo=dt.UTC)


class TestWindow:
    def test_a_daytime_window(self) -> None:
        assert quiet_until("12:00", "13:30", "UTC", utc(12, 15)) == utc(13, 30)
        assert quiet_until("12:00", "13:30", "UTC", utc(13, 30)) is None  # end is exclusive
        assert quiet_until("12:00", "13:30", "UTC", utc(11, 59)) is None

    def test_a_window_past_midnight(self) -> None:
        # 22:00 to 07:00: late evening ends tomorrow morning, early morning ends today.
        assert quiet_until("22:00", "07:00", "UTC", utc(23)) == utc(7, day=18)
        assert quiet_until("22:00", "07:00", "UTC", utc(3)) == utc(7)
        assert quiet_until("22:00", "07:00", "UTC", utc(12)) is None

    def test_read_in_the_channels_zone(self) -> None:
        # 22:00 to 07:00 at +04:00 is 18:00 to 03:00 UTC.
        assert quiet_until("22:00", "07:00", "+04:00", utc(19)) == utc(3, day=18)
        assert quiet_until("22:00", "07:00", "+04:00", utc(4)) is None

    def test_an_iana_zone_follows_daylight_saving(self) -> None:
        try:
            parse_zone("Europe/London")
        except QuietHoursError:
            pytest.skip("no time zone data on this machine")
        summer = dt.datetime(2026, 7, 1, 21, 30, tzinfo=dt.UTC)  # 22:30 BST
        assert quiet_until("22:00", "07:00", "Europe/London", summer) == dt.datetime(
            2026, 7, 2, 6, 0, tzinfo=dt.UTC
        )

    def test_no_window_means_never_quiet(self) -> None:
        assert quiet_until(None, None, "UTC", utc(3)) is None


class TestValidation:
    @pytest.mark.parametrize(
        ("start", "end", "zone", "why"),
        [
            ("22:00", None, "UTC", "both"),
            ("22:00", "22:00", "UTC", "same time"),
            ("24:00", "07:00", "UTC", "HH:MM"),
            ("7:00", "08:00", "UTC", "HH:MM"),
            ("22:00", "07:00", "+15:00", "offset"),
            ("22:00", "07:00", "Mars/Olympus_Mons", "UTC offset such as"),
        ],
    )
    def test_refused(self, start, end, zone, why) -> None:
        with pytest.raises(QuietHoursError, match=why):
            validate(start, end, zone)

    def test_accepted(self) -> None:
        validate(None, None, "UTC")
        validate("22:00", "07:00", "-03:30")
        validate("00:00", "06:00", "utc")
