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
