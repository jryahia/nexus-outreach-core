"""The Hunter - scraping engine built on Scrapling.

Every scraper takes the ``job`` handle so it can stream progress to the UI and
abort the moment STOP is pressed, and returns a list of lead dicts using the
schema in ``core.purifier.LEAD_FIELDS``.

Nothing here is allowed to kill the worker thread over one bad page: a site
that times out is logged and skipped. The one exception is ``LoginRequired``,
which is raised deliberately because it needs a decision from the user.
"""

from __future__ import annotations

import asyncio
import re
import threading
from typing import Any, Callable
from urllib.parse import quote_plus, urljoin, urlparse

from scrapling.fetchers import Fetcher, StealthyFetcher

from core import killfeed, outpost, vault
from core.config import AppConfig
from core.purifier import (
    LEAD_FIELDS,
    classify_lead,
    extract_emails,
    extract_phone,
    is_role_address,
)

__all__ = [
    "LEAD_FIELDS", "SOURCES", "KEYWORD_SOURCES", "LoginRequired",
    "scrape_google_maps", "scrape_instagram", "scrape_tiktok",
    "scrape_reddit", "scrape_discord",
]

SOURCES = ("Google Maps", "Instagram", "TikTok", "Reddit", "Discord")

# Sources driven by a plain keyword rather than a handle or a hashtag. The Hunt
# tab branches on this instead of hard-coding source names.
KEYWORD_SOURCES = ("Reddit", "Discord")

# Default test query, per the brief.
DEFAULT_MAPS_KEYWORD = "Video production agency"
DEFAULT_MAPS_LOCATION = "New York"

# Load-bearing. Without these Google answers from an EU IP with
# consent.google.com and the result feed never renders - measured 27s of
# nothing versus 2.8s of real results. Do not remove as "dead config".
GOOGLE_CONSENT_COOKIES = [
    {"name": "SOCS", "value": "CAESHAgBEhJnd3NfMjAyMzA4MTAtMF9SQzIaAmVuIAEaBgiA_LyaBg",
     "domain": ".google.com", "path": "/"},
    {"name": "CONSENT", "value": "YES+cb.20220419-08-p0.en+FX+111",
     "domain": ".google.com", "path": "/"},
]

CONTACT_PATHS = ("", "/contact", "/contact-us", "/contacts", "/about",
                 "/about-us", "/impressum", "/kontakt")

MAX_FEED_SCROLLS = 40
_MAILTO_RE = re.compile(r'mailto:([^"\'?>\s]+)', re.I)
# Bios rarely carry an email but often carry the company URL, which does.
_URL_IN_TEXT_RE = re.compile(r"(?:https?://|www\.)[\w.-]+\.[A-Za-z]{2,24}(?:/\S*)?")
_IG_BIO_RE = re.compile(r'"biography"\s*:\s*"((?:[^"\\]|\\.)*)"')
_IG_USER_RE = re.compile(r'"username"\s*:\s*"([A-Za-z0-9._]{2,30})"')
_IG_EXTERNAL_RE = re.compile(r'"external_url"\s*:\s*"((?:[^"\\]|\\.)*)"')
_TT_SIG_RE = re.compile(r'"signature"\s*:\s*"((?:[^"\\]|\\.)*)"')
_TT_LINK_RE = re.compile(r'"bioLink"\s*:\s*\{\s*"link"\s*:\s*"((?:[^"\\]|\\.)*)"')


SKIPPED_NOTE = "Skipped (Already in Vault)"


class ProxyPool:
    """Round-robin over the configured proxies.

    Thread-safe because the hunter now runs several fetches at once, and two
    tasks taking the same index would put two concurrent requests through one
    exit address - which is the pattern a residential pool exists to avoid.
    """

    def __init__(self, proxies: list[str]) -> None:
        self._proxies = list(proxies)
        self._index = 0
        self._lock = threading.Lock()

    def __bool__(self) -> bool:
        return bool(self._proxies)

    def __len__(self) -> int:
        return len(self._proxies)

    def next(self) -> str:
        if not self._proxies:
            return ""
        with self._lock:
            proxy = self._proxies[self._index % len(self._proxies)]
            self._index += 1
        return proxy

    @staticmethod
    def redact(proxy: str) -> str:
        """A proxy line without its credentials. Safe for a log."""
        if not proxy:
            return "direct"
        tail = proxy.rsplit("@", 1)[-1]
        scheme = proxy.split("://", 1)[0] if "://" in proxy else ""
        return f"{scheme}://{tail}" if scheme else tail


# Answers that mean "bot protection noticed", as opposed to "page is missing".
BLOCKED_STATUS = (403, 429, 503)
MAX_FETCH_ATTEMPTS = 3
BACKOFF_BASE = 2.5      # seconds: 2.5, 5, 10


def resilient_fetch(fetch, job: Any, label: str, pool: "ProxyPool | None" = None,
                    attempts: int = MAX_FETCH_ATTEMPTS):
    """Call ``fetch(extra)`` until it stops being turned away.

    Scrapling returns the blocked page rather than raising, so a 403 or a 429
    looks like a successful fetch with no results - the hunt would quietly
    report "no leads" instead of "you are being throttled". This retries with
    an exponential backoff and, where a pool exists, a different exit address
    each time.

    The wait uses ``job.wait``, not sleep, so STOP still lands immediately
    inside a backoff.
    """
    page = None
    for attempt in range(1, attempts + 1):
        extra = {}
        if pool:
            proxy = pool.next()
            if proxy:
                extra["proxy"] = proxy
        page = fetch(extra)
        status = getattr(page, "status", 200)
        if status not in BLOCKED_STATUS:
            return page
        if attempt == attempts or job.cancelled:
            announce(f"BLOCKED  {label} answered {status} after {attempt} "
                     f"attempt(s)", killfeed.FAIL)
            job.report(log=f"{label} answered {status} after {attempt} attempts")
            return page
        pause = BACKOFF_BASE * (2 ** (attempt - 1))
        announce(f"RETRY    {label} answered {status}, backing off "
                 f"{pause:.0f}s via {ProxyPool.redact(extra.get('proxy', ''))}",
                 killfeed.WARN)
        job.report(log=f"{label} answered {status}; retry {attempt + 1} "
                       f"in {pause:.0f}s")
        if job.wait(pause):
            return page          # STOP pressed mid-backoff
    return page


def stealth_kwargs(cfg: AppConfig, ghost: bool = False) -> dict:
    """Extra fetcher arguments for the current stealth posture.

    Ghost Protocol is honest about what it can deliver. Two of these are real
    and take effect immediately; the third depends on the operator:

    * ``block_webrtc`` stops the browser announcing the real local address
      through WebRTC, which is the usual way a proxied scrape leaks its origin.
    * ``disable_resources`` drops images and fonts, which both speeds the crawl
      and shrinks the fingerprint surface.
    * ``proxy`` is only applied when NEXUS_PROXY is set. Without it the traffic
      still leaves from this machine's own address, and the UI says so rather
      than implying cover that does not exist.
    """
    if not ghost:
        return {}
    # Scrapling already generates a matching real User-Agent and a full
    # browserforge fingerprint on every launch, so there is no UA to inject
    # and no canvas hook to install here - that work is done before this
    # function is reached. What Ghost adds on top is the WebRTC block, a
    # smaller request surface, and an exit address.
    extra: dict[str, Any] = {
        "block_webrtc": True,        # stops the real local IP leaking past a proxy
        "disable_resources": True,   # fewer requests, smaller fingerprint
        "allow_webgl": True,         # deliberately ON: absent WebGL flags a bot
    }
    if cfg.has_proxy:
        extra["proxy"] = cfg.proxies[0]
    return extra


class LoginRequired(RuntimeError):
    """The platform served a login wall. The message carries the fix."""


def announce(text: str, level: str = killfeed.INFO) -> None:
    """One line to the live terminal. Never raises into a scrape."""
    try:
        killfeed.push(text, level)
    except Exception:
        pass


def load_known(skip_known: bool, job: Any) -> dict[str, set[str]]:
    """Match keys for everything already on file, or empty sets when disabled.

    One query per run. Every later check is a set lookup in memory, so the
    dedup gate costs nothing per lead.
    """
    if not skip_known:
        return {"domains": set(), "handles": set(), "names": set(), "emails": set()}
    try:
        known = vault.known_targets()
    except Exception as exc:
        job.report(log=f"WARNING vault unreadable ({type(exc).__name__}) - "
                       "scraping without the dedup gate")
        return {"domains": set(), "handles": set(), "names": set(), "emails": set()}
    total = sum(len(v) for v in known.values())
    if total:
        job.report(log=f"Dedup gate armed: {len(known['domains'])} known sites, "
                       f"{len(known['handles'])} handles, {len(known['names'])} names")
    return known


def _unescape_json_text(raw: str) -> str:
    """Decode the \\uXXXX and \\n escapes found in inline JSON blobs."""
    if not raw:
        return ""
    try:
        import json

        return json.loads(f'"{raw}"')
    except Exception:
        return raw


def _blank_lead(source: str) -> dict:
    lead = {k: "" for k in LEAD_FIELDS}
    lead["source"] = source
    return lead


def _domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


# ---------------------------------------------------------------------------
# Website email discovery - the step that makes Maps leads usable for B2B
# ---------------------------------------------------------------------------
def emails_from_website(url: str, *, timeout: int = 20, job: Any = None) -> list[str]:
    """Visit a business site and its contact pages, harvest addresses.

    Plain HTTP with browser TLS impersonation, not a headless browser: these
    are small business sites, and spending three seconds of Chromium per site
    would make a 100-lead run take an hour.
    """
    if not url:
        return []
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    site_domain = _domain(url)
    found: list[str] = []
    seen: set[str] = set()

    for path in CONTACT_PATHS:
        if job is not None and job.cancelled:
            break
        target = urljoin(url, path) if path else url
        try:
            page = Fetcher.get(target, timeout=timeout, impersonate="chrome",
                               stealthy_headers=True, follow_redirects=True,
                               retries=1)
        except Exception as exc:
            if job is not None and path == "":
                job.report(log=f"  site unreachable {target} ({type(exc).__name__})")
            continue
        if page.status >= 400:
            continue

        html = page.html_content
        candidates = _MAILTO_RE.findall(html) + extract_emails(html)
        for addr in extract_emails(" ".join(candidates)):
            if addr not in seen:
                seen.add(addr)
                found.append(addr)

        # A named address on the company's own domain is the best case; once
        # we have one there is no reason to keep crawling this site.
        if any(_domain("http://" + a.split("@")[1]) == site_domain
               and not is_role_address(a) for a in found):
            break

    return _rank_emails(found, site_domain)


def _rank_emails(emails: list[str], site_domain: str) -> list[str]:
    """Best first: own-domain named address, own-domain role, then the rest."""

    def score(addr: str) -> tuple[int, int]:
        same = addr.split("@")[-1] == site_domain
        return (0 if same else 1, 1 if is_role_address(addr) else 0)

    return sorted(dict.fromkeys(emails), key=score)


# How many targets are enriched at once. Each one is a small HTTP crawl of a
# business site, so the ceiling is politeness rather than memory: five parallel
# requests to five different domains is ordinary traffic, and the same five
# aimed at one host is not. Kept low deliberately.
ENRICH_CONCURRENCY = 5


async def _enrich_one(lead: dict, cfg: AppConfig, job: Any, semaphore,
                      ghost: bool = False) -> dict:
    """Resolve one lead's email inside the concurrency budget.

    emails_from_website is a synchronous crawl built on curl_cffi, so it runs
    in a worker thread rather than being rewritten as a coroutine. asyncio is
    doing what it is good at here - holding many slow IO operations open at
    once - without a rewrite of the fetch layer underneath it.
    """
    async with semaphore:
        if job.cancelled or not lead.get("website"):
            return lead
        try:
            emails = await asyncio.to_thread(
                emails_from_website, lead["website"],
                timeout=max(10, cfg.request_timeout // 1000), job=job)
        except Exception as exc:
            job.report(log=f"  {lead['website']} failed ({type(exc).__name__})")
            return lead
        if emails:
            lead["email"] = emails[0]
    return lead


async def _enrich_all(leads: list[dict], cfg: AppConfig, job: Any,
                      ghost: bool = False) -> None:
    semaphore = asyncio.Semaphore(ENRICH_CONCURRENCY)
    tasks = [asyncio.create_task(_enrich_one(lead, cfg, job, semaphore, ghost))
             for lead in leads]
    done = 0
    try:
        for coro in asyncio.as_completed(tasks):
            await coro
            done += 1
            job.report(current=done,
                       message=f"{done}/{len(leads)} sites checked")
            if job.cancelled:
                break
    finally:
        # STOP has to cancel what is in flight, not just stop scheduling more.
        # Without this a cancelled hunt would keep crawling in the background
        # while the UI reported it stopped.
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def enrich_websites(leads: list[dict], cfg: AppConfig, job: Any,
                    ghost: bool = False) -> list[dict]:
    """Fill in emails for a batch of leads, several at a time.

    Runs its own event loop on the worker thread it is already on, which is
    the same shape core.killfeed uses. No loop is threaded through Streamlit.
    """
    targets = [lead for lead in leads if lead.get("website")]
    if not targets:
        return leads
    announce(f"ENRICH   {len(targets)} sites, {ENRICH_CONCURRENCY} at a time",
             killfeed.INFO)
    asyncio.run(_enrich_all(targets, cfg, job, ghost))
    return leads


# ---------------------------------------------------------------------------
# Google Maps
# ---------------------------------------------------------------------------
def _feed_scroller(job: Any, wanted: int) -> Callable:
    """page_action that scrolls the results feed until it stops growing."""

    def action(page):
        try:
            page.wait_for_selector('div[role="feed"]', timeout=25000)
        except Exception:
            return page  # consent wall or no results; caller reports it
        previous = 0
        stalled = 0
        for _ in range(MAX_FEED_SCROLLS):
            if job.cancelled:
                break
            count = page.locator("div.Nv2PK").count()
            if count >= wanted:
                break
            if count == previous:
                stalled += 1
                if stalled >= 3:
                    break
            else:
                stalled = 0
                previous = count
                job.report(message=f"Loading results: {count} found")
            page.evaluate(
                "() => { const f = document.querySelector('div[role=\"feed\"]');"
                " if (f) f.scrollTop = f.scrollHeight; }"
            )
            page.wait_for_timeout(1400)
        return page

    return action


def scrape_google_maps(
    *,
    job: Any,
    cfg: AppConfig,
    keyword: str = DEFAULT_MAPS_KEYWORD,
    location: str = DEFAULT_MAPS_LOCATION,
    max_results: int = 40,
    skip_known: bool = True,
    ghost: bool = False,
) -> list[dict]:
    keyword = (keyword or DEFAULT_MAPS_KEYWORD).strip()
    location = (location or DEFAULT_MAPS_LOCATION).strip()
    query = f"{keyword} in {location}" if location else keyword
    url = f"https://www.google.com/maps/search/{quote_plus(query)}?hl=en&gl=us"

    job.report(total=max_results, message=f"Searching Maps: {query}")
    page = StealthyFetcher.fetch(
        url,
        headless=cfg.headless,
        timeout=max(cfg.request_timeout, 90000),
        network_idle=False,
        cookies=GOOGLE_CONSENT_COOKIES,
        page_action=_feed_scroller(job, max_results),
        **stealth_kwargs(cfg, ghost),
    )

    cards = page.css("div.Nv2PK")
    if not cards:
        title = (page.css("title::text") or [""])[0]
        if "continue to google" in str(title).lower():
            raise LoginRequired(
                "Google served its consent wall. The bypass cookies were rejected - "
                "retry, or set a non-EU proxy in .env."
            )
        job.report(message="No results for that query")
        return []

    job.report(log=f"{len(cards)} map cards, resolving websites")
    known = load_known(skip_known, job)

    leads: list[dict] = []
    for card in cards:
        if job.cancelled or len(leads) >= max_results:
            break
        try:
            lead = _parse_map_card(card, location or query)
        except Exception as exc:
            job.report(log=f"card skipped ({type(exc).__name__})")
            continue
        if lead:
            leads.append(lead)

    job.report(total=len(leads), current=0,
               message=f"{len(leads)} businesses, checking sites for emails")

    # The dedup gate runs first and in one pass, so the concurrent stage only
    # ever sees targets worth spending a request on. A run-local `seen` set
    # rides alongside the vault snapshot: with several fetches in flight, two
    # cards for the same business would otherwise both clear a gate that only
    # knows what was on file when the hunt started.
    fresh: list[dict] = []
    skipped = 0
    seen: set[str] = set()
    for index, lead in enumerate(leads, start=1):
        if job.cancelled:
            job.report(log="Stopped before the website crawl")
            break

        match = vault.match_known(lead, known)
        if match:
            skipped += 1
            job.report(current=index, log=f"{SKIPPED_NOTE}: {lead['name']} ({match})",
                       message=f"{index}/{len(leads)} - {skipped} already on file")
            continue

        key = vault.norm_domain(lead.get("website", "")) or \
            vault.norm_name(lead.get("name", ""))
        if key and key in seen:
            skipped += 1
            job.report(current=index,
                       log=f"{SKIPPED_NOTE}: {lead['name']} (duplicate in this run)")
            continue
        if key:
            seen.add(key)
        fresh.append(lead)

    # The slow half, run several at a time.
    job.report(total=len(fresh), current=0,
               message=f"Checking {len(fresh)} sites for addresses")
    enrich_websites(fresh, cfg, job, ghost)
    for lead in fresh:
        mark = lead["email"] or "no email"
        job.report(log=f"{lead['name']} [{lead['lead_type']}] -> {mark}")

    hits = sum(1 for lead in fresh if lead["email"])
    tail = f", {skipped} already in the vault" if skipped else ""
    job.report(message=f"{len(fresh)} new businesses, {hits} with an email{tail}")
    announce(f"HUNT     Maps '{query}': {len(fresh)} new, {hits} with an email",
             killfeed.OK if hits else killfeed.WARN)
    outpost.fire(outpost.HUNT_FINISHED, source="Google Maps", query=query,
                 found=len(fresh), with_email=hits, skipped=skipped)
    return fresh


def _parse_map_card(card, location: str) -> dict | None:
    anchors = card.css("a.hfpxzc")
    if not anchors:
        return None
    name = (anchors[0].attrib.get("aria-label") or "").strip()
    if not name:
        return None

    text = card.get_all_text(strip=True)
    # Paid placement. Its "website" is an /aclk ad redirect, so emailing it
    # would hit whoever bought the keyword rather than a local business.
    if "Sponsored" in text:
        return None

    website = ""
    for link in card.css('a[data-value="Website"]'):
        href = link.attrib.get("href") or ""
        if href.startswith("http"):
            website = href
            break
    if not website and card.css('a[data-value="Website"]'):
        return None  # only an ad redirect was offered

    category = _parse_card_category(text, name)

    lead = _blank_lead("Google Maps")
    lead.update(
        name=name,
        website=website,
        phone=extract_phone(text),
        location=location,
        category=category,
        lead_type=classify_lead(name=name, category=category, source="Google Maps"),
    )
    return lead


def _parse_card_category(text: str, name: str) -> str:
    """The business category off a Maps card, e.g. "Video production service".

    A card reads like "Tone Films Tone Films 5.0 Video production service -
    28-07 Jackson Ave - Open ...", with middle dots as separators. The category
    is the best classifier input available, better than the business name.
    """
    body = text.replace(name, " ", 2)
    # Drop the rating that sits between the name and the category.
    body = re.sub(r"\b\d\.\d\b(\s*\(\d[\d,]*\))?", "·", body, count=1)
    for chunk in re.split(r"[·\n]", body):
        part = chunk.strip(" ·\n\t")
        if not part or any(ch.isdigit() for ch in part):
            continue
        if part.lower().startswith(("open", "closed", "closes", "opens",
                                    "temporarily", "permanently", "website",
                                    "directions")):
            continue
        if 3 <= len(part) <= 60:
            return part
    return ""


# ---------------------------------------------------------------------------
# Instagram
# ---------------------------------------------------------------------------
# Location markers a bio uses. Written as codepoint escapes rather than pasted
# glyphs so this file stays free of literal emoji.
_PIN = "\U0001F4CD"          # round pushpin
_GLOBE = "[\U0001F30D-\U0001F30F]"  # globe showing any hemisphere
_LOCATION_MARK_RE = re.compile(rf"(?:{_PIN}|{_GLOBE})\s*([^\n|]{{2,48}})")
_LOCATION_PHRASE_RE = re.compile(
    r"\b(?:based in|located in|serving|from)\s+([A-Z][\w.\-]*(?:[ ,]+[A-Z][\w.\-]*){0,3})"
)
_CITY_STATE_RE = re.compile(r"\b([A-Z][a-z]+(?:[ -][A-Z][a-z]+)*,\s?[A-Z]{2})\b")


def _location_from_bio(bio: str) -> str:
    """City or region named in a bio, or ''.

    Three passes, most explicit first: a pin or globe marker, then a phrase
    like "based in Rome", then a bare "Los Angeles, CA".
    """
    if not bio:
        return ""
    for pattern in (_LOCATION_MARK_RE, _LOCATION_PHRASE_RE, _CITY_STATE_RE):
        match = pattern.search(bio)
        if match:
            value = match.group(1).strip(" .,-|")
            # Strip a trailing emoji or symbol run the marker regex swept up.
            value = re.sub(r"[^\w\s,.'\-]+$", "", value).strip()
            if 2 <= len(value) <= 48:
                return value
    return ""


def _site_from_bio(bio: str) -> str:
    """First plausible company URL mentioned in a bio."""
    for raw in _URL_IN_TEXT_RE.findall(bio or ""):
        url = raw if raw.startswith("http") else "https://" + raw
        host = _domain(url)
        if host and host not in {"instagram.com", "tiktok.com", "linktr.ee", "bit.ly"}:
            return url.rstrip(".,)")
    return ""


def _enrich_from_site(lead: dict, cfg: AppConfig, job: Any) -> None:
    """A bio with no email but a company link is still a B2B lead - go get it."""
    if lead["email"] or not lead["website"] or job.cancelled:
        return
    try:
        emails = emails_from_website(
            lead["website"], timeout=max(10, cfg.request_timeout // 1000), job=job
        )
    except Exception as exc:
        job.report(log=f"  {lead['website']} failed ({type(exc).__name__})")
        return
    if emails:
        lead["email"] = emails[0]
        job.report(log=f"  bio link {lead['website']} -> {emails[0]}")
    else:
        job.report(log=f"  bio link {lead['website']} -> nothing")


def _ig_walled(page) -> bool:
    """True when Instagram bounced us to the login page.

    Checked on the FINAL url, not the body: every public Instagram page links
    to /accounts/login in its header, so a substring test on the HTML flags
    working pages as walled.
    """
    return "/accounts/login" in str(page.url) or "loginForm" in page.html_content[:20000]


def _ig_fetch(url: str, cfg: AppConfig, ghost: bool = False):
    return StealthyFetcher.fetch(
        url,
        headless=cfg.headless,
        timeout=max(cfg.request_timeout, 60000),
        network_idle=False,
        google_search=True,
        **stealth_kwargs(cfg, ghost),
    )


def _ig_profile(handle: str, cfg: AppConfig, job: Any,
                ghost: bool = False) -> dict | None:
    handle = handle.lstrip("@").strip("/")
    page = _ig_fetch(f"https://www.instagram.com/{quote_plus(handle)}/", cfg, ghost)
    html = page.html_content

    if _ig_walled(page):
        raise LoginRequired(
            f"Instagram sent @{handle} to the login page. That account is gated for "
            "logged-out visitors - try a different account, or wait a few minutes."
        )

    bio_match = _IG_BIO_RE.search(html)
    bio = _unescape_json_text(bio_match.group(1)) if bio_match else ""
    if not bio:
        meta = page.css('meta[name="description"]')
        if meta:
            bio = meta[0].attrib.get("content", "")

    external = _IG_EXTERNAL_RE.search(html)
    website = _unescape_json_text(external.group(1)) if external else ""

    emails = extract_emails(bio)
    lead = _blank_lead("Instagram")
    lead.update(
        name=_display_name(page) or handle,
        handle=f"@{handle}",
        email=emails[0] if emails else "",
        website=website or _site_from_bio(bio),
        phone=extract_phone(bio),
        location=_location_from_bio(bio),
        category=bio[:140].replace("\n", " ").strip(),
        lead_type=classify_lead(name=handle, bio=bio, source="Instagram"),
    )
    _enrich_from_site(lead, cfg, job)
    job.report(log=f"@{handle} [{lead['lead_type']}] -> "
                   f"{lead['email'] or 'no email found'}")
    return lead


def _display_name(page) -> str:
    """The human-readable account name from the page title, if there is one."""
    title = str((page.css("title::text") or [""])[0])
    name = title.split("(")[0].split("|")[0].strip()
    return name if 1 < len(name) <= 80 else ""


def scrape_instagram(
    *, job: Any, cfg: AppConfig, target: str, max_results: int = 40,
    skip_known: bool = True, ghost: bool = False,
) -> list[dict]:
    target = (target or "").strip()
    if not target:
        return []

    if target.startswith("#") or "explore/tags" in target:
        tag = target.lstrip("#").split("/")[-1].strip()
        job.report(message=f"Instagram hashtag #{tag}")
        page = _ig_fetch(
            f"https://www.instagram.com/explore/tags/{quote_plus(tag)}/", cfg, ghost)
        html = page.html_content
        if _ig_walled(page):
            raise LoginRequired(
                f"Instagram sent #{tag} to the login page. Low-volume tags are gated "
                "for logged-out visitors - use a broader tag, or a single @account."
            )
        handles = list(dict.fromkeys(_IG_USER_RE.findall(html)))[:max_results]
        if not handles:
            job.report(message=f"No public accounts surfaced for #{tag}")
            return []
        job.report(log=f"{len(handles)} accounts from #{tag}")
    else:
        handles = [target.lstrip("@").strip("/")]

    known = load_known(skip_known, job)
    job.report(total=len(handles), current=0, message=f"Reading {len(handles)} bios")
    leads: list[dict] = []
    skipped = 0
    for index, handle in enumerate(handles, start=1):
        if job.cancelled:
            job.report(log="Stopped during Instagram crawl")
            break

        # The dedup gate sits before the profile fetch, so a handle we already
        # hold never costs a browser page load at all.
        match = vault.match_known({"handle": handle, "name": handle}, known)
        if match:
            skipped += 1
            job.report(current=index, log=f"{SKIPPED_NOTE}: @{handle} ({match})")
            continue

        try:
            lead = _ig_profile(handle, cfg, job, ghost)
            if lead:
                leads.append(lead)
        except LoginRequired:
            raise
        except Exception as exc:
            job.report(log=f"@{handle} failed ({type(exc).__name__}: {exc})")
        job.report(current=index)

    hits = sum(1 for lead in leads if lead["email"])
    tail = f", {skipped} already in the vault" if skipped else ""
    job.report(message=f"{len(leads)} bios read, {hits} with an email{tail}")
    announce(f"HUNT     Instagram '{target}': {len(leads)} bios, {hits} with an email",
             killfeed.OK if hits else killfeed.WARN)
    outpost.fire(outpost.HUNT_FINISHED, source="Instagram", query=target,
                 found=len(leads), with_email=hits, skipped=skipped)
    return leads


# ---------------------------------------------------------------------------
# TikTok
# ---------------------------------------------------------------------------
TIKTOK_WALL_HINT = (
    "TikTok requires a one-time login from this machine. Run:\n"
    "    .venv\\Scripts\\python.exe tools\\tiktok_login.py\n"
    "sign in, close the window, then retry. The session is stored in "
    "TIKTOK_PROFILE_DIR and reused after that."
)


def _tt_walled(page) -> bool:
    html = page.html_content
    title = str((page.css("title::text") or [""])[0])
    return ("Log in | TikTok" in title
            or "enter_method=mandatory" in str(page.url)
            or "/login?redirect_url" in html[:4000])


def _tt_profile(handle: str, cfg: AppConfig, job: Any,
                ghost: bool = False) -> dict | None:
    handle = handle.lstrip("@").strip("/")
    kwargs: dict[str, Any] = {
        "headless": cfg.headless,
        "timeout": max(cfg.request_timeout, 60000),
        "network_idle": False,
        "google_search": True,
    }
    if cfg.tiktok_profile_dir:
        kwargs["user_data_dir"] = cfg.tiktok_profile_dir
    kwargs.update(stealth_kwargs(cfg, ghost))

    page = StealthyFetcher.fetch(f"https://www.tiktok.com/@{quote_plus(handle)}", **kwargs)
    if _tt_walled(page):
        raise LoginRequired(TIKTOK_WALL_HINT)

    html = page.html_content
    sig = _TT_SIG_RE.search(html)
    bio = _unescape_json_text(sig.group(1)) if sig else ""
    link = _TT_LINK_RE.search(html)
    website = _unescape_json_text(link.group(1)) if link else ""

    emails = extract_emails(bio)
    lead = _blank_lead("TikTok")
    lead.update(
        name=_display_name(page) or handle,
        handle=f"@{handle}",
        email=emails[0] if emails else "",
        website=website or _site_from_bio(bio),
        phone=extract_phone(bio),
        location=_location_from_bio(bio),
        category=bio[:140].replace("\n", " ").strip(),
        lead_type=classify_lead(name=handle, bio=bio, source="TikTok"),
    )
    _enrich_from_site(lead, cfg, job)
    job.report(log=f"@{handle} [{lead['lead_type']}] -> "
                   f"{lead['email'] or 'no email found'}")
    return lead


def scrape_tiktok(
    *, job: Any, cfg: AppConfig, target: str, max_results: int = 40,
    skip_known: bool = True, ghost: bool = False,
) -> list[dict]:
    target = (target or "").strip()
    if not target:
        return []

    if target.startswith("#") or "/tag/" in target:
        tag = target.lstrip("#").split("/")[-1].strip()
        job.report(message=f"TikTok hashtag #{tag}")
        kwargs: dict[str, Any] = {
            "headless": cfg.headless,
            "timeout": max(cfg.request_timeout, 60000),
            "network_idle": False,
            "google_search": True,
        }
        if cfg.tiktok_profile_dir:
            kwargs["user_data_dir"] = cfg.tiktok_profile_dir
        kwargs.update(stealth_kwargs(cfg, ghost))
        page = StealthyFetcher.fetch(f"https://www.tiktok.com/tag/{quote_plus(tag)}", **kwargs)
        if _tt_walled(page):
            raise LoginRequired(TIKTOK_WALL_HINT)
        handles = list(dict.fromkeys(
            re.findall(r'"uniqueId"\s*:\s*"([A-Za-z0-9._]{2,30})"', page.html_content)
        ))[:max_results]
        if not handles:
            job.report(message=f"No accounts surfaced for #{tag}")
            return []
        job.report(log=f"{len(handles)} accounts from #{tag}")
    else:
        handles = [target.lstrip("@").strip("/")]

    known = load_known(skip_known, job)
    job.report(total=len(handles), current=0, message=f"Reading {len(handles)} bios")
    leads: list[dict] = []
    skipped = 0
    for index, handle in enumerate(handles, start=1):
        if job.cancelled:
            job.report(log="Stopped during TikTok crawl")
            break

        # The dedup gate sits before the profile fetch, so a handle we already
        # hold never costs a browser page load at all.
        match = vault.match_known({"handle": handle, "name": handle}, known)
        if match:
            skipped += 1
            job.report(current=index, log=f"{SKIPPED_NOTE}: @{handle} ({match})")
            continue

        try:
            lead = _tt_profile(handle, cfg, job, ghost)
            if lead:
                leads.append(lead)
        except LoginRequired:
            raise
        except Exception as exc:
            job.report(log=f"@{handle} failed ({type(exc).__name__}: {exc})")
        job.report(current=index)

    hits = sum(1 for lead in leads if lead["email"])
    tail = f", {skipped} already in the vault" if skipped else ""
    job.report(message=f"{len(leads)} bios read, {hits} with an email{tail}")
    announce(f"HUNT     TikTok '{target}': {len(leads)} bios, {hits} with an email",
             killfeed.OK if hits else killfeed.WARN)
    outpost.fire(outpost.HUNT_FINISHED, source="TikTok", query=target,
                 found=len(leads), with_email=hits, skipped=skipped)
    return leads


# ---------------------------------------------------------------------------
# Reddit - community search
# ---------------------------------------------------------------------------
# Verified against the live page rather than guessed. Two routes were ruled out
# first, both measured:
#   * old.reddit.com/search redirects a logged-out fetch to /login.
#   * the .json endpoints answer 403 without OAuth since the 2023 API change.
# The rendered community search is what actually serves results, so it goes
# through StealthyFetcher exactly like Instagram and TikTok do.
REDDIT_SEARCH = "https://www.reddit.com/search/?q={query}&type=communities"

_REDDIT_CARD = 'div[data-testid="search-community"]'
_SUBREDDIT_HREF_RE = re.compile(r"^/r/([A-Za-z0-9_]{2,30})/?$")
# "28K weekly visitors" sits in the card text under the blurb.
_REDDIT_SIZE_RE = re.compile(r"([\d.]+\s*[KMB]?)\s*weekly visitors", re.I)


def _reddit_walled(page) -> bool:
    """True when Reddit served a login or block page instead of results.

    Checked on the FINAL url plus the title: a logged-out old.reddit fetch
    redirects to /login, and the rate-limit page keeps a 200 status while the
    title changes, so the status code alone proves nothing.
    """
    if "/login" in str(page.url):
        return True
    title = str((page.css("title::text") or [""])[0]).lower()
    return "welcome to reddit" in title or "blocked" in title


def _parse_reddit_card(card) -> dict | None:
    """One community card into a lead, or None if it carries no subreddit."""
    handle = ""
    for anchor in card.css('a[href^="/r/"]'):
        match = _SUBREDDIT_HREF_RE.match((anchor.attrib.get("href") or "").strip())
        if match:
            handle = match.group(1)
            break
    if not handle:
        return None

    text = card.get_all_text(strip=True)
    # The card reads "Name / r/Name / description". Drop the two identity lines
    # so the description does not repeat the name back twice.
    skip = {handle.lower(), f"r/{handle}".lower()}
    body = [line.strip() for line in text.splitlines()
            if line.strip() and line.strip().lower() not in skip]
    description = " ".join(body)

    size = _REDDIT_SIZE_RE.search(text)
    members = f"{size.group(1).strip()} weekly visitors" if size else ""
    detail = " - ".join(part for part in (members, description) if part)

    lead = _blank_lead("Reddit")
    lead.update(
        name=f"r/{handle}",
        handle=f"r/{handle}",
        website=f"https://www.reddit.com/r/{handle}/",
        category=detail[:400],
        lead_type=classify_lead(name=handle, bio=description, source="Reddit"),
    )
    # A sidebar blurb occasionally carries a contact address.
    emails = extract_emails(description)
    if emails:
        lead["email"] = emails[0]
    return lead


def scrape_reddit(
    *, job: Any, cfg: AppConfig, target: str, max_results: int = 40,
    skip_known: bool = True, ghost: bool = False,
) -> list[dict]:
    """Public subreddit search for a niche keyword.

    Returns communities, not people. A subreddit is a place to post an offer or
    to mine for individual creators, so it is stored as intelligence in the
    vault rather than as an emailable contact.
    """
    keyword = (target or "").strip().lstrip("#")
    if not keyword:
        return []

    job.report(message=f"Reddit communities: {keyword}")
    page = StealthyFetcher.fetch(
        REDDIT_SEARCH.format(query=quote_plus(keyword)),
        headless=cfg.headless,
        timeout=max(cfg.request_timeout, 90000),
        network_idle=False,
        google_search=True,
        **stealth_kwargs(cfg, ghost),
    )
    if _reddit_walled(page):
        raise LoginRequired(
            "Reddit served a login wall instead of search results. The "
            "logged-out search page is rate limited per IP - wait a few "
            "minutes and retry, or set a proxy in .env."
        )

    cards = page.css(_REDDIT_CARD)
    if not cards:
        job.report(message=f"No communities surfaced for '{keyword}'")
        return []

    known = load_known(skip_known, job)
    job.report(total=min(len(cards), max_results), current=0,
               log=f"{len(cards)} communities matched '{keyword}'")

    leads: list[dict] = []
    skipped = 0
    for index, card in enumerate(cards, start=1):
        if job.cancelled or len(leads) >= max_results:
            break
        try:
            lead = _parse_reddit_card(card)
        except Exception as exc:
            job.report(log=f"card skipped ({type(exc).__name__})")
            continue
        if not lead:
            continue

        # The same dedup gate every other source uses. norm_domain keeps the
        # /r/ segment, so two subreddits never collapse onto "reddit.com".
        match = vault.match_known(lead, known)
        if match:
            skipped += 1
            job.report(current=index, log=f"{SKIPPED_NOTE}: {lead['name']} ({match})")
            continue

        leads.append(lead)
        job.report(current=index, message=f"{len(leads)} communities",
                   log=f"{lead['name']} [{lead['lead_type']}]")

    tail = f", {skipped} already in the vault" if skipped else ""
    job.report(message=f"{len(leads)} subreddits found{tail}")
    announce(f"HUNT     Reddit '{keyword}': {len(leads)} communities", killfeed.OK)
    outpost.fire(outpost.HUNT_FINISHED, source="Reddit", query=keyword,
                 found=len(leads), skipped=skipped)
    return leads


# ---------------------------------------------------------------------------
# Discord - public server directory
# ---------------------------------------------------------------------------
# Disboard is the public listing site. Its search page is Cloudflare-fronted,
# so it needs the stealth browser rather than a plain fetch. Discadia was
# tested as a second directory and did not resolve, so it is deliberately
# absent rather than silently returning nothing.
DISBOARD_SEARCH = "https://disboard.org/search?keyword={query}"

_DISBOARD_CARD = "div.listing-card"
_DISBOARD_JOIN_RE = re.compile(r"/server/join/(\d+)")


def _discord_walled(page) -> bool:
    """True when Cloudflare served a challenge instead of the listing."""
    title = str((page.css("title::text") or [""])[0]).lower()
    return ("just a moment" in title or "attention required" in title
            or "cloudflare" in title)


def _parse_disboard_card(card) -> dict | None:
    """One Disboard listing into a lead, or None without a join link."""
    joins = card.css('a[href*="/server/join/"]')
    if not joins:
        return None
    match = _DISBOARD_JOIN_RE.search((joins[0].attrib.get("href") or "").strip())
    if not match:
        return None
    server_id = match.group(1)

    name_nodes = card.css("div.server-name")
    name = name_nodes[0].get_all_text(strip=True).strip() if name_nodes else ""
    if not name:
        return None

    tags = [t.get_all_text(strip=True).strip() for t in card.css("a.tag")]
    body_nodes = card.css("div.server-body")
    body = body_nodes[0].get_all_text(strip=True) if body_nodes else ""
    # server-body carries the tag list above the blurb, so the tags are removed
    # to leave the description as the description.
    description = " ".join(
        line.strip() for line in body.splitlines()
        if line.strip() and line.strip() not in tags
    )

    detail = " - ".join(p for p in (", ".join(tags[:5]), description) if p)
    lead = _blank_lead("Discord")
    lead.update(
        name=name,
        handle=f"discord:{server_id}",
        # The Disboard join URL, not a raw discord.gg invite: the listing link
        # is public and stable, a bare invite code expires.
        website=f"https://disboard.org/server/join/{server_id}",
        category=detail[:400],
        lead_type=classify_lead(name=name,
                                bio=f"{description} {' '.join(tags)}",
                                source="Discord"),
    )
    emails = extract_emails(description)
    if emails:
        lead["email"] = emails[0]
    return lead


def scrape_discord(
    *, job: Any, cfg: AppConfig, target: str, max_results: int = 40,
    skip_known: bool = True, ghost: bool = False,
) -> list[dict]:
    """Public Discord server directory search.

    Returns servers, not people, and the stored link is the public listing
    address rather than a raw invite code.
    """
    keyword = (target or "").strip().lstrip("#")
    if not keyword:
        return []

    job.report(message=f"Discord servers: {keyword}")
    page = StealthyFetcher.fetch(
        DISBOARD_SEARCH.format(query=quote_plus(keyword)),
        headless=cfg.headless,
        timeout=max(cfg.request_timeout, 90000),
        network_idle=False,
        google_search=True,
        **stealth_kwargs(cfg, ghost),
    )
    if _discord_walled(page):
        raise LoginRequired(
            "Disboard served a Cloudflare challenge instead of the listing. "
            "Retry in a few minutes, or set HEADLESS=false in .env so the "
            "challenge can resolve in a visible window."
        )

    cards = page.css(_DISBOARD_CARD)
    if not cards:
        job.report(message=f"No servers surfaced for '{keyword}'")
        return []

    known = load_known(skip_known, job)
    job.report(total=min(len(cards), max_results), current=0,
               log=f"{len(cards)} servers matched '{keyword}'")

    leads: list[dict] = []
    skipped = 0
    for index, card in enumerate(cards, start=1):
        if job.cancelled or len(leads) >= max_results:
            break
        try:
            lead = _parse_disboard_card(card)
        except Exception as exc:
            job.report(log=f"card skipped ({type(exc).__name__})")
            continue
        if not lead:
            continue

        match = vault.match_known(lead, known)
        if match:
            skipped += 1
            job.report(current=index, log=f"{SKIPPED_NOTE}: {lead['name']} ({match})")
            continue

        leads.append(lead)
        job.report(current=index, message=f"{len(leads)} servers",
                   log=f"{lead['name']} [{lead['lead_type']}]")

    tail = f", {skipped} already in the vault" if skipped else ""
    job.report(message=f"{len(leads)} Discord servers found{tail}")
    announce(f"HUNT     Discord '{keyword}': {len(leads)} servers", killfeed.OK)
    outpost.fire(outpost.HUNT_FINISHED, source="Discord", query=keyword,
                 found=len(leads), skipped=skipped)
    return leads
