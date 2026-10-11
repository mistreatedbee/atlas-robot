"""Build config/events/us_tier1.csv from dates transcribed from OFFICIAL sources (retrieved 2026-10-11).

Sources:
* CPI: BLS release archive https://www.bls.gov/bls/news-release/cpi.htm (archive file names cpi_MMDDYYYY encode the
  release date; convention verified against the embargo lines of cpi_09112026 and empsit_11202025) and the
  upcoming schedule https://www.bls.gov/schedule/news_release/cpi.htm (08:30 ET).
* Employment Situation: https://www.bls.gov/bls/news-release/empsit.htm and
  https://www.bls.gov/schedule/news_release/empsit.htm (08:30 ET).
* FOMC: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm (meeting dates; the decision is on the
  second day). Statement time 14:00 ET verified on the 2025-09-17 statement ("For release at 2:00 p.m. EDT");
  assumed 14:00 ET for the other meetings (standard practice; to be spot-checked).
  The 2025-08-22 notation vote (Statement on Longer-Run Goals) is not a rate decision and is excluded.

`known_since` = release - 1 day for every event: a deliberately LATE assumption (schedules are published months
ahead), so a backtest can never benefit from knowing a (re)scheduled date earlier than it plausibly was.
Usage: python scripts/build_us_events.py
"""

from __future__ import annotations

import csv
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

CPI = {  # reference month -> release date (MMDDYYYY from BLS archive/schedule)
    "2024-01": "02132024", "2024-02": "03122024", "2024-03": "04102024", "2024-04": "05152024",
    "2024-05": "06122024", "2024-06": "07112024", "2024-07": "08142024", "2024-08": "09112024",
    "2024-09": "10102024", "2024-10": "11132024", "2024-11": "12112024", "2024-12": "01152025",
    "2025-01": "02122025", "2025-02": "03122025", "2025-03": "04102025", "2025-04": "05132025",
    "2025-05": "06112025", "2025-06": "07152025", "2025-07": "08122025", "2025-08": "09112025",
    "2025-09": "10242025", "2025-11": "12182025", "2025-12": "01132026",
    "2026-01": "02132026", "2026-02": "03112026", "2026-03": "04102026", "2026-04": "05122026",
    "2026-05": "06102026", "2026-06": "07142026", "2026-07": "08122026", "2026-08": "09112026",
    "2026-10": "11102026", "2026-11": "12102026", "2026-12": "01132027",
    "2027-01": "02112027", "2027-02": "03102027", "2027-03": "04132027", "2027-04": "05122027",
    "2027-05": "06102027", "2027-06": "07142027", "2027-07": "08112027", "2027-08": "09142027",
    "2027-09": "10142027", "2027-10": "11102027", "2027-11": "12102027",
}
# NOTE: CPI for September 2026 is absent: neither the archive (latest = August 2026) nor the upcoming schedule
# (first = October 2026) listed it when retrieved. October 2025 CPI was not published (appropriations lapse).

NFP = {
    "2024-01": "02022024", "2024-02": "03082024", "2024-03": "04052024", "2024-04": "05032024",
    "2024-05": "06072024", "2024-06": "07052024", "2024-07": "08022024", "2024-08": "09062024",
    "2024-09": "10042024", "2024-10": "11012024", "2024-11": "12062024", "2024-12": "01102025",
    "2025-01": "02072025", "2025-02": "03072025", "2025-03": "04042025", "2025-04": "05022025",
    "2025-05": "06062025", "2025-06": "07032025", "2025-07": "08012025", "2025-08": "09052025",
    "2025-09": "11202025", "2025-11": "12162025", "2025-12": "01092026",
    "2026-01": "02112026", "2026-02": "03062026", "2026-03": "04032026", "2026-04": "05082026",
    "2026-05": "06052026", "2026-06": "07022026", "2026-07": "08072026", "2026-08": "09042026",
    "2026-09": "10022026", "2026-10": "11062026", "2026-11": "12042026", "2026-12": "01082027",
    "2027-01": "02052027", "2027-02": "03052027", "2027-03": "04022027", "2027-04": "05072027",
    "2027-05": "06042027", "2027-06": "07022027", "2027-07": "08062027", "2027-08": "09032027",
    "2027-09": "10082027", "2027-10": "11052027", "2027-11": "12032027",
}
# October 2025 Employment Situation was not published (appropriations lapse).

FOMC = [  # decision day = second day of the meeting; * = Summary of Economic Projections
    ("2024-01-31", False), ("2024-03-20", True), ("2024-05-01", False), ("2024-06-12", True),
    ("2024-07-31", False), ("2024-09-18", True), ("2024-11-07", False), ("2024-12-18", True),
    ("2025-01-29", False), ("2025-03-19", True), ("2025-05-07", False), ("2025-06-18", True),
    ("2025-07-30", False), ("2025-09-17", True), ("2025-10-29", False), ("2025-12-10", True),
    ("2026-01-28", False), ("2026-03-18", True), ("2026-04-29", False), ("2026-06-17", True),
    ("2026-07-29", False), ("2026-09-16", True), ("2026-10-28", False), ("2026-12-09", True),
]


def utc_ms(day: date, at: time) -> int:
    return int(datetime.combine(day, at, tzinfo=NY).astimezone(timezone.utc).timestamp() * 1000)


def mmddyyyy(s: str) -> date:
    return date(int(s[4:]), int(s[:2]), int(s[2:4]))


def main() -> None:
    rows = []
    for ref, d in CPI.items():
        rows.append(("CPI", ref, mmddyyyy(d), time(8, 30), "https://www.bls.gov/bls/news-release/cpi.htm", ""))
    for ref, d in NFP.items():
        rows.append(("NFP", ref, mmddyyyy(d), time(8, 30), "https://www.bls.gov/bls/news-release/empsit.htm", ""))
    for d, sep in FOMC:
        note = "SEP meeting; " if sep else ""
        note += "14:00 ET verified" if d == "2025-09-17" else "14:00 ET assumed (standard)"
        rows.append(("FOMC", d[:7], date.fromisoformat(d), time(14, 0),
                     "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", note))
    rows.sort(key=lambda r: utc_ms(r[2], r[3]))
    out = Path(__file__).resolve().parents[1] / "config" / "events" / "us_tier1.csv"
    with out.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["event_id", "category", "tier", "reference", "release_ny", "release_utc_ms",
                    "known_since_utc_ms", "source", "notes"])
        for cat, ref, day, at, src, note in rows:
            ms = utc_ms(day, at)
            w.writerow([f"{cat}-{ref}", cat, 1, ref, f"{day.isoformat()} {at.strftime('%H:%M')}", ms,
                        ms - 86_400_000, src, note])
    print(f"wrote {len(rows)} events to {out}")


if __name__ == "__main__":
    main()
