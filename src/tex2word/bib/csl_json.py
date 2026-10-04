"""Parse CSL JSON bibliography data into tex2word's citation IR."""

from __future__ import annotations

import json
from typing import Any

from .. import ir


def parse_csl_json(source: str) -> dict[str, ir.CSLItem]:
    """Parse a standard CSL JSON array into ``{citekey: CSLItem}``."""
    data: Any = json.loads(source)
    if not isinstance(data, list):
        raise ValueError("CSL JSON must be an array")

    items: dict[str, ir.CSLItem] = {}
    for index, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise ValueError(f"CSL JSON item {index} must be an object")
        if "id" not in entry:
            raise ValueError(f"CSL JSON item {index} is missing 'id'")
        if "type" not in entry:
            raise ValueError(f"CSL JSON item {index} is missing 'type'")

        item_id = entry["id"]
        item_type = entry["type"]
        if not isinstance(item_id, str) or not item_id:
            raise ValueError(f"CSL JSON item {index} has an invalid 'id'")
        if not isinstance(item_type, str) or not item_type:
            raise ValueError(f"CSL JSON item {index} has an invalid 'type'")

        fields = {key: value for key, value in entry.items() if key not in {"id", "type"}}
        items[item_id] = ir.CSLItem(id=item_id, type=item_type, csl_fields=fields)

    return items
