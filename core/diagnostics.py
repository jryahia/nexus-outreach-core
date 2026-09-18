"""The Verifier - a deep health scan of everything NEXUS depends on.

Split into two halves on purpose:

* ``run_static_checks`` is filesystem and import work only. Fast enough to run
  inline on every render of the Diagnostic tab.
* ``run_smtp_checks`` opens a real socket per mailbox and takes seconds each,
  so it runs on a worker thread through ``run_full_scan``.

Nothing in here ever prints an app password. Credentials are reported as
"set" or "missing", and failure details carry the mailbox plus the SMTP code.
"""

from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.config import (
    CLEAN_DIR,
    ENV_PATH,
    LOG_PATH,
    RAW_DIR,
    ROOT,
    AppConfig,
    parse_accounts_report,
)

OK = "ok"
WARN = "warn"
FAIL = "fail"

ENVIRONMENT = "Environment"
CREDENTIALS = "Credentials"
RHYTHM = "Sending rhythm"
SCRAPER = "Scraping engine"
STORAGE = "Storage"
MAILBOXES = "Mailboxes"
VAULT = "Vault and templates"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    group: str = ""

    @property
    def passed(self) -> bool:
        return self.status == OK


@dataclass
class ScanResult:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "", group: str = "") -> Check:
        check = Check(name=name, status=status, detail=detail, group=group)
        self.checks.append(check)
        return check

    def tally(self) -> tuple[int, int, int]:
        ok = sum(1 for c in self.checks if c.status == OK)
        warn = sum(1 for c in self.checks if c.status == WARN)
        fail = sum(1 for c in self.checks if c.status == FAIL)
        return ok, warn, fail

    @property
    def healthy(self) -> bool:
        return not any(c.status == FAIL for c in self.checks)


# ---------------------------------------------------------------------------
# (c) Scraping engine
# ---------------------------------------------------------------------------
def browser_registry() -> Path:
    """Where Playwright and Patchright keep downloaded browsers."""
    override = os.getenv("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if override:
        return Path(override)
    system = platform.system()
    if system == "Windows":
        return Path(os.getenv("LOCALAPPDATA", Path.home())) / "ms-playwright"
    if system == "Darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def find_browsers() -> list[str]:
    """Installed Chromium builds, by folder name.

    Read off the filesystem rather than by starting a driver: launching the
    node driver just to ask for a path costs a second and leaves async
    teardown noise in a worker thread.
    """
    registry = browser_registry()
    if not registry.is_dir():
        return []
    return sorted(p.name for p in registry.iterdir()
                  if p.is_dir() and p.name.startswith("chromium"))


def _check_import(result: ScanResult, module: str, label: str,
                  hint: str, group: str) -> Any:
    try:
        imported = __import__(module, fromlist=["__version__"])
    except Exception as exc:
        result.add(label, FAIL, f"{type(exc).__name__}: {exc}. {hint}", group)
        return None
    version = getattr(imported, "__version__", "")
    result.add(label, OK, f"version {version}" if version else "importable", group)
    return imported


# ---------------------------------------------------------------------------
# (a) + (c) + (d): everything that needs no network
# ---------------------------------------------------------------------------
def run_static_checks(cfg: AppConfig, result: ScanResult | None = None) -> ScanResult:
    result = result or ScanResult()

    # -- Environment --------------------------------------------------------
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    result.add("Python runtime", OK if sys.version_info >= (3, 10) else FAIL,
               f"{version} on {platform.system()}", ENVIRONMENT)

    if ENV_PATH.exists():
        result.add(".env file", OK, str(ENV_PATH), ENVIRONMENT)
    else:
        result.add(".env file", FAIL,
                   "Missing. Copy .env.example to .env and fill it in.", ENVIRONMENT)

    # -- Credentials (a) ----------------------------------------------------
    raw_accounts = os.getenv("ZOHO_ACCOUNTS", "")
    accounts, problems = parse_accounts_report(raw_accounts, cfg.smtp.from_name)

    if accounts:
        result.add("ZOHO_ACCOUNTS format", OK,
                   f"{len(accounts)} mailbox(es) parsed for rotation", CREDENTIALS)
    elif raw_accounts.strip():
        result.add("ZOHO_ACCOUNTS format", FAIL,
                   "Set, but no usable entry could be parsed.", CREDENTIALS)
    elif cfg.senders:
        result.add("ZOHO_ACCOUNTS format", WARN,
                   "Empty - falling back to the single ZOHO_EMAIL account. "
                   "Add a second mailbox to enable rotation.", CREDENTIALS)
    else:
        result.add("ZOHO_ACCOUNTS format", FAIL,
                   "No mailbox configured. Set ZOHO_ACCOUNTS or ZOHO_EMAIL.",
                   CREDENTIALS)

    for problem in problems:
        result.add(f"Entry {problem['entry']}", FAIL, problem["issue"], CREDENTIALS)

    result.add("SMTP host", OK if cfg.smtp.host else FAIL,
               cfg.smtp.host or "ZOHO_SMTP_HOST is empty", CREDENTIALS)
    port_ok = cfg.smtp.port in (465, 587)
    result.add("SMTP port", OK if port_ok else WARN,
               f"{cfg.smtp.port} ({'SSL' if cfg.smtp.use_ssl else 'STARTTLS'})"
               + ("" if port_ok else " - Zoho expects 465 for SSL or 587 for STARTTLS"),
               CREDENTIALS)
    result.add("App password", OK if cfg.smtp.app_password else FAIL,
               "set" if cfg.smtp.app_password else "missing", CREDENTIALS)
    result.add("From name", OK if cfg.smtp.from_name else WARN,
               cfg.smtp.from_name or "FROM_NAME is empty - emails send with a bare "
                                     "address, which reads as spam", CREDENTIALS)

    # -- Sending rhythm (a) -------------------------------------------------
    if cfg.min_delay <= 0 or cfg.max_delay <= 0:
        result.add("Send gap", FAIL, "Delays must be positive.", RHYTHM)
    elif cfg.max_delay < cfg.min_delay:
        result.add("Send gap", FAIL,
                   f"MAX_DELAY_SECONDS ({cfg.max_delay}) is below "
                   f"MIN_DELAY_SECONDS ({cfg.min_delay}).", RHYTHM)
    elif cfg.min_delay < 60:
        result.add("Send gap", WARN,
                   f"{cfg.min_delay}s is fast enough to look automated.", RHYTHM)
    else:
        result.add("Send gap", OK,
                   f"{cfg.min_delay // 60}-{cfg.max_delay // 60} min between sends", RHYTHM)

    if cfg.coffee_every <= 0:
        result.add("Coffee break", WARN, "Disabled. Long runs will look robotic.", RHYTHM)
    elif cfg.coffee_max < cfg.coffee_min:
        result.add("Coffee break", FAIL,
                   "COFFEE_BREAK_MAX_SECONDS is below the minimum.", RHYTHM)
    else:
        result.add("Coffee break", OK,
                   f"every {cfg.coffee_every} sends, "
                   f"{cfg.coffee_min // 60}-{cfg.coffee_max // 60} min", RHYTHM)

    result.add("Daily cap", OK if cfg.daily_cap > 0 else WARN,
               f"{cfg.daily_cap} emails per campaign" if cfg.daily_cap > 0
               else "No cap set - a large list will send in one run", RHYTHM)
    result.add("Unsubscribe line", OK if cfg.unsubscribe_line else WARN,
               cfg.unsubscribe_line
               or "Empty. Cold outreach needs an opt-out under CAN-SPAM and GDPR.",
               RHYTHM)

    # -- Scraping engine (c) ------------------------------------------------
    _check_import(result, "scrapling", "scrapling", "pip install -r requirements.txt",
                  SCRAPER)
    try:
        from scrapling.fetchers import Fetcher, StealthyFetcher  # noqa: F401

        result.add("Fetchers (stealth)", OK, "StealthyFetcher and Fetcher importable",
                   SCRAPER)
    except Exception as exc:
        result.add("Fetchers (stealth)", FAIL,
                   f"{type(exc).__name__}: {exc}. "
                   'pip install "scrapling[fetchers]"', SCRAPER)

    _check_import(result, "playwright", "playwright", "part of scrapling[fetchers]",
                  SCRAPER)
    _check_import(result, "patchright", "patchright", "part of scrapling[fetchers]",
                  SCRAPER)
    _check_import(result, "curl_cffi", "curl_cffi", "part of scrapling[fetchers]",
                  SCRAPER)
    _check_import(result, "plotly", "plotly", "pip install plotly", SCRAPER)

    browsers = find_browsers()
    if browsers:
        result.add("Browser binaries", OK,
                   f"{len(browsers)} build(s) in {browser_registry()}: "
                   + ", ".join(browsers[-3:]), SCRAPER)
    else:
        result.add("Browser binaries", FAIL,
                   f"Nothing in {browser_registry()}. Run:  scrapling install",
                   SCRAPER)

    # -- Storage (d) --------------------------------------------------------
    for label, folder in (("data/raw", RAW_DIR), ("data/clean", CLEAN_DIR),
                          ("data (log)", LOG_PATH.parent)):
        result.add(f"Write access {label}", *_probe_write(folder), group=STORAGE)

    if LOG_PATH.exists():
        try:
            size = LOG_PATH.stat().st_size
            result.add("Campaign log", OK, f"{LOG_PATH.name}, {size:,} bytes", STORAGE)
        except OSError as exc:
            result.add("Campaign log", WARN, str(exc), STORAGE)
    else:
        result.add("Campaign log", WARN,
                   "No sends recorded yet. It is created on the first campaign.",
                   STORAGE)

    # -- The Vault ----------------------------------------------------------
    try:
        from core import templates, vault

        vault.init_db()
        mode = vault.journal_mode().lower()
        counts = vault.stats()
        result.add("SQLite database", OK,
                   f"{vault.DB_PATH.name}: {counts['raw']:,} raw leads, "
                   f"{counts['clean']:,} clean, {counts['logs']:,} send records",
                   VAULT)
        result.add("Write-ahead logging", OK if mode == "wal" else WARN,
                   f"journal_mode={mode}"
                   + ("" if mode == "wal" else " - a reader can block the campaign "
                                               "writer without WAL"),
                   VAULT)
        result.add("Blacklist", OK,
                   f"{counts['blacklist']:,} address(es) will never be contacted",
                   VAULT)
        result.add("CSV migration", OK if vault.get_meta(vault.MIGRATION_KEY) else WARN,
                   f"legacy CSVs folded in on {vault.get_meta(vault.MIGRATION_KEY)}"
                   if vault.get_meta(vault.MIGRATION_KEY) else "not run yet", VAULT)
    except Exception as exc:
        result.add("SQLite database", FAIL, f"{type(exc).__name__}: {exc}", VAULT)

    try:
        loaded = templates.load_all()
        for template in loaded:
            result.add(f"Template {template.variant}", OK,
                       f"subject: {template.subject[:60]}", VAULT)
    except Exception as exc:
        result.add("A/B templates", FAIL, str(exc), VAULT)

    if cfg.sender_count > 1 and cfg.sender_count % 2 == 0:
        result.add("A/B vs rotation", WARN,
                   f"{cfg.sender_count} mailboxes with 2 variants alternate in step, "
                   "so each variant is always sent by the same mailbox. An odd "
                   "number of mailboxes keeps the test clean.", VAULT)

    if (ROOT / ".gitignore").exists():
        result.add(".gitignore", OK, "Present - .env and data/ stay out of git", STORAGE)
    else:
        result.add(".gitignore", WARN,
                   "Missing. Credentials and scraped data could be committed.", STORAGE)

    return result


def _probe_write(folder: Path) -> tuple[str, str]:
    """Actually write a file - a permissions check that only stats is a guess."""
    probe = Path(folder) / ".clipagent-write-probe"
    try:
        Path(folder).mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return OK, str(folder)
    except Exception as exc:
        return FAIL, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# (b) Mailboxes - the slow half
# ---------------------------------------------------------------------------
def run_smtp_checks(cfg: AppConfig, result: ScanResult | None = None,
                    job: Any = None) -> ScanResult:
    result = result or ScanResult()
    from core import cannon  # imported here to keep diagnostics importable alone

    senders = cfg.senders
    if not senders:
        result.add("Zoho login", FAIL,
                   "No mailbox configured, so nothing to test.", MAILBOXES)
        return result

    for index, sender in enumerate(senders, start=1):
        if job is not None and job.cancelled:
            result.add("Zoho login", WARN, "Scan stopped before finishing.", MAILBOXES)
            break
        if job is not None:
            job.report(message=f"Testing mailbox {index}/{len(senders)}: {sender.email}")
        ok, detail = cannon.verify_smtp(cfg, sender)
        result.add(f"Mailbox {index}: {sender.email}", OK if ok else FAIL,
                   detail, MAILBOXES)
        if job is not None:
            job.report(current=index, total=len(senders), log=detail)
    return result


def run_full_scan(*, job: Any, cfg: AppConfig, include_smtp: bool = True) -> ScanResult:
    """Everything, in order, with progress. Designed for a worker thread."""
    result = ScanResult()
    job.report(total=1, current=0, message="Checking config, engine and storage")
    run_static_checks(cfg, result)
    ok, warn, fail = result.tally()
    job.report(log=f"Static checks: {ok} passed, {warn} warnings, {fail} failed")

    if include_smtp and not job.cancelled:
        job.report(message="Testing Zoho mailboxes")
        run_smtp_checks(cfg, result, job)

    ok, warn, fail = result.tally()
    job.report(current=1, total=1,
               message=f"{ok} passed, {warn} warnings, {fail} failed")
    return result
