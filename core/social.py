"""Social session state, read from the browser profile a login wrote.

TikTok and Instagram wall every logged-out request. The fix is a persistent
Chromium profile (``TIKTOK_PROFILE_DIR`` / ``IG_PROFILE_DIR``) written by a
one-time sign-in; every later scrape reuses its cookies. This module answers
one question for the UI - is that profile actually signed in? - so a hunt can
warn before it starts instead of dying on a login wall mid-run.

The honest part is not trusting the folder. Chromium creates and fills a
``user_data_dir`` the instant it launches, before any login, so "the folder
exists" is a false green. The real signal is the session cookie itself, read
from the profile's Cookies database. Three states, never a bare yes/no:

* ``CONNECTED``  - a session cookie for the platform is present and unexpired
* ``UNKNOWN``    - a profile exists but the cookie is absent, expired, or the
                   database is locked (the browser is open right now)
* ``NOT_SET``    - no profile directory is configured at all

Everything takes an explicit ``path`` so the tests read a synthetic cookie
database and never need a browser.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from core.config import ROOT

CONNECTED = "connected"
UNKNOWN = "unknown"
NOT_SET = "not_set"

# Chromium timestamps are microseconds since 1601-01-01; unix epoch is this many
# seconds after that, so a cookie's expiry converts with one add and a scale.
_CHROMIUM_EPOCH_OFFSET = 11_644_473_600


@dataclass(frozen=True)
class Platform:
    key: str
    label: str
    env_var: str
    login_url: str
    cookie_names: tuple[str, ...]   # any one present means signed in
    host_hint: str                  # the cookie's domain must contain this


PLATFORMS = {
    "tiktok": Platform(
        key="tiktok", label="TikTok", env_var="TIKTOK_PROFILE_DIR",
        login_url="https://www.tiktok.com/login",
        cookie_names=("sessionid", "sid_tt"), host_hint="tiktok"),
    "instagram": Platform(
        key="instagram", label="Instagram", env_var="IG_PROFILE_DIR",
        login_url="https://www.instagram.com/accounts/login/",
        cookie_names=("sessionid",), host_hint="instagram"),
}


def default_dir(platform: str) -> str:
    """Where a profile lands when the operator has not chosen a folder."""
    return str(ROOT / "data" / f"{platform}-profile")


def profile_dir(platform: str, configured: str = "") -> str:
    """The profile folder for a platform: the configured one, or the default."""
    return (configured or "").strip() or default_dir(platform)


def _cookie_db(profile: Path) -> Path | None:
    """The Cookies SQLite inside a Chromium profile, whichever layout it uses.

    Newer Chromium nests it under Default/Network; older builds keep it at
    Default/Cookies. Returns the first that exists.
    """
    for rel in ("Default/Network/Cookies", "Default/Cookies", "Network/Cookies"):
        candidate = profile / rel
        if candidate.exists():
            return candidate
    return None


def _has_live_session(db_path: Path, platform: Platform,
                      now: float | None = None) -> bool:
    """True when the profile holds an unexpired session cookie for the platform.

    Opened read-only and defensively: the file is locked while the browser is
    running, and a lock (or any read error) is not proof of being logged out,
    so it raises to be reported as UNKNOWN rather than a false NOT_SET.
    """
    now = time.time() if now is None else now
    chromium_now = (now + _CHROMIUM_EPOCH_OFFSET) * 1_000_000
    uri = f"file:{db_path}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=1)
    try:
        placeholders = ",".join("?" * len(platform.cookie_names))
        rows = conn.execute(
            f"SELECT host_key, expires_utc, length(COALESCE(value, encrypted_value)) "
            f"FROM cookies WHERE name IN ({placeholders})",
            platform.cookie_names,
        ).fetchall()
    finally:
        conn.close()

    for host_key, expires_utc, value_len in rows:
        if platform.host_hint not in (host_key or "").lower():
            continue
        if not value_len:                       # a name with no value is not a login
            continue
        # expires_utc == 0 marks a session cookie (no stored expiry); treat it
        # as live. Otherwise it must be in the future.
        if expires_utc and expires_utc <= chromium_now:
            continue
        return True
    return False


def status(platform: str, configured_dir: str = "",
           now: float | None = None) -> str:
    """CONNECTED, UNKNOWN or NOT_SET for one platform."""
    plat = PLATFORMS[platform]
    configured = (configured_dir or "").strip()
    profile = Path(profile_dir(platform, configured))

    # A profile is "not set" only when nothing is configured and no default
    # folder has ever been created. Once a folder exists, the question becomes
    # whether it holds a session, which is UNKNOWN until proven CONNECTED.
    if not configured and not profile.exists():
        return NOT_SET

    db_path = _cookie_db(profile)
    if db_path is None:
        return UNKNOWN
    try:
        return CONNECTED if _has_live_session(db_path, plat, now) else UNKNOWN
    except Exception:
        # Locked (browser open) or unreadable: says nothing about login state.
        return UNKNOWN


def status_all(cfg) -> dict[str, str]:
    """Status for every platform, read from the app config's profile dirs."""
    return {
        "tiktok": status("tiktok", getattr(cfg, "tiktok_profile_dir", "")),
        "instagram": status("instagram", getattr(cfg, "ig_profile_dir", "")),
    }
