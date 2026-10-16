"""Fiber strand inventory and the express / fusion map at an enclosure."""

from __future__ import annotations

from typing import Optional

from .fields import parse_count, parse_range


def build_splice_rows(
    strand_count: int,
    a_cable_id: str,
    b_cable_id: str,
    enclosure_id: str,
    fusions: Optional[list[dict]] = None,
    opens: Optional[list[dict]] = None,
    a_layer_id: str = "",
    b_layer_id: str = "",
) -> list[dict]:
    """Default every strand express A.n -> B.n, then apply opens and fusions.

    A fusion of A 47-48 onto C starting at strand 1 removes those strands from
    the express map. The other strands stay on the same numbers.
    """
    fusions = fusions or []
    opens = opens or []
    assigned: dict[int, dict] = {}
    for number in range(1, strand_count + 1):
        assigned[number] = _row(
            enclosure_id,
            a_cable_id,
            number,
            b_cable_id,
            number,
            "EXPRESS",
            a_layer_id,
            b_layer_id,
        )
    for opened in opens:
        for number in range(int(opened["a_from"]), int(opened["a_to"]) + 1):
            assigned.pop(number, None)
    for fusion in fusions:
        start = int(fusion["b_start"])
        target = fusion["b_cable_id"]
        target_layer = fusion.get("b_layer_id") or ""
        for offset, number in enumerate(range(int(fusion["a_from"]), int(fusion["a_to"]) + 1)):
            if number < 1 or number > strand_count:
                continue
            assigned[number] = _row(
                enclosure_id,
                a_cable_id,
                number,
                target,
                start + offset,
                "FUSION",
                a_layer_id,
                target_layer,
            )
    return [assigned[number] for number in sorted(assigned)]


def _row(enclosure_id, a_cable, a_strand, b_cable, b_strand, splice_type, a_layer, b_layer) -> dict:
    return {
        "splice_enc_id": enclosure_id,
        "a_cable_id": a_cable,
        "a_strand_no": a_strand,
        "a_layer_id": a_layer,
        "b_cable_id": b_cable,
        "b_strand_no": b_strand,
        "b_layer_id": b_layer,
        "splice_type": splice_type,
        "status": "proposed",
    }


def validate_edits(strand_count: int, fusions: list[dict], opens: list[dict]) -> list[str]:
    """Reject overlapping or out-of-range strand edits before anything is written."""
    problems = []
    seen: dict[int, str] = {}

    def cover(label: str, start: int, end: int) -> None:
        if start < 1 or end > strand_count or start > end:
            problems.append(f"{label} is outside strands 1-{strand_count}.")
            return
        for number in range(start, end + 1):
            if number in seen:
                problems.append(f"Strand {number} is in both {seen[number]} and {label}.")
            seen[number] = label

    for fusion in fusions:
        cover(
            f"fusion {fusion['a_from']}-{fusion['a_to']}",
            int(fusion["a_from"]),
            int(fusion["a_to"]),
        )
    for opened in opens:
        cover(
            f"open {opened['a_from']}-{opened['a_to']}",
            int(opened["a_from"]),
            int(opened["a_to"]),
        )
    return problems


def compress_splices(rows: list[dict]) -> list[str]:
    """Turn per-strand rows into ranges a technician can read."""
    ordered = sorted(rows, key=lambda row: (str(row.get("a_cable_id")), int(row["a_strand_no"])))
    if not ordered:
        return []
    groups = []
    current = None
    for row in ordered:
        if current and _continues(current, row):
            current["a_to"] = int(row["a_strand_no"])
            current["b_to"] = int(row["b_strand_no"])
            continue
        current = {
            "a_cable_id": row["a_cable_id"],
            "a_from": int(row["a_strand_no"]),
            "a_to": int(row["a_strand_no"]),
            "b_cable_id": row.get("b_cable_id"),
            "b_from": int(row["b_strand_no"]) if row.get("b_strand_no") else None,
            "b_to": int(row["b_strand_no"]) if row.get("b_strand_no") else None,
            "splice_type": row.get("splice_type") or "",
        }
        groups.append(current)
    lines = []
    for group in groups:
        a_range = _fmt(group["a_from"], group["a_to"])
        kind = (group["splice_type"] or "splice").lower()
        if group["b_cable_id"] and group["b_from"]:
            b_range = _fmt(group["b_from"], group["b_to"])
            lines.append(
                f"{group['a_cable_id']} strands {a_range} {kind} to {group['b_cable_id']} strands {b_range}"
            )
        else:
            lines.append(f"{group['a_cable_id']} strands {a_range} {kind}")
    return lines


def _continues(group: dict, row: dict) -> bool:
    if row.get("a_cable_id") != group["a_cable_id"]:
        return False
    if (row.get("splice_type") or "") != group["splice_type"]:
        return False
    if row.get("b_cable_id") != group["b_cable_id"]:
        return False
    if int(row["a_strand_no"]) != group["a_to"] + 1:
        return False
    if group["b_to"] is None or row.get("b_strand_no") is None:
        return False
    return int(row["b_strand_no"]) == group["b_to"] + 1


def _fmt(start: int, end: int) -> str:
    return str(start) if start == end else f"{start}-{end}"


def strand_numbers_for(values: dict) -> list[int]:
    """Prefer an explicit count, then a port range such as '1-2'."""
    count = parse_count(values.get("strand_count"))
    if count:
        return list(range(1, count + 1))
    for key in ("a_port", "z_port", "port"):
        numbers = parse_range(values.get(key))
        if numbers:
            return numbers
    return []
