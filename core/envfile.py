"""Rewrite specific keys in a .env file without disturbing the rest.

The Control panel writes credentials the operator types - mailboxes, the Apollo
key, the proxy list - back into .env so they survive a restart. That file also
holds hand-written settings and comments, so this is a surgical rewriter, not a
dump: it replaces the lines for the keys it is given and leaves every other
line, comment and blank exactly where it was.

Every function takes an explicit ``path`` (defaulting to the real ``.env``) so
a test never touches the operator's live file. Writes are atomic - a temp file
in the same directory, then ``os.replace`` - so a crash mid-write can never
leave a half-written .env that strands the mailboxes.

Values are written as one single-quoted line each. MAILBOXES in particular is
written on one line (its parser accepts comma separation), so the multi-line
quoted form a hand-edited .env may use is never something this has to round
trip.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from core.config import ENV_PATH

# Characters that would break MAILBOXES round-tripping through parse_accounts,
# which splits entries on comma and newline and reads a display name after a
# pipe. A password or name containing one of these cannot be stored, so it is
# rejected at the door rather than written as data that will not parse back.
FORBIDDEN_IN_ENTRY = (",", "\n", "\r", "|", '"', "'")

# A .env value is wrapped in single quotes, so a literal single quote in the
# value would end it early. Applies to every value this module writes.
FORBIDDEN_IN_VALUE = ("\n", "\r", "'")


class EnvWriteError(ValueError):
    """A value that cannot be written to .env without corrupting it."""


def _quote(value: str) -> str:
    """A value as a single-quoted .env assignment fragment."""
    for bad in FORBIDDEN_IN_VALUE:
        if bad in value:
            raise EnvWriteError(
                f"value contains {bad!r}, which cannot be stored in .env")
    return f"'{value}'"


def read_all(path: Path | None = None) -> list[str]:
    """Every line of the file, or [] when it does not exist yet."""
    target = Path(path) if path is not None else ENV_PATH
    if not target.exists():
        return []
    return target.read_text(encoding="utf-8").splitlines()


def _key_of(line: str) -> str:
    """The KEY in a ``KEY=value`` line, or '' for a comment or blank."""
    stripped = line.lstrip()
    if not stripped or stripped.startswith("#"):
        return ""
    name, sep, _ = stripped.partition("=")
    return name.strip() if sep else ""


def set_values(updates: dict[str, str], path: Path | None = None) -> Path:
    """Apply ``{KEY: value}`` to the file, preserving everything else.

    A key already present is rewritten in place, keeping its position. A key not
    present is appended. A key mapped to ``None`` is left untouched; map it to
    ``""`` to write an explicit empty assignment (which is what clears a stale
    value, since ``load_dotenv(override=True)`` only overwrites keys that are
    actually present in the file).
    """
    target = Path(path) if path is not None else ENV_PATH
    pending = {k: v for k, v in updates.items() if v is not None}
    quoted = {k: f"{k}={_quote(v)}" for k, v in pending.items()}

    out: list[str] = []
    written: set[str] = set()
    for line in read_all(target):
        key = _key_of(line)
        if key in quoted:
            out.append(quoted[key])
            written.add(key)
        else:
            out.append(line)

    appended = [quoted[k] for k in pending if k not in written]
    if appended and out and out[-1].strip():
        # A hand-edited file may end without a trailing blank; keep the appended
        # block visually separate from whatever preceded it.
        out.append("")
    out.extend(appended)

    _atomic_write(target, "\n".join(out) + "\n")
    return target


def _atomic_write(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# --- MAILBOXES serialisation ------------------------------------------------
def mailbox_entry(*, host: str, port: int, email: str, password: str,
                  from_name: str = "") -> str:
    """One MAILBOXES entry in the host:port:email:password|name form.

    Rejects any field that would not survive the round trip through
    parse_accounts, so a stored list always parses back to what was entered.
    """
    email = (email or "").strip().lower()
    password = password or ""
    from_name = (from_name or "").strip()
    host = (host or "").strip()

    if not email or "@" not in email:
        raise EnvWriteError(f"'{email}' is not an email address")
    if not password:
        raise EnvWriteError(f"{email} has no app password")
    if not host:
        raise EnvWriteError(f"{email} has no SMTP host")
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise EnvWriteError(f"port '{port}' is not a number")
    if not 1 <= port <= 65535:
        raise EnvWriteError(f"port {port} is out of range")

    for field, label in ((password, "app password"), (from_name, "from name"),
                         (host, "host"), (email, "email")):
        for bad in FORBIDDEN_IN_ENTRY:
            if bad in field:
                raise EnvWriteError(
                    f"the {label} contains {bad!r}, which MAILBOXES cannot store")

    entry = f"{host}:{port}:{email}:{password}"
    return f"{entry}|{from_name}" if from_name else entry


def serialise_mailboxes(rows: list[dict]) -> str:
    """A list of mailbox dicts into a single-line MAILBOXES value.

    Each row: host, port, email, password, from_name. Raises on the first row
    that cannot be stored, naming it, so nothing partial is ever written.
    """
    entries = [mailbox_entry(host=r.get("host", ""), port=r.get("port", 0),
                             email=r.get("email", ""),
                             password=r.get("password", ""),
                             from_name=r.get("from_name", ""))
               for r in rows]
    return ",".join(entries)


def serialise_proxies(proxies: list[str]) -> str:
    """A proxy list into the comma-separated NEXUS_PROXY value."""
    clean = [p.strip() for p in proxies if p and p.strip()]
    for proxy in clean:
        for bad in FORBIDDEN_IN_VALUE + (",",):
            if bad in proxy:
                raise EnvWriteError(f"proxy '{proxy}' contains {bad!r}")
    return ",".join(clean)
