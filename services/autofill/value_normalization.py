"""Deterministic, lossless comparison rules for ATS form values."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Iterable


_US_STATES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas", "ca": "california",
    "co": "colorado", "ct": "connecticut", "de": "delaware", "fl": "florida", "ga": "georgia",
    "hi": "hawaii", "id": "idaho", "il": "illinois", "in": "indiana", "ia": "iowa",
    "ks": "kansas", "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland",
    "ma": "massachusetts", "mi": "michigan", "mn": "minnesota", "ms": "mississippi", "mo": "missouri",
    "mt": "montana", "ne": "nebraska", "nv": "nevada", "nh": "new hampshire", "nj": "new jersey",
    "nm": "new mexico", "ny": "new york", "nc": "north carolina", "nd": "north dakota", "oh": "ohio",
    "ok": "oklahoma", "or": "oregon", "pa": "pennsylvania", "ri": "rhode island", "sc": "south carolina",
    "sd": "south dakota", "tn": "tennessee", "tx": "texas", "ut": "utah", "vt": "vermont",
    "va": "virginia", "wa": "washington", "wv": "west virginia", "wi": "wisconsin", "wy": "wyoming",
    "dc": "district of columbia", "as": "american samoa", "gu": "guam", "mp": "northern mariana islands",
    "pr": "puerto rico", "vi": "u s virgin islands",
}
_REGION_ALIASES = {**_US_STATES, **{name: name for name in _US_STATES.values()},
                   "u.s. virgin islands": "u s virgin islands", "us virgin islands": "u s virgin islands"}
_COUNTRY_ALIASES = {
    "us": "united states", "usa": "united states", "u s": "united states", "u s a": "united states",
    "united states of america": "united states", "uk": "united kingdom", "u k": "united kingdom",
    "great britain": "united kingdom", "england": "united kingdom", "uae": "united arab emirates",
    "u a e": "united arab emirates", "south korea": "korea republic of", "republic of korea": "korea republic of",
}


def normal_text(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _token_text(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", normal_text(value)).strip()


def region_key(value: object) -> str:
    key = _token_text(value)
    return _REGION_ALIASES.get(key, key)


def country_key(value: object) -> str:
    key = _token_text(value)
    return _COUNTRY_ALIASES.get(key, key)


def phone_key(value: object) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    # North-American controls vary only by display country prefix.  Do not
    # remove a non-NANP prefix: it could change a real number's identity.
    return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits


def date_key(value: object) -> str:
    text = normal_text(value)
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return text


def equivalent_value(*, actual: object, expected: object, role: str = "", label: str = "",
                     input_type: str = "") -> bool:
    a, e = normal_text(actual), normal_text(expected)
    if not a or not e:
        return False
    field_kind = f"{role} {label} {input_type}".casefold()
    if "phone" in field_kind:
        return phone_key(a) == phone_key(e)
    if input_type.casefold() == "date" or "date" in normal_text(label):
        return date_key(a) == date_key(e)
    if role.casefold() in {"select", "combobox", "listbox"}:
        return a == e or region_key(a) == region_key(e) or country_key(a) == country_key(e)
    return _token_text(a) == _token_text(e)


@dataclass(frozen=True)
class SelectResolution:
    status: str  # exact | unique_alias | ambiguous | none
    value: str | None = None


def resolve_select_option(options: Iterable[object], wanted: object) -> SelectResolution:
    """Resolve only exact or one unambiguous normalized option label."""
    option_values = [str(item or "").strip() for item in options if str(item or "").strip()]
    exact = [item for item in option_values if normal_text(item) == normal_text(wanted)]
    if len(exact) == 1:
        return SelectResolution("exact", exact[0])
    if len(exact) > 1:
        return SelectResolution("ambiguous")
    aliases = [item for item in option_values if (
        region_key(item) == region_key(wanted) or country_key(item) == country_key(wanted)
    )]
    if len(aliases) == 1:
        return SelectResolution("unique_alias", aliases[0])
    return SelectResolution("ambiguous" if len(aliases) > 1 else "none")
