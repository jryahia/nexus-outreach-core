"""One-time social sign-in for the in-app Connect buttons.

Launched as a subprocess by the SYSTEM CONTROL tab:

    python tools/social_login.py tiktok
    python tools/social_login.py instagram

Opens a visible browser bound to the platform's profile folder. The operator
signs in by hand; the session cookies are written into that folder and every
later scrape reuses them. The window closes itself after the wait, or when the
operator closes it.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrapling.fetchers import StealthyFetcher  # noqa: E402

from core import social  # noqa: E402
from core.config import load_config  # noqa: E402

WAIT_SECONDS = 300


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in social.PLATFORMS:
        print("usage: social_login.py [tiktok|instagram]")
        return 2

    platform = social.PLATFORMS[argv[1]]
    cfg = load_config()
    # config field names: tiktok_profile_dir / ig_profile_dir
    field = "tiktok_profile_dir" if platform.key == "tiktok" else "ig_profile_dir"
    configured = getattr(cfg, field, "")
    profile_dir = social.profile_dir(platform.key, configured)
    Path(profile_dir).mkdir(parents=True, exist_ok=True)

    print(f"Profile folder: {profile_dir}")
    print(f"A browser window will open for {platform.label}. Sign in, then")
    print(f"close the window - or leave it and it closes after "
          f"{WAIT_SECONDS // 60} minutes.\n")

    StealthyFetcher.fetch(
        platform.login_url,
        headless=False,
        user_data_dir=profile_dir,
        timeout=(WAIT_SECONDS + 60) * 1000,
        wait=WAIT_SECONDS * 1000,
        network_idle=False,
    )

    print(f"\n{platform.label} session saved to {profile_dir}")
    print("Set it in SYSTEM CONTROL or .env if it is not already there:")
    print(f"{platform.env_var}={profile_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
