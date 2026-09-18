"""The Cannon - multi-inbox Zoho sender with spintax, rotation and rest breaks.

Four things make this safe to run unattended:

* Sends rotate round-robin across every mailbox in ``cfg.senders``, so no single
  Zoho account carries the whole volume. The active sender owns the From header
  and the Message-ID domain - a rotated send that still claims to be from
  mailbox #1 is worse than not rotating at all.
* The gap between emails is ``random.uniform(min_delay, max_delay)`` and, every
  ``cfg.coffee_every`` sends, a longer coffee break. Both wait on
  ``job.wait(...)``, i.e. ``threading.Event.wait``, so STOP lands mid-gap
  instead of minutes late.
* Every send writes a row to ``data/campaign_log.csv`` and to ``job.rows``, so
  the Analytics tab survives a rerun and a restart.
* Nothing raises out of the worker thread. A per-lead failure is logged and the
  campaign continues; a mailbox that fails authentication is dropped from the
  rotation and the rest carry on. Only losing every mailbox aborts the run.
"""

from __future__ import annotations

import random
import re
import smtplib
import ssl
import time
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Any

from core import vault
from core.config import AppConfig, SenderAccount
from core.templates import Template

MERGE_FIELDS = ("name", "website", "location", "handle", "email")

# Innermost {a|b|c} group - one with no nested braces. Resolving repeatedly
# from the inside out is what gives nested spintax for free.
_SPIN_RE = re.compile(r"\{([^{}]*)\}")

_PLACEHOLDER_RE = re.compile(r"\[(" + "|".join(MERGE_FIELDS) + r")\]", re.I)

MAX_SPIN_PASSES = 20

LOG_FIELDS = ("timestamp", "email", "sender", "status", "subject", "detail",
              "variant", "campaign")

SENT = "Sent"
FAILED = "Failed"
SKIPPED = "Skipped"
PREVIEW = "Preview"  # dry run: rendered and logged, never put on the wire


# ---------------------------------------------------------------------------
# Templating
# ---------------------------------------------------------------------------
def spin(text: str, rng: random.Random | None = None) -> str:
    """Resolve {Hi|Hello|Hey} spintax, innermost group first, nesting allowed."""
    if not text:
        return ""
    rng = rng or random
    out = text
    for _ in range(MAX_SPIN_PASSES):
        new = _SPIN_RE.sub(lambda m: rng.choice(m.group(1).split("|")), out)
        if new == out:
            return new
        out = new
    return out  # malformed template - return best effort rather than loop


def render(template: str, lead: dict, rng: random.Random | None = None) -> str:
    """Apply spintax, then substitute [name] / [website] / [location] / ... ."""
    text = spin(template, rng)

    def sub(match: re.Match[str]) -> str:
        return str(lead.get(match.group(1).lower(), "") or "").strip()

    return _PLACEHOLDER_RE.sub(sub, text)


def build_message(cfg: AppConfig, sender: SenderAccount, lead: dict, subject: str,
                  body: str, rng: random.Random | None = None) -> EmailMessage:
    """Build one email. The From header and Message-ID follow the ACTIVE sender."""
    msg = EmailMessage()
    msg["From"] = formataddr((sender.from_name or cfg.smtp.from_name or "", sender.email))
    msg["To"] = lead["email"]
    msg["Subject"] = render(subject, lead, rng)
    if cfg.smtp.reply_to:
        msg["Reply-To"] = cfg.smtp.reply_to
    msg["Message-ID"] = make_msgid(domain=sender.domain)

    text = render(body, lead, rng)
    footer = "\n".join(p for p in (cfg.unsubscribe_line, cfg.postal_address) if p)
    if footer:
        text = f"{text.rstrip()}\n\n--\n{footer}\n"
    msg.set_content(text)
    return msg


# ---------------------------------------------------------------------------
# SMTP
# ---------------------------------------------------------------------------
def _connect(cfg: AppConfig, sender: SenderAccount) -> smtplib.SMTP:
    context = ssl.create_default_context()
    if cfg.smtp.use_ssl:
        server: smtplib.SMTP = smtplib.SMTP_SSL(
            cfg.smtp.host, cfg.smtp.port, timeout=30, context=context
        )
    else:
        server = smtplib.SMTP(cfg.smtp.host, cfg.smtp.port, timeout=30)
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
    server.login(sender.email, sender.app_password)
    return server


class SenderPool:
    """One live SMTP connection per mailbox, reconnected on demand.

    Zoho closes idle sessions well inside a five-minute gap, and with rotation
    a given mailbox may sit unused for much longer than that, so every
    connection is probed with NOOP before use rather than trusted.
    """

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self._connections: dict[str, smtplib.SMTP] = {}
        self.disabled: dict[str, str] = {}

    def is_enabled(self, sender: SenderAccount) -> bool:
        return sender.email not in self.disabled

    def disable(self, sender: SenderAccount, reason: str) -> None:
        self.disabled[sender.email] = reason
        self.drop(sender)

    def drop(self, sender: SenderAccount) -> None:
        server = self._connections.pop(sender.email, None)
        if server is not None:
            try:
                server.close()
            except Exception:
                pass

    def get(self, sender: SenderAccount) -> smtplib.SMTP:
        server = self._connections.get(sender.email)
        if server is not None:
            try:
                code, _ = server.noop()
                if code == 250:
                    return server
            except Exception:
                pass
            self.drop(sender)
        server = _connect(self.cfg, sender)
        self._connections[sender.email] = server
        return server

    def close_all(self) -> None:
        for server in list(self._connections.values()):
            try:
                server.quit()
            except Exception:
                pass
        self._connections.clear()


def verify_smtp(cfg: AppConfig, sender: SenderAccount | None = None) -> tuple[bool, str]:
    """Open a connection and authenticate one mailbox. Never raises."""
    if sender is None:
        missing = cfg.smtp.missing_fields()
        if missing:
            return False, "Missing in .env: " + ", ".join(missing)
        sender = SenderAccount(cfg.smtp.email, cfg.smtp.app_password, cfg.smtp.from_name)
    try:
        server = _connect(cfg, sender)
    except smtplib.SMTPAuthenticationError as exc:
        return False, (
            f"{sender.email}: Zoho rejected the login ({exc.smtp_code}). Use an "
            "app-specific password from Zoho Security > App Passwords."
        )
    except Exception as exc:
        return False, f"{sender.email}: {type(exc).__name__}: {exc}"
    try:
        server.noop()
        return True, f"{sender.email} connected to {cfg.smtp.host}:{cfg.smtp.port}"
    finally:
        try:
            server.quit()
        except Exception:
            pass


def verify_all_senders(cfg: AppConfig) -> list[tuple[bool, str]]:
    """Check every mailbox in the rotation. Used by the Setup tab."""
    if not cfg.senders:
        return [verify_smtp(cfg)]
    return [verify_smtp(cfg, sender) for sender in cfg.senders]


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------
def log_event(job: Any, email: str, sender: str, status: str, subject: str = "",
              detail: str = "", variant: str = "", campaign: str = "") -> dict:
    """Record one send to job.rows (live table) and the vault (durable history).

    The database write is the one that survives a crash; job.rows only feeds
    the table on screen. A failed write must never stop a campaign, so it is
    logged into the activity feed rather than raised.
    """
    row = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "email": email,
        "sender": sender,
        "status": status,
        "subject": subject,
        "detail": detail,
        "variant": variant,
        "campaign": campaign,
    }
    if job is not None:
        job.report(row=row)
    try:
        vault.log_event(row)
    except Exception as exc:
        if job is not None:
            job.report(log=f"WARNING could not write to the vault: "
                           f"{type(exc).__name__}: {exc}")
    return row


def read_log(limit: int | None = None) -> list[dict]:
    """Every send this machine has ever made, oldest first."""
    try:
        return vault.read_log(limit)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Campaign
# ---------------------------------------------------------------------------
NO_MAILBOX = "(no mailbox configured)"


def _senders_for(cfg: AppConfig) -> list[SenderAccount]:
    if cfg.senders:
        return list(cfg.senders)
    if cfg.smtp.email:
        return [SenderAccount(cfg.smtp.email, cfg.smtp.app_password, cfg.smtp.from_name)]
    # Dry runs are deliberately allowed before credentials exist. Name the gap
    # rather than writing a blank Sender column into the analytics log.
    return [SenderAccount(NO_MAILBOX, "", cfg.smtp.from_name)]


def assign_variant(index: int, templates: list[Template]) -> Template:
    """Lead 1 gets A, lead 2 gets B, lead 3 gets A - straight alternation."""
    return templates[index % len(templates)]


def rotation_is_confounded(senders: list[SenderAccount],
                           templates: list[Template]) -> bool:
    """True when variant and mailbox move in lockstep.

    Alternating variants positionally while also rotating mailboxes positionally
    means that with an even number of mailboxes, variant A is always sent by
    mailbox 1 and variant B always by mailbox 2. The A/B result then measures
    the mailboxes as much as the copy.
    """
    if len(templates) < 2 or len(senders) < 2:
        return False
    return len(senders) % len(templates) == 0


def send_campaign(
    *,
    job: Any,
    cfg: AppConfig,
    leads: list[dict],
    subject: str = "",
    body: str = "",
    templates: list[Template] | None = None,
    min_delay: int,
    max_delay: int,
    dry_run: bool = False,
    campaign: str = "",
) -> dict:
    rng = random.Random()
    if max_delay < min_delay:
        min_delay, max_delay = max_delay, min_delay

    variants = list(templates or [Template(variant="", subject=subject, body=body)])
    senders = _senders_for(cfg)
    campaign = campaign or datetime.now().strftime("campaign-%Y%m%d-%H%M")

    queue = leads[: cfg.daily_cap] if cfg.daily_cap > 0 else list(leads)
    held_back = leads[len(queue):]

    stats = {"sent": 0, "failed": 0, "skipped": len(held_back), "blocked": 0,
             "dry_run": dry_run, "senders": len(senders),
             "variants": {t.variant: 0 for t in variants if t.variant}}

    rotation = (f"rotating {len(senders)} mailboxes" if len(senders) > 1
                else f"single mailbox {senders[0].email}")
    ab = f", A/B across {len(variants)} templates" if len(variants) > 1 else ""
    job.report(total=len(queue), current=0,
               message=("Dry run - nothing will be sent" if dry_run
                        else f"Starting campaign, {rotation}"))
    job.report(log=f"{len(queue)} leads, {rotation}{ab}")

    if rotation_is_confounded(senders, variants):
        job.report(log=f"WARNING {len(senders)} mailboxes and {len(variants)} variants "
                       "alternate in step - each variant will always be sent by the "
                       "same mailbox, which confounds the A/B result. Use an odd "
                       "number of mailboxes.")

    for lead in held_back:
        log_event(job, lead.get("email", ""), "-", SKIPPED,
                  detail=f"over daily cap of {cfg.daily_cap}", campaign=campaign)
    if held_back:
        job.report(log=f"Daily cap {cfg.daily_cap}: holding back {len(held_back)} leads")

    # One query for the whole queue rather than one per lead. Nothing else
    # writes Sent rows mid-run, so a single lookup stays accurate.
    try:
        blocked = vault.blocked_lookup([lead.get("email", "") for lead in queue],
                                       sent_status=SENT)
    except Exception as exc:
        blocked = {}
        job.report(log=f"WARNING blacklist unavailable ({type(exc).__name__}) - "
                       "sending without it")
    if blocked:
        job.report(log=f"Blacklist: {len(blocked)} of {len(queue)} leads will be skipped")

    pool = SenderPool(cfg)
    sent_since_break = 0

    try:
        for index, lead in enumerate(queue):
            if job.cancelled:  # honoured even if STOP lands during an SMTP send
                job.report(log=f"Stopped before lead {index + 1}")
                break

            email = (lead.get("email") or "").strip().lower()

            # The blacklist gate. Checked before the message is built, so a
            # blocked address never reaches an SMTP connection at all.
            reason = blocked.get(email)
            if reason:
                stats["blocked"] += 1
                stats["skipped"] += 1
                log_event(job, email, "-", SKIPPED, detail=reason, campaign=campaign)
                job.report(log=f"skipped {email} | {reason}",
                           current=index + 1,
                           message=f"{stats['sent']} sent, {stats['blocked']} skipped")
                continue

            template = assign_variant(index, variants)

            # Round-robin: lead 1 -> mailbox 1, lead 2 -> mailbox 2, and so on,
            # skipping any mailbox that has been dropped from the rotation.
            sender = senders[index % len(senders)]
            if not pool.is_enabled(sender):
                alive = [s for s in senders if pool.is_enabled(s)]
                if not alive:
                    raise RuntimeError(
                        "Every Zoho mailbox failed to authenticate. Check "
                        "ZOHO_ACCOUNTS in .env."
                    )
                sender = alive[index % len(alive)]

            tag = f"[{template.variant}] " if template.variant else ""
            subject_line = ""
            try:
                msg = build_message(cfg, sender, lead, template.subject,
                                    template.body, rng)
                subject_line = msg["Subject"]
                if dry_run:
                    first = (msg.get_content().strip().splitlines() or [""])[0]
                    log_event(job, email, sender.email, PREVIEW, subject_line, first,
                              template.variant, campaign)
                    job.report(log=f"[dry] {tag}{sender.email} -> {email} | "
                                   f"{subject_line}")
                else:
                    pool.get(sender).send_message(msg)
                    log_event(job, email, sender.email, SENT, subject_line, "",
                              template.variant, campaign)
                    job.report(log=f"sent {tag}{sender.email} -> {email} | "
                                   f"{subject_line}")
                stats["sent"] += 1
                sent_since_break += 1
                if template.variant:
                    stats["variants"][template.variant] += 1
            except smtplib.SMTPAuthenticationError as exc:
                # One bad mailbox must not end the run - drop it and carry on.
                pool.disable(sender, f"auth failed ({exc.smtp_code})")
                stats["failed"] += 1
                log_event(job, email, sender.email, FAILED, subject_line,
                          f"auth failed ({exc.smtp_code}) - mailbox removed",
                          template.variant, campaign)
                job.report(log=f"AUTH FAILED {sender.email} - dropped from rotation")
                if len(pool.disabled) >= len(senders):
                    raise RuntimeError(
                        "Every Zoho mailbox failed to authenticate. Check "
                        "ZOHO_ACCOUNTS in .env."
                    ) from exc
                continue  # retry this lead on the next mailbox
            except (smtplib.SMTPRecipientsRefused, smtplib.SMTPDataError) as exc:
                stats["failed"] += 1
                log_event(job, email, sender.email, FAILED, subject_line,
                          f"{type(exc).__name__}: {exc}", template.variant, campaign)
                job.report(log=f"BOUNCED {email} | {type(exc).__name__}: {exc}")
                pool.drop(sender)
            except Exception as exc:
                stats["failed"] += 1
                log_event(job, email, sender.email, FAILED, subject_line,
                          f"{type(exc).__name__}: {exc}", template.variant, campaign)
                job.report(log=f"FAILED {email} | {type(exc).__name__}: {exc}")
                pool.drop(sender)

            job.report(current=index + 1,
                       message=f"{stats['sent']} sent, {stats['failed']} failed")

            if index + 1 >= len(queue):
                break

            # Coffee break first, so a break and a normal gap never stack.
            if cfg.coffee_every > 0 and sent_since_break >= cfg.coffee_every:
                sent_since_break = 0
                rest = rng.uniform(cfg.coffee_min, cfg.coffee_max)
                back = time.strftime("%H:%M", time.localtime(time.time() + rest))
                job.report(message=f"Coffee break {rest/60:.0f} min - back around {back}",
                           log=f"Coffee break after {cfg.coffee_every} sends "
                               f"({rest/60:.0f} min)",
                           state={"phase": "break", "seconds": rest,
                                  "until": time.time() + rest, "back_at": back})
                if job.wait(rest):
                    job.report(log="Stopped during the coffee break", state={})
                    break
                job.report(state={})
                continue

            delay = rng.uniform(min_delay, max_delay)
            resume = time.strftime("%H:%M:%S", time.localtime(time.time() + delay))
            job.report(message=f"Waiting {delay/60:.1f} min - next send around {resume}",
                       state={"phase": "delay", "seconds": delay,
                              "until": time.time() + delay, "back_at": resume})
            if job.wait(delay):  # Event.wait: STOP returns immediately
                job.report(log="Stopped during the delay", state={})
                break
            job.report(state={})
    finally:
        pool.close_all()

    if pool.disabled:
        stats["disabled_senders"] = dict(pool.disabled)
    verb = "previewed" if dry_run else "sent"
    tail = f", {stats['blocked']} skipped by the blacklist" if stats["blocked"] else ""
    job.report(message=f"{stats['sent']} {verb}, {stats['failed']} failed{tail}")
    return stats
