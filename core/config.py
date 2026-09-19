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


# Ports that mean "connect in the clear, then upgrade with STARTTLS". Anything
# else is treated as implicit TLS, because 465 is the only port in common use
# that expects the handshake before the greeting. Gmail rejects the wrong one
# outright rather than negotiating, so this has to be decided per mailbox.
STARTTLS_PORTS = frozenset({587, 25, 2525})

DEFAULT_HOST = "smtp.zoho.eu"
DEFAULT_PORT = 465

# Host and port inferred from the email domain, so a mailbox can be written as
# just "address:password" and still reach the right provider.
KNOWN_PROVIDERS: dict[str, tuple[str, int]] = {
    "gmail.com": ("smtp.gmail.com", 587),
    "googlemail.com": ("smtp.gmail.com", 587),
    "zoho.com": ("smtp.zoho.com", 465),
    "zoho.eu": ("smtp.zoho.eu", 465),
    "outlook.com": ("smtp-mail.outlook.com", 587),
    "hotmail.com": ("smtp-mail.outlook.com", 587),
    "live.com": ("smtp-mail.outlook.com", 587),
    "yahoo.com": ("smtp.mail.yahoo.com", 465),
    "icloud.com": ("smtp.mail.me.com", 587),
    "me.com": ("smtp.mail.me.com", 587),
    "fastmail.com": ("smtp.fastmail.com", 465),
    "protonmail.com": ("smtp.protonmail.ch", 587),
    "yandex.com": ("smtp.yandex.com", 465),
}


@dataclass(frozen=True)
class SenderAccount:
    """One mailbox in the rotation, with the transport it needs.

    Host and port live on the account rather than on one global block, because
    a pool is allowed to mix providers: a Gmail mailbox on 587 and a Zoho
    mailbox on 465 rotate side by side, and each has to be dialled its own way.
    """

    email: str
    app_password: str
    from_name: str = ""
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT

    @property
    def label(self) -> str:
        return self.email

    @property
    def domain(self) -> str:
        _, sep, domain = self.email.partition("@")
        return domain.strip() if sep and domain.strip() else "localhost"

    @property
    def use_ssl(self) -> bool:
        """True for implicit TLS, False when the session starts in the clear."""
        return self.port not in STARTTLS_PORTS

    @property
    def transport(self) -> str:
        return "SSL" if self.use_ssl else "STARTTLS"

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"


def provider_for(email: str) -> tuple[str, int] | None:
    """The SMTP endpoint a well-known address belongs to, or None."""
    _, _, domain = (email or "").partition("@")
    return KNOWN_PROVIDERS.get(domain.strip().lower())


def mask_entry(entry: str) -> str:
    """Show an account entry without its password. Used in diagnostics output.

    Splits from the right so the masked form works for both shapes: the long
    "host:port:email:password" and the short "email:password".
    """
    body, pipe, name = entry.partition("|")
    head, sep, _ = body.rpartition(":")
    masked = f"{head}:***" if sep else body
    return f"{masked}|{name}" if pipe else masked


def _split_entry(entry: str, default_host: str = DEFAULT_HOST,
                 default_port: int = DEFAULT_PORT
                 ) -> tuple[str, int, str, str, str | None]:
    """One MAILBOXES entry into (host, port, email, password, error).

    Two shapes are accepted:

        smtp.gmail.com:587:someone@gmail.com:app password
        someone@gmail.com:app password

    The long form is split at most three times, so the password keeps every
    colon it contains - app passwords from several providers are generated with
    them. The short form infers the endpoint from the address domain, falling
    back to the configured default for a private domain.

    A display name may follow a pipe on either shape.
    """
    body, pipe, name = entry.partition("|")
    parts = body.split(":", 3)

    if len(parts) == 4 and "@" in parts[2]:
        host, raw_port, email, password = parts
        try:
            port = int(raw_port.strip())
        except ValueError:
            return "", 0, "", "", f"port '{raw_port.strip()}' is not a number"
        if not 1 <= port <= 65535:
            return "", 0, "", "", f"port {port} is out of range"
        return host.strip(), port, email.strip().lower(), password.strip(), None

    if len(parts) >= 2 and "@" in parts[0]:
        # Short form: address first, password takes the rest of the line.
        email = parts[0].strip().lower()
        password = ":".join(parts[1:]).strip()
        endpoint = provider_for(email)
        host, port = endpoint if endpoint else (default_host, default_port)
        return host, port, email, password, None

    if len(parts) < 2:
        return "", 0, "", "", "no colon between the address and the password"
    return "", 0, "", "", "could not find an email address in the entry"


def parse_accounts_report(
    raw: str, default_from_name: str = "",
    default_host: str = DEFAULT_HOST, default_port: int = DEFAULT_PORT,
) -> tuple[list[SenderAccount], list[dict]]:
    """Parse MAILBOXES and say what was thrown away and why.

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

        masked = mask_entry(entry)
        host, port, email, password, error = _split_entry(
            entry, default_host, default_port)
        if error:
            problems.append({"entry": masked, "issue": error})
            continue

        _, pipe, name = entry.partition("|")
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
        elif not host:
            problems.append({"entry": masked, "issue": "no SMTP host for this address"})
        else:
            seen.add(email)
            accounts.append(SenderAccount(email=email, app_password=password,
                                          from_name=name, host=host, port=port))

    return accounts, problems


def parse_accounts(raw: str, default_from_name: str = "",
                   default_host: str = DEFAULT_HOST,
                   default_port: int = DEFAULT_PORT) -> list[SenderAccount]:
    """Parse MAILBOXES into a sender list.

        MAILBOXES="smtp.gmail.com:587:a@gmail.com:pass1,
                   smtp.zoho.eu:465:b@example.com:pass2"

    Entries separate on comma or newline, so a multi-line .env value works too.
    A short form is also accepted and infers the endpoint from the address:

        MAILBOXES="a@gmail.com:pass1"

    In the long form the entry is split at most three times, so a password
    containing colons survives intact. An optional display name goes after a
    pipe on either shape:

        smtp.gmail.com:587:a@gmail.com:pass1|Display Name

    The pipe is used rather than another colon because a colon inside the
    password would make the next field ambiguous.

    Malformed entries are dropped silently. Use ``parse_accounts_report`` when
    you need to know what was dropped.
    """
    accounts, _ = parse_accounts_report(raw, default_from_name,
                                        default_host, default_port)
    return accounts


@dataclass
class SmtpConfig:
    """The primary mailbox, plus the fallback endpoint for short entries.

    Host and port here are no longer the transport every send uses - each
    SenderAccount carries its own. They remain as the default applied to a
    short-form entry whose domain is not a known provider, and they describe
    the first mailbox so the Setup and Diagnostic views have something to show.
    """

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
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
            "SMTP_HOST": self.host,
            "MAILBOXES": self.email,
            "MAILBOXES password": self.app_password,
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
    # Ghost Protocol. A proxy string Scrapling understands, or "" for none.
    proxy: str = ""
    solve_challenges: bool = True
    # The Outpost. Treated as a credential: it usually embeds an auth token.
    webhook_url: str = ""
    webhook_detail: bool = False

    @property
    def has_proxy(self) -> bool:
        return bool(self.proxy.strip())

    @property
    def proxies(self) -> list[str]:
        """NEXUS_PROXY as a list. One entry, or a comma-separated pool.

        A residential pool is usually handed over as a comma-separated line,
        so both shapes are accepted and a single proxy is simply a pool of one.
        """
        raw = (self.proxy or "").replace("\n", ",")
        return [part.strip() for part in raw.split(",") if part.strip()]

    @property
    def proxy_count(self) -> int:
        return len(self.proxies)

    @property
    def sender_count(self) -> int:
        return len(self.senders)


def load_config(refresh: bool = True) -> AppConfig:
    """Read .env into an AppConfig. Safe to call on every Streamlit rerun."""
    load_dotenv(ENV_PATH, override=refresh)

    from_name = os.getenv("FROM_NAME", "").strip()

    # The endpoint applied to a short entry whose domain is not a known
    # provider. ZOHO_SMTP_HOST is still read so an .env written before the
    # rename keeps working.
    default_host = (os.getenv("SMTP_HOST", "").strip()
                    or os.getenv("ZOHO_SMTP_HOST", "").strip()
                    or DEFAULT_HOST)
    default_port = _int("SMTP_PORT", 0) or _int("ZOHO_SMTP_PORT", 0) or DEFAULT_PORT

    raw_mailboxes = os.getenv("MAILBOXES", "").strip()
    if not raw_mailboxes:
        # Migration path: an .env from before the rename still runs untouched.
        # Those entries are all "address:password", which the short form reads,
        # and their endpoint comes from the old ZOHO_SMTP_* pair above.
        raw_mailboxes = os.getenv("ZOHO_ACCOUNTS", "").strip()
    senders = parse_accounts(raw_mailboxes, from_name, default_host, default_port)

    # Single-mailbox fallback, for an .env that never used a list at all.
    if not senders:
        single_email = (os.getenv("SMTP_EMAIL", "").strip()
                        or os.getenv("ZOHO_EMAIL", "").strip()).lower()
        single_password = (os.getenv("SMTP_PASSWORD", "").strip()
                           or os.getenv("ZOHO_APP_PASSWORD", "").strip())
        if single_email and single_password:
            endpoint = provider_for(single_email) or (default_host, default_port)
            senders = [SenderAccount(single_email, single_password, from_name,
                                     host=endpoint[0], port=endpoint[1])]

    primary = senders[0] if senders else None
    smtp = SmtpConfig(
        # Mirrors the first mailbox when there is one, so the Setup and
        # Diagnostic views describe what will actually be dialled.
        host=primary.host if primary else default_host,
        port=primary.port if primary else default_port,
        use_ssl=primary.use_ssl if primary else (default_port not in STARTTLS_PORTS),
        email=primary.email if primary else "",
        app_password=primary.app_password if primary else "",
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
        proxy=os.getenv("NEXUS_PROXY", "").strip(),
        solve_challenges=_bool("NEXUS_SOLVE_CHALLENGES", True),
        webhook_url=os.getenv("NEXUS_WEBHOOK_URL", "").strip(),
        webhook_detail=_bool("NEXUS_WEBHOOK_DETAIL", False),
        tiktok_profile_dir=os.getenv("TIKTOK_PROFILE_DIR", "").strip(),
        ig_profile_dir=os.getenv("IG_PROFILE_DIR", "").strip(),
    )
