"""The dashboard's hourly traffic series (M21).

`hourly_series` groups by hour in the database, so it returns **only hours that have
records**. That is the right query — it ships a handful of rows instead of a day of
traffic — but it is the wrong thing to plot directly, in two ways that both mislead:

* one busy hour in a 24-hour window is a single category, and a bar chart sizes one
  category to the whole plot area, drawing a solid block.
* dropped gaps are drawn adjacent. Three scattered hours render as three neighbouring
  bars, which reads as continuous activity rather than three isolated bursts.

So the window is filled before it is plotted, and these tests pin that: what comes back
covers every hour asked for, in order, with zeros where nothing happened.
"""

from __future__ import annotations

import datetime as dt

from app.services.dashboard import bucket_for, fill_hourly_gaps


def hour(h: int) -> dt.datetime:
    return dt.datetime(2026, 8, 14, h, 0, tzinfo=dt.UTC)


class TestFillHourlyGaps:
    def test_a_single_busy_hour_becomes_the_whole_window(self) -> None:
        """One busy hour must not become a bar spanning the whole plot."""
        filled = fill_hourly_gaps(
            [{"hour": hour(13), "requests": 4, "tokens": 18}],
            since=hour(10),
            until=hour(14),
        )
        assert [p["requests"] for p in filled] == [0, 0, 0, 4, 0]
        assert [p["tokens"] for p in filled] == [0, 0, 0, 18, 0]
        assert len(filled) == 5, "every hour of the window, not just the busy one"

    def test_gaps_stay_gaps(self) -> None:
        """Two bursts an hour apart must not be drawn side by side."""
        filled = fill_hourly_gaps(
            [
                {"hour": hour(10), "requests": 3, "tokens": 30},
                {"hour": hour(12), "requests": 5, "tokens": 50},
            ],
            since=hour(10),
            until=hour(12),
        )
        assert [p["requests"] for p in filled] == [3, 0, 5]

    def test_an_idle_window_is_still_a_window(self) -> None:
        """No traffic is a shape, not an absence.

        Returning nothing would leave the caller unable to tell "idle" from "the query
        failed", which are the two things a dashboard most needs to distinguish.
        """
        filled = fill_hourly_gaps([], since=hour(9), until=hour(12))
        assert len(filled) == 4
        assert all(p["requests"] == 0 and p["tokens"] == 0 for p in filled)

    def test_the_result_is_ordered(self) -> None:
        """Chart.js plots in array order, so out-of-order input would plot time backwards."""
        filled = fill_hourly_gaps(
            [
                {"hour": hour(12), "requests": 1, "tokens": 10},
                {"hour": hour(10), "requests": 2, "tokens": 20},
            ],
            since=hour(10),
            until=hour(12),
        )
        assert [p["hour"] for p in filled] == [hour(10), hour(11), hour(12)]

    def test_records_outside_the_window_are_dropped(self) -> None:
        """A row older than `since` would otherwise stretch the axis past what was asked."""
        filled = fill_hourly_gaps(
            [
                {"hour": hour(8), "requests": 99, "tokens": 999},
                {"hour": hour(11), "requests": 2, "tokens": 20},
            ],
            since=hour(10),
            until=hour(12),
        )
        assert [p["requests"] for p in filled] == [0, 2, 0]

    def test_a_naive_timestamp_does_not_crash_the_dashboard(self) -> None:
        """Postgres returns tz-aware datetimes, but a driver or fixture may not.

        Comparing naive and aware datetimes raises `TypeError`, and the dashboard is the
        one page that must not 500 — it is where someone looks to find out what is wrong.
        """
        filled = fill_hourly_gaps(
            # Naive on purpose — that is the input under test. noqa, because the rule is
            # right everywhere else and this is the one place the absence matters.
            [{"hour": dt.datetime(2026, 8, 14, 11, 0), "requests": 7, "tokens": 70}],  # noqa: DTZ001
            since=hour(10),
            until=hour(12),
        )
        assert [p["requests"] for p in filled] == [0, 7, 0]


def day(d: int, h: int = 0) -> dt.datetime:
    return dt.datetime(2026, 8, d, h, 0, tzinfo=dt.UTC)


class TestDailyBuckets:
    """Past 7 days the chart has a bar per UTC day (E37)."""

    def test_the_bar_size_follows_the_range(self) -> None:
        assert bucket_for(1) == "hour"
        assert bucket_for(168) == "hour"
        assert bucket_for(169) == "day"
        assert bucket_for(2160) == "day"

    def test_every_day_of_the_window_with_zeros_between(self) -> None:
        filled = fill_hourly_gaps(
            [
                {"hour": day(2), "requests": 3, "tokens": 30},
                {"hour": day(5), "requests": 1, "tokens": 4},
            ],
            since=day(1, 15),
            until=day(6, 9),
            unit="day",
        )
        assert [p["hour"] for p in filled] == [day(d) for d in range(1, 7)]
        assert [p["requests"] for p in filled] == [0, 3, 0, 0, 1, 0]

    def test_a_ninety_day_window_is_ninety_one_bars_not_two_thousand(self) -> None:
        until = day(31, 10)
        filled = fill_hourly_gaps(
            [], since=until - dt.timedelta(hours=2160), until=until, unit="day"
        )
        # The first and last days are partial, so 90 days touch 91 calendar days.
        assert len(filled) == 91

    def test_a_row_in_another_zone_lands_on_its_utc_day(self) -> None:
        late = dt.datetime(2026, 8, 3, 1, 0, tzinfo=dt.timezone(dt.timedelta(hours=4)))
        filled = fill_hourly_gaps(
            [{"hour": late, "requests": 2, "tokens": 2}],
            since=day(1),
            until=day(3),
            unit="day",
        )
        # 01:00 at UTC+4 on the 3rd is 21:00 UTC on the 2nd.
        assert [p["requests"] for p in filled] == [0, 2, 0]


class TestMergeModelRows:
    """E98: rollups and the records after them added per model."""

    def test_latency_is_weighted_by_requests(self) -> None:
        from app.services.dashboard import merge_model_rows

        rolled = [
            {
                "model": "a",
                "requests": 3,
                "prompt_tokens": 30,
                "completion_tokens": 3,
                "avg_latency_ms": 100.0,
                "errors": 1,
                "fallbacks": 0,
            }
        ]
        recent = [
            {
                "model": "a",
                "requests": 1,
                "prompt_tokens": 10,
                "completion_tokens": 1,
                "avg_latency_ms": 500.0,
                "errors": 0,
                "fallbacks": 2,
                "ttft_p50_ms": 9.0,
            },
            {
                "model": "b",
                "requests": 9,
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "avg_latency_ms": 10.0,
                "errors": 0,
                "fallbacks": 0,
            },
        ]
        merged = merge_model_rows(rolled, recent)
        assert [r["model"] for r in merged] == ["b", "a"]
        assert merged[1] == {
            "model": "a",
            "requests": 4,
            "prompt_tokens": 40,
            "completion_tokens": 4,
            "errors": 1,
            "fallbacks": 2,
            "avg_latency_ms": 200.0,
        }
        assert merge_model_rows([], []) == []
