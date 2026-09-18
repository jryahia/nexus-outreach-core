"""One-time TikTok sign-in.

TikTok answers every logged-out profile request with a mandatory-login
redirect. Run this once, sign in by hand, close the window: the session is
written to TIKTOK_PROFILE_DIR and every later scrape reuses it.

    .venv\\Scripts\\python.exe tools\\tiktok_login.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrapling.fetchers import StealthyFetcher  # noqa: E402

from core.config import ROOT, load_config  # noqa: E402

LOGIN_URL = "https://www.tiktok.com/login"
WAIT_SECONDS = 300


def main() -> int:
    cfg = load_config()
    profile_dir = cfg.tiktok_profile_dir or str(ROOT / "data" / "tiktok-profile")
    Path(profile_dir).mkdir(parents=True, exist_ok=True)

    print(f"Profile folder: {profile_dir}")
    print("A browser window will open. Sign in, then leave it alone -")
    print(f"it closes by itself after {WAIT_SECONDS // 60} minutes.\n")

    StealthyFetcher.fetch(
        LOGIN_URL,
        headless=False,
        user_data_dir=profile_dir,
        timeout=(WAIT_SECONDS + 60) * 1000,
        wait=WAIT_SECONDS * 1000,
        network_idle=False,
    )

    if not cfg.tiktok_profile_dir:
        print("\nAdd this line to your .env:")
        print(f"TIKTOK_PROFILE_DIR={profile_dir}")
    print("\nDone. Retry the TikTok hunt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
