"""Helpers for reMarkable document and page tags.

The tablet stores tags in a document's ``.content`` file, not in ``.metadata``:

- ``tags``: document tags, ``[{"name": "work", "timestamp": 1700000000000}, ...]``
- ``pageTags``: page tags, ``[{"name": "todo", "pageId": "...", "timestamp": ...}, ...]``

Older tooling (and some tests) use plain strings, so every reader accepts both.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, List, Optional


def tag_names(raw: Any) -> List[str]:
    """Return unique tag names from a raw ``tags``/``pageTags`` value, in order."""
    names: List[str] = []
    if not isinstance(raw, list):
        return names
    for entry in raw:
        if isinstance(entry, str):
            name = entry
        elif isinstance(entry, dict):
            name = entry.get("name")
        else:
            continue
        if isinstance(name, str) and name.strip() and name not in names:
            names.append(name)
    return names


def clean_tag_list(tags: Optional[Iterable[str]]) -> List[str]:
    """Strip, drop empties and de-duplicate (case-insensitively) user-supplied tags."""
    result: List[str] = []
    seen = set()
    for tag in tags or []:
        if not isinstance(tag, str):
            continue
        name = tag.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            result.append(name)
    return result


def tag_entries(names: Iterable[str], timestamp_ms: Optional[int] = None) -> List[dict]:
    """Build ``.content`` tag entries for ``names``."""
    ts = int(timestamp_ms if timestamp_ms is not None else time.time() * 1000)
    return [{"name": name, "timestamp": ts} for name in clean_tag_list(names)]


def apply_tag_changes(
    content: dict,
    add: Optional[Iterable[str]] = None,
    remove: Optional[Iterable[str]] = None,
    timestamp_ms: Optional[int] = None,
) -> List[str]:
    """Add/remove document tags in a parsed ``.content`` dict; return the new names.

    Existing entries keep their original timestamps and spelling. Removal is
    case-insensitive; adding a tag that already exists (in any case) is a no-op.
    """
    ts = int(timestamp_ms if timestamp_ms is not None else time.time() * 1000)
    remove_lower = {t.lower() for t in clean_tag_list(remove)}

    kept: List[dict] = []
    for entry in content.get("tags") or []:
        if isinstance(entry, str):
            entry = {"name": entry, "timestamp": ts}
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        if entry["name"].lower() in remove_lower:
            continue
        kept.append(entry)

    present = {e["name"].lower() for e in kept}
    for name in clean_tag_list(add):
        if name.lower() not in present:
            kept.append({"name": name, "timestamp": ts})
            present.add(name.lower())

    content["tags"] = kept
    return [e["name"] for e in kept]


def matches_tags(
    filter_tags: Optional[Iterable[str]],
    doc_tags: Iterable[str],
    page_tags: Iterable[str] = (),
) -> bool:
    """True when any filter tag matches a document or page tag (case-insensitive)."""
    wanted = {t.lower() for t in clean_tag_list(filter_tags)}
    if not wanted:
        return True
    have = {t.lower() for t in doc_tags} | {t.lower() for t in page_tags}
    return bool(wanted & have)
