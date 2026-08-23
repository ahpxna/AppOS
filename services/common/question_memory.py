"""Human-confirmed, scope-aware answer memory for deterministic autofill."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RememberedAnswer:
    value: str
    answer_kind: str = "text"


def normalize_question(value: str) -> str:
    """Normalize wording for exact repeat detection without semantic guessing."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (value or "").casefold())).strip()
