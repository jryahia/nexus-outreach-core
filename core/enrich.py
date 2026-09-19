"""Deep extraction - who is this, not just what address is on the page.

Regex over a contact page answers the wrong question. It returns
``info@studio.com`` with the same confidence as ``anna@studio.com``, and a
campaign that opens "Hi there" to a shared inbox performs nothing like one that
opens "Hi Anna" to the person who signs the cheques.

So this module does two things the plain scraper does not:

* **Bio-link traversal.** A creator's site is usually not a site. It is a
  Linktree, a Beacons page, a Bento - a list of buttons rendered client-side,
  where the real destination (and often the booking address) sits behind one
  more hop. Those hosts are recognised and followed.
* **Decision-maker extraction.** Emails are scored against the roles named
  around them, so a founder beats a support desk, and a name found beside the
  address is carried with it.

The semantic step is deliberately a seam. ``extract_decision_maker`` runs a
heuristic today and takes an ``extractor`` callable, so a model can be dropped
in later without the call sites changing - same signature, same return shape,
one function swapped.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Callable
from urllib.parse import urljoin, urlparse

from core.purifier import extract_emails, is_role_address

# Hosts that are a list of links rather than a website. Following one is worth
# a browser launch; following a normal site is not.
BIO_LINK_HOSTS = frozenset({
    "linktr.ee", "beacons.ai", "beacons.page", "bento.me", "campsite.bio",
    "carrd.co", "taplink.cc", "linkin.bio", "solo.to", "lnk.bio",
    "koji.to", "milkshake.app", "shorby.com", "allmylinks.com",
    "withkoji.com", "flowcode.com", "linkpop.com", "komi.io",
    "stan.store", "snipfeed.co", "pillar.io", "hoo.be",
})

# Social profiles carry a bio, and a bio carries the booking address more often
# than the site does.
SOCIAL_HOSTS = frozenset({
    "instagram.com", "tiktok.com", "youtube.com", "twitter.com", "x.com",
    "facebook.com", "linkedin.com", "threads.net",
})

# Ordered: the first match wins, so the most senior title is the one recorded.
# A "Founder & Creative Director" should read as Founder.
ROLE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Founder", ("founder", "co-founder", "cofounder", "owner", "proprietor")),
    ("CEO", ("ceo", "chief executive", "managing director", "amministratore")),
    ("Director", ("creative director", "art director", "director", "direttore")),
    ("Producer", ("executive producer", "producer", "produttore")),
    ("Editor", ("editor-in-chief", "lead editor", "video editor", "editor")),
    ("Manager", ("talent manager", "booking manager", "manager", "responsabile")),
    ("Marketing", ("head of marketing", "cmo", "marketing lead", "growth")),
    ("Partner", ("partner", "principal", "associate")),
)

# Roughly "two to four capitalised words", which is what a human name looks
# like in a byline. Deliberately strict: a loose pattern turns every heading
# into a person.
NAME_RE = re.compile(
    r"\b([A-Z][a-z'À-ſ]{1,20}(?:\s+[A-Z][a-z'À-ſ]{1,20}){1,2})\b")

# Phrases that match NAME_RE but are never people.
NOT_A_NAME = frozenset({
    "Privacy Policy", "Terms Of", "Contact Us", "About Us", "Cookie Policy",
    "All Rights", "Read More", "Learn More", "Get Started", "Sign Up",
    "Our Team", "Our Work", "Case Study", "Video Production", "Social Media",
    "New York", "Los Angeles", "United States", "United Kingdom",
})

# Individual words that cannot appear in a person's name. Two sources: the job
# titles themselves - "Creative Director" is shaped exactly like a name and is
# not one - and the verbs a contact block opens with, which NAME_RE happily
# swallows into "Contact Marco Bianchi".
_ROLE_WORDS = {word
               for _, needles in ROLE_PATTERNS
               for phrase in needles
               for word in phrase.replace("-", " ").split()}
NAME_STOP_WORDS = _ROLE_WORDS | {
    "contact", "email", "mail", "call", "phone", "write", "reach", "hello",
    "hi", "our", "the", "team", "office", "support", "sales", "info",
    "enquiries", "general", "head", "lead", "chief", "senior", "junior",
    "address", "get", "in", "touch", "us", "we", "book", "booking", "press",
}


def _clean_name(candidate: str) -> str:
    """A candidate reduced to the words that could belong to a person.

    Trimmed from both ends rather than filtered throughout: a name is a
    contiguous run, so "Contact Marco Bianchi" is Marco Bianchi, while
    "Creative Director" is nothing at all once its words are removed.
    """
    words = candidate.split()
    while words and words[0].lower() in NAME_STOP_WORDS:
        words.pop(0)
    while words and words[-1].lower() in NAME_STOP_WORDS:
        words.pop()
    if len(words) < 2:
        return ""                      # a lone first name is not worth a claim
    if any(word.lower() in NAME_STOP_WORDS for word in words):
        return ""
    return " ".join(words)

# How far either side of an address to look for a role or a name. A contact
# block is small; widening this starts pulling in the next person on the page.
CONTEXT_WINDOW = 220


@dataclass
class DecisionMaker:
    """One extracted contact. The shape a model would have to return too."""

    email: str = ""
    name: str = ""
    role: str = ""
    confidence: float = 0.0
    source: str = "heuristic"

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def found(self) -> bool:
        return bool(self.email)


def host_of(url: str) -> str:
    try:
        host = urlparse(url if "//" in url else f"https://{url}").netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def is_bio_link(url: str) -> bool:
    """True when this address is a link-in-bio page rather than a website."""
    return host_of(url) in BIO_LINK_HOSTS


def is_social(url: str) -> bool:
    return host_of(url) in SOCIAL_HOSTS


def _strip_markup(html: str) -> str:
    """Readable text from a document, using lxml where it is available.

    lxml arrives with the scraping stack, but the fallback keeps this module
    importable and testable without it.
    """
    if not html:
        return ""
    try:
        import lxml.html

        doc = lxml.html.fromstring(html)
        for bad in doc.xpath("//script|//style|//noscript"):
            bad.getparent().remove(bad)
        # itertext, not text_content: the latter concatenates block elements
        # with nothing between them, so "<h3>Anna Rossi</h3><p>Founder</p>"
        # arrives as "Anna RossiFounder" and no name pattern can survive it.
        # That is the exact shape of the team page this module exists to read.
        text = " ".join(chunk for chunk in doc.itertext())
    except Exception:
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html,
                      flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t\r\f\v]+", " ", text)


def role_near(text: str) -> str:
    """The most senior role named in a fragment, or ''."""
    lowered = text.lower()
    for label, needles in ROLE_PATTERNS:
        for needle in needles:
            if needle in lowered:
                return label
    return ""


def name_near(text: str, email: str = "") -> str:
    """A person's name in a fragment, preferring one echoed by the address.

    ``anna.rossi@studio.it`` beside "Anna Rossi, Founder" is the easy case and
    the one worth getting right: the local part corroborates the name, which
    is what separates a real byline from a passing capitalised phrase.
    """
    candidates = []
    for match in NAME_RE.finditer(text):
        raw = match.group(1).strip()
        if raw in NOT_A_NAME:
            continue
        cleaned = _clean_name(raw)
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)
    if not candidates:
        return ""

    local = email.split("@", 1)[0].lower() if email else ""
    local_parts = {p for p in re.split(r"[._\-+0-9]+", local) if len(p) > 2}
    if local_parts:
        for candidate in candidates:
            words = {w.lower() for w in candidate.split()}
            if words & local_parts:
                return candidate
    return candidates[0]


def _context(text: str, index: int, span: int = CONTEXT_WINDOW) -> str:
    return text[max(0, index - span): index + span]


def extract_decision_maker(
    html_content: str,
    *,
    url: str = "",
    extractor: Callable[[str, str], dict] | None = None,
) -> dict:
    """Best contact on a page, as ``{"name", "email", "role", ...}``.

    ``extractor`` is the seam a model plugs into. Give it a callable taking
    ``(html_content, url)`` and returning that same dict and it takes over; it
    is tried first, and anything it fails to fill is completed by the
    heuristic rather than lost. Nothing here calls a network service, and the
    default path never leaves the machine.
    """
    if extractor is not None:
        try:
            given = extractor(html_content, url) or {}
        except Exception:
            given = {}
        if given.get("email"):
            merged = DecisionMaker(
                email=str(given.get("email", "")).strip().lower(),
                name=str(given.get("name", "")).strip(),
                role=str(given.get("role", "")).strip(),
                confidence=float(given.get("confidence", 0.9)),
                source=str(given.get("source", "model")),
            )
            if not merged.name or not merged.role:
                fallback = _heuristic(html_content)
                merged.name = merged.name or fallback.name
                merged.role = merged.role or fallback.role
            return merged.as_dict()

    return _heuristic(html_content).as_dict()


def _heuristic(html_content: str) -> DecisionMaker:
    text = _strip_markup(html_content)
    addresses = extract_emails(text) + extract_emails(html_content)
    seen: list[str] = []
    for address in addresses:
        if address not in seen:
            seen.append(address)
    if not seen:
        return DecisionMaker()

    best = DecisionMaker()
    for address in seen:
        index = text.lower().find(address.lower())
        window = _context(text, index) if index >= 0 else text[:CONTEXT_WINDOW]
        role = role_near(window)
        name = name_near(window, address)
        personal = not is_role_address(address)

        # A named human at a senior title is the target. A shared inbox with no
        # name attached is the fallback, never the answer if anything better
        # exists on the page.
        score = 0.25
        if personal:
            score += 0.35
        if role:
            score += 0.25
        if name:
            score += 0.15
        candidate = DecisionMaker(email=address, name=name, role=role,
                                  confidence=round(min(score, 0.99), 2))
        if candidate.confidence > best.confidence:
            best = candidate
    return best


def summarise(found: dict) -> str:
    """One line for the kill-feed. Says what was learned, not that it ran."""
    if not found or not found.get("email"):
        return ""
    who = found.get("name") or "unknown"
    role = found.get("role")
    if role and found.get("name"):
        return f"Found {role}: {found['name']} - {found['email']}"
    if role:
        return f"Found {role}: {found['email']}"
    if found.get("name"):
        return f"Found {who} - {found['email']}"
    return f"Found {found['email']}"


def outbound_links(page, base_url: str = "", limit: int = 12) -> list[str]:
    """Destinations worth following from a bio-link page.

    Links back into the same host are dropped, and so are the provider's own
    signup and share links - a Beacons page advertises Beacons, and crawling
    that is a crawl of nothing.
    """
    host = host_of(base_url)
    found: list[str] = []
    try:
        anchors = page.css("a[href]")
    except Exception:
        return []
    for anchor in anchors:
        href = (anchor.attrib.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:")):
            continue
        if href.startswith("mailto:"):
            found.append(href)
            continue
        absolute = urljoin(base_url, href) if base_url else href
        target = host_of(absolute)
        # Subdomains count as the same host. A Beacons page footers its own
        # account.beacons.ai signup link, and an exact-match test lets that
        # through as if it were the creator's site.
        if not target or target == host or (
                host and (target.endswith("." + host) or host.endswith("." + target))):
            continue
        if target in BIO_LINK_HOSTS:
            continue                 # a bio page linking another bio page
        if absolute not in found:
            found.append(absolute)
        if len(found) >= limit:
            break
    return found


def mailtos(html: str) -> list[str]:
    """Addresses behind mailto links, which regex over text alone misses."""
    raw = re.findall(r'mailto:([^"\'?>\s]+)', html or "", re.I)
    return extract_emails(" ".join(raw))


__all__ = [
    "BIO_LINK_HOSTS", "SOCIAL_HOSTS", "DecisionMaker", "extract_decision_maker",
    "is_bio_link", "is_social", "host_of", "outbound_links", "mailtos",
    "role_near", "name_near", "summarise",
]
