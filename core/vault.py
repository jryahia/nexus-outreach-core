"""The Vault - SQLite storage. Single source of truth for leads and sends.

Three tables:

* ``leads``          every scraped business, raw and purified, grouped by batch
* ``campaign_logs``  one row per send attempt, including A/B variant
* ``blacklist``      addresses never to contact again

Threading model, because this is the part that decides whether the promise of
"zero data loss" holds:

* One short-lived connection per operation, opened through ``connect()``.
  A connection is never cached in module scope or session state - the campaign
  worker writes while the two-second Analytics fragment reads, and sharing one
  handle across those threads is exactly the corruption this module exists to
  prevent.
* ``journal_mode=WAL`` set once at init, so a reader never blocks the writer.
* ``busy_timeout`` on every connect, so the rare write collision waits instead
  of raising.

Nothing here deletes a CSV. The old files stay on disk as a fallback.
"""

from __future__ import annotations

import csv
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

from core.config import CLEAN_DIR, DATA_DIR, LOG_PATH, RAW_DIR

DB_PATH = DATA_DIR / "leads.db"
BUSY_TIMEOUT_MS = 5000

# Kept in lockstep with purifier.LEAD_FIELDS. A mismatch surfaces as a KeyError
# inside executemany, on the worker thread, mid-scrape - selftest asserts parity.
LEAD_COLUMNS = ("name", "email", "website", "phone", "source", "handle", "location",
                "lead_type", "category")
LOG_COLUMNS = ("timestamp", "email", "sender", "status", "subject", "detail",
               "variant", "campaign")

RAW = "raw"
CLEAN = "clean"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS leads (
    id         INTEGER PRIMARY KEY,
    batch      TEXT NOT NULL,
    stage      TEXT NOT NULL,
    name       TEXT DEFAULT '',
    email      TEXT DEFAULT '',
    website    TEXT DEFAULT '',
    phone      TEXT DEFAULT '',
    source     TEXT DEFAULT '',
    handle     TEXT DEFAULT '',
    location   TEXT DEFAULT '',
    lead_type  TEXT DEFAULT '',
    category   TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(batch, email, name)
);
CREATE INDEX IF NOT EXISTS idx_leads_stage ON leads(stage, batch);
CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email);
CREATE INDEX IF NOT EXISTS idx_leads_website ON leads(website);
CREATE INDEX IF NOT EXISTS idx_leads_handle ON leads(handle);

CREATE TABLE IF NOT EXISTS campaign_logs (
    id        INTEGER PRIMARY KEY,
    timestamp TEXT NOT NULL,
    email     TEXT DEFAULT '',
    sender    TEXT DEFAULT '',
    status    TEXT DEFAULT '',
    subject   TEXT DEFAULT '',
    detail    TEXT DEFAULT '',
    variant   TEXT DEFAULT '',
    campaign  TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_logs_email_status ON campaign_logs(email, status);
CREATE INDEX IF NOT EXISTS idx_logs_status ON campaign_logs(status);

CREATE TABLE IF NOT EXISTS blacklist (
    email    TEXT PRIMARY KEY,
    reason   TEXT DEFAULT '',
    added_at TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Connection handling
# ---------------------------------------------------------------------------
@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """A fresh connection per operation, committed or rolled back on exit."""
    conn = sqlite3.connect(path or DB_PATH, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db(path: Path | None = None, migrate: bool = True) -> Path:
    """Create the schema, enable WAL, and fold any legacy CSVs in once."""
    target = path or DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(target)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        _add_missing_columns(conn)
        conn.commit()
    if migrate:
        migrate_csvs(target)
        backfill_lead_types(target)
        normalise_locations(target)
    return target


# Columns added after the first release. CREATE TABLE IF NOT EXISTS is skipped
# entirely on an existing database, so a new field in _SCHEMA never reaches a
# vault that already holds rows - every later INSERT then fails with
# "no such column". These ALTERs are what actually upgrade it, in place,
# without touching the data.
_ADDED_COLUMNS = (
    ("leads", "lead_type", "TEXT DEFAULT ''"),
    ("leads", "category", "TEXT DEFAULT ''"),
)


def _add_missing_columns(conn: sqlite3.Connection) -> list[str]:
    added = []
    for table, column, decl in _ADDED_COLUMNS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            added.append(f"{table}.{column}")
    return added


def table_columns(table: str = "leads", path: Path | None = None) -> list[str]:
    with connect(path) as conn:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def get_meta(key: str, default: str = "") -> str:
    with connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------
def _normalise(lead: dict) -> dict:
    row = {k: str(lead.get(k) or "").strip() for k in LEAD_COLUMNS}
    row["email"] = row["email"].lower()
    return row


def save_leads(batch: str, stage: str, leads: Iterable[dict],
               path: Path | None = None) -> int:
    """Insert a batch. INSERT OR IGNORE, so re-saving the same batch is a no-op."""
    rows = [_normalise(lead) for lead in leads]
    if not rows:
        return 0
    stamp = _now()
    # Built from LEAD_COLUMNS rather than written out: a hand-listed INSERT
    # silently drops any column added later, which is how lead_type went
    # missing on write while every other layer already knew about it.
    fields = ("batch", "stage", *LEAD_COLUMNS, "created_at")
    sql = (f"INSERT OR IGNORE INTO leads ({', '.join(fields)}) "
           f"VALUES ({', '.join(':' + f for f in fields)})")
    with connect(path) as conn:
        before = conn.total_changes
        conn.executemany(
            sql,
            [{**row, "batch": batch, "stage": stage, "created_at": stamp}
             for row in rows],
        )
        return conn.total_changes - before


def list_batches(stage: str, path: Path | None = None) -> list[dict]:
    """Batches newest first, with their size. Replaces globbing a folder."""
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT batch, COUNT(*) AS leads, MAX(created_at) AS created_at "
            "FROM leads WHERE stage = ? GROUP BY batch ORDER BY created_at DESC",
            (stage,),
        ).fetchall()
    return [dict(row) for row in rows]


def load_leads(batch: str, path: Path | None = None) -> list[dict]:
    with connect(path) as conn:
        rows = conn.execute(
            f"SELECT {', '.join(LEAD_COLUMNS)} FROM leads WHERE batch = ? "
            "ORDER BY id", (batch,),
        ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Deduplication - "have we already got this target?"
# ---------------------------------------------------------------------------
# Hosts where the first path segment IS the identity. reddit.com/r/VideoEditing
# and reddit.com/r/editors are two different targets; collapsing both to
# "reddit.com" would make the dedup gate reject every subreddit after the first
# one ever saved. Same for a Discord invite and a Disboard listing.
_PATH_SCOPED_HOSTS = {
    "reddit.com", "discord.gg", "discord.com", "disboard.org", "t.me",
    "linktr.ee", "patreon.com", "medium.com",
}


def norm_domain(url: str) -> str:
    """Bare registrable host, lowercased.

    http://x.com/ and https://www.x.com/nyc-video are the same business, so the
    path and the www are dropped before comparing. The exception is a
    multi-tenant host in ``_PATH_SCOPED_HOSTS``, where the first path segment
    is kept because it is what identifies the target.
    """
    value = (url or "").strip().lower()
    if not value:
        return ""
    if "//" in value:
        value = value.split("//", 1)[1]
    value = value.split("?", 1)[0].split("#", 1)[0]
    # Split host from path BEFORE discarding the path, so a scoped host can
    # still see its first segment.
    host, _, path = value.partition("/")
    host = host.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if host not in _PATH_SCOPED_HOSTS:
        return host
    segments = [seg for seg in path.split("/") if seg]
    if not segments:
        return host
    # reddit.com/r/Name -> reddit.com/r/name; disboard.org/server/join/123 keeps
    # the id, which is the server's identity.
    if segments[0] in {"r", "user", "u", "server"} and len(segments) > 1:
        scope = "/".join(segments[:3] if segments[0] == "server" else segments[:2])
    else:
        scope = segments[0]
    return f"{host}/{scope}"


def norm_handle(handle: str) -> str:
    return (handle or "").strip().lstrip("@").strip("/").lower()


def norm_name(name: str) -> str:
    return " ".join((name or "").split()).lower()


def known_targets(path: Path | None = None) -> dict[str, set[str]]:
    """Every target already in the vault, as normalised match keys.

    One query for the whole run: the hunter checks each candidate against
    these sets in memory rather than hitting SQLite per lead.
    """
    with connect(path) as conn:
        rows = conn.execute("SELECT website, handle, name, email FROM leads").fetchall()
    known = {"domains": set(), "handles": set(), "names": set(), "emails": set()}
    for row in rows:
        domain = norm_domain(row["website"])
        if domain:
            known["domains"].add(domain)
        handle = norm_handle(row["handle"])
        if handle:
            known["handles"].add(handle)
        name = norm_name(row["name"])
        if name:
            known["names"].add(name)
        email = (row["email"] or "").strip().lower()
        if email:
            known["emails"].add(email)
    return known


def match_known(lead: dict, known: dict[str, set[str]]) -> str:
    """Why this target is already on file, or '' if it is new."""
    domain = norm_domain(lead.get("website", ""))
    if domain and domain in known["domains"]:
        return f"website {domain}"
    handle = norm_handle(lead.get("handle", ""))
    if handle and handle in known["handles"]:
        return f"handle @{handle}"
    email = (lead.get("email") or "").strip().lower()
    if email and email in known["emails"]:
        return f"email {email}"
    name = norm_name(lead.get("name", ""))
    if name and name in known["names"]:
        return f"name {lead.get('name')}"
    return ""


def search_leads(stage: str | None = None, lead_type: str = "", source: str = "",
                 text: str = "", limit: int = 1000,
                 path: Path | None = None) -> list[dict]:
    """The CRM query behind the intelligence table."""
    where, params = [], []
    if stage:
        where.append("stage = ?")
        params.append(stage)
    if lead_type:
        where.append("lead_type = ?")
        params.append(lead_type)
    if source:
        where.append("source = ?")
        params.append(source)
    if text:
        where.append("(name LIKE ? OR email LIKE ? OR location LIKE ? "
                     "OR category LIKE ? OR handle LIKE ?)")
        params.extend([f"%{text}%"] * 5)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with connect(path) as conn:
        rows = conn.execute(
            f"SELECT {', '.join(LEAD_COLUMNS)}, batch FROM leads {clause} "
            f"ORDER BY id DESC LIMIT {int(limit)}", params,
        ).fetchall()
    return [dict(row) for row in rows]


def distinct_values(column: str, path: Path | None = None) -> list[str]:
    """Values present in a lead column, for building UI filters."""
    if column not in LEAD_COLUMNS:
        return []
    with connect(path) as conn:
        rows = conn.execute(
            f"SELECT DISTINCT {column} AS v FROM leads "
            f"WHERE {column} <> '' ORDER BY v"
        ).fetchall()
    return [row["v"] for row in rows]


def count_leads(stage: str | None = None, path: Path | None = None) -> int:
    with connect(path) as conn:
        if stage:
            row = conn.execute("SELECT COUNT(*) AS n FROM leads WHERE stage = ?",
                               (stage,)).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS n FROM leads").fetchone()
    return int(row["n"])


def delete_batch(batch: str, path: Path | None = None) -> int:
    with connect(path) as conn:
        cur = conn.execute("DELETE FROM leads WHERE batch = ?", (batch,))
        return cur.rowcount


# ---------------------------------------------------------------------------
# Campaign log
# ---------------------------------------------------------------------------
def log_event(row: dict, path: Path | None = None) -> None:
    payload = {key: str(row.get(key) or "") for key in LOG_COLUMNS}
    payload["timestamp"] = payload["timestamp"] or _now()
    payload["email"] = payload["email"].lower()
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO campaign_logs "
            "(timestamp, email, sender, status, subject, detail, variant, campaign) "
            "VALUES (:timestamp, :email, :sender, :status, :subject, :detail,"
            " :variant, :campaign)",
            payload,
        )


def read_log(limit: int | None = None, path: Path | None = None) -> list[dict]:
    query = f"SELECT {', '.join(LOG_COLUMNS)} FROM campaign_logs ORDER BY id"
    if limit:
        query = (f"SELECT {', '.join(LOG_COLUMNS)} FROM campaign_logs "
                 f"ORDER BY id DESC LIMIT {int(limit)}")
    with connect(path) as conn:
        rows = conn.execute(query).fetchall()
    result = [dict(row) for row in rows]
    return list(reversed(result)) if limit else result


def status_counts(path: Path | None = None) -> dict[str, int]:
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM campaign_logs GROUP BY status"
        ).fetchall()
    return {row["status"]: int(row["n"]) for row in rows}


def variant_counts(path: Path | None = None) -> list[dict]:
    """Sends per A/B variant per status - what the comparison chart reads."""
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT variant, status, COUNT(*) AS n FROM campaign_logs "
            "WHERE variant <> '' GROUP BY variant, status ORDER BY variant"
        ).fetchall()
    return [dict(row) for row in rows]


def clear_log(path: Path | None = None) -> int:
    with connect(path) as conn:
        cur = conn.execute("DELETE FROM campaign_logs")
        return cur.rowcount


# ---------------------------------------------------------------------------
# Blacklist
# ---------------------------------------------------------------------------
def add_to_blacklist(email: str, reason: str = "manual",
                     path: Path | None = None) -> None:
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO blacklist(email, reason, added_at) VALUES(?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET reason = excluded.reason",
            (email.strip().lower(), reason, _now()),
        )


def remove_from_blacklist(email: str, path: Path | None = None) -> int:
    with connect(path) as conn:
        cur = conn.execute("DELETE FROM blacklist WHERE email = ?",
                           (email.strip().lower(),))
        return cur.rowcount


def list_blacklist(path: Path | None = None) -> list[dict]:
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT email, reason, added_at FROM blacklist ORDER BY added_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def blocked_lookup(emails: Iterable[str], sent_status: str = "Sent",
                   path: Path | None = None) -> dict[str, str]:
    """Which of these addresses must not be contacted, and why.

    Only ``sent_status`` rows count as "already contacted". A dry run writes
    Preview rows, and if those counted, one rehearsal would permanently
    blacklist the entire list.
    """
    wanted = [e.strip().lower() for e in emails if e and e.strip()]
    if not wanted:
        return {}
    blocked: dict[str, str] = {}
    with connect(path) as conn:
        for chunk_start in range(0, len(wanted), 400):
            chunk = wanted[chunk_start:chunk_start + 400]
            marks = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT email, reason FROM blacklist WHERE email IN ({marks})", chunk
            ):
                blocked[row["email"]] = f"blacklisted ({row['reason']})"
            for row in conn.execute(
                f"SELECT DISTINCT email FROM campaign_logs "
                f"WHERE status = ? AND email IN ({marks})", (sent_status, *chunk)
            ):
                blocked.setdefault(row["email"], "already emailed in a past campaign")
    return blocked


def is_blocked(email: str, sent_status: str = "Sent",
               path: Path | None = None) -> str | None:
    return blocked_lookup([email], sent_status, path).get(email.strip().lower())


# ---------------------------------------------------------------------------
# One-time CSV migration
# ---------------------------------------------------------------------------
MIGRATION_KEY = "migrated_csv_v1"


def migrate_csvs(path: Path | None = None) -> dict[str, int]:
    """Fold the legacy CSV files into the database exactly once.

    Idempotent twice over: a meta flag stops the second run, and every insert
    is INSERT OR IGNORE against a UNIQUE constraint, so even a forced re-run
    cannot duplicate a row.
    """
    summary = {"raw": 0, "clean": 0, "logs": 0}
    if get_meta(MIGRATION_KEY):
        return summary

    for folder, stage in ((RAW_DIR, RAW), (CLEAN_DIR, CLEAN)):
        for csv_path in sorted(Path(folder).glob("*.csv")):
            try:
                with csv_path.open("r", newline="", encoding="utf-8-sig") as fh:
                    rows = list(csv.DictReader(fh))
            except Exception:
                continue
            summary[stage] += save_leads(csv_path.stem, stage, rows, path)

    if LOG_PATH.exists():
        try:
            with LOG_PATH.open("r", newline="", encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    log_event({**row, "variant": row.get("variant", ""),
                               "campaign": row.get("campaign", "migrated")}, path)
                    summary["logs"] += 1
        except Exception:
            pass

    set_meta(MIGRATION_KEY, _now())
    return summary


BACKFILL_KEY = "backfilled_lead_type_v1"
LOCATION_FIX_KEY = "normalised_location_v1"


def normalise_locations(path: Path | None = None) -> int:
    """Reduce a stored search query to the city it ended with.

    Early Maps runs stored the whole composed query ("Video production agency
    in New York") in ``location``. The CRM view wants a city, so those rows are
    trimmed once, in place.
    """
    if get_meta(LOCATION_FIX_KEY):
        return 0
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT id, location FROM leads WHERE location LIKE '% in %'"
        ).fetchall()
        updates = [(row["location"].rsplit(" in ", 1)[-1].strip(), row["id"])
                   for row in rows]
        if updates:
            conn.executemany("UPDATE leads SET location = ? WHERE id = ?", updates)
    set_meta(LOCATION_FIX_KEY, _now())
    return len(updates)


def backfill_lead_types(path: Path | None = None) -> int:
    """Classify rows that predate the lead_type column. Runs once."""
    if get_meta(BACKFILL_KEY):
        return 0
    from core.purifier import classify_lead

    with connect(path) as conn:
        rows = conn.execute(
            "SELECT id, name, category, source FROM leads "
            "WHERE lead_type IS NULL OR lead_type = ''"
        ).fetchall()
        updates = [
            (classify_lead(name=row["name"], category=row["category"],
                           source=row["source"]), row["id"])
            for row in rows
        ]
        if updates:
            conn.executemany("UPDATE leads SET lead_type = ? WHERE id = ?", updates)
    set_meta(BACKFILL_KEY, _now())
    return len(updates)


def journal_mode(path: Path | None = None) -> str:
    with connect(path) as conn:
        return str(conn.execute("PRAGMA journal_mode").fetchone()[0])


def stats(path: Path | None = None) -> dict:
    """Everything the Analytics tab and the diagnostic need, in one round trip."""
    with connect(path) as conn:
        raw = conn.execute("SELECT COUNT(*) AS n FROM leads WHERE stage='raw'").fetchone()
        clean = conn.execute(
            "SELECT COUNT(*) AS n FROM leads WHERE stage='clean'").fetchone()
        logs = conn.execute("SELECT COUNT(*) AS n FROM campaign_logs").fetchone()
        blocked = conn.execute("SELECT COUNT(*) AS n FROM blacklist").fetchone()
    return {"raw": int(raw["n"]), "clean": int(clean["n"]),
            "logs": int(logs["n"]), "blacklist": int(blocked["n"])}
