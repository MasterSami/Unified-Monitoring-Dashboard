"""SiteScope multi-instance auto-load: config parsing + scheduler job."""

from __future__ import annotations

from pathlib import Path

from app.config import Settings


def test_demo_map_parses_windows_paths_and_multiple_instances():
    s = Settings(
        mock_mode=False,
        sitescope_demo_files=(
            r"SiteScope-141=D:\umd\sis141.tsv;"
            r"SiteScope-140=D:\umd\sis140.tsv;"
            r"SiteScope-34=D:\umd\sis34.tsv"
        ),
    )
    assert s.sitescope_demo_map == [
        ("SiteScope-141", r"D:\umd\sis141.tsv"),
        ("SiteScope-140", r"D:\umd\sis140.tsv"),
        ("SiteScope-34", r"D:\umd\sis34.tsv"),
    ]


def test_demo_map_merges_legacy_single_and_dedupes():
    s = Settings(
        mock_mode=False,
        sitescope_demo_file=r"C:\a\sis141.tsv",
        sitescope_demo_instance="SiteScope-141",
        sitescope_demo_files=r"SiteScope-141=C:\dup\again.tsv;SiteScope-140=C:\a\sis140.tsv",
    )
    # legacy entry wins for SiteScope-141; the duplicate instance is dropped.
    assert s.sitescope_demo_map == [
        ("SiteScope-141", r"C:\a\sis141.tsv"),
        ("SiteScope-140", r"C:\a\sis140.tsv"),
    ]


def test_demo_map_empty_by_default():
    assert Settings(mock_mode=False).sitescope_demo_map == []


def test_shadowed_entries_are_reported_not_silently_dropped():
    """Stale copies of the same instance decide the winner by ordering alone.

    A list that has grown several paths per server looks like it configures all
    of them; it does not. The losers are surfaced so a moved file is visible
    rather than merely inactive.
    """
    s = Settings(
        mock_mode=False,
        sitescope_demo_files=(
            r"SiteScope-141=C:\old\sis141.tsv;"
            r"SiteScope-141=D:\umd\sis141.tsv;"
            r"SiteScope-34=app\collectors\SIS\sis34.tsv"
        ),
    )
    assert s.sitescope_demo_map == [
        ("SiteScope-141", r"C:\old\sis141.tsv"),
        ("SiteScope-34", r"app\collectors\SIS\sis34.tsv"),
    ]
    assert s.sitescope_demo_shadowed == [("SiteScope-141", r"D:\umd\sis141.tsv")]


def test_a_missing_file_records_a_failed_run_instead_of_vanishing(monkeypatch):
    """A moved .tsv must show as a broken source, not as an absent one.

    Only logging the problem made the whole instance disappear from the
    dashboard, which reads as "SiteScope has no data" rather than "SiteScope
    cannot find its file".
    """
    from app import scheduler

    recorded: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        scheduler, "_record_feed_failure",
        lambda platform, instance, reason: recorded.append(
            (platform, instance, reason)
        ),
    )

    scheduler._load_sitescope_file("SiteScope-34", r"C:\nope\sis34.tsv")

    assert len(recorded) == 1
    platform, instance, reason = recorded[0]
    assert platform == "sitescope"
    assert instance == "SiteScope-34"
    assert r"C:\nope\sis34.tsv" in reason


def test_autoload_job_loads_every_instance(tmp_path, monkeypatch):
    # Two redacted files -> two SiteScope instances loaded in one job run.
    from tests.test_sitescope import REAL_FIELDS, _line

    f141 = tmp_path / "sis141.tsv"
    f140 = tmp_path / "sis140.tsv"
    f141.write_text(_line(REAL_FIELDS) + "\n", encoding="utf-8")
    f140.write_text(_line(REAL_FIELDS) + "\n", encoding="utf-8")

    loaded: list[tuple[str, str]] = []
    from app import scheduler

    monkeypatch.setattr(
        scheduler, "_load_sitescope_file",
        lambda instance, path: loaded.append((instance, path)),
    )

    class FakeSettings:
        sitescope_demo_map = [
            ("SiteScope-141", str(f141)),
            ("SiteScope-140", str(f140)),
        ]

    monkeypatch.setattr(scheduler, "get_settings", lambda: FakeSettings())
    scheduler._run_sitescope_demo_job()
    assert loaded == [
        ("SiteScope-141", str(f141)),
        ("SiteScope-140", str(f140)),
    ]
