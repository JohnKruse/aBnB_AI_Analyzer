"""Conservative, evidence-backed extraction of listing features.

Legacy columns (``has_ac``, ``has_washer`` and ``bathroom_privacy``) remain for
CSV/UI compatibility. New ``*_evidence`` columns retain present/absent/unknown
states and the text or structured field supporting the decision.
"""

from __future__ import annotations

import ast
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Tuple

import pandas as pd

EXTRACTION_VERSION = "listing-evidence-v3"

ACCOMMODATION_TYPES = (
    "Entire home/apt",
    "Private room",
    "Shared room",
    "Hotel room",
    "Unknown",
)


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, tuple, dict, set)):
        return False
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _parse_collection(value: Any) -> Any:
    """Parse JSON/Python-literal columns without ambiguous pd.isna checks."""
    if _is_missing(value):
        return []
    if isinstance(value, (list, tuple, dict)):
        return value
    if not isinstance(value, str) or not value.strip():
        return []
    for parser in (json.loads, ast.literal_eval):
        try:
            return parser(value)
        except (ValueError, SyntaxError, TypeError, json.JSONDecodeError):
            pass
    return []


def _strings_from_collection(value: Any, field: str) -> List[Tuple[str, str]]:
    """Return ``(text, source path)`` leaves from common Airbnb nested shapes."""
    root = _parse_collection(value)
    found: List[Tuple[str, str]] = []

    def walk(item: Any, path: str) -> None:
        if isinstance(item, str):
            if item.strip():
                found.append((item.strip(), path))
        elif isinstance(item, dict):
            for key in ("title", "name", "text", "label", "value"):
                child = item.get(key)
                if isinstance(child, str) and child.strip():
                    found.append((child.strip(), f"{path}.{key}"))
            # Preserve structured availability as a separate explicit source.
            for key in ("values", "amenities", "items"):
                child = item.get(key)
                if isinstance(child, (list, tuple, dict)):
                    walk(child, f"{path}.{key}")
        elif isinstance(item, (list, tuple)):
            for index, child in enumerate(item):
                walk(child, f"{path}[{index}]")

    walk(root, field)
    # Repeated title/value fields are common; retain the earliest source only.
    unique: List[Tuple[str, str]] = []
    seen = set()
    for text, source in found:
        key = text.casefold()
        if key not in seen:
            seen.add(key)
            unique.append((text, source))
    return unique


def _text_sources(row: Any, include_reviews: bool = False) -> List[Tuple[str, str]]:
    sources: List[Tuple[str, str]] = []
    for key in ("name", "title", "description", "amenities", "images_details", "images"):
        raw = row.get(key)
        if key in {"amenities", "images_details", "images"}:
            sources.extend(_strings_from_collection(raw, key))
        elif not _is_missing(raw) and str(raw).strip():
            sources.append((str(raw).strip(), key))
    if include_reviews:
        reviews = row.get("reviews_text")
        if not _is_missing(reviews) and str(reviews).strip():
            # Review evidence is intentionally clipped to bound extraction work.
            sources.append((str(reviews)[:12000], "reviews_text"))
    return sources


def _result(state: str, evidence: List[Dict[str, str]], conflicts: bool = False) -> Dict[str, Any]:
    return {
        "state": state,
        "evidence": evidence,
        "conflict": bool(conflicts),
        "extraction_version": EXTRACTION_VERSION,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def _evidence(source: str, text: str) -> Dict[str, str]:
    return {"source": source, "text": text[:500]}


def _fact(state: Any, evidence: List[Dict[str, str]], *, value: Any = None, conflict: bool = False) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "state": state,
        "evidence": evidence,
        "conflict": bool(conflict),
        "extraction_version": EXTRACTION_VERSION,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    if value is not None or isinstance(state, str) and state in {"unknown", "conflict"}:
        result["value"] = value
    return result


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row.get(key, default)
    except AttributeError:
        try:
            return row[key]
        except (KeyError, TypeError, IndexError):
            return default


def _is_explicit_true(value: Any) -> bool:
    if isinstance(value, bool) or type(value).__name__ == "bool_":
        return bool(value)
    if isinstance(value, str):
        return value.strip().casefold() in {"true", "1", "yes"}
    return False


def _is_explicit_false(value: Any) -> bool:
    if isinstance(value, bool) or type(value).__name__ == "bool_":
        return not bool(value)
    if isinstance(value, str):
        return value.strip().casefold() in {"false", "0", "no"}
    return False


def _clauses(text: str) -> List[str]:
    """Split short evidence clauses so one amenity does not color another."""
    return [part.strip() for part in re.split(r"[;.!?\n]+|\b(?:but|however|although)\b", text, flags=re.I) if part.strip()]


def extract_amenity_evidence(row: Any, amenity: str) -> Dict[str, Any]:
    """Return ``present``, ``absent`` or ``unknown`` with direct evidence.

    Only explicit statements resolve a fact. A missing amenity entry is unknown,
    and a generic "laundry" mention does not establish washer access.
    """
    key = amenity.strip().lower().replace(" ", "_")
    if key in {"ac", "air_conditioning", "air_conditioner"}:
        positive = re.compile(r"\b(?:air\s*conditioning|air\s*con|aircon|a/c|ac)\b", re.I)
        negative_before = re.compile(
            r"\b(?:no|without|lacks?|not\s+(?:provided|available|included)|unavailable)\s+"
            r"(?:any\s+)?(?:air\s*conditioning|air\s*con|aircon|a/c|ac)\b", re.I,
        )
        negative_after = re.compile(
            r"\b(?:air\s*conditioning|air\s*con|aircon|a/c|ac)\b"
            r"(?:\s+\w+){0,3}\s+(?:is\s+|are\s+|was\s+|were\s+)?"
            r"(?:not\s+(?:available|provided|included|working|functional)|unavailable|excluded|not\s+supplied)\b", re.I,
        )
        relevant = lambda text: bool(positive.search(text))
    elif key in {"washer", "clothes_washer", "washing_machine"}:
        positive = re.compile(r"\b(?:clothes\s+washer|washing\s+machine|washer)\b", re.I)
        negative_before = re.compile(
            r"\b(?:no|without|lacks?|not\s+(?:provided|available|included)|unavailable)\s+"
            r"(?:a\s+|any\s+)?(?:clothes\s+washer|washing\s+machine|washer)\b", re.I,
        )
        negative_after = re.compile(
            r"\b(?:clothes\s+washer|washing\s+machine|washer)\b"
            r"(?:\s+\w+){0,3}\s+(?:is\s+|are\s+|was\s+|were\s+)?"
            r"(?:not\s+(?:available|provided|included|working|functional)|unavailable|excluded|not\s+supplied|is\s+not)\b", re.I,
        )
        relevant = lambda text: bool(positive.search(text))
    else:
        raise ValueError(f"Unsupported amenity: {amenity}")

    positives: List[Dict[str, str]] = []
    negatives: List[Dict[str, str]] = []
    amenity_sources = _strings_from_collection(row.get("amenities"), "amenities")
    # These strings are nested and explicit, so `available: false` can resolve absence.
    for text, source in amenity_sources:
        available = None
        if source.startswith("amenities"):
            # Source normally ends in .title/.name; availability belongs to the
            # containing amenity value object.
            parent_path = source.rsplit(".", 1)[0] if "." in source else source
            value = _at_index(row.get("amenities"), parent_path, "amenities")
            if isinstance(value, dict):
                available = value.get("available")
        if not relevant(text):
            continue
        if available is False or str(available).strip().lower() in {"false", "0", "no"}:
            negatives.append(_evidence(source + ".available", text))
        else:
            for clause in _clauses(text):
                if not relevant(clause):
                    continue
                if negative_before.search(clause) or negative_after.search(clause):
                    negatives.append(_evidence(source, clause))
                else:
                    positives.append(_evidence(source, clause))

    # Descriptive text is useful when explicit, but never infer from silence.
    for text, source in _text_sources(row):
        if source.startswith("amenities") or source.startswith("images"):
            continue
        for clause in _clauses(text):
            if not relevant(clause):
                continue
            if negative_before.search(clause) or negative_after.search(clause):
                negatives.append(_evidence(source, clause))
            else:
                positives.append(_evidence(source, clause))

    if positives and negatives:
        return _result("unknown", positives + negatives, conflicts=True)
    if negatives:
        return _result("absent", negatives)
    if positives:
        return _result("present", positives)
    return _result("unknown", [])


def _at_index(root: Any, source: str, field: str) -> Any:
    """Resolve the list item source path emitted by ``_strings_from_collection``."""
    if not source.startswith(field):
        return None
    current = _parse_collection(root)
    for part in re.findall(r"\[(\d+)\]|\.([A-Za-z_]+)", source[len(field):]):
        index, key = part
        if index:
            if not isinstance(current, (list, tuple)) or int(index) >= len(current):
                return None
            current = current[int(index)]
        elif key:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
    return current


def extract_bathroom_evidence(row: Any) -> Dict[str, Any]:
    """Classify bathroom access only from explicit bathroom-specific evidence."""
    shared_pattern = re.compile(
        r"\b(?:shared|communal)\s+(?:(?:full|half|private|external|outside)\s+)?bath(?:room)?\b"
        r"|\bbath(?:room)?\s+(?:is\s+)?shared\b", re.I,
    )
    ensuite_pattern = re.compile(r"\b(?:en[- ]?suite|ensuite|attached\s+(?:private\s+)?bath(?:room)?|bath(?:room)?\s+inside\s+(?:the\s+)?bedroom)\b", re.I)
    external_pattern = re.compile(r"\b(?:private|dedicated|exclusive)\s+(?:external\s+|outside\s+)?(?:full\s+|half\s+)?bath(?:room)?\b|\bexclusive\s+use\s+of\s+(?:the\s+)?bath(?:room)?\b", re.I)
    negative_pattern = re.compile(r"\b(?:not|no|without|never)\s+(?:a\s+|an\s+|the\s+)?(?:private|dedicated|exclusive|en[- ]?suite|ensuite)\b", re.I)
    negative_shared_pattern = re.compile(r"\b(?:not|no|never)\s+(?:a\s+|an\s+|the\s+)?shared\s+bath(?:room)?\b|\bbath(?:room)?\s+(?:is\s+)?not\s+shared\b", re.I)

    evidence: Dict[str, List[Dict[str, str]]] = {
        "ensuite_exclusive": [], "external_exclusive": [], "shared": [],
    }
    for text, source in _text_sources(row, include_reviews=True):
        for clause in _clauses(text):
            # A denial of private access is not proof of shared access.
            if negative_pattern.search(clause):
                continue
            if shared_pattern.search(clause) and not negative_shared_pattern.search(clause):
                evidence["shared"].append(_evidence(source, clause))
            if ensuite_pattern.search(clause):
                evidence["ensuite_exclusive"].append(_evidence(source, clause))
            elif external_pattern.search(clause):
                evidence["external_exclusive"].append(_evidence(source, clause))

    populated = [state for state, items in evidence.items() if items]
    if len(populated) > 1:
        return _result("conflict", [item for group in evidence.values() for item in group], conflicts=True)
    if not populated:
        return _result("unknown", [])
    state = populated[0]
    return _result(state, evidence[state])


def extract_bathroom_privacy(row: Any) -> str:
    """Compatibility label for the original UI and CSV schema."""
    state = extract_bathroom_evidence(row)["state"]
    if state in {"ensuite_exclusive", "external_exclusive"}:
        return "Private / Dedicated"
    if state == "shared":
        return "Shared"
    if state == "conflict":
        return "Conflicting Evidence"
    return "Undetermined"


def has_air_conditioning(row: Any) -> bool:
    """Legacy boolean: True only when AC is explicitly confirmed present."""
    return extract_amenity_evidence(row, "air_conditioning")["state"] == "present"


def has_clothes_washer(row: Any) -> bool:
    """Legacy boolean: True only when a clothes washer is explicitly confirmed."""
    return extract_amenity_evidence(row, "washer")["state"] == "present"


def extract_accommodation_type_evidence(row: Any) -> Dict[str, Any]:
    """Classify accommodation from Airbnb's explicit room_type field only."""
    raw = _row_get(row, "room_type")
    if _is_missing(raw) or not isinstance(raw, str) or not raw.strip():
        return _fact("Unknown", [], value="Unknown")
    normalized = re.sub(r"\s+", " ", raw.strip()).casefold()
    aliases = {
        "entire home/apt": "Entire home/apt",
        "entire home": "Entire home/apt",
        "entire apartment": "Entire home/apt",
        "entire place": "Entire home/apt",
        "private room": "Private room",
        "shared room": "Shared room",
        "hotel room": "Hotel room",
    }
    state = aliases.get(normalized, "Unknown")
    return _fact(state, [_evidence("room_type", raw)], value=state)


def _parse_explicit_count(value: Any, kind: str) -> Any:
    """Parse a whole numeric count; an explicit Studio means zero bedrooms."""
    if isinstance(value, bool) or _is_missing(value):
        return None
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return int(number) if number >= 0 and number.is_integer() else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if kind == "bedroom" and re.fullmatch(r"studio(?:\s+apartment)?", text, re.I):
        return 0
    suffix = r"(?:\s*bedrooms?)?" if kind == "bedroom" else r"(?:\s*beds?)?"
    match = re.fullmatch(rf"(\d+)(?:\.0+)?{suffix}", text, re.I)
    return int(match.group(1)) if match else None


def _bedinfo_items(row: Any) -> List[Tuple[str, str]]:
    """Read and deduplicate BEDINFO bodies from Airbnb overview structures."""
    found: List[Tuple[str, str]] = []

    def visit(value: Any, path: str) -> None:
        parsed = _parse_collection(value) if isinstance(value, str) else value
        if isinstance(parsed, dict):
            for key in ("primaryLine", "mapPrimaryLine"):
                child = parsed.get(key)
                if isinstance(child, (list, tuple, dict)):
                    visit(child, f"{path}.{key}")
            body, kind = parsed.get("body"), parsed.get("type")
            if isinstance(body, str) and str(kind or "").strip().casefold() == "bedinfo" and body.strip():
                found.append((body.strip(), f"{path}.body"))
            for key, child in parsed.items():
                if key not in {"primaryLine", "mapPrimaryLine", "body"} and isinstance(child, (dict, list, tuple)):
                    visit(child, f"{path}.{key}")
        elif isinstance(parsed, (list, tuple)):
            for index, child in enumerate(parsed):
                visit(child, f"{path}[{index}]")

    for field in ("structuredContent", "structured_content", "primaryLine", "mapPrimaryLine"):
        value = _row_get(row, field)
        if not _is_missing(value):
            visit(value, field)

    unique: List[Tuple[str, str]] = []
    seen = set()
    for text, source in found:
        key = re.sub(r"\s+", " ", text.strip()).casefold()
        if key not in seen:
            seen.add(key)
            unique.append((text, source))
    return unique


def _structured_count_entries(row: Any, kind: str) -> List[Tuple[int, str, str]]:
    entries: List[Tuple[int, str, str]] = []
    for text, source in _bedinfo_items(row):
        if kind == "bedroom":
            if re.fullmatch(r"studio(?:\s+apartment)?", text.strip(), re.I):
                entries.append((0, source, text))
                continue
            match = re.fullmatch(r"(\d+)\s*bedrooms?", text.strip(), re.I)
        else:
            match = re.fullmatch(r"(\d+)\s*.+?\s*beds?", text.strip(), re.I)
            if match is None:
                match = re.fullmatch(r"(\d+)\s*beds?", text.strip(), re.I)
        if match:
            entries.append((int(match.group(1)), source, text))
    return entries


def _extract_count_evidence(row: Any, kind: str) -> Dict[str, Any]:
    field_names = ("bedroom_count", "bedrooms") if kind == "bedroom" else ("bed_count", "beds")
    explicit: List[Tuple[int, str, str]] = []
    invalid: List[Dict[str, str]] = []
    for field in field_names:
        raw = _row_get(row, field)
        if _is_missing(raw):
            continue
        parsed = _parse_explicit_count(raw, kind)
        if parsed is None:
            invalid.append(_evidence(field, f"Unparsed explicit count: {raw}"))
        else:
            explicit.append((parsed, field, str(raw)))

    structured = _structured_count_entries(row, kind)
    if kind == "bed" and structured:
        # Some listings provide a total (for example, "3 beds") alongside
        # typed components (for example, "2 double beds" and "1 sofa bed").
        # Prefer the aggregate and verify it against the component breakdown.
        aggregate = [
            value for value, _, text in structured
            if re.fullmatch(r"\d+\s*beds?", text.strip(), re.I)
        ]
        components = [
            value for value, _, text in structured
            if not re.fullmatch(r"\d+\s*beds?", text.strip(), re.I)
        ]
        aggregate_values = list(dict.fromkeys(aggregate))
        if len(aggregate_values) > 1:
            structured_values = aggregate_values
        elif aggregate_values:
            total = aggregate_values[0]
            structured_values = [total]
            if components and sum(components) != total:
                structured_values.append(sum(components))
        else:
            # Mirrored primaryLine/mapPrimaryLine arrays were deduplicated by
            # body text, so distinct typed bodies are independent bed groups.
            structured_values = [sum(value for value, _, _ in structured)]
    else:
        structured_values = list(dict.fromkeys(value for value, _, _ in structured))

    explicit_values = list(dict.fromkeys(value for value, _, _ in explicit))
    evidence = [
        _evidence(source, f"{text} ({value} {kind}{'s' if value != 1 else ''})")
        for value, source, text in explicit
    ]
    evidence.extend(_evidence(source, text) for _, source, text in structured)
    evidence.extend(invalid)

    if len(explicit_values) > 1:
        return _fact("conflict", evidence, value=None, conflict=True)
    if len(structured_values) > 1:
        return _fact("conflict", evidence, value=None, conflict=True)
    if explicit_values:
        explicit_value = explicit_values[0]
        if structured_values and structured_values[0] != explicit_value:
            return _fact("conflict", evidence, value=None, conflict=True)
        return _fact("known", evidence, value=explicit_value)
    if structured_values:
        return _fact("known", evidence, value=structured_values[0])
    return _fact("unknown", evidence, value=None)


def extract_bed_count_evidence(row: Any) -> Dict[str, Any]:
    """Count explicitly listed beds, including sofa beds, without guessing."""
    return _extract_count_evidence(row, "bed")


def extract_bedroom_count_evidence(row: Any) -> Dict[str, Any]:
    """Count explicit bedrooms; titles and room type do not establish the count."""
    return _extract_count_evidence(row, "bedroom")


def _cancellation_context_matches(row: Any, search_context: Any) -> bool:
    """Require the provider-filter match to belong to the active saved trip."""
    if not isinstance(search_context, dict) or not _is_explicit_true(search_context.get("free_cancellation")):
        return False
    expected = {
        "cancellation_search_check_in": search_context.get("check_in"),
        "cancellation_search_check_out": search_context.get("check_out"),
        "cancellation_search_currency": search_context.get("currency"),
        "cancellation_search_trip_revision": search_context.get("trip_revision"),
    }
    if any(_is_missing(value) for value in expected.values()):
        return False
    for field, wanted in expected.items():
        actual = _row_get(row, field)
        if _is_missing(actual):
            return False
        if field == "cancellation_search_currency":
            if str(actual).strip().upper() != str(wanted).strip().upper():
                return False
        elif field == "cancellation_search_trip_revision":
            try:
                if int(float(actual)) != int(float(wanted)):
                    return False
            except (TypeError, ValueError, OverflowError):
                return False
        elif str(actual).strip() != str(wanted).strip():
            return False
    return _is_explicit_true(_row_get(row, "free_cancellation_search_match"))


def _cancellation_policy_sources(row: Any) -> List[Tuple[str, str]]:
    fields = (
        "cancellation_policy",
        "cancellation_policy_text",
        "cancellation_terms",
        "refund_policy",
        "refund_policy_text",
    )
    sources: List[Tuple[str, str]] = []
    for field in fields:
        value = _row_get(row, field)
        if isinstance(value, str) and value.strip():
            sources.append((value.strip(), field))
        elif isinstance(value, (dict, list, tuple)):
            sources.extend(_strings_from_collection(value, field))

    structured = _parse_collection(_row_get(row, "structuredContent", _row_get(row, "structured_content")))
    policy_keys = {"cancellationpolicy", "cancellationpolicytext", "refundpolicy", "cancellationterms"}

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = re.sub(r"[^a-z]", "", str(key).casefold())
                next_path = f"{path}.{key}"
                if normalized in policy_keys:
                    if isinstance(child, str) and child.strip():
                        sources.append((child.strip(), next_path))
                    elif isinstance(child, dict):
                        for subkey, subvalue in child.items():
                            if isinstance(subvalue, str) and subvalue.strip():
                                sources.append((subvalue.strip(), f"{next_path}.{subkey}"))
                elif isinstance(child, (dict, list, tuple)):
                    walk(child, next_path)
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(structured, "structuredContent")
    unique: List[Tuple[str, str]] = []
    seen = set()
    for text, source in sources:
        marker = (re.sub(r"\s+", " ", text).casefold(), source)
        if marker not in seen:
            seen.add(marker)
            unique.append((text, source))
    return unique


def _policy_text_state(text: str) -> Any:
    normalized = text.casefold()
    nonrefundable = re.search(
        r"\bnon[\s-]?refundable\b|\bno\s+refunds?\b|\b(?:not|never)\s+refundable\b",
        normalized,
    )
    negative_free = re.search(
        r"\bno\s+free\s+cancell?ation\b|"
        r"\bfree\s+cancell?ation\b(?:\s+(?:is|was|will\s+be))?\s+"
        r"(?:not\s+(?:available|offered|provided|included)|unavailable|excluded)\b",
        normalized,
    )
    free = re.search(r"\bfree\s+cancell?ation\b|\bcancell?ation\s+for\s+free\b", normalized)
    if negative_free:
        free = None
    conditional = re.search(
        r"\bpartial\s+refund\b|\b(?:full\s+)?refund(?:able)?\s+(?:until|before|by|within|after)\b|"
        r"\bfree\s+cancell?ation\b.{0,100}\b(?:until|before|by|within)\b|"
        r"\bcancell?ation\s+fee\b|\brefundable\s+until\b",
        normalized,
    )
    if nonrefundable and (free or conditional):
        return "conflict"
    if nonrefundable:
        return "nonrefundable"
    if conditional:
        return "conditional"
    if negative_free:
        return "not_free"
    if free:
        return "free"
    return None


def extract_cancellation_evidence(row: Any, search_context: Any = None) -> Dict[str, Any]:
    """Classify explicit cancellation terms or a context-matched provider filter.

    Silence, review mentions, and generic labels such as "Flexible" do not
    establish cancellation terms. A false free-cancellation boolean proves
    only that cancellation is not free, represented by the not_free state.
    """
    claims: List[Tuple[str, Dict[str, str]]] = []
    boolean_value = _row_get(row, "free_cancellation")
    if _is_explicit_true(boolean_value):
        claims.append(("free", _evidence("free_cancellation", str(boolean_value))))
    elif _is_explicit_false(boolean_value):
        claims.append(("not_free", _evidence("free_cancellation", str(boolean_value))))

    for text, source in _cancellation_policy_sources(row):
        state = _policy_text_state(text)
        if state:
            claims.append((state, _evidence(source, text)))

    if _cancellation_context_matches(row, search_context):
        context_text = (
            "Provider free-cancellation search matched; this source does not specify the cancellation deadline."
        )
        claims.append(("free", _evidence("provider_filter.free_cancellation_search_match", context_text)))

    if not claims:
        return _fact("unknown", [])
    states = {state for state, _ in claims}
    evidence = [item for _, item in claims]
    if "conflict" in states or ("free" in states and states.intersection({"not_free", "nonrefundable"})):
        return _fact("unknown", evidence, conflict=True)
    if "nonrefundable" in states:
        if "conditional" in states:
            return _fact("unknown", evidence, conflict=True)
        return _fact("nonrefundable", evidence)
    if "conditional" in states:
        return _fact("conditional", evidence)
    if "free" in states:
        return _fact("free", evidence)
    return _fact("not_free", evidence)


def enrich_properties_dataframe(df: pd.DataFrame, *, search_context: Any = None) -> pd.DataFrame:
    """Add evidence states while preserving legacy compatibility columns."""
    enriched = df.copy()
    if enriched.empty:
        for column in (
            "bathroom_privacy", "bathroom_evidence", "ac_evidence", "washer_evidence",
            "accommodation_type", "accommodation_type_evidence", "bed_count", "bed_count_evidence",
            "bedroom_count", "bedroom_count_evidence", "cancellation_state", "cancellation_evidence",
        ):
            enriched[column] = pd.Series(dtype="object")
        for column in ("has_ac", "has_washer"):
            enriched[column] = pd.Series(dtype="bool")
        return enriched

    bath_facts = enriched.apply(extract_bathroom_evidence, axis=1)
    ac_facts = enriched.apply(lambda row: extract_amenity_evidence(row, "air_conditioning"), axis=1)
    washer_facts = enriched.apply(lambda row: extract_amenity_evidence(row, "washer"), axis=1)
    accommodation_facts = enriched.apply(extract_accommodation_type_evidence, axis=1)
    bed_facts = enriched.apply(extract_bed_count_evidence, axis=1)
    bedroom_facts = enriched.apply(extract_bedroom_count_evidence, axis=1)
    cancellation_facts = enriched.apply(lambda row: extract_cancellation_evidence(row, search_context), axis=1)
    enriched["bathroom_evidence"] = bath_facts
    enriched["ac_evidence"] = ac_facts
    enriched["washer_evidence"] = washer_facts
    enriched["accommodation_type_evidence"] = accommodation_facts
    enriched["bed_count_evidence"] = bed_facts
    enriched["bedroom_count_evidence"] = bedroom_facts
    enriched["cancellation_evidence"] = cancellation_facts
    enriched["bathroom_privacy"] = bath_facts.map(
        lambda fact: "Private / Dedicated" if fact["state"] in {"ensuite_exclusive", "external_exclusive"}
        else "Shared" if fact["state"] == "shared"
        else "Conflicting Evidence" if fact["state"] == "conflict"
        else "Undetermined"
    )
    enriched["ac_state"] = ac_facts.map(lambda fact: fact["state"])
    enriched["washer_state"] = washer_facts.map(lambda fact: fact["state"])
    enriched["has_ac"] = enriched["ac_state"].eq("present")
    enriched["has_washer"] = enriched["washer_state"].eq("present")
    enriched["accommodation_type"] = accommodation_facts.map(lambda fact: fact["state"])
    enriched["bed_count"] = bed_facts.map(lambda fact: fact["value"])
    enriched["bedroom_count"] = bedroom_facts.map(lambda fact: fact["value"])
    enriched["cancellation_state"] = cancellation_facts.map(lambda fact: fact["state"])
    return enriched

