#!/usr/bin/env python3
"""Find out why the Digital View assets are not showing up.

Walks the whole path the data takes — env var, file on disk, workbook parse,
database rows — and stops at the first thing that is wrong, saying what to do
about it. Run it from the project folder:

    .venv\\Scripts\\python.exe tools\\diagnose_digitalview.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BAR = "-" * 72
OK, BAD, WARN = "[ OK ]", "[FAIL]", "[WARN]"


def fail(message: str, *fixes: str) -> None:
    print(f"\n{BAD} {message}")
    for fix in fixes:
        print(f"       -> {fix}")
    print(f"\n{BAR}\nStopped here. Fix the above and run this again.")
    sys.exit(1)


def main() -> None:
    print(BAR)
    print("DIGITAL VIEW ASSET IMPORT — DIAGNOSIS")
    print(BAR)

    # 1. Is the setting there at all, and under the right name?
    from app.config import get_settings

    settings = get_settings()
    path = (settings.digitalview_asset_file or "").strip()
    print(f"\n1. DIGITALVIEW_ASSET_FILE = {path or '(empty)'}")

    if not path:
        legacy = os.getenv("HUAWEI_ASSET_FILE", "").strip()
        hints = [
            "Add this line to .env (no quotes around the path):",
            r"     DIGITALVIEW_ASSET_FILE=D:\umd\BaseAssetImportTemplate_En.xlsx",
            "Then STOP the server and start it again — .env is read once, at startup.",
        ]
        if legacy:
            hints.insert(
                0,
                f"Found HUAWEI_ASSET_FILE={legacy} instead. That was the old name; "
                "rename the key to DIGITALVIEW_ASSET_FILE.",
            )
        fail("the setting is empty, so nothing is ever loaded", *hints)

    # 2. Does the file exist where the setting points?
    p = Path(path)
    print(f"2. resolved path          = {p.resolve() if p.exists() else p}")
    if not p.exists():
        fail(
            "no file at that path",
            "Check for a typo, and that the path is not wrapped in quotes in .env.",
            "A path with backslashes must NOT be inside double quotes — \\U in "
            r"C:\Users is read as an escape sequence.",
        )
    if p.is_dir():
        fail("that path is a folder, not the .xlsx file",
             "Point the setting at the workbook itself.")

    size = p.stat().st_size
    print(f"{OK} file exists, {size / 1024:.0f} KB")
    if size == 0:
        fail("the file is empty", "Export it again from the Digital View UI.")

    # 3. Does it parse, and what is in it?
    print("\n3. parsing the workbook…")
    from app.digitalview_assets import parse_workbook

    try:
        inventory = parse_workbook(p)
    except Exception as exc:  # noqa: BLE001 — this is the diagnosis
        fail(
            f"the workbook could not be read ({type(exc).__name__}: {exc})",
            "Make sure it is the BaseAssetImportTemplate export, saved as .xlsx.",
            "If it was saved as .xls, open it in Excel and Save As .xlsx.",
        )

    if inventory.count == 0:
        print(f"{WARN} the file parsed but held no assets")
        print("       sheets looked at:")
        for sheet, count in inventory.sheets_read.items():
            print(f"         {sheet:26} {count}")
        fail(
            "no assets found in any known sheet",
            "The export may be a blank template, or its sheets renamed.",
            "Expected sheets: VM Operating System, PM Operating System, "
            "Rack Server, Storage Device.",
        )

    print(f"{OK} parsed {inventory.count} asset(s)")
    for sheet, count in inventory.sheets_read.items():
        if count:
            print(f"       {sheet:26} {count}")
    cores = sum(h["metrics"].get("cores", 0) for h in inventory.hosts)
    mem = sum(h["metrics"].get("mem_total_gb", 0) for h in inventory.hosts)
    print(f"       capacity: {cores} cores, {mem / 1024:.1f} TB RAM")

    # 4. Are they in the database?
    print("\n4. checking the database…")
    from sqlalchemy import func, select

    from app.db import SessionLocal
    from app.models import Host, SourcePlatform

    db = SessionLocal()
    try:
        stored = db.scalar(
            select(func.count(Host.id)).where(
                Host.source_platform == SourcePlatform.digitalview
            )
        )
        legacy_rows = db.scalar(
            select(func.count(Host.id)).where(Host.source_platform == "huawei")
        )
    finally:
        db.close()

    print(f"   rows with platform 'digitalview' : {stored}")
    if legacy_rows:
        print(f"{WARN} {legacy_rows} row(s) still under the old 'huawei' value")
        print("       -> restart the app once; init_db migrates them automatically.")

    if stored == 0:
        print()
        print(f"{WARN} the file is fine but nothing has been imported yet.")
        print("       The load runs from the scheduler, which only starts when the")
        print("       app does. Two things to check:")
        print("       1. Did you restart the server AFTER editing .env?")
        print("       2. Does the startup log include a line like:")
        print("            digitalview asset inventory enabled: <path> (checked every 5 min)")
        print()
        answer = input("       Import them now from here? [y/N] ").strip().lower()
        if answer == "y":
            from app.digitalview_assets import load_into_db

            db = SessionLocal()
            try:
                result = load_into_db(
                    db, settings.digitalview_instance or "DigitalView", p
                )
                db.commit()
                print(f"\n{OK} imported {result.count} asset(s). Reload the dashboard.")
            finally:
                db.close()
        return

    print(f"\n{OK} {stored} Digital View asset(s) are in the database.")
    print("     If the dashboard still shows 0, you are looking at a different")
    print("     database than this script — check DATABASE_URL in .env.")


if __name__ == "__main__":
    main()
