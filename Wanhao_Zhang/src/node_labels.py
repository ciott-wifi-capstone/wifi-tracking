#!/usr/bin/env python3
"""Map deployment labels to complete registered MAC address variants.

Names and complete MACs use exact variant matching. Short MAC fragments retain
legacy prefix and suffix matching for interactive commands.
"""

from __future__ import annotations

import json
import pathlib
import re

DEFAULT_PATH = pathlib.Path(__file__).resolve().parents[1] / "wipro_nodes.json"
MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def mac_variants(mac: str) -> set[str]:
    """Return the registered MAC and its final-byte increment, modulo 256."""
    cleaned = mac.strip().upper()
    parts = cleaned.split(":")
    if len(parts) != 6:
        return {cleaned}
    last = int(parts[5], 16)
    return {":".join(parts),
            ":".join(parts[:5] + [f"{(last + 1) & 0xFF:02X}"])}


def prefix_of(mac: str) -> str:
    """Return a five-octet display prefix for legacy callers."""
    cleaned = mac.strip().upper()
    parts = cleaned.split(":")
    if len(parts) != 6:
        return cleaned
    return ":".join(parts[:5])


def load_table(path: pathlib.Path | str | None = None) -> dict[str, str]:
    """Load a complete-MAC-to-label table, or return an empty table on read failure."""
    target = pathlib.Path(path) if path else DEFAULT_PATH
    if not target.exists():
        return {}
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    nodes = document.get("nodes", document if isinstance(document, dict) else {})
    table: dict[str, str] = {}
    for label, value in nodes.items():
        if label.startswith("_"):
            continue
        mac = value.get("mac") if isinstance(value, dict) else value
        if isinstance(mac, str) and mac:
            table[mac.strip().upper()] = label
    return table


def label_for(mac: str, table: dict[str, str] | None = None) -> str:
    """Resolve a registered MAC variant, falling back to its final two octets."""
    table = table if table is not None else load_table()
    wanted = mac.strip().upper()
    for registered, label in table.items():
        if wanted in mac_variants(registered):
            return label
    parts = wanted.split(":")
    return ":".join(parts[-2:]) if len(parts) == 6 else wanted


def resolve(name_or_mac: str, table: dict[str, str] | None = None) -> list[str]:
    """Expand a label or complete MAC into variants; retain fragment tokens."""
    table = table if table is not None else load_table()
    token = name_or_mac.strip().upper()
    named: list[str] = []
    for registered, label in table.items():
        if label.upper() == token:
            named.extend(sorted(mac_variants(registered)))
    if named:
        return named
    if MAC_RE.match(token):
        return sorted(mac_variants(token))
    return [token]          # Preserve fragment tokens for legacy matching.


def matches(mac: str, token: str, table: dict[str, str] | None = None) -> bool:
    """Match complete address variants exactly, or match a legacy fragment."""
    upper = mac.strip().upper()
    for candidate in resolve(token, table):
        candidate = candidate.upper()
        if ":" in candidate and candidate.count(":") == 5:
            if upper == candidate:                 # Complete addresses require equality.
                return True
        elif upper.startswith(candidate) or upper.endswith(candidate):
            return True                            # Legacy fragment matching.
    return False
