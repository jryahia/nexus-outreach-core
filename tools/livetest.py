"""Live scrape check. Hits Google Maps and Instagram for real.

    .venv\\Scripts\\python.exe tools\\livetest.py [maps|instagram|tiktok]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import hunter, purifier  # noqa: E402
from core.config import load_config  # noqa: E402
from ui.state import Job  # noqa: E402


class PrintJob(Job):
    def report(self, message=None, current=None, total=None, log=None):
        super().report(message=message, current=current, total=total, log=log)
        if log:
            print("   .", log)


def run(label: str, fn, **kwargs) -> list[dict]:
    job = PrintJob(key=label)
    print(f"\n=== {label} ===")
    t = time.perf_counter()
    try:
        leads = fn(job=job, cfg=load_config(), **kwargs)
    except hunter.LoginRequired as exc:
        print(f"  LOGIN WALL (handled cleanly, thread alive):\n  {exc}")
        return []
    except Exception as exc:
        print(f"  ERROR {type(exc).__name__}: {exc}")
        return []
    secs = time.perf_counter() - t
    with_email = [lead for lead in leads if lead["email"]]
    print(f"  {len(leads)} leads in {secs:.1f}s, {len(with_email)} with an email")
    for lead in leads[:12]:
        print(f"   {lead['name'][:38]:38} | {lead['email'] or '-':32} | {lead['website'][:40]}")
    res = purifier.purify(leads, drop_generic=True)
    print(f"  purified: kept {res.kept}, role {res.generic}, dup {res.duplicates}, "
          f"invalid {res.invalid}, no-email {res.no_email}")
    return leads


which = (sys.argv[1] if len(sys.argv) > 1 else "all").lower()

if which in ("all", "maps"):
    run("Google Maps", hunter.scrape_google_maps,
        keyword=hunter.DEFAULT_MAPS_KEYWORD,
        location=hunter.DEFAULT_MAPS_LOCATION,
        max_results=12)

if which in ("all", "instagram"):
    run("Instagram hashtag", hunter.scrape_instagram,
        target="#realestateagent", max_results=4)

if which in ("all", "tiktok"):
    run("TikTok", hunter.scrape_tiktok, target="@nba", max_results=1)
