"""Offline self-test. No network, no SMTP socket.

    .venv\\Scripts\\python.exe tools\\selftest.py
"""

from __future__ import annotations

import inspect
import random
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import cannon, hunter, purifier, templates, vault  # noqa: E402
from core.config import (  # noqa: E402
    AppConfig, SenderAccount, SmtpConfig, parse_accounts, parse_accounts_report,
)
from ui.state import Job, start_job  # noqa: E402

FAILURES: list[str] = []

# Never touch the real vault from a test run. Every vault call defaults to
# vault.DB_PATH, so redirecting it here redirects cannon too.
TEST_DB = Path(tempfile.gettempdir()) / "clipagent-selftest.db"
for suffix in ("", "-wal", "-shm"):
    Path(str(TEST_DB) + suffix).unlink(missing_ok=True)
vault.DB_PATH = TEST_DB
vault.init_db(TEST_DB, migrate=False)


def check(label: str, condition: bool, detail: object = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{'  ' + str(detail) if detail else ''}")
    if not condition:
        FAILURES.append(label)


# ---------------------------------------------------------------------------
print("\nspintax")
rng = random.Random(7)
picks = {cannon.spin("{Hi|Hello|Hey} there", rng) for _ in range(60)}
check("all three variants appear", picks == {"Hi there", "Hello there", "Hey there"},
      str(sorted(picks)))
check("nested groups resolve",
      cannon.spin("{Hi {Bob|Ann}|Yo}", random.Random(1)) in {"Hi Bob", "Hi Ann", "Yo"})
check("no braces survive", "{" not in cannon.spin("{a|b} {c|{d|e}}", rng))
check("plain text untouched", cannon.spin("no spintax here", rng) == "no spintax here")
check("empty is safe", cannon.spin("", rng) == "")
check("malformed does not hang", isinstance(cannon.spin("{a|b", rng), str))

# ---------------------------------------------------------------------------
print("\nmulti-sender parsing")
one = parse_accounts("a@x.com:pass1")
check("single account", [(s.email, s.app_password) for s in one] == [("a@x.com", "pass1")])

two = parse_accounts("a@x.com:pass1,b@y.com:pass2")
check("comma separated", [s.email for s in two] == ["a@x.com", "b@y.com"], str(two))

multi = parse_accounts("a@x.com:pass1\nb@y.com:pass2\n")
check("newline separated", [s.email for s in multi] == ["a@x.com", "b@y.com"])

colon = parse_accounts("a@x.com:pa:ss:1")
check("password keeps its colons", colon[0].app_password == "pa:ss:1",
      colon[0].app_password)

named = parse_accounts("a@x.com:pass1|Display Name", "Fallback")
check("pipe sets the display name", named[0].from_name == "Display Name")
check("default name applied", parse_accounts("a@x.com:p", "Fallback")[0].from_name
      == "Fallback")
check("empty value yields nothing", parse_accounts("") == [])
check("junk lines ignored", parse_accounts("not-an-account,b@y.com:p")[0].email == "b@y.com")
check("duplicates collapsed", len(parse_accounts("a@x.com:1,a@x.com:2")) == 1)
check("quotes stripped", parse_accounts('"a@x.com:pass1"')[0].email == "a@x.com")
check("domain falls back when there is no @",
      SenderAccount("nothing", "", "").domain == "localhost")
check("dry run without credentials still names a sender",
      cannon._senders_for(AppConfig())[0].email == cannon.NO_MAILBOX)

# ---------------------------------------------------------------------------
print("\nrender + message build")
lead = {"name": "Tone Films", "website": "http://tonefilms.com/",
        "location": "New York", "email": "sam@tonefilms.com", "handle": ""}
out = cannon.render("{Hi|Hello} [name], saw [website] in [location].", lead, random.Random(3))
check("placeholders substituted",
      "Tone Films" in out and "tonefilms.com" in out and "New York" in out, out)
check("unknown placeholder left alone", "[nope]" in cannon.render("[nope]", lead))

sender = SenderAccount("me@mydomain.com", "x", "Sender")
cfg = AppConfig(
    smtp=SmtpConfig(email=sender.email, app_password="x", from_name="Sender",
                    reply_to="reply@mydomain.com"),
    senders=[sender],
    unsubscribe_line="Reply STOP and I will not contact you again.",
)
msg = cannon.build_message(cfg, sender, lead, "Idea for [name]", "{Hi|Hello} [name],",
                           random.Random(5))
check("From header formatted", msg["From"] == "Sender <me@mydomain.com>", msg["From"])
check("To is the lead", msg["To"] == "sam@tonefilms.com")
check("subject merged", msg["Subject"] == "Idea for Tone Films", msg["Subject"])
check("reply-to set", msg["Reply-To"] == "reply@mydomain.com")
check("unsubscribe footer appended", "Reply STOP" in msg.get_content())
check("message-id on the sender domain", "@mydomain.com>" in msg["Message-ID"],
      msg["Message-ID"])

# ---------------------------------------------------------------------------
print("\nemail extraction")
html = """
 <a href="mailto:Sarah.Lee@Tonefilms.com">write us</a>
 <img src="/assets/logo@2x.png"> sentry@o1234.ingest.sentry.io
 press (at) tonefilms (dot) com  info@tonefilms.com
"""
got = purifier.extract_emails(html)
check("mailto harvested", "sarah.lee@tonefilms.com" in got, str(got))
check("obfuscated (at)/(dot) decoded", "press@tonefilms.com" in got, str(got))
check("image filename rejected", not any(g.endswith(".png") for g in got))
check("analytics domain rejected", not any("sentry.io" in g for g in got))
check("role prefix detected", purifier.is_role_address("info@x.com"))
check("careers inbox detected", purifier.is_role_address("resume@x.com"))
check("wholesales is not sales", not purifier.is_role_address("wholesales@x.com"))

# ---------------------------------------------------------------------------
print("\npurify")
fixture = [
    {"name": "A", "email": "Anna@alpha.com"},
    {"name": "A dup", "email": "anna@ALPHA.com"},
    {"name": "B", "email": "info@beta.com"},
    {"name": "C", "email": "not-an-email"},
    {"name": "D", "email": ""},
    {"name": "E", "email": "erik@epsilon.io"},
]
res = purifier.purify(fixture, drop_generic=True)
check("kept 2", res.kept == 2, str([lead["email"] for lead in res.leads]))
check("1 duplicate", res.duplicates == 1)
check("1 invalid", res.invalid == 1)
check("1 blank", res.no_email == 1)
check("1 role dropped", res.generic == 1)
check("total counted", res.total_in == 6)
check("role kept when toggled off", purifier.purify(fixture, drop_generic=False).kept == 3)

tmp = Path(tempfile.gettempdir()) / "clipagent-selftest.csv"
purifier.save_csv(res.leads, tmp)
round_trip = purifier.load_csv(tmp)
check("csv round-trip", [r["email"] for r in round_trip] == [r["email"] for r in res.leads])
check("csv has full schema", set(round_trip[0]) == set(purifier.LEAD_FIELDS))
tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
def make_leads(count: int) -> list[dict]:
    return [{"name": f"Biz {i}", "email": f"a{i}@x.com", "website": "", "location": "NY",
             "handle": "", "phone": "", "source": "test"} for i in range(count)]


print("\nround-robin rotation, 3 mailboxes x 7 leads")
senders = [SenderAccount(f"box{i}@mydomain.com", "pw", f"Sender {i}") for i in (1, 2, 3)]
rot_cfg = AppConfig(
    smtp=SmtpConfig(email=senders[0].email, app_password="pw", from_name="Sender"),
    senders=senders, daily_cap=0, coffee_every=0,
)
rot_job = Job(key="rotation")
rot_stats = cannon.send_campaign(
    job=rot_job, cfg=rot_cfg, leads=make_leads(7), subject="Hi [name]",
    body="{Hi|Hello} [name]", min_delay=0, max_delay=0, dry_run=True,
)
used = [row["sender"] for row in rot_job.snapshot()["rows"]]
expected = [senders[i % 3].email for i in range(7)]
check("all 7 previewed", rot_stats["sent"] == 7, str(rot_stats))
check("senders cycle 1,2,3,1,2,3,1", used == expected, str(used[:4]))
check("stats record the rotation size", rot_stats["senders"] == 3)

# The From header must follow the ACTIVE sender, not mailbox #1.
froms = [cannon.build_message(rot_cfg, senders[i % 3], make_leads(7)[i],
                              "s", "b")["From"] for i in range(7)]
check("From header follows the active sender",
      froms == [f"Sender {(i % 3) + 1} <box{(i % 3) + 1}@mydomain.com>" for i in range(7)],
      froms[0])

print("\nevent log")
rows = rot_job.snapshot()["rows"]
check("one row per lead", len(rows) == 7)
check("row schema matches the table", set(rows[0]) == set(cannon.LOG_FIELDS), str(set(rows[0])))
check("dry run marked Preview, never Sent",
      {r["status"] for r in rows} == {cannon.PREVIEW})
persisted = cannon.read_log()
check("log persisted to sqlite", len(persisted) == 7, str(len(persisted)))
check("vault keeps the sender column", persisted[1]["sender"] == senders[1].email)
check("WAL enabled", vault.journal_mode() == "wal", vault.journal_mode())

print("\ndaily cap")
cap_cfg = AppConfig(smtp=rot_cfg.smtp, senders=senders, daily_cap=3, coffee_every=0)
cap_job = Job(key="cap")
cap_stats = cannon.send_campaign(
    job=cap_job, cfg=cap_cfg, leads=make_leads(5), subject="s", body="b",
    min_delay=0, max_delay=0, dry_run=True,
)
check("cap sends only 3", cap_stats["sent"] == 3, str(cap_stats))
check("cap skips the rest", cap_stats["skipped"] == 2)
check("skipped rows logged",
      sum(1 for r in cap_job.snapshot()["rows"] if r["status"] == cannon.SKIPPED) == 2)

# ---------------------------------------------------------------------------
print("\ncoffee break, interruptible")
brk_cfg = AppConfig(
    smtp=rot_cfg.smtp, senders=senders[:1], daily_cap=0,
    coffee_every=2, coffee_min=600, coffee_max=600,
)
brk_job = Job(key="break")
brk_stats: dict = {}


def run_break() -> None:
    brk_stats.update(cannon.send_campaign(
        job=brk_job, cfg=brk_cfg, leads=make_leads(5), subject="s", body="b",
        min_delay=0, max_delay=0, dry_run=True,
    ))


thread = threading.Thread(target=run_break, daemon=True)
thread.start()
time.sleep(1.0)
snap = brk_job.snapshot()
check("break starts after 2 sends", snap["current"] == 2, str(snap["current"]))
check("break message shown", "Coffee break" in snap["message"], snap["message"])
check("break logged", any("Coffee break" in line for line in snap["log"]))
check("phase published as break", snap["state"].get("phase") == "break", str(snap["state"]))
check("break countdown has a duration", snap["state"].get("seconds") == 600)
check("break countdown has an end time",
      snap["state"].get("until", 0) > time.time(), str(snap["state"].get("until")))
check("break names a return time", bool(snap["state"].get("back_at")))

start = time.perf_counter()
brk_job.stop_event.set()
thread.join(timeout=5)
elapsed = time.perf_counter() - start
check("STOP honoured mid-break", elapsed < 1.0, f"{elapsed:.3f}s")
check("thread exited", not thread.is_alive())
check("break logged the stop",
      any("Stopped during the coffee break" in line for line in brk_job.snapshot()["log"]))
check("phase cleared after the stop", brk_job.snapshot()["state"] == {},
      str(brk_job.snapshot()["state"]))

# ---------------------------------------------------------------------------
print("\ndiagnostics")
from core import diagnostics  # noqa: E402

scan = diagnostics.run_static_checks(AppConfig())
by_name = {c.name: c for c in scan.checks}
check("scan covers every group",
      {c.group for c in scan.checks} >= {diagnostics.ENVIRONMENT, diagnostics.CREDENTIALS,
                                         diagnostics.RHYTHM, diagnostics.SCRAPER,
                                         diagnostics.STORAGE})
check("scrapling detected", by_name["scrapling"].status == diagnostics.OK,
      by_name["scrapling"].detail)
check("stealth fetchers detected", by_name["Fetchers (stealth)"].status == diagnostics.OK)
check("browser binaries found", by_name["Browser binaries"].status == diagnostics.OK,
      by_name["Browser binaries"].detail)
check("plotly detected", by_name["plotly"].status == diagnostics.OK)
check("data folders writable",
      all(c.status == diagnostics.OK for c in scan.checks
          if c.name.startswith("Write access")))
check("missing credentials reported as failure",
      by_name["App password"].status == diagnostics.FAIL)
check("no password ever printed",
      not any("pass" in (c.detail or "").lower() and ":" in (c.detail or "")
              for c in scan.checks))

bad = diagnostics.run_static_checks(
    AppConfig(smtp=SmtpConfig(email="a@b.com", app_password="x"),
              senders=[SenderAccount("a@b.com", "x")],
              min_delay=600, max_delay=60, coffee_every=5, coffee_min=900, coffee_max=60)
)
bad_by_name = {c.name: c for c in bad.checks}
check("inverted send gap caught", bad_by_name["Send gap"].status == diagnostics.FAIL,
      bad_by_name["Send gap"].detail)
check("inverted coffee range caught",
      bad_by_name["Coffee break"].status == diagnostics.FAIL)

accounts, problems = parse_accounts_report("good@x.com:p,broken-entry,bad@:p,c@y.com:")
check("report keeps the good account", [a.email for a in accounts] == ["good@x.com"])
check("report names each problem", len(problems) == 3, str(problems))
check("problem entries are masked",
      all(":***" in p["entry"] or ":" not in p["entry"] for p in problems), str(problems))

smtp_scan = diagnostics.run_smtp_checks(AppConfig())
check("smtp check without credentials fails cleanly",
      len(smtp_scan.checks) == 1 and smtp_scan.checks[0].status == diagnostics.FAIL,
      smtp_scan.checks[0].detail)


# ---------------------------------------------------------------------------
print("\nthe vault")
# Its own database: the diagnostics section above runs init_db(migrate=True),
# which folds the real CSVs into whatever DB_PATH points at.
VAULT_DB = Path(tempfile.gettempdir()) / "clipagent-vault-test.db"
for suffix in ("", "-wal", "-shm"):
    Path(str(VAULT_DB) + suffix).unlink(missing_ok=True)
vault.DB_PATH = VAULT_DB
vault.init_db(VAULT_DB, migrate=False)

vault.save_leads("batch-1", vault.RAW, make_leads(4))
vault.save_leads("batch-1", vault.RAW, make_leads(4))          # re-save is a no-op
check("leads stored once", vault.count_leads(vault.RAW) == 4, vault.count_leads(vault.RAW))
check("batch listing", [b["batch"] for b in vault.list_batches(vault.RAW)] == ["batch-1"])
check("round-trip keeps fields",
      vault.load_leads("batch-1")[0]["email"] == "a0@x.com")
vault.save_leads("clean-batch-1", vault.CLEAN, make_leads(2))
check("stages counted apart",
      (vault.count_leads(vault.RAW), vault.count_leads(vault.CLEAN)) == (4, 2))

# A worker thread writes while the main thread reads - the exact pattern the
# campaign job and the Analytics fragment use.
concurrent_error: list[str] = []


def writer() -> None:
    try:
        for i in range(30):
            vault.log_event({"timestamp": "", "email": f"t{i}@x.com", "sender": "s",
                             "status": "Preview", "subject": "s", "detail": "",
                             "variant": "A", "campaign": "concurrency"})
    except Exception as exc:
        concurrent_error.append(f"{type(exc).__name__}: {exc}")


writer_thread = threading.Thread(target=writer, daemon=True)
writer_thread.start()
reads = 0
while writer_thread.is_alive():
    try:
        vault.read_log()
        reads += 1
    except Exception as exc:
        concurrent_error.append(f"read: {type(exc).__name__}: {exc}")
        break
writer_thread.join(timeout=10)
check("concurrent read while writing", not concurrent_error, str(concurrent_error[:1]))
check("all concurrent writes landed",
      sum(1 for r in vault.read_log() if r["campaign"] == "concurrency") == 30)
vault.clear_log()

# ---------------------------------------------------------------------------
print("\nmigration is idempotent")
mig_db = Path(tempfile.gettempdir()) / "clipagent-migration.db"
for suffix in ("", "-wal", "-shm"):
    Path(str(mig_db) + suffix).unlink(missing_ok=True)
vault.DB_PATH = mig_db
vault.init_db(mig_db, migrate=True)
first = vault.stats()
vault.migrate_csvs(mig_db)                     # second run must change nothing
second = vault.stats()
check("migration imported the CSVs", first["raw"] > 0 or first["clean"] > 0, str(first))
check("second migration changes nothing", first == second, f"{first} vs {second}")
check("migration flag recorded", bool(vault.get_meta(vault.MIGRATION_KEY)))
for suffix in ("", "-wal", "-shm"):
    Path(str(mig_db) + suffix).unlink(missing_ok=True)
for suffix in ("", "-wal", "-shm"):
    Path(str(TEST_DB) + suffix).unlink(missing_ok=True)
vault.DB_PATH = TEST_DB
vault.init_db(TEST_DB, migrate=False)

# ---------------------------------------------------------------------------
print("\nblacklist")
bl_cfg = AppConfig(smtp=SmtpConfig(email="me@x.com", app_password="pw"),
                   senders=[SenderAccount("me@x.com", "pw", "Me")],
                   daily_cap=0, coffee_every=0)
targets = make_leads(3)

dry_job = Job(key="bl-dry")
cannon.send_campaign(job=dry_job, cfg=bl_cfg, leads=targets, subject="s", body="b",
                     min_delay=0, max_delay=0, dry_run=True)
after_dry = Job(key="bl-dry2")
stats_after_dry = cannon.send_campaign(job=after_dry, cfg=bl_cfg, leads=targets,
                                       subject="s", body="b", min_delay=0, max_delay=0,
                                       dry_run=True)
check("a dry run never blacklists anyone", stats_after_dry["blocked"] == 0,
      str(stats_after_dry))
check("dry run still previews everything", stats_after_dry["sent"] == 3)

# A real Sent row must block the address next time.
vault.log_event({"timestamp": "", "email": "a1@x.com", "sender": "me@x.com",
                 "status": cannon.SENT, "subject": "s", "detail": "",
                 "variant": "A", "campaign": "past"})
vault.add_to_blacklist("a2@x.com", "bounced hard")
gate_job = Job(key="bl-gate")
gate = cannon.send_campaign(job=gate_job, cfg=bl_cfg, leads=targets, subject="s",
                            body="b", min_delay=0, max_delay=0, dry_run=True)
check("past Sent row blocks that address", gate["blocked"] == 2, str(gate))
check("only the untouched lead goes out", gate["sent"] == 1)
skipped_rows = [r for r in gate_job.snapshot()["rows"] if r["status"] == cannon.SKIPPED]
check("skipped rows carry a reason", len(skipped_rows) == 2, str(len(skipped_rows)))
check("reason names the blacklist",
      any("blacklisted" in r["detail"] for r in skipped_rows), str(skipped_rows))
check("reason names the past campaign",
      any("already emailed" in r["detail"] for r in skipped_rows))
check("skipped rows persisted",
      sum(1 for r in vault.read_log() if r["status"] == cannon.SKIPPED) == 2)
check("is_blocked agrees", bool(vault.is_blocked("a2@x.com")))
check("removing from the blacklist clears it",
      vault.remove_from_blacklist("a2@x.com") == 1 and not vault.is_blocked("a2@x.com"))
vault.clear_log()

# ---------------------------------------------------------------------------
print("\nA/B testing")
templates.ensure_defaults()
variants = templates.load_all()
check("two templates load", [t.variant for t in variants] == ["A", "B"])
check("subject parsed off the front", all(t.subject and t.body for t in variants))
check("parse handles a missing subject",
      templates.parse("just a body").subject == "")

ab_cfg = AppConfig(smtp=SmtpConfig(email="box1@x.com", app_password="pw"),
                   senders=[SenderAccount(f"box{i}@x.com", "pw", f"S{i}")
                            for i in (1, 2, 3)],
                   daily_cap=0, coffee_every=0)
ab_job = Job(key="ab")
ab_stats = cannon.send_campaign(job=ab_job, cfg=ab_cfg, leads=make_leads(6),
                                templates=variants, min_delay=0, max_delay=0,
                                dry_run=True)
rows = ab_job.snapshot()["rows"]
check("variants alternate A,B,A,B,A,B",
      [r["variant"] for r in rows] == ["A", "B", "A", "B", "A", "B"],
      str([r["variant"] for r in rows]))
check("split is even", ab_stats["variants"] == {"A": 3, "B": 3}, str(ab_stats["variants"]))
pairs = {(r["sender"], r["variant"]) for r in rows}
check("every mailbox sends both variants", len(pairs) == 6, str(sorted(pairs)))
check("variant stored in the vault",
      {r["variant"] for r in vault.read_log()} == {"A", "B"})
counts = {(r["variant"], r["status"]): r["n"] for r in vault.variant_counts()}
check("variant_counts feeds the chart",
      counts.get(("A", cannon.PREVIEW)) == 3 and counts.get(("B", cannon.PREVIEW)) == 3,
      str(counts))

check("even mailbox count is flagged as confounded",
      cannon.rotation_is_confounded(ab_cfg.senders[:2], variants))
check("odd mailbox count is clean",
      not cannon.rotation_is_confounded(ab_cfg.senders, variants))
vault.clear_log()


# ---------------------------------------------------------------------------
print("\nCRM schema parity")
check("LEAD_FIELDS matches vault.LEAD_COLUMNS",
      set(purifier.LEAD_FIELDS) == set(vault.LEAD_COLUMNS),
      set(purifier.LEAD_FIELDS) ^ set(vault.LEAD_COLUMNS))
check("every CRM field is present",
      {"name", "email", "phone", "location", "lead_type", "source", "website"}
      <= set(purifier.LEAD_FIELDS))
check("a blank lead carries the full schema",
      set(hunter._blank_lead("Google Maps")) == set(purifier.LEAD_FIELDS))

print("\nclassifier")
cases = [
    # (name, bio, category, source, expected) - ordering traps included
    ("video production agency", "", "", "Google Maps", "Agency"),
    ("Tone Films", "", "Video production service", "Google Maps", "Production Studio"),
    ("someclipper", "I clip podcasts into shorts. DM for rates", "", "TikTok", "Clipper"),
    ("janedoe", "Freelance video editor, available for hire", "", "Instagram",
     "Freelancer"),
    ("bigcreator", "YouTuber and streamer", "", "Instagram", "Content Creator"),
    ("Smith Photography", "", "Photographer", "Google Maps", "Photographer"),
    ("Acme Solutions LLC", "", "", "Google Maps", "Service Business"),
    ("", "", "", "Instagram", "Content Creator"),
]
for name, bio, category, source, expected in cases:
    got = purifier.classify_lead(name=name, bio=bio, category=category, source=source)
    check(f"{expected:18} <- {(name or bio or source)[:34]!r}", got == expected, got)
check("unknown input never returns an empty cell",
      purifier.classify_lead() == purifier.UNKNOWN_TYPE, purifier.classify_lead())
check("category outranks the name",
      purifier.classify_lead(name="Bob Freelance", category="Video production service")
      == "Production Studio")

print("\nphone extraction")
real_bio = ("\U0001F4CD Los Angeles \n\U0001F3E0 Luxury Real Estate & New Development"
            "\n\U0001F4F2 310.743.5398\n\U0001F468500M+ | Team Sales")
check("finds the number in a real bio",
      purifier.extract_phone(real_bio) == "310.743.5398",
      purifier.extract_phone(real_bio))
check("does not read follower counts as phones",
      purifier.extract_phone("500M+ followers 12K likes 80% reply rate") == "",
      purifier.extract_phone("500M+ followers 12K likes 80% reply rate"))
check("keeps a leading paren",
      purifier.extract_phone("Open 24 hours - (212) 765-5555") == "(212) 765-5555",
      purifier.extract_phone("Open 24 hours - (212) 765-5555"))
check("handles an international format",
      purifier.extract_phone("Call +39 06 1234 5678") == "+39 06 1234 5678")
check("empty input is safe", purifier.extract_phone("") == "")
check("too few digits rejected", purifier.extract_phone("call 12-34") == "")

print("\nlocation extraction")
check("pin marker", hunter._location_from_bio(real_bio) == "Los Angeles",
      hunter._location_from_bio(real_bio))
check("phrase form",
      hunter._location_from_bio("Agency based in Rome, we make films") == "Rome")
check("city and state",
      hunter._location_from_bio("Weddings in Austin, TX all summer") == "Austin, TX")
check("nothing to find", hunter._location_from_bio("just a bio") == "")

print("\nMaps card category parsing")
card = ("Tone Films Tone Films 5.0 (48) Video production service · "
        "28-07 Jackson Ave 5th Floor · Open · Closes 10 PM · (917) 267-9264")
check("category pulled off the card",
      hunter._parse_card_category(card, "Tone Films") == "Video production service",
      hunter._parse_card_category(card, "Tone Films"))
check("opening hours are not mistaken for a category",
      "Open" not in hunter._parse_card_category(card, "Tone Films"))

print("\npurifier fills a missing type")
filled = purifier.purify([{"name": "Acme Agency", "email": "sam@acme.com",
                           "source": "Google Maps"}])
check("classified on the way through", filled.leads[0]["lead_type"] == "Agency",
      filled.leads[0]["lead_type"])
check("csv round-trip keeps the CRM fields",
      set(purifier.LEAD_FIELDS) <= set(filled.leads[0]))

# ---------------------------------------------------------------------------
print("\nschema upgrade against a copy of the real vault")
import shutil  # noqa: E402

from core.config import DATA_DIR  # noqa: E402

REAL_DB = DATA_DIR / "leads.db"
if REAL_DB.exists():
    copy_db = Path(tempfile.gettempdir()) / "clipagent-realcopy.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(copy_db) + suffix).unlink(missing_ok=True)
    shutil.copy2(REAL_DB, copy_db)

    saved_db = vault.DB_PATH
    vault.DB_PATH = copy_db
    # Strip the new columns to recreate a pre-upgrade database.
    with vault.connect(copy_db) as conn:
        for column in ("lead_type", "category"):
            try:
                conn.execute(f"ALTER TABLE leads DROP COLUMN {column}")
            except Exception:
                pass
        conn.execute("DELETE FROM meta WHERE key = ?", (vault.BACKFILL_KEY,))
    before_cols = vault.table_columns(path=copy_db)
    before_rows = vault.stats(copy_db)

    vault.init_db(copy_db, migrate=False)
    vault.backfill_lead_types(copy_db)
    after_cols = vault.table_columns(path=copy_db)
    after_rows = vault.stats(copy_db)

    check("started without lead_type", "lead_type" not in before_cols, before_cols[-3:])
    check("lead_type added in place", "lead_type" in after_cols)
    check("category added in place", "category" in after_cols)
    check("no rows lost", before_rows == after_rows, f"{before_rows} vs {after_rows}")
    # Derived from the copy, not hard-coded: the real vault grows every time a
    # live scrape is saved, and a fixed number turns this into a false alarm.
    total_leads = before_rows["raw"] + before_rows["clean"]
    check("real leads preserved",
          after_rows["raw"] + after_rows["clean"] == total_leads, after_rows)

    classified = [r for r in vault.search_leads(path=copy_db, limit=10000)
                  if r["lead_type"]]
    check("every existing row was backfilled", len(classified) == total_leads,
          f"{len(classified)} of {total_leads}")
    check("backfill produced real labels",
          {r["lead_type"] for r in classified} <= {
              "Agency", "Production Studio", "Clipper", "Content Creator",
              "Freelancer", "Photographer", "Service Business",
              purifier.UNKNOWN_TYPE},
          {r["lead_type"] for r in classified})

    vault.init_db(copy_db, migrate=False)          # second run must be a no-op
    check("second upgrade changes nothing",
          vault.table_columns(path=copy_db) == after_cols
          and vault.stats(copy_db) == after_rows)

    vault.DB_PATH = saved_db
    for suffix in ("", "-wal", "-shm"):
        Path(str(copy_db) + suffix).unlink(missing_ok=True)
else:
    check("real vault present to test against", False, "data/leads.db missing")

print("\nCRM queries")
for suffix in ("", "-wal", "-shm"):
    Path(str(TEST_DB) + suffix).unlink(missing_ok=True)
vault.DB_PATH = TEST_DB
vault.init_db(TEST_DB, migrate=False)
vault.save_leads("crm", vault.RAW, [
    {"name": "Acme Agency", "email": "a@acme.com", "lead_type": "Agency",
     "source": "Google Maps", "location": "Rome", "phone": "+39 06 1234 5678"},
    {"name": "clipguy", "email": "c@clip.com", "lead_type": "Clipper",
     "source": "TikTok", "location": "Berlin"},
])
check("search returns everything", len(vault.search_leads()) == 2)
check("filter by type", [r["name"] for r in vault.search_leads(lead_type="Clipper")]
      == ["clipguy"])
check("filter by source",
      [r["name"] for r in vault.search_leads(source="Google Maps")] == ["Acme Agency"])
check("free-text search hits the city",
      [r["name"] for r in vault.search_leads(text="Rome")] == ["Acme Agency"])
check("distinct types for the filter",
      sorted(vault.distinct_values("lead_type")) == ["Agency", "Clipper"])
check("phone survives the round trip",
      vault.search_leads(lead_type="Agency")[0]["phone"] == "+39 06 1234 5678")
for suffix in ("", "-wal", "-shm"):
    Path(str(TEST_DB) + suffix).unlink(missing_ok=True)
vault.init_db(TEST_DB, migrate=False)

# ---------------------------------------------------------------------------
print("\ndedup keys")
check("domain ignores scheme, www and path",
      vault.norm_domain("http://www.Tonefilms.com/nyc-video/") == "tonefilms.com",
      vault.norm_domain("http://www.Tonefilms.com/nyc-video/"))
check("domain drops a port", vault.norm_domain("https://x.com:8443/a") == "x.com")
check("empty url is safe", vault.norm_domain("") == "")

# Multi-tenant hosts keep their first path segment. Without this every
# subreddit after the first one saved collapses onto "reddit.com", and the
# dedup gate then rejects the entire source as already-seen.
check("subreddit keeps its path scope",
      vault.norm_domain("https://www.reddit.com/r/VideoEditing/")
      == "reddit.com/r/videoediting",
      vault.norm_domain("https://www.reddit.com/r/VideoEditing/"))
check("two subreddits do not collide",
      vault.norm_domain("https://www.reddit.com/r/VideoEditing/")
      != vault.norm_domain("https://www.reddit.com/r/editors/"))
check("disboard invite keeps its server id",
      vault.norm_domain("https://disboard.org/server/join/12345")
      == "disboard.org/server/join/12345")
check("two discord servers do not collide",
      vault.norm_domain("https://disboard.org/server/join/111")
      != vault.norm_domain("https://disboard.org/server/join/222"))
check("a bare reddit link still normalises",
      vault.norm_domain("https://reddit.com") == "reddit.com")
check("handle loses its at sign", vault.norm_handle("@SomeUser/") == "someuser")
check("name collapses whitespace", vault.norm_name("  Tone   Films ") == "tone films")

for suffix in ("", "-wal", "-shm"):
    Path(str(TEST_DB) + suffix).unlink(missing_ok=True)
vault.DB_PATH = TEST_DB
vault.init_db(TEST_DB, migrate=False)
vault.save_leads("seed", vault.RAW, [
    {"name": "Tone Films", "email": "a@tonefilms.com",
     "website": "http://tonefilms.com/nyc", "handle": "", "source": "Google Maps"},
    {"name": "clipguy", "email": "", "website": "", "handle": "@clipguy",
     "source": "TikTok"},
])
known = vault.known_targets()
check("known set built", len(known["domains"]) == 1 and len(known["handles"]) == 1,
      known)
check("match on a different url for the same site",
      vault.match_known({"website": "https://www.tonefilms.com/about"}, known)
      == "website tonefilms.com")
check("match on handle",
      vault.match_known({"handle": "clipguy"}, known) == "handle @clipguy")
check("match on email",
      vault.match_known({"email": "A@Tonefilms.com"}, known).startswith("email"))
check("match on name",
      vault.match_known({"name": "tone   films"}, known).startswith("name"))
check("a new target does not match",
      vault.match_known({"website": "https://brand-new.example",
                         "name": "Brand New"}, known) == "")

print("\ndedup gate in the hunter")
gate_job = Job(key="gate")
armed = hunter.load_known(True, gate_job)
check("gate loads the vault keys", len(armed["domains"]) == 1, armed["domains"])
check("gate logs that it is armed",
      any("Dedup gate armed" in line for line in gate_job.snapshot()["log"]))
off_job = Job(key="gateoff")
disarmed = hunter.load_known(False, off_job)
check("gate disabled yields empty sets",
      all(not values for values in disarmed.values()), disarmed)
check("disabled gate never matches",
      vault.match_known({"website": "https://tonefilms.com"}, disarmed) == "")
check("skip note is the wording asked for",
      hunter.SKIPPED_NOTE == "Skipped (Already in Vault)", hunter.SKIPPED_NOTE)
for name in ("scrape_google_maps", "scrape_instagram", "scrape_tiktok"):
    import inspect as _inspect
    params = _inspect.signature(getattr(hunter, name)).parameters
    check(f"{name} takes skip_known",
          "skip_known" in params and params["skip_known"].default is True)

# ---------------------------------------------------------------------------
print("\ngeocoding, offline")
from core import geo, network  # noqa: E402

check("bundled city resolves", geo.resolve("Rome") == geo.CITIES["rome"])
check("case and spacing ignored", geo.resolve("  NEW YORK ") == geo.CITIES["new york"])
check("state suffix stripped", geo.resolve("Austin, TX") == geo.CITIES["austin"])
check("falls back past a district",
      geo.resolve("Brooklyn, New York") == geo.CITIES["new york"])
check("unknown city returns None", geo.resolve("Nowhereville") is None)
check("empty is safe", geo.resolve("") is None)

geo.remember("Nowhereville", 1.5, 2.5)
check("cached coordinate is found", geo.resolve("Nowhereville") == (1.5, 2.5))
check("cache survives a fresh read", "nowhereville" in geo.cached())

points, unplaced = geo.city_points([
    {"location": "Rome", "email": "a@b.com", "lead_type": "Agency"},
    {"location": "Rome", "email": "", "lead_type": "Clipper"},
    {"location": "New York", "email": "c@d.com", "lead_type": "Agency"},
    {"location": "Atlantis", "email": "", "lead_type": "Agency"},
    {"location": "", "email": "", "lead_type": ""},
])
by_city = {p["city"]: p for p in points}
check("cities grouped", sorted(by_city) == ["New York", "Rome"], sorted(by_city))
check("lead volume counted", by_city["Rome"]["leads"] == 2)
check("emails counted separately", by_city["Rome"]["emails"] == 1)
check("types joined for the tooltip", "Agency" in by_city["Rome"]["types"])
check("busiest city sorts first", points[0]["city"] == "Rome")
check("unplaced reported, not dropped", unplaced == ["Atlantis (1)"], unplaced)

# ---------------------------------------------------------------------------
print("\nnetwork graph")
rows = [
    {"name": "Bliss Agency", "email": "a@bliss.it", "lead_type": "Agency",
     "location": "Rome", "phone": "+39 06 1"},
    {"name": "KSD", "email": "", "lead_type": "Agency", "location": "Rome"},
    {"name": "Tone Films", "email": "b@tone.com", "lead_type": "Production Studio",
     "location": "New York"},
]
graph = network.build(rows)
ids = {node["id"] for node in graph.nodes}
check("a hub per type", {network.TYPE_PREFIX + "Agency",
                         network.TYPE_PREFIX + "Production Studio"} <= ids)
check("a hub per location", {network.LOCATION_PREFIX + "Rome",
                             network.LOCATION_PREFIX + "New York"} <= ids)
check("a node per lead", graph.shown == 3 and graph.total == 3)
check("two edges per lead", len(graph.edges) == 6, len(graph.edges))
check("leads with an email glow brighter",
      {n["color"] for n in graph.nodes if n["id"].startswith(network.LEAD_PREFIX)}
      == {network.LEAD_COLOR, network.LEAD_DIM})
check("every edge points at a real node",
      all(e["source"] in ids and e["target"] in ids for e in graph.edges))
check("signature is stable", network.build(rows).signature == graph.signature)
check("signature moves when the data does",
      network.build(rows[:2]).signature != graph.signature)

capped = network.build(rows, max_nodes=1)
check("node cap respected", capped.shown == 1 and capped.hidden == 2)
check("overflow node added",
      any(n["id"] == network.OVERFLOW_ID for n in capped.nodes))
check("empty input is safe", network.build([]).total == 0)

options = network.physics_options()
check("physics uses the barnesHut solver",
      options["physics"]["solver"] == "barnesHut")
check("stabilisation enabled so it settles",
      options["physics"]["stabilization"]["enabled"] is True)
check("groups overridden, agraph would send null",
      options["groups"] == {})
check("arrows disabled the way vis.js spells it",
      options["edges"]["arrows"]["to"]["enabled"] is False)
check("spring length is tunable",
      network.physics_options(spring_length=300)["physics"]["barnesHut"]
      ["springLength"] == 300)
check("repulsion is negative, as barnesHut wants",
      network.physics_options(repulsion=9000)["physics"]["barnesHut"]
      ["gravitationalConstant"] == -9000)

for suffix in ("", "-wal", "-shm"):
    Path(str(TEST_DB) + suffix).unlink(missing_ok=True)
vault.init_db(TEST_DB, migrate=False)


# ---------------------------------------------------------------------------
print("\ndry-run campaign, stopped mid-delay")
job = Job(key="selftest")
stats: dict = {}


def run() -> None:
    stats.update(cannon.send_campaign(
        job=job, cfg=cfg, leads=make_leads(3), subject="Hi [name]",
        body="{Hi|Hello} [name]", min_delay=120, max_delay=360, dry_run=True,
    ))


thread = threading.Thread(target=run, daemon=True)
thread.start()
time.sleep(1.5)
snap = job.snapshot()
check("first email previewed without sending", snap["current"] == 1, str(snap["current"]))
check("waiting message shows the gap", "Waiting" in snap["message"], snap["message"])

start = time.perf_counter()
job.stop_event.set()
thread.join(timeout=5)
elapsed = time.perf_counter() - start
check("STOP honoured mid-delay", elapsed < 1.0, f"{elapsed:.3f}s")
check("thread exited", not thread.is_alive())
check("campaign returned stats", stats.get("sent") == 1, str(stats))
check("dry run logged, nothing sent",
      any(line.startswith("[dry]") for line in job.snapshot()["log"]))

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
print("\njob dispatch")
# Regression: start_job's second parameter used to be called `target`, which is
# also the keyword every social scraper takes. Every Instagram, TikTok, Reddit
# and Discord hunt therefore died with "got multiple values for argument
# 'target'" the instant the button was pressed - before any worker thread
# existed, so no job ever reported the failure. Positional-only parameters make
# the collision impossible; these checks make sure it stays that way.
_sig = inspect.signature(start_job)
_kinds = [p.kind for p in _sig.parameters.values()]
check("start_job takes its callable positionally",
      _kinds[:2] == [inspect.Parameter.POSITIONAL_ONLY,
                     inspect.Parameter.POSITIONAL_ONLY],
      str([k.name for k in _kinds]))
for _kw in ("target", "keyword", "location", "fn", "key"):
    try:
        _sig.bind("hunt", lambda **kw: None, cfg=None, max_results=10,
                  skip_known=True, **{_kw: "x"})
        _ok = True
    except TypeError as _exc:
        _ok = False
    check(f"a scraper kwarg named {_kw!r} still dispatches", _ok)

# Every hunter the Hunt tab can dispatch must accept the kwargs it sends.
for _name, _fn in (("scrape_google_maps", hunter.scrape_google_maps),
                   ("scrape_instagram", hunter.scrape_instagram),
                   ("scrape_tiktok", hunter.scrape_tiktok),
                   ("scrape_reddit", hunter.scrape_reddit),
                   ("scrape_discord", hunter.scrape_discord)):
    _p = inspect.signature(_fn).parameters
    _args = {"keyword": "k", "location": "l"} if _name == "scrape_google_maps" \
        else {"target": "t"}
    try:
        inspect.signature(_fn).bind(job=None, cfg=None, max_results=10,
                                    skip_known=True, **_args)
        _ok = True
    except TypeError:
        _ok = False
    check(f"{_name} accepts what the Hunt tab sends", _ok, str(list(_p)))

print("\ncommunity sources")
check("Reddit and Discord are selectable",
      {"Reddit", "Discord"} <= set(hunter.SOURCES), str(hunter.SOURCES))
check("both are keyword driven",
      hunter.KEYWORD_SOURCES == ("Reddit", "Discord"))
check("a community classifies as Community, not Unclassified",
      purifier.classify_lead(name="SomeServer", source="Discord") == "Community")
check("a keyword in the blurb outranks the source default",
      purifier.classify_lead(name="r/x", bio="video editor", source="Reddit")
      == "Clipper")
# An empty keyword must cost zero network calls, not scrape everything.
check("blank keyword returns nothing, offline",
      hunter.scrape_reddit(job=Job(key="t"), cfg=cfg, target="  ") == []
      and hunter.scrape_discord(job=Job(key="t"), cfg=cfg, target="") == [])

for suffix in ("", "-wal", "-shm"):
    Path(str(TEST_DB) + suffix).unlink(missing_ok=True)
print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILED: {FAILURES}"))
raise SystemExit(1 if FAILURES else 0)
