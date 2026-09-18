"""The Purifier - dedupe, validate, filter.

Also owns the shared lead schema and the email-extraction helpers, because The
Hunter needs them too and this keeps the import graph one-directional
(hunter -> purifier, never the reverse).
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from email_validator import EmailNotValidError, validate_email

LEAD_FIELDS = ("name", "email", "website", "phone", "source", "handle", "location",
               "lead_type", "category")

# Role inboxes. Matched against the exact local-part, never as a substring, so
# "wholesales@" is not mistaken for "sales@".
GENERIC_PREFIXES = {
    "info", "support", "contact", "hello", "help", "admin", "sales",
    "noreply", "no-reply", "donotreply", "office", "team", "mail",
    "enquiries", "inquiries", "enquiry", "service", "services", "billing",
    "accounts", "webmaster", "postmaster", "abuse", "press", "media",
    "careers", "jobs", "hr", "legal", "privacy", "marketing", "newsletter",
    "resume", "resumes", "apply", "applications", "hiring", "talent",
    "recruiting", "recruitment", "internships",
    "booking", "bookings", "reservations", "orders", "shop", "studio",
}

EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}"
)

# Obfuscations that small business sites use to dodge naive scrapers.
_DEOBFUSCATE = (
    (re.compile(r"\s*\(at\)\s*|\s*\[at\]\s*|\s+at\s+", re.I), "@"),
    (re.compile(r"\s*\(dot\)\s*|\s*\[dot\]\s*|\s+dot\s+", re.I), "."),
)

# Domains that show up in page source but never belong to the business:
# analytics, CDNs, site builders, stock placeholders.
NOISE_DOMAINS = {
    "sentry.io", "sentry-cdn.com", "wixpress.com", "wix.com", "squarespace.com",
    "godaddy.com", "shopify.com", "cloudflare.com", "google.com", "gstatic.com",
    "googleapis.com", "facebook.com", "example.com", "example.org", "domain.com",
    "yourdomain.com", "email.com", "sentry.wixpress.com", "w3.org", "schema.org",
    "jquery.com", "bootstrapcdn.com", "fontawesome.com", "adobe.com",
}

# File extensions that the regex can swallow out of image filenames such as
# "logo@2x.png" or a spam-protected "[email protected]" asset.
_BAD_TLDS = {
    "png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js", "json", "ico",
    "woff", "woff2", "ttf", "eot", "mp4", "webm", "pdf", "php", "html",
}


@dataclass
class PurifyResult:
    leads: list[dict] = field(default_factory=list)
    total_in: int = 0
    no_email: int = 0
    invalid: int = 0
    duplicates: int = 0
    generic: int = 0

    @property
    def kept(self) -> int:
        return len(self.leads)


# ---------------------------------------------------------------------------
# Extraction helpers (used by The Hunter)
# ---------------------------------------------------------------------------
def extract_emails(text: str) -> list[str]:
    """Pull plausible business emails out of raw HTML or bio text.

    Handles (at)/(dot) obfuscation and drops the usual false positives:
    image filenames, analytics and site-builder domains.
    """
    if not text:
        return []
    cleaned = text
    for pattern, repl in _DEOBFUSCATE:
        cleaned = pattern.sub(repl, cleaned)

    found: list[str] = []
    seen: set[str] = set()
    for raw in EMAIL_RE.findall(cleaned):
        addr = raw.strip(".-_").lower()
        local, _, domain = addr.partition("@")
        tld = domain.rsplit(".", 1)[-1]
        if tld in _BAD_TLDS or domain in NOISE_DOMAINS:
            continue
        if any(domain.endswith("." + noise) for noise in NOISE_DOMAINS):
            continue
        if len(local) < 2 or len(domain) < 4 or ".." in addr:
            continue
        if addr not in seen:
            seen.add(addr)
            found.append(addr)
    return found


def is_role_address(email: str) -> bool:
    local = email.split("@", 1)[0].lower()
    return local in GENERIC_PREFIXES


# ---------------------------------------------------------------------------
# CRM enrichment: phone numbers and lead classification
# ---------------------------------------------------------------------------
UNKNOWN_TYPE = "Unclassified"

# Ordered, and the order is the logic. The first rule that matches wins, so
# the most specific label has to come first: "video production agency" must
# land on Agency, not Content Creator, even though it contains "video".
# Do not sort these or fold them into a plain dict.
_TYPE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Agency", ("agency", "agencia", "agenzia", "agentur", "consultancy",
                "consulting", "marketing firm", "media group", "media house",
                "collective", "partners", "associates")),
    ("Production Studio", ("production", "productions", "studio", "studios",
                           "post production", "film", "films", "cinema",
                           "pictures", "media co", "video company")),
    # Freelancer outranks Clipper: "freelance video editor" is more useful in a
    # CRM as a freelancer than as a clipper, and "freelance" is the stronger,
    # rarer signal of the two.
    # "dm for rates" is deliberately absent: it says someone is open to work,
    # not what they are, and it outranks the real signal in a bio like
    # "I clip podcasts into shorts. DM for rates".
    ("Freelancer", ("freelance", "freelancer", "self employed", "self-employed",
                    "available for hire", "open for work", "hire me")),
    ("Clipper", ("clipper", "clipping", "clips", "editor", "editing",
                 "shorts", "reels editor", "video editor", "montage")),
    ("Content Creator", ("creator", "influencer", "youtuber", "streamer",
                         "tiktoker", "vlogger", "podcast", "ugc")),
    ("Photographer", ("photograph", "photography", "photographer", "foto")),
    ("Service Business", ("service", "services", "solutions", "ltd", "llc",
                          "inc", "gmbh", "srl", "company")),
)

_SOURCE_DEFAULTS = {
    "Google Maps": "Service Business",
    "Instagram": "Content Creator",
    "TikTok": "Content Creator",
    # A subreddit or a Discord server is a community, not a business. Without
    # these every community row would fall through to Unclassified.
    "Reddit": "Community",
    "Discord": "Community",
}


def classify_lead(name: str = "", bio: str = "", category: str = "",
                  source: str = "") -> str:
    """Best-guess CRM category for a target.

    The Google Maps category ("Video production service") is the strongest
    signal when present, so it is scanned first; then the bio, then the name.
    Falls back to something sensible per source rather than an empty cell.
    """
    for haystack in (category, bio, name):
        text = (haystack or "").lower()
        if not text.strip():
            continue
        for label, keywords in _TYPE_RULES:
            if any(keyword in text for keyword in keywords):
                return label
    return _SOURCE_DEFAULTS.get(source, UNKNOWN_TYPE)


# 7+ digits once separators are stripped. Two details that matter:
#   - the separator class holds a literal space, never \s: with \s a bio like
#     "310.743.5398\n500M+" matches straight across the newline into the
#     follower count, blows the digit ceiling and the real number is lost.
#   - a leading "(" is part of the number, so "(212) 765-5555" keeps its paren.
# The trailing guard rejects stats that read like numbers - "500M+", "12K", "80%".
_PHONE_RE = re.compile(r"(\+?\(?\d[\d ().\-/]{6,}\d)")
_PHONE_REJECT = re.compile(r"^[\d ().+\-/]*$")


def extract_phone(text: str) -> str:
    """First plausible phone number in a blob of text, or ''.

    Shared by the Maps card parser and the social bio parsers so both paths
    accept and reject exactly the same things.
    """
    if not text:
        return ""
    for match in _PHONE_RE.finditer(text):
        candidate = match.group(1).strip()
        digits = re.sub(r"\D", "", candidate)
        if not 7 <= len(digits) <= 15:
            continue
        tail = text[match.end():match.end() + 1]
        if tail in {"M", "K", "B", "m", "k", "%"}:
            continue          # "500M+ followers", not a phone number
        if not _PHONE_REJECT.match(candidate):
            continue
        return candidate
    return ""


def normalise_lead(lead: dict, source: str = "") -> dict:
    """Coerce any dict into the canonical lead schema."""
    out = {k: str(lead.get(k) or "").strip() for k in LEAD_FIELDS}
    if source and not out["source"]:
        out["source"] = source
    out["email"] = out["email"].lower()
    return out


# ---------------------------------------------------------------------------
# The Purifier proper
# ---------------------------------------------------------------------------
def purify(leads: list[dict], *, drop_generic: bool = True) -> PurifyResult:
    """Dedupe, syntax-validate and optionally strip role addresses.

    Deliverability check is deliberately off: a DNS lookup per row makes a
    300-lead list take minutes and fails on transient network trouble.
    """
    res = PurifyResult(total_in=len(leads))
    seen: set[str] = set()

    for raw in leads:
        lead = normalise_lead(raw)
        email = lead["email"]

        if not email:
            res.no_email += 1
            continue
        try:
            email = validate_email(email, check_deliverability=False).normalized.lower()
        except EmailNotValidError:
            res.invalid += 1
            continue
        if email in seen:
            res.duplicates += 1
            continue
        if drop_generic and is_role_address(email):
            res.generic += 1
            continue

        seen.add(email)
        lead["email"] = email
        # Older rows and hand-made CSVs arrive unclassified; never let an empty
        # cell reach the CRM table.
        if not lead["lead_type"]:
            lead["lead_type"] = classify_lead(
                name=lead["name"], category=lead["category"], source=lead["source"]
            )
        res.leads.append(lead)

    return res


def save_csv(leads: list[dict], path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(LEAD_FIELDS))
        writer.writeheader()
        for lead in leads:
            writer.writerow(normalise_lead(lead))
    return path


def load_csv(path: Path) -> list[dict]:
    path = Path(path)
    with path.open("r", newline="", encoding="utf-8-sig") as fh:
        return [normalise_lead(row) for row in csv.DictReader(fh)]
