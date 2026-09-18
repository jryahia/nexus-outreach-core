"""Configuration loading. Single source of truth for credentials and limits."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
CLEAN_DIR = DATA_DIR / "clean"
TEMPLATE_DIR = ROOT / "templates"
LOG_PATH = DATA_DIR / "campaign_log.csv"  # legacy; kept as a migration source
ENV_PATH = ROOT / ".env"

for _d in (RAW_DIR, CLEAN_DIR, TEMPLATE_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def _bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, "").strip())
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class SenderAccount:
    """One Zoho mailbox in the rotation."""

    email: str
    app_password: str
    from_name: str = ""

    @property
    def label(self) -> str:
        return self.email

    @property
    def domain(self) -> str:
        _, sep, domain = self.email.partition("@")
        return domain.strip() if sep and domain.strip() else "localhost"


def mask_entry(entry: str) -> str:
    """Show an account entry without its password. Used in diagnostics output."""
    head, sep, _ = entry.partition(":")
    return f"{head}:***" if sep else head


def parse_accounts_report(
    raw: str, default_from_name: str = ""
) -> tuple[list[SenderAccount], list[dict]]:
    """Parse ZOHO_ACCOUNTS and say what was thrown away and why.

    The plain ``parse_accounts`` drops malformed entries silently, which is
    right for the send path and useless for a diagnostic: the user needs to be
    told that their third mailbox never made it into the rotation.
    """
    accounts: list[SenderAccount] = []
    problems: list[dict] = []
    seen: set[str] = set()

    for chunk in raw.replace("\r", "\n").replace(",", "\n").split("\n"):
        entry = chunk.strip().strip('"').strip("'")
        if not entry or entry.startswith("#"):
            continue

        email, sep, remainder = entry.partition(":")
        masked = mask_entry(entry)
        if not sep:
            problems.append({"entry": masked, "issue": "no colon between email and password"})
            continue

        email = email.strip().lower()
        password, pipe, name = remainder.partition("|")
        password = password.strip()
        name = name.strip() if pipe else default_from_name

        local, at, domain = email.partition("@")
        if not email:
            problems.append({"entry": masked, "issue": "empty email"})
        elif not at:
            problems.append({"entry": masked, "issue": "email has no @"})
        elif not local:
            problems.append({"entry": masked, "issue": "email has nothing before the @"})
        elif "." not in domain or domain.startswith(".") or domain.endswith("."):
            # "bad@" and "bad@localhost" would sail past a bare "@ in email"
            # test and only fail later, at send time.
            problems.append({"entry": masked, "issue": f"domain '{domain}' is not valid"})
        elif not password:
            problems.append({"entry": masked, "issue": "empty app password"})
        elif email in seen:
            problems.append({"entry": masked, "issue": "duplicate mailbox, ignored"})
        else:
            seen.add(email)
            accounts.append(SenderAccount(email=email, app_password=password,
                                          from_name=name))

    return accounts, problems


def parse_accounts(raw: str, default_from_name: str = "") -> list[SenderAccount]:
    """Parse ZOHO_ACCOUNTS into a sender list.

        ZOHO_ACCOUNTS="a@x.com:pass1,b@y.com:pass2"

    Entries separate on comma or newline, so a multi-line .env value works too.
    The email is split on the FIRST colon only - an app password containing a
    colon survives intact. An optional display name goes after a pipe:

        a@x.com:pass1|Display Name

    The pipe is used rather than a third colon because a colon inside the
    password would make a third field ambiguous.

    Malformed entries are dropped silently. Use ``parse_accounts_report`` when
    you need to know what was dropped.
    """
    accounts, _ = parse_accounts_report(raw, default_from_name)
    return accounts


@dataclass
class SmtpConfig:
    host: str = "smtp.zoho.eu"
    port: int = 465
    use_ssl: bool = True
    email: str = ""
    app_password: str = ""
    from_name: str = ""
    reply_to: str = ""

    @property
    def is_complete(self) -> bool:
        return bool(self.host and self.port and self.email and self.app_password)

    def missing_fields(self) -> list[str]:
        pairs = {
            "ZOHO_SMTP_HOST": self.host,
            "ZOHO_EMAIL": self.email,
            "ZOHO_APP_PASSWORD": self.app_password,
        }
        return [k for k, v in pairs.items() if not v]


@dataclass
class AppConfig:
    smtp: SmtpConfig = field(default_factory=SmtpConfig)
    senders: list[SenderAccount] = field(default_factory=list)
    min_delay: int = 120
    max_delay: int = 360
    daily_cap: int = 50
    coffee_every: int = 25
    coffee_min: int = 900
    coffee_max: int = 1500
    unsubscribe_line: str = ""
    postal_address: str = ""
    headless: bool = True
    request_timeout: int = 30000
    tiktok_profile_dir: str = ""
    ig_profile_dir: str = ""

    @property
    def sender_count(self) -> int:
        return len(self.senders)


def load_config(refresh: bool = True) -> AppConfig:
    """Read .env into an AppConfig. Safe to call on every Streamlit rerun."""
    load_dotenv(ENV_PATH, override=refresh)

    from_name = os.getenv("FROM_NAME", "").strip()
    senders = parse_accounts(os.getenv("ZOHO_ACCOUNTS", ""), from_name)

    # Single-account fallback. Also keeps cfg.smtp meaningful when ZOHO_ACCOUNTS
    # is set, because verify_smtp and the Setup tab read it.
    if not senders:
        single_email = os.getenv("ZOHO_EMAIL", "").strip().lower()
        single_password = os.getenv("ZOHO_APP_PASSWORD", "").strip()
        if single_email and single_password:
            senders = [SenderAccount(single_email, single_password, from_name)]

    primary = senders[0] if senders else None
    smtp = SmtpConfig(
        host=os.getenv("ZOHO_SMTP_HOST", "smtp.zoho.eu").strip(),
        port=_int("ZOHO_SMTP_PORT", 465),
        use_ssl=_bool("ZOHO_SMTP_SSL", True),
        email=primary.email if primary else os.getenv("ZOHO_EMAIL", "").strip(),
        app_password=(primary.app_password if primary
                      else os.getenv("ZOHO_APP_PASSWORD", "").strip()),
        from_name=primary.from_name if primary else from_name,
        reply_to=os.getenv("REPLY_TO", "").strip(),
    )

    return AppConfig(
        smtp=smtp,
        senders=senders,
        min_delay=_int("MIN_DELAY_SECONDS", 120),
        max_delay=_int("MAX_DELAY_SECONDS", 360),
        daily_cap=_int("DAILY_SEND_CAP", 50),
        coffee_every=_int("COFFEE_BREAK_EVERY", 25),
        coffee_min=_int("COFFEE_BREAK_MIN_SECONDS", 900),
        coffee_max=_int("COFFEE_BREAK_MAX_SECONDS", 1500),
        unsubscribe_line=os.getenv("UNSUBSCRIBE_LINE", "").strip(),
        postal_address=os.getenv("POSTAL_ADDRESS", "").strip(),
        headless=_bool("HEADLESS", True),
        request_timeout=_int("REQUEST_TIMEOUT", 30000),
        tiktok_profile_dir=os.getenv("TIKTOK_PROFILE_DIR", "").strip(),
        ig_profile_dir=os.getenv("IG_PROFILE_DIR", "").strip(),
    )
