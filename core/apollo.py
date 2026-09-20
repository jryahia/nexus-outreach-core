"""Apollo.io ingestion: a sanctioned B2B data source.

Apollo is a licensed, opt-in contact database with an official REST API, so
there is no wall to bypass and no session to mimic. This module is a thin,
offline-testable client around ``/v1/mixed_people/search`` plus a mapping from
Apollo's person records to the vault's lead schema.

Two deliberate design choices, both so the tests never touch the network:

* The HTTP call is a single injectable callable (``configure(post=...)`` or a
  ``post=`` argument threaded through). Nothing here imports ``requests`` at
  call time except the one default transport, which the tests replace.
* The auth mechanism is one named constant. Apollo has shipped both an
  ``X-Api-Key`` header and an ``api_key`` body field across versions; this
  client sends the header form, which is the one Apollo's current docs show.
  It is NOT verified against a live key here - there is none on this machine -
  so ``build_request`` is written to be asserted on its shape, and a wrong
  guess is a one-line change in one place.
"""
from __future__ import annotations

import json
import re
from typing import Any, Protocol
from urllib import request as _urllib

# --- the endpoint -----------------------------------------------------------
SEARCH_URL = "https://api.apollo.io/v1/mixed_people/search"

# Apollo caps a page at 100 records. Asking for more is silently truncated, so
# the client paginates rather than over-requesting.
MAX_PER_PAGE = 100

# How many pages one ingest will pull, regardless of the requested lead count,
# so a huge ``max_results`` cannot turn into an unbounded spend against the
# account's API credits.
MAX_PAGES = 20

# The auth header Apollo's current REST docs specify. Kept as one constant so a
# version change is a single edit, not a hunt through the module.
API_KEY_HEADER = "X-Api-Key"

# Apollo returns this literal string (and/or ``email_status == "locked"``) when
# a plan has not unlocked a contact's address. It is a placeholder, not an
# address: letting it reach the vault would dedup real leads against a shared
# fake domain, the same collapse the subreddit norm_domain bug caused. It is
# dropped, and the lead is kept without an email.
_LOCKED_EMAIL_RE = re.compile(r"email_not_unlocked|not_unlocked|^locked$", re.I)
_LOCKED_STATUSES = {"locked", "not_unlocked", "unavailable"}


class Transport(Protocol):
    def __call__(self, url: str, *, headers: dict[str, str],
                 body: bytes, timeout: float) -> tuple[int, bytes]: ...


def _urllib_post(url: str, *, headers: dict[str, str], body: bytes,
                 timeout: float) -> tuple[int, bytes]:
    """The one real network call, kept tiny and replaceable.

    Uses the stdlib so the module has no import-time dependency on requests;
    the tests inject their own transport and never reach this.
    """
    req = _urllib.Request(url, data=body, headers=headers, method="POST")
    with _urllib.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed https URL)
        return resp.status, resp.read()


_post: Transport = _urllib_post


def configure(post: Transport | None = None) -> None:
    """Swap the transport. The tests call this with a canned responder."""
    global _post
    _post = post or _urllib_post


class ApolloError(RuntimeError):
    """A request that Apollo refused or that came back unusable."""


class AuthError(ApolloError):
    """The API key is missing, malformed, or rejected (401/403)."""


def build_request(*, api_key: str, title: str = "", keywords: str = "",
                  page: int = 1, per_page: int = MAX_PER_PAGE) -> tuple[
                      str, dict[str, str], bytes]:
    """The url, headers and JSON body for one search page.

    Split out from the call so a test can assert exactly what would go on the
    wire - the auth header shape especially - without a transport at all.
    """
    payload: dict[str, Any] = {
        "page": max(1, int(page)),
        "per_page": max(1, min(int(per_page), MAX_PER_PAGE)),
    }
    titles = [t.strip() for t in re.split(r"[,\n]", title) if t.strip()]
    if titles:
        payload["person_titles"] = titles
    words = keywords.strip()
    if words:
        # Apollo matches this against title, company and keywords - the broad
        # "what field is this business in" filter.
        payload["q_keywords"] = words
    headers = {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "Accept": "application/json",
        API_KEY_HEADER: api_key,
    }
    return SEARCH_URL, headers, json.dumps(payload).encode("utf-8")


def search_people(*, api_key: str, title: str = "", keywords: str = "",
                  page: int = 1, per_page: int = MAX_PER_PAGE,
                  timeout: float = 30.0) -> dict[str, Any]:
    """One page of raw results, or an ApolloError explaining the refusal."""
    if not (api_key or "").strip():
        raise AuthError("APOLLO_API_KEY is not set. Add it to .env to ingest.")
    url, headers, body = build_request(
        api_key=api_key, title=title, keywords=keywords, page=page,
        per_page=per_page)
    try:
        status, raw = _post(url, headers=headers, body=body, timeout=timeout)
    except Exception as exc:  # network, DNS, TLS - all transient to the caller
        raise ApolloError(f"Apollo request failed: {type(exc).__name__}: {exc}")
    if status in (401, 403):
        raise AuthError("Apollo rejected the API key (HTTP "
                        f"{status}). Check APOLLO_API_KEY in .env.")
    if status == 429:
        raise ApolloError("Apollo rate limit hit (HTTP 429). Wait and retry.")
    if status >= 400:
        raise ApolloError(f"Apollo returned HTTP {status}.")
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ApolloError(f"Apollo response was not JSON: {type(exc).__name__}")


def _clean_email(person: dict[str, Any]) -> str:
    """A real address, or '' when Apollo only returned a locked placeholder."""
    status = str(person.get("email_status") or "").strip().lower()
    if status in _LOCKED_STATUSES:
        return ""
    email = str(person.get("email") or "").strip().lower()
    if not email or "@" not in email or _LOCKED_EMAIL_RE.search(email):
        return ""
    return email


def _location(person: dict[str, Any]) -> str:
    parts = [str(person.get(k) or "").strip()
             for k in ("city", "state", "country")]
    return ", ".join(p for p in parts if p)


def _phone(org: dict[str, Any]) -> str:
    phone = org.get("primary_phone")
    if isinstance(phone, dict):
        return str(phone.get("number") or "").strip()
    return str(org.get("phone") or org.get("sanitized_phone") or "").strip()


def map_person(person: dict[str, Any]) -> dict[str, Any]:
    """One Apollo record flattened to the vault's lead fields.

    Returns a plain dict of the LEAD_FIELDS keys the vault understands; the
    worker fills ``source`` and ``lead_type``. No email is invented: a locked
    record becomes a lead with an empty email, never a fake one.
    """
    org = person.get("organization") or {}
    if not isinstance(org, dict):
        org = {}
    name = (str(person.get("name") or "").strip()
            or " ".join(str(person.get(k) or "").strip()
                        for k in ("first_name", "last_name")).strip())
    company = str(org.get("name") or "").strip()
    title = str(person.get("title") or "").strip()
    # The CRM's "category" column is free text; title at company is the most
    # useful single line an operator can read there.
    category = " @ ".join(p for p in (title, company) if p)
    return {
        "name": name,
        "email": _clean_email(person),
        "website": str(org.get("website_url") or org.get("domain") or "").strip(),
        "phone": _phone(org),
        # The public LinkedIn URL is the person's stable identity; it is what
        # dedup keys on so the same contact from two searches lands once.
        "handle": str(person.get("linkedin_url") or "").strip(),
        "location": _location(person),
        "category": category,
        "title": title,
        "company": company,
    }


def people(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The person records in a response, tolerant of an empty page."""
    rows = payload.get("people")
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _pagination_int(payload: dict[str, Any], key: str) -> int:
    pagination = payload.get("pagination")
    if isinstance(pagination, dict):
        try:
            return int(pagination.get(key) or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def total_available(payload: dict[str, Any]) -> int:
    """How many records Apollo says the query matches in all, or 0."""
    return _pagination_int(payload, "total_entries")


def total_pages(payload: dict[str, Any]) -> int:
    """How many pages Apollo says the query spans, or 0 when it did not say."""
    return _pagination_int(payload, "total_pages")


def has_more(payload: dict[str, Any], page: int, per_page: int) -> bool:
    """Whether another page is worth fetching after this one.

    Trusts Apollo's own pagination metadata when present - a short page is not
    reliably the last one - and falls back to the record-count heuristic only
    when the metadata is missing.
    """
    pages = total_pages(payload)
    if pages:
        return page < pages
    return len(people(payload)) >= per_page
