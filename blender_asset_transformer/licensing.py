"""Detect, store, merge, and write license records. Records information; not legal advice."""

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

PROPERTY = "bat_license"
ATTRIBUTION_FILE = "ATTRIBUTION.md"
UNKNOWN = "License unknown"
LICENSE_NAME_HINTS = ("license", "licence", "readme", "eula", "copying", "credits")
LICENSE_SUFFIXES = {"", ".txt", ".md"}

# Checked in order; share-alike before plain CC-BY so "CC BY-SA" isn't matched as CC-BY.
LICENSE_PATTERNS = (
    ("Asset Store EULA", re.compile(r"asset\s*store.{0,40}(eula|end\s*user\s*license)", re.I)),
    ("CC0", re.compile(r"\bCC0\b|creativecommons\.org/publicdomain/zero", re.I)),
    ("CC-BY-SA", re.compile(r"\bCC[\s-]*BY[\s-]*SA\b|creativecommons\.org/licenses/by-sa", re.I)),
    (
        "CC-BY",
        re.compile(r"\bCC[\s-]*BY\b(?![\s-]*(SA|NC|ND))|creativecommons\.org/licenses/by/", re.I),
    ),
    ("MIT", re.compile(r"\bMIT License\b|Permission is hereby granted, free of charge", re.I)),
)
RESTRICTIVE = {"Asset Store EULA", "CC-BY-SA", UNKNOWN}

_PUBLISHER_RE = re.compile(r"^\s*(?:publisher|author)\s*:\s*(.+)$", re.I | re.M)
_COPYRIGHT_RE = re.compile(
    r"copyright\s*(?:\(c\)|©)?\s*(?:\d{4}(?:\s*-\s*\d{4})?,?)?\s*(.+)$", re.I | re.M
)
_ATTRIBUTION_RE = re.compile(r"^\s*(?:attribution|credit)s?\s*:\s*(.+)$", re.I | re.M)


@dataclass(frozen=True)
class Record:
    pack: str
    publisher: str
    license: str
    attribution: str
    source: str


def license_files(pack_dir: Path) -> list[Path]:
    """License/readme text files anywhere in the pack."""
    return sorted(
        p
        for p in pack_dir.rglob("*")
        if p.is_file()
        and p.suffix.lower() in LICENSE_SUFFIXES
        and any(h in p.stem.lower() for h in LICENSE_NAME_HINTS)
    )


def classify(text: str) -> str:
    """License type by text match; UNKNOWN unless exactly one type matches."""
    found = {name for name, pattern in LICENSE_PATTERNS if pattern.search(text)}
    return found.pop() if len(found) == 1 else UNKNOWN


def _first(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return match.group(1).strip() if match else ""


def detect(pack: str, pack_dir: Path) -> Record:
    """Build a record for a pack from its license files; UNKNOWN if none match."""
    for path in license_files(pack_dir):
        text = path.read_text(encoding="utf-8", errors="replace")
        kind = classify(text)
        if kind != UNKNOWN:
            publisher = _first(_PUBLISHER_RE, text) or _first(_COPYRIGHT_RE, text)
            source = path.relative_to(pack_dir).as_posix()
            return Record(pack, publisher, kind, _first(_ATTRIBUTION_RE, text), source)
    return Record(pack, "", UNKNOWN, "", "")


def dumps(records: list[Record]) -> str:
    return json.dumps([asdict(r) for r in records])


def loads(data: str) -> list[Record]:
    return [Record(**item) for item in json.loads(data)] if data else []


def merge(*groups: list[Record]) -> list[Record]:
    """Union of records, order kept, duplicates dropped."""
    return list(dict.fromkeys(r for group in groups for r in group))


def restrictive(records: list[Record]) -> list[Record]:
    return [r for r in records if r.license in RESTRICTIVE]


def attribution_markdown(asset: str, records: list[Record]) -> str:
    lines = [
        f"# Attribution: {asset}",
        "",
        "Recorded by Blender Asset Transformer. Not legal advice.",
        "",
    ]
    for r in records:
        lines += [
            f"## {r.pack}",
            f"- License: {r.license}",
            f"- Publisher: {r.publisher or 'not found'}",
            f"- Attribution: {r.attribution or 'none stated'}",
            f"- Source file: {r.source or 'none found'}",
            "",
        ]
    return "\n".join(lines)
