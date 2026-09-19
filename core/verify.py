"""Zero-bounce armour - does this domain accept mail at all?

A bounce costs more than a skipped lead. Mailbox providers score a sending
domain on how much of its traffic bounces, and a list scraped off the open web
always carries some addresses whose domain has no mail server behind it: a
parked domain, a typo'd TLD, a site that moved. Every one of those is a hard
bounce charged against the sender.

So the domain is resolved before the message is built, and the result is
cached - a scraped list of two hundred leads usually spans a few dozen domains,
and resolving the same one two hundred times would be slower than the SMTP
handshake it is protecting.

Two distinctions carry the whole module:

* **Definitive vs transient.** NXDOMAIN, or an authoritative "no such record",
  means the domain cannot receive mail and the lead is dropped. A timeout or a
  SERVFAIL means the resolver had a bad moment, and dropping on that would
  throw away a good lead permanently. Transient failures send anyway.
* **Implicit MX.** RFC 5321 says a domain with no MX record but a valid A or
  AAAA record still accepts mail at that address. Treating a missing MX as
  undeliverable would drop small self-hosted domains, which is exactly the
  kind of business this tool hunts.

The resolver is injectable so the offline test suite never touches the network.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Protocol

DEFAULT_TIMEOUT = 4.0

# Verdicts.
DELIVERABLE = "deliverable"       # has MX, or an implicit A/AAAA fallback
NO_MAIL_SERVER = "no_mail_server"  # definitive: nothing will ever accept mail
UNKNOWN = "unknown"                # transient failure; do not punish the lead

DROP_REASON = "MX_INVALID: Dropped to protect sender reputation."


@dataclass(frozen=True)
class DomainVerdict:
    domain: str
    status: str
    detail: str = ""

    @property
    def deliverable(self) -> bool:
        """Only a definitive negative stops a send."""
        return self.status != NO_MAIL_SERVER


class Resolver(Protocol):
    def __call__(self, domain: str, record: str, timeout: float) -> list[str]:
        """Return records, [] for a definitive empty answer, raise otherwise."""


class DefinitiveNegative(Exception):
    """The domain answered, and the answer was no."""


class TransientFailure(Exception):
    """The resolver could not answer. Says nothing about the domain."""


def dns_resolver(domain: str, record: str, timeout: float) -> list[str]:
    """The real resolver, on dnspython.

    dnspython ships with email-validator, which this project already depends
    on, so MX checking costs no new package.
    """
    import dns.exception
    import dns.resolver

    try:
        answer = dns.resolver.resolve(domain, record, lifetime=timeout)
        return [str(item) for item in answer]
    except dns.resolver.NXDOMAIN as exc:
        raise DefinitiveNegative(f"no such domain") from exc
    except dns.resolver.NoAnswer as exc:
        return []                       # the domain exists, this record does not
    except dns.resolver.NoNameservers as exc:
        raise TransientFailure("no nameservers answered") from exc
    except dns.exception.Timeout as exc:
        raise TransientFailure("resolver timed out") from exc
    except Exception as exc:            # noqa: BLE001 - never break a campaign
        raise TransientFailure(f"{type(exc).__name__}") from exc


class MailDomainCache:
    """Per-domain verdicts, resolved once and reused.

    Guarded by a lock because the campaign worker and the diagnostics scan can
    both reach it, and because the hunter resolves concurrently.
    """

    def __init__(self, resolve: Callable | None = None,
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self._resolve = resolve or dns_resolver
        self._timeout = timeout
        self._cache: dict[str, DomainVerdict] = {}
        self._lock = threading.Lock()
        self.lookups = 0        # actual resolver calls
        self.hits = 0           # answered from cache

    def verdict(self, address_or_domain: str) -> DomainVerdict:
        domain = self._domain_of(address_or_domain)
        if not domain:
            return DomainVerdict("", NO_MAIL_SERVER, "no domain in the address")

        with self._lock:
            cached = self._cache.get(domain)
            if cached is not None:
                self.hits += 1
                return cached

        result = self._lookup(domain)
        with self._lock:
            self._cache[domain] = result
            self.lookups += 1
        return result

    def _lookup(self, domain: str) -> DomainVerdict:
        try:
            records = self._resolve(domain, "MX", self._timeout)
        except DefinitiveNegative as exc:
            return DomainVerdict(domain, NO_MAIL_SERVER, str(exc))
        except TransientFailure as exc:
            return DomainVerdict(domain, UNKNOWN, str(exc))

        if records:
            return DomainVerdict(domain, DELIVERABLE, f"{len(records)} MX record(s)")

        # No MX. RFC 5321 implicit MX: an A or AAAA record still accepts mail.
        for record in ("A", "AAAA"):
            try:
                if self._resolve(domain, record, self._timeout):
                    return DomainVerdict(domain, DELIVERABLE,
                                         f"no MX, implicit {record} fallback")
            except DefinitiveNegative:
                return DomainVerdict(domain, NO_MAIL_SERVER, "no such domain")
            except TransientFailure as exc:
                return DomainVerdict(domain, UNKNOWN, str(exc))

        return DomainVerdict(domain, NO_MAIL_SERVER, "no MX and no address record")

    @staticmethod
    def _domain_of(value: str) -> str:
        value = (value or "").strip().lower()
        if "@" in value:
            value = value.rsplit("@", 1)[-1]
        return value.strip(" .")

    def stats(self) -> dict:
        with self._lock:
            undeliverable = sum(1 for v in self._cache.values()
                                if v.status == NO_MAIL_SERVER)
            return {"domains": len(self._cache), "lookups": self.lookups,
                    "cache_hits": self.hits, "undeliverable": undeliverable}


# One cache per process, so a second campaign reuses the first one's answers.
_cache = MailDomainCache()


def cache() -> MailDomainCache:
    return _cache


def configure(resolve: Callable | None = None,
              timeout: float = DEFAULT_TIMEOUT) -> MailDomainCache:
    """Replace the shared cache, optionally with a different resolver.

    The seam exists so the test suite stays offline. Gating sends on DNS would
    otherwise make every campaign test depend on a working resolver, and a
    suite that fails when the wifi drops is a suite nobody trusts.
    """
    global _cache
    _cache = MailDomainCache(resolve=resolve, timeout=timeout)
    return _cache


def verdict(address: str) -> DomainVerdict:
    return _cache.verdict(address)


def deliverable(address: str) -> bool:
    """False only when the domain definitively cannot receive mail."""
    return _cache.verdict(address).deliverable
