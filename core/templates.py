"""A/B templates.

Two files, ``templates/template_A.txt`` and ``templates/template_B.txt``. Each
is a subject line, a blank line, then the body:

    Subject: {Quick question|Quick idea} for [name]

    {Hi|Hello|Hey} [name],
    ...

Spintax and [placeholders] work exactly as they do in a single template - the
A/B split is about comparing two different angles, not two different engines.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.config import TEMPLATE_DIR

VARIANTS = ("A", "B")
SUBJECT_PREFIX = "subject:"

DEFAULT_A = """Subject: {Quick question|Quick idea} for [name]

{Hi|Hello|Hey} [name],

{I came across|I found} your site at [website] and noticed you are
{growing|expanding} in [location].

{Would you be open to|Any interest in} a short call this week?

Best,
"""

DEFAULT_B = """Subject: [name] - {a thought|an idea} on your video work

{Hi|Hello} [name],

{I have been following|I have been watching} what you are doing in [location].
{Most teams your size|Teams at your stage} lose hours to the same bottleneck.

{Worth a quick chat?|Open to a 10 minute call?}

Best,
"""

DEFAULTS = {"A": DEFAULT_A, "B": DEFAULT_B}


class TemplateError(RuntimeError):
    """A template file is missing, empty, or has no subject line."""


@dataclass(frozen=True)
class Template:
    variant: str
    subject: str
    body: str

    @property
    def is_usable(self) -> bool:
        return bool(self.subject.strip() and self.body.strip())


def path_for(variant: str) -> Path:
    return TEMPLATE_DIR / f"template_{variant.upper()}.txt"


def ensure_defaults() -> list[Path]:
    """Create any missing template file so the app works on a first boot."""
    created = []
    for variant in VARIANTS:
        target = path_for(variant)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(DEFAULTS[variant], encoding="utf-8")
            created.append(target)
    return created


def parse(text: str, variant: str = "A") -> Template:
    """Split 'Subject: ...' off the front. Everything after it is the body."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    subject = ""
    body_start = 0
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        if line.strip().lower().startswith(SUBJECT_PREFIX):
            subject = line.split(":", 1)[1].strip()
            body_start = index + 1
        break
    body = "\n".join(lines[body_start:]).strip("\n")
    return Template(variant=variant.upper(), subject=subject, body=body)


def load(variant: str) -> Template:
    target = path_for(variant)
    if not target.exists():
        raise TemplateError(f"{target.name} is missing. Open the Campaign tab and "
                            f"save variant {variant.upper()}.")
    template = parse(target.read_text(encoding="utf-8"), variant)
    if not template.subject.strip():
        raise TemplateError(f"{target.name} has no 'Subject:' line.")
    if not template.body.strip():
        raise TemplateError(f"{target.name} has a subject but no body.")
    return template


def save(variant: str, subject: str, body: str) -> Path:
    target = path_for(variant)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"Subject: {subject.strip()}\n\n{body.strip()}\n",
                      encoding="utf-8")
    return target


def load_all(variants: tuple[str, ...] = VARIANTS) -> list[Template]:
    """Every requested variant, or a TemplateError naming the broken file."""
    return [load(variant) for variant in variants]
