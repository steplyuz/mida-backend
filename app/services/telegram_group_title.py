"""Group title: '<deal number> | <product title>'. The deal number always survives truncation."""

from __future__ import annotations

import unicodedata

MAX_TITLE_LENGTH = 255
FALLBACK_TITLE = "Bitim"


def _printable(text: str) -> str:
    kept = (ch for ch in text if unicodedata.category(ch)[0] not in {"C"})
    return " ".join("".join(kept).split())


def build_group_title(deal_number: str, product_title: str) -> str:
    prefix = f"{deal_number} | "
    room = MAX_TITLE_LENGTH - len(prefix)
    product = _printable(product_title) or FALLBACK_TITLE
    if len(product) > room:
        product = product[: room - 1].rstrip() + "…"
    return prefix + product
