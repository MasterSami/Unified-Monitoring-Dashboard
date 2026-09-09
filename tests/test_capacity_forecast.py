"""Capacity forecasting: the packed-string parsers and the trend maths.

The forecast is a straight line, so the value of these tests is not that the
arithmetic works — numpy's does — but that the code refuses to answer when a
straight line would mislead: a resized volume, a scattered series, a host that
stopped reporting. Each of those has cost somebody a wrong decision at 3 a.m.
somewhere, which is why they are gates rather than footnotes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.capacity_history import (
    normalize_subject,
    parse_dynatrace_disks,
    parse_zabbix_drive_cell,
    parse_zabbix_drive_pair,
    series_for_host,
)
from app.config import get_settings
from app.forecast import (
    CRITICAL,
    INSUFFICIENT,
    NOISY,
    OK,
    WARNING,
    WATCH,
    describe,
    fit_series,
)

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


# --- Packed-string parsers --------------------------------------------------


class TestZabbixDriveParser:
    """``VM Space For Each Drive`` — ``"<label> : <gb>"``, several label shapes."""

    @pytest.mark.parametrize(
        "cell, expected",
        [
            # The plain label the report writes when _fs_label resolved cleanly.
            ("/var/lib : 2028.7", ("/var/lib", 2028.7)),
            # The label that kept its Zabbix item-name wrapper, which is what
            # the report actually emits for a "FS [x]: Space" item name. The
            # value is after the LAST colon, not the first.
            ("FS [/var/lib]: Space : 2028.7", ("/var/lib", 2028.7)),
            ("Filesystem [/opt]: Total space : 500", ("/opt", 500.0)),
            # Windows: the drive letter's own colon must not split the cell.
            ("FS [C:]: Space : 114.4", ("C:", 114.4)),
            ("C: : 114.4", ("C:", 114.4)),
            # Tolerated variations.
            ("  /data : 0.0  ", ("/data", 0.0)),
            ("/data: 12", ("/data", 12.0)),
            ("/eu : 1,5", ("/eu", 1.5)),
        ],
    )
    def test_parses_every_label_shape(self, cell, expected):
        assert parse_zabbix_drive_cell(cell) == expected

    @pytest.mark.parametrize(
        "cell", ["N/A", "", "   ", None, 42, "no number here", ": 12"]
    )
    def test_rejects_anything_that_is_not_a_labelled_number(self, cell):
        assert parse_zabbix_drive_cell(cell) is None

    def test_pairs_the_two_report_columns(self):
        assert parse_zabbix_drive_pair(
            "FS [/var]: Space : 500.0", "FS [/var]: Used space : 431.0"
        ) == ("/var", 431.0, 500.0)

    def test_refuses_to_pair_two_different_mounts(self):
        """A used figure from another drive would give a meaningless ratio."""
        assert parse_zabbix_drive_pair("/var : 500.0", "/opt : 431.0") is None
        assert parse_zabbix_drive_pair("/var : 500.0", "N/A") is None


class TestDynatraceDiskParser:
    """``Disks (used / total GB)`` — ``"C:\\: 80.1 / 114.4 GB | ..."``."""

    def test_parses_the_packed_multi_drive_cell(self):
        assert parse_dynatrace_disks(
            r"C:\: 80.1 / 114.4 GB | D:\: 61.7 / 150.0 GB"
        ) == [("C:", 80.1, 114.4), ("D:", 61.7, 150.0)]

    def test_handles_unix_mounts_and_missing_unit(self):
        assert parse_dynatrace_disks("/var/lib: 12.5 / 100.0 GB | /: 4 / 50") == [
            ("/var/lib", 12.5, 100.0),
            ("/", 4.0, 50.0),
        ]

    def test_one_bad_segment_does_not_cost_the_others(self):
        assert parse_dynatrace_disks(
            r"C:\: 80.1 / 114.4 GB | garbage | E:\: 1.0 / 2.0 GB"
        ) == [("C:", 80.1, 114.4), ("E:", 1.0, 2.0)]

    @pytest.mark.parametrize("cell", ["", "   ", None, 42, "no disks here"])
    def test_empty_and_unparseable_cells_yield_nothing(self, cell):
        assert parse_dynatrace_disks(cell) == []

    def test_trailing_backslash_is_normalized_away(self):
        """``C:\\`` and ``C:`` are the same volume and must share one series."""
        assert normalize_subject(r"C:\ ") == "C:"
        assert normalize_subject("FS [C:]: Space") == "C:"
        assert normalize_subject("/") == "/"


def test_series_for_host_reads_structured_and_packed_drive_data():
    """Both shapes reach the history table as the same per-mount series."""
    structured = series_for_host(
        {
            "cpu_pct": 40.0,
            "mem_pct": 50.0,
            "metrics": {
                "cores": 8, "cpu_used_cores": 3.2,
                "mem_total_gb": 64, "mem_used_gb": 32,
                "filesystems": [
                    {"subject": "FS [/var]", "used_gb": 90.0, "total_gb": 100.0}
                ],
            },
        }
    )
    assert ("disk", "/var", 90.0, 100.0, 90.0) in structured
    assert ("memory", "", 32, 64, 50.0) in structured

    packed = series_for_host(
        {"metrics": {"disks_packed": r"C:\: 80.0 / 100.0 GB"}}
    )
    assert packed == [("disk", "C:", 80.0, 100.0, 80.0)]


# --- Trend maths ------------------------------------------------------------


def _series(
    days: int,
    start: float,
    slope: float,
    *,
    total: float | None = 500.0,
    noise: float = 0.0,
    resize_on: int | None = None,
    total_before: float | None = None,
    pct_before_end: float = 88.0,
    slope_before: float = 0.9,
):
    """Build ``(stamps, pcts, totals)`` for a synthetic daily series.

    ``resize_on`` splits the window: days before it describe a smaller volume
    (``total_before``) that was nearly full, days from it onward describe the
    current one starting at ``start``.
    """
    stamps, pcts, totals = [], [], []
    for day in range(days):
        stamps.append(NOW - timedelta(days=days - 1 - day))
        if resize_on is not None and day < resize_on:
            pcts.append(pct_before_end - (resize_on - 1 - day) * slope_before)
            totals.append(total_before)
        else:
            offset = day if resize_on is None else day - resize_on
            wobble = noise if day % 2 else -noise
            pcts.append(start + offset * slope + wobble)
            totals.append(total)
    return stamps, pcts, totals


class TestSlopeAndEta:
    """A steady climb should produce the slope and the date it implies."""

    def test_rising_series_gives_slope_eta_and_critical_band(self):
        fit = fit_series(*_series(30, 59.0, 0.8))
        assert fit.slope_pct_per_day == pytest.approx(0.8, abs=1e-6)
        assert fit.r_squared == pytest.approx(1.0)
        assert fit.current_pct == pytest.approx(82.2, abs=0.01)
        # (90 - 82.2) / 0.8 = 9.75 days -> inside the 14-day critical band.
        assert fit.days_to_threshold_90 == pytest.approx(9.8, abs=0.1)
        assert fit.days_to_full == pytest.approx(22.3, abs=0.1)
        assert fit.classification == CRITICAL

    @pytest.mark.parametrize(
        "slope, start, expected",
        [
            # The ETA runs from where the series ENDS (start + 29 * slope),
            # so each case is chosen for the current value it lands on.
            (0.80, 59.00, CRITICAL),  # ends 82.2 -> ~10 days
            (0.25, 76.25, WARNING),   # ends 83.5 -> ~26 days
            (0.12, 79.68, WATCH),     # ends 83.2 -> ~57 days
            (0.02, 55.00, OK),        # ends 55.6 -> ~1700 days
        ],
    )
    def test_bands_follow_time_to_threshold(self, slope, start, expected):
        assert fit_series(*_series(30, start, slope)).classification == expected

    def test_flat_series_is_ok_and_perfectly_described(self):
        """No variance to explain means the line is exact, not unreliable."""
        fit = fit_series(*_series(30, 62.0, 0.0))
        assert fit.classification == OK
        assert fit.r_squared == pytest.approx(1.0)
        assert fit.days_to_threshold_90 is None
        assert fit.days_to_full is None

    def test_shrinking_series_gets_no_eta(self):
        fit = fit_series(*_series(30, 55.0, -0.3))
        assert fit.classification == OK
        assert fit.slope_pct_per_day == pytest.approx(-0.3, abs=1e-6)
        assert fit.days_to_threshold_90 is None

    def test_series_already_past_the_threshold_reads_as_due_now(self):
        """Past 90% there is nothing left to forecast — report the present."""
        fit = fit_series(*_series(30, 80.0, 0.5))   # ends at 94.5%
        assert fit.days_to_threshold_90 == 0.0
        assert fit.classification == CRITICAL
        assert describe_row(fit).startswith("Already 94.5%")

    def test_an_eta_beyond_the_horizon_is_dropped_rather_than_shown(self):
        """A date ten years out is arithmetic, not a forecast."""
        fit = fit_series(*_series(30, 40.0, 0.0001))
        assert fit.classification == OK
        assert fit.days_to_threshold_90 is None


class TestAlreadyFull:
    """A volume at or over the line is a fact about now, not a forecast.

    Found in production: a 420 GB volume pinned at 99.99% reported "100.0% ·
    0.00%/day · 13 days to full". Both operands had been rounded away by the
    display, so the row read as (100 − 100) ÷ 0 = 13.
    """

    def _pinned_at_ceiling(self):
        """99.966% creeping by 0.0008 %/day — the production series."""
        stamps = [NOW - timedelta(days=29 - d) for d in range(30)]
        return stamps, [99.966 + d * 0.0008 for d in range(30)], [420.0] * 30

    def test_a_volume_at_the_ceiling_is_full_now_not_full_later(self):
        fit = fit_series(*self._pinned_at_ceiling())
        assert fit.current_pct == pytest.approx(99.99, abs=0.01)
        assert fit.classification == CRITICAL
        # The bug: 0.01 percentage points divided by a slope of 0.0008.
        assert fit.days_to_full == 0.0
        assert fit.days_to_threshold_90 == 0.0
        assert "no space left" in describe_row(fit)

    def test_headroom_below_the_floor_never_produces_a_date(self):
        """Whatever the slope, the last half-percent is not worth dividing."""
        for slope in (0.0001, 0.0008, 0.01, 0.5):
            stamps = [NOW - timedelta(days=29 - d) for d in range(30)]
            pcts = [99.7 + d * slope for d in range(30)]
            fit = fit_series(stamps, pcts, [420.0] * 30)
            assert fit.days_to_full == 0.0, slope

    def test_a_full_volume_that_stopped_growing_is_still_critical(self):
        """Flat at 100% must not read as "ok" — it is the most urgent row."""
        stamps = [NOW - timedelta(days=29 - d) for d in range(30)]
        fit = fit_series(stamps, [100.0] * 30, [420.0] * 30)
        assert fit.classification == CRITICAL
        assert fit.slope_pct_per_day <= 0        # not growing...
        assert "no space left" in describe_row(fit)   # ...but still full

    def test_a_volume_over_the_line_but_draining_is_reported_honestly(self):
        stamps = [NOW - timedelta(days=29 - d) for d in range(30)]
        fit = fit_series(stamps, [100.0 - d * 0.05 for d in range(30)], [420.0] * 30)
        assert fit.classification == CRITICAL
        assert fit.days_to_full is None          # it is emptying, not filling
        assert "not growing" in describe_row(fit)

    def test_a_volume_over_the_line_and_rising_says_so(self):
        stamps = [NOW - timedelta(days=29 - d) for d in range(30)]
        fit = fit_series(stamps, [88.0 + d * 0.1 for d in range(30)], [420.0] * 30)
        assert fit.classification == CRITICAL
        assert "still rising" in describe_row(fit)

    def test_a_long_dated_full_estimate_is_dropped_from_a_critical_row(self):
        """"Full in 7 years" beside an already-critical volume is noise."""
        stamps = [NOW - timedelta(days=29 - d) for d in range(30)]
        fit = fit_series(stamps, [90.5 + d * 0.002 for d in range(30)], [420.0] * 30)
        assert fit.classification == CRITICAL
        assert fit.days_to_full is None

    def test_below_the_line_is_untouched_by_any_of_this(self):
        fit = fit_series(*_series(30, 59.0, 0.8))
        assert fit.classification == CRITICAL
        assert fit.days_to_threshold_90 == pytest.approx(9.8, abs=0.1)
        assert fit.days_to_full == pytest.approx(22.3, abs=0.1)


class TestRSquaredSuppression:
    """Below the R² floor the slope survives but the dates do not."""

    def test_scattered_rising_series_is_noisy_with_no_dates(self):
        fit = fit_series(*_series(30, 50.0, 0.35, noise=9.0))
        assert fit.classification == NOISY
        assert fit.r_squared < 0.3
        assert fit.slope_pct_per_day is not None  # the trend is still reported
        assert fit.days_to_threshold_90 is None
        assert fit.days_to_full is None
        assert "too scattered" in fit.reason

    def test_the_same_slope_with_clean_data_does_get_dates(self):
        """Only the scatter differs, so only the scatter can explain the change."""
        fit = fit_series(*_series(30, 50.0, 0.35, noise=0.0))
        assert fit.r_squared >= 0.3
        assert fit.classification != NOISY
        assert fit.days_to_threshold_90 is not None

    def test_the_floor_is_configurable(self, monkeypatch):
        settings = get_settings().model_copy(update={"forecast_min_r_squared": 0.99})
        fit = fit_series(*_series(30, 50.0, 0.35, noise=1.2), settings=settings)
        assert fit.classification == NOISY

    def test_a_noisy_but_falling_series_is_ok_not_noisy(self):
        """With no date to suppress, confidence in the slope is beside the point."""
        fit = fit_series(*_series(30, 70.0, -0.4, noise=9.0))
        assert fit.classification == OK
        assert fit.days_to_threshold_90 is None

    def test_a_flat_jittery_series_is_ok_not_noisy(self):
        """Memory on an idle host: scattered, but going nowhere for centuries.

        Jitter around a flat line leaves a slope of a thousandth of a percent
        a day, which crosses 90% some time next century. Calling that "noisy"
        would fill the page with rows that need no attention, so the horizon
        check runs before the R² check: with no date worth giving, how well the
        line fits is beside the point.
        """
        fit = fit_series(*_series(30, 55.0, 0.0, noise=2.0))
        assert fit.classification == OK
        assert fit.days_to_threshold_90 is None

    def test_the_r_squared_check_still_applies_within_the_horizon(self):
        """The same scatter, on a series that does cross 90% soon, is noisy."""
        fit = fit_series(*_series(30, 60.0, 0.4, noise=9.0))
        assert fit.classification == NOISY
        assert fit.days_to_threshold_90 is None


class TestResizeWindow:
    """A volume that changed size mid-window is fitted only from the change on."""

    def test_only_samples_after_the_resize_are_used(self):
        # 100 GB filling to 88%, extended to 400 GB on day 15, then +1.2 %/day.
        fit = fit_series(
            *_series(30, 22.0, 1.2, total=400.0, resize_on=15, total_before=100.0)
        )
        assert fit.sample_count == 15               # not 30
        assert fit.slope_pct_per_day == pytest.approx(1.2, abs=1e-6)
        assert fit.total_value == pytest.approx(400.0)
        assert fit.classification == WARNING
        assert "resize" in fit.reason

    def test_fitting_across_the_resize_would_have_inverted_the_trend(self):
        """The bug this guards: the cliff reads as a steep, reassuring fall."""
        stamps, pcts, totals = _series(
            30, 22.0, 1.2, total=400.0, resize_on=15, total_before=100.0
        )
        # Same percentages, but with the size change hidden from the fitter.
        blind = fit_series(stamps, pcts, [None] * len(totals))
        assert blind.slope_pct_per_day < 0          # "draining", and wrong
        assert blind.days_to_threshold_90 is None
        # With the sizes present, the real climb is recovered.
        assert fit_series(stamps, pcts, totals).slope_pct_per_day > 0

    def test_a_growth_within_tolerance_is_not_treated_as_a_resize(self):
        """Reporting jitter of a percent or two must not truncate the window."""
        stamps, pcts, totals = _series(30, 60.0, 0.5)
        totals = [t * (1.02 if i % 2 else 1.0) for i, t in enumerate(totals)]
        assert fit_series(stamps, pcts, totals).sample_count == 30

    def test_a_resize_too_recent_to_fit_is_reported_as_such(self):
        fit = fit_series(
            *_series(30, 22.0, 1.2, total=400.0, resize_on=26, total_before=100.0)
        )
        assert fit.classification == INSUFFICIENT
        assert "resized" in fit.reason


class TestQualityGates:
    """Series that cannot support a trend are skipped with a stated reason."""

    def test_too_few_points(self):
        fit = fit_series(*_series(6, 50.0, 1.0))
        assert fit.classification == INSUFFICIENT
        assert "6 daily point" in fit.reason
        assert fit.days_to_threshold_90 is None

    def test_span_too_short_even_with_enough_points(self):
        """Twelve samples inside three days say nothing about next month."""
        stamps = [NOW - timedelta(hours=6 * i) for i in range(12)]
        fit = fit_series(stamps, [50.0 + i for i in range(12)], [500.0] * 12)
        assert fit.classification == INSUFFICIENT
        assert "spans" in fit.reason or "daily point" in fit.reason

    def test_zero_sized_volume(self):
        fit = fit_series(*_series(30, 50.0, 0.5, total=0.0))
        assert fit.classification == INSUFFICIENT
        assert "size is zero" in fit.reason

    def test_a_host_that_is_not_reporting_is_not_fitted(self):
        fit = fit_series(
            *_series(30, 59.0, 0.8), host_ok=False, host_reason="host status is unknown"
        )
        assert fit.classification == INSUFFICIENT
        assert fit.reason == "host status is unknown"

    def test_percentage_only_templates_still_forecast(self):
        """Zabbix templates that report pused and no absolutes are common."""
        stamps, pcts, _ = _series(30, 59.0, 0.8)
        fit = fit_series(stamps, pcts, [None] * 30)
        assert fit.classification == CRITICAL
        assert fit.total_value is None


def test_daily_resampling_stops_a_busy_day_from_pulling_the_line():
    """Twenty samples on one day must weigh the same as one on every other."""
    stamps, pcts, totals = [], [], []
    for day in range(20):
        stamps.append(NOW - timedelta(days=19 - day))
        pcts.append(50.0 + day * 0.5)
        totals.append(500.0)
    fit_even = fit_series(stamps, pcts, totals)

    # The same series, but the first day was polled twenty times at a low value.
    for _ in range(20):
        stamps.append(NOW - timedelta(days=19))
        pcts.append(50.0)
        totals.append(500.0)
    fit_lumpy = fit_series(stamps, pcts, totals)

    assert fit_lumpy.sample_count == fit_even.sample_count == 20
    assert fit_lumpy.slope_pct_per_day == pytest.approx(
        fit_even.slope_pct_per_day, abs=1e-9
    )


def describe_row(fit):
    """Render a :class:`SeriesFit` through the same summary the UI uses."""

    class _Row:
        classification = fit.classification
        reason = fit.reason
        r_squared = fit.r_squared
        slope_pct_per_day = fit.slope_pct_per_day
        days_to_threshold_90 = fit.days_to_threshold_90
        days_to_full = fit.days_to_full
        current_pct = fit.current_pct

    return describe(_Row())


@pytest.mark.parametrize(
    "series, expected",
    [
        (_series(30, 59.0, 0.8), "At current trend: 90% in ~10 days"),
        (_series(30, 45.0, -0.3), "At current trend: stable or shrinking"),
        (_series(6, 50.0, 1.0), "No forecast — only 6 daily point(s); needs 10"),
        (_series(30, 50.0, 0.35, noise=9.0), None),  # noisy -> mentions R²
    ],
)
def test_plain_english_summary(series, expected):
    text = describe_row(fit_series(*series))
    if expected is None:
        assert "too scattered" in text
    else:
        assert text == expected
