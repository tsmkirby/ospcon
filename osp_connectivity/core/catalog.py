"""Look up asset ids across every layer in the open project."""

from __future__ import annotations

from .fields import is_blank, text_of

ASSET_ROLES = {
    "ap",
    "cctv",
    "data_jack",
    "pos",
    "idf",
    "patch_panel",
    "fdp",
    "structure",
    "handhole",
    "splice_enc",
    "building",
}
DEVICE_ROLES = {"ap", "cctv", "data_jack", "pos"}


def feature_id(feature: dict):
    return text_of(feature.get("id")) or text_of((feature.get("values") or {}).get("id"))


def same_feature(left: dict, right: dict) -> bool:
    return left.get("layer_id") == right.get("layer_id") and left.get("fid") == right.get("fid")


def build_index(features: list[dict]) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = {}
    for feature in features:
        identifier = feature_id(feature)
        if not identifier:
            continue
        feature["id"] = identifier
        index.setdefault(identifier.casefold(), []).append(feature)
    return index


def lookup(index: dict, text, skip: dict | None = None) -> list[dict]:
    identifier = text_of(text)
    if not identifier:
        return []
    hits = list(index.get(identifier.casefold(), []))
    if skip is not None:
        hits = [hit for hit in hits if not same_feature(hit, skip)]
    return hits


def resolve_end(index: dict, text, owner: dict | None = None) -> list[dict]:
    """Prefer a device, IDF, or structure over another cable with the same text."""
    hits = lookup(index, text, owner)
    assets = [hit for hit in hits if hit.get("role") in ASSET_ROLES]
    if assets:
        return assets
    return [hit for hit in hits if hit.get("role") == "cable"]


def duplicate_cable_ids(cables: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for cable in cables:
        identifier = feature_id(cable)
        if not identifier:
            continue
        grouped.setdefault(identifier.casefold(), []).append(cable)
    return {key: rows for key, rows in grouped.items() if len(rows) > 1}


def end_convention(cables: list[dict], index: dict) -> str:
    """Learn which end holds the device from the cables that are already filled in.

    Vibe copper stores the AP on A and the IDF on Z. Empty projects stay unknown
    so the associate dialog can ask.
    """
    a_device = a_idf = z_device = z_idf = 0
    for cable in cables:
        values = cable.get("values") or {}
        for end_name, device_counter, idf_counter in (
            ("a_end", "a_device", "a_idf"),
            ("z_end", "z_device", "z_idf"),
        ):
            hits = resolve_end(index, values.get(end_name), cable)
            is_device = any(hit.get("role") in DEVICE_ROLES for hit in hits)
            is_idf = any(hit.get("role") == "idf" for hit in hits)
            if end_name == "a_end":
                a_device += int(is_device)
                a_idf += int(is_idf)
            else:
                z_device += int(is_device)
                z_idf += int(is_idf)
    if a_device > a_idf and z_idf >= z_device and (a_device or z_idf):
        return "a_device"
    if a_idf > a_device and z_device > z_idf:
        return "a_idf"
    return "unknown"
