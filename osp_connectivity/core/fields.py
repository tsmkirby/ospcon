"""Field aliases, blank values, media, and id patterns shared by both projects."""

from __future__ import annotations

import re
from typing import Iterable, Optional


def is_blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


def text_of(value) -> Optional[str]:
    if is_blank(value):
        return None
    return str(value).strip()


def parse_count(value) -> Optional[int]:
    """Read a strand count from 12, 12.0, or '12F'. Ignores empty values."""
    if isinstance(value, bool) or is_blank(value):
        return None
    if isinstance(value, (int, float)):
        count = int(value)
        return count if count > 0 else None
    match = re.search(r"(\d+)", str(value))
    if not match:
        return None
    count = int(match.group(1))
    return count if count > 0 else None


def parse_range(value) -> list[int]:
    """Parse '1-2', '7-8', '1', or '1-46, 49-144' into strand numbers."""
    if isinstance(value, bool) or is_blank(value):
        return []
    if isinstance(value, (int, float)):
        number = int(value)
        return [number] if number > 0 else []
    numbers: list[int] = []
    for part in re.split(r"[,;\s]+", str(value).strip()):
        if not part:
            continue
        span = re.match(r"^(\d+)\s*-\s*(\d+)$", part)
        if span:
            start, end = int(span.group(1)), int(span.group(2))
            if start > end:
                start, end = end, start
            numbers.extend(range(start, end + 1))
        elif part.isdigit():
            numbers.append(int(part))
    return numbers


def ranges_overlap(left, right) -> bool:
    left_numbers = parse_range(left)
    right_numbers = parse_range(right)
    if left_numbers and right_numbers:
        return bool(set(left_numbers) & set(right_numbers))
    left_text = text_of(left)
    right_text = text_of(right)
    return bool(left_text and right_text and left_text.casefold() == right_text.casefold())


def media_of(values: dict) -> str:
    """Return 'fiber', 'copper', or 'unknown' from the fields a cable actually has."""
    media = (text_of(values.get("media")) or "").lower()
    if media.startswith("copper"):
        return "copper"
    if "fiber" in media:
        return "fiber"
    cable_type = (text_of(values.get("cable_type")) or "").lower()
    if "fiber" in cable_type or "os2" in cable_type or re.search(r"\bom[1-5]\b", cable_type):
        return "fiber"
    if "cat" in cable_type or "copper" in cable_type:
        return "copper"
    uplink = (text_of(values.get("uplink")) or "").lower()
    if "fiber" in uplink:
        return "fiber"
    if "copper" in uplink:
        return "copper"
    if parse_count(values.get("strand_count")):
        return "fiber"
    return "unknown"


_ID_CANDIDATES = {
    "cable": ["cable_id", "id"],
    "duct": ["segment_id"],
    "ap": ["ap_id", "device_id"],
    "cctv": ["cctv_id", "device_id"],
    "pos": ["pos_id", "device_id"],
    "idf": ["idf_id", "mdf_idf_id"],
    "structure": ["support_id", "structure_id"],
    "handhole": ["access_id", "structure_id", "structure_name", "access_name"],
    "patch_panel": ["pp_id"],
    "fdp": ["fdp_id"],
    "splice_enc": ["splice_enc_id"],
    "data_jack": ["jack_id"],
    "building": ["building_id"],
}

_VALUE_CANDIDATES = {
    "name": [
        "cable_name",
        "ap_name",
        "device_name",
        "cctv_name",
        "pos_name",
        "idf_name",
        "mdf_idf_name",
        "pp_name",
        "fdp_name",
        "splice_enc_name",
        "access_name",
        "Name",
        "name",
    ],
    "media": ["media_type"],
    "cable_type": ["cable_type"],
    "strand_count": ["strand_count", "fiber_count"],
    "a_end": ["a_end_location", "a_end_id"],
    "z_end": ["z_end_location", "z_end_id"],
    "a_end_type": ["a_end_type"],
    "z_end_type": ["z_end_type"],
    "a_panel": ["a_end_panel_id"],
    "z_panel": ["z_end_panel_id"],
    "a_port": ["a_end_port"],
    "z_port": ["z_end_port"],
    "panel": ["patch_panel_id", "patch_pane_id"],
    "port": ["patch_panel_port", "panel_port", "panel_position"],
    "cable_ref": ["cable_id"],
    "mount": ["mount_structure_id", "support_id"],
    "uplink": ["uplink_type"],
    "length_m": ["gis_length_m"],
    "length_f": ["gis_length_f"],
    "service": ["service_type"],
    "status": ["status"],
    "structure_type": ["structure_type", "support_type", "location_type"],
    "venue": ["venue_code"],
    "idf_ref": ["mdf_idf_id"],
}


def id_candidates(role: Optional[str]) -> list[str]:
    return list(_ID_CANDIDATES.get(role or "", []))


def map_fields(role: Optional[str], field_names: Iterable[str]) -> dict[str, str]:
    """Map canonical names to the first real field present on this layer."""
    available = list(field_names)
    lookup = {name.casefold(): name for name in available}
    mapped: dict[str, str] = {}

    def take(canonical: str, candidates: list[str]) -> None:
        for candidate in candidates:
            actual = lookup.get(candidate.casefold())
            if actual:
                mapped[canonical] = actual
                return

    if role in _ID_CANDIDATES:
        take("id", _ID_CANDIDATES[role])
    for canonical, candidates in _VALUE_CANDIDATES.items():
        if canonical == "cable_ref" and role == "cable":
            continue
        if canonical == "id":
            continue
        take(canonical, candidates)
    return mapped


def id_pattern(value: str) -> tuple[str, int]:
    """Split 'vib.fib.0004' or 'VIB-CB-0001' into a prefix and a zero-pad width."""
    text = text_of(value) or ""
    match = re.match(r"^(.*?)(\d+)$", text)
    if not match:
        return (text + "-" if text else "OSP-"), 4
    return match.group(1), len(match.group(2))


def next_id(existing: Iterable[str], sample: str) -> str:
    """Next id that follows the numbering already used by sample."""
    prefix, width = id_pattern(sample)
    highest = 0
    for value in existing:
        text = text_of(value)
        if not text:
            continue
        other_prefix, _width = id_pattern(text)
        if other_prefix.casefold() != prefix.casefold():
            continue
        match = re.match(r"^(.*?)(\d+)$", text)
        if match:
            highest = max(highest, int(match.group(2)))
    return f"{prefix}{highest + 1:0{width}d}"


def enclosure_id(existing: Iterable[str], cable_id: str, template_style: bool) -> str:
    """Template enclosures use VIB-EN-0001. A dotted cable id uses the same venue with .enc."""
    if template_style:
        return enclosure_id_from_prefix(existing, suggest_enclosure_prefix(cable_id))
    prefix, _width = id_pattern(cable_id or "osp.0000")
    parts = prefix.rstrip(".-").split(".")
    if len(parts) >= 2:
        enc_prefix = parts[0] + ".enc."
    else:
        enc_prefix = prefix + "enc."
    return next_id(list(existing) + [enc_prefix + "0000"], enc_prefix + "0000")


def suggest_enclosure_prefix(cable_id: str) -> str:
    """Venue code a technician can confirm, such as VIB or OSP."""
    text = text_of(cable_id) or ""
    match = re.match(r"^([A-Za-z]{2,8})-", text)
    if match:
        return match.group(1).upper()
    dotted = [part for part in text.split(".") if part]
    if len(dotted) >= 2 and dotted[0].isalpha():
        return dotted[0].upper()
    return "OSP"


def enclosure_id_from_prefix(existing: Iterable[str], prefix: str) -> str:
    """Build the next PREFIX-EN-0001 id from the prefix the user typed."""
    text = (prefix or "").strip().strip("-")
    if not text:
        raise ValueError("Enter an enclosure prefix.")
    if text.upper().endswith("-EN"):
        sample = f"{text}-0000"
    else:
        sample = f"{text}-EN-0000"
    return next_id(list(existing), sample)
