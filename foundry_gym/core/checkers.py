"""Deterministic scoring primitives shared by all environment verifiers.

These are the "minimal inline checkers" the sprint plan expects `veredict`
(prompt 7) to later consolidate/supersede. Keep them dependency-free (stdlib
only) and pure.

Anti-reward-hacking notes (see docs/verifier-audits.md for the full audit):
- ``canonical`` enforces *exact* builtin types via ``type(x) is T`` so candidate
  objects with permissive ``__eq__``/``__len__`` cannot spoof comparisons.
- ``extract_json_response`` refuses ambiguous outputs (multiple top-level JSON
  candidates outside fences) instead of "helpfully" picking one, so a policy
  cannot shotgun many candidate answers and get credit for the best.
- ``extract_final_answer`` takes only the *last* well-formed answer marker and
  requires a single value inside it.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Optional, Tuple

# ---------------------------------------------------------------------------
# canonicalization — the __eq__-spoofing defense
# ---------------------------------------------------------------------------

_SCALARS = (bool, int, float, str, type(None))


class CanonicalError(ValueError):
    """Raised when a value cannot be canonicalized to exact builtin types."""


def canonical(value: Any, _depth: int = 0) -> Any:
    """Recursively convert to exact builtin types, rejecting impostors.

    Uses ``type(x) is T`` (not isinstance) so subclasses — the classic vector
    for overriding __eq__ to always return True — are rejected. bool is checked
    before int (bool is an int subclass but a *distinct* exact type here).
    Tuples normalize to lists. Dict keys must be str. Floats that are NaN are
    rejected (NaN breaks equality semantics). Depth capped to stop
    billion-laughs style recursion.
    """
    if _depth > 32:
        raise CanonicalError("value too deeply nested")
    t = type(value)
    if t is bool or t is str or t is type(None):
        return value
    if t is int:
        return value
    if t is float:
        if math.isnan(value):
            raise CanonicalError("NaN is not comparable")
        return value
    if t is list or t is tuple:
        return [canonical(v, _depth + 1) for v in value]
    if t is dict:
        out = {}
        for k, v in value.items():
            if type(k) is not str:
                raise CanonicalError(f"non-str dict key: {k!r}")
            out[k] = canonical(v, _depth + 1)
        return out
    raise CanonicalError(f"unsupported type: {t.__name__}")


def canonical_equal(a: Any, b: Any, rel_tol: float = 0.0, abs_tol: float = 0.0) -> bool:
    """Deep equality on canonicalized values; optional numeric tolerance.

    Returns False (never raises) if either side fails canonicalization.
    bool and int/float are never considered equal to each other
    (True == 1 spoofing is rejected).
    """
    try:
        ca, cb = canonical(a), canonical(b)
    except CanonicalError:
        return False
    return _ceq(ca, cb, rel_tol, abs_tol)


def _ceq(a: Any, b: Any, rel_tol: float, abs_tol: float) -> bool:
    ta, tb = type(a), type(b)
    # bool must match bool exactly; never cross-compare with numerics.
    if (ta is bool) != (tb is bool):
        return False
    if ta is bool:
        return a is b
    if ta in (int, float) and tb in (int, float):
        if rel_tol or abs_tol:
            return math.isclose(float(a), float(b), rel_tol=rel_tol, abs_tol=abs_tol)
        return a == b and (ta is tb or float(a) == float(b))
    if ta is not tb:
        return False
    if ta is list:
        return len(a) == len(b) and all(
            _ceq(x, y, rel_tol, abs_tol) for x, y in zip(a, b)
        )
    if ta is dict:
        return a.keys() == b.keys() and all(
            _ceq(a[k], b[k], rel_tol, abs_tol) for k in a
        )
    return a == b


# ---------------------------------------------------------------------------
# JSON extraction — strict, ambiguity-refusing
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


def extract_json_response(text: str, max_len: int = 200_000) -> Tuple[Optional[Any], str]:
    """Extract exactly one JSON value from a model response.

    Policy (in order):
      1. If one or more fenced code blocks parse as JSON, use the LAST parsing
         one (models restate their final answer last).
      2. Else, if the whole stripped text parses as JSON, use it.
      3. Else, scan for balanced top-level JSON objects/arrays; if EXACTLY one
         parses, use it. If several parse, refuse: ambiguous output earns
         nothing (anti answer-shotgunning).

    Returns (value, "ok") or (None, reason).
    """
    if not isinstance(text, str):
        return None, "response is not text"
    if len(text) > max_len:
        return None, "response too long"
    fenced = _FENCE_RE.findall(text)
    parsed_fenced = []
    for block in fenced:
        try:
            parsed_fenced.append(json.loads(block.strip()))
        except (json.JSONDecodeError, ValueError):
            continue
    if parsed_fenced:
        return parsed_fenced[-1], "ok"
    stripped = text.strip()
    if stripped:
        try:
            return json.loads(stripped), "ok"
        except (json.JSONDecodeError, ValueError):
            pass
    candidates = _scan_balanced_json(text)
    if len(candidates) == 1:
        return candidates[0], "ok"
    if len(candidates) > 1:
        return None, f"ambiguous: {len(candidates)} top-level JSON values found"
    return None, "no JSON value found"


def _scan_balanced_json(text: str, limit: int = 8) -> list:
    """Find top-level balanced {...}/[...] spans that parse as JSON."""
    out = []
    i, n = 0, len(text)
    while i < n and len(out) <= limit:
        c = text[i]
        if c in "{[":
            close = "}" if c == "{" else "]"
            depth = 0
            in_str = False
            esc = False
            j = i
            while j < n:
                ch = text[j]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                elif ch == '"':
                    in_str = True
                elif ch in "{[":
                    depth += 1
                elif ch in "]}":
                    depth -= 1
                    if depth == 0:
                        if ch == close:
                            span = text[i : j + 1]
                            try:
                                out.append(json.loads(span))
                            except (json.JSONDecodeError, ValueError):
                                pass
                        i = j
                        break
                j += 1
        i += 1
    return out


# ---------------------------------------------------------------------------
# minimal JSON-schema-ish validation (subset; stdlib only)
# ---------------------------------------------------------------------------

def schema_check(value: Any, schema: dict, path: str = "$") -> list:
    """Validate ``value`` against a minimal schema dialect.

    Supported keys: type (object/array/string/number/integer/boolean/null),
    required, properties, items, enum, additionalProperties (bool),
    minItems/maxItems, pattern (for strings).
    Returns a list of error strings; empty == valid.
    """
    errors: list = []
    stype = schema.get("type")
    if stype:
        ok = {
            "object": lambda v: type(v) is dict,
            "array": lambda v: type(v) is list,
            "string": lambda v: type(v) is str,
            "number": lambda v: type(v) in (int, float) and type(v) is not bool,
            "integer": lambda v: type(v) is int,
            "boolean": lambda v: type(v) is bool,
            "null": lambda v: v is None,
        }.get(stype)
        if ok is None:
            errors.append(f"{path}: unknown schema type {stype!r}")
            return errors
        if not ok(value):
            errors.append(f"{path}: expected {stype}, got {type(value).__name__}")
            return errors
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in enum")
    if stype == "object":
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"{path}.{req}: required property missing")
        for k, v in value.items():
            if k in props:
                errors.extend(schema_check(v, props[k], f"{path}.{k}"))
            elif schema.get("additionalProperties", True) is False:
                errors.append(f"{path}.{k}: additional property not allowed")
    if stype == "array":
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(value):
                errors.extend(schema_check(item, item_schema, f"{path}[{i}]"))
    if stype == "string" and "pattern" in schema:
        if not re.search(schema["pattern"], value):
            errors.append(f"{path}: does not match pattern")
    return errors


# ---------------------------------------------------------------------------
# final-answer extraction (math_logic and friends)
# ---------------------------------------------------------------------------

_ANSWER_LINE_RE = re.compile(r"(?im)^\s*answer\s*:\s*(.+?)\s*$")
_BOXED_RE = re.compile(r"\\boxed\{([^{}]*)\}")


def extract_final_answer(text: str, max_len: int = 100_000) -> Tuple[Optional[str], str]:
    """Extract the model's single final answer token/expression.

    Takes the LAST `Answer: ...` line, or the LAST \\boxed{...} if no answer
    line exists. The captured value must be a single whitespace-free token or a
    short expression without further 'Answer:' markers — a value that itself
    contains multiple comma/space-separated alternatives is rejected unless the
    task expects a tuple (caller handles that by parsing the returned string).

    Returns (answer_string, "ok") or (None, reason).
    """
    if not isinstance(text, str):
        return None, "response is not text"
    if len(text) > max_len:
        return None, "response too long"
    matches = _ANSWER_LINE_RE.findall(text)
    if matches:
        return matches[-1].strip(), "ok"
    boxed = _BOXED_RE.findall(text)
    if boxed:
        return boxed[-1].strip(), "ok"
    return None, "no final answer marker (expected 'Answer: <value>')"


def parse_number(token: str) -> Optional[float]:
    """Parse a numeric token strictly. Accepts ints, floats, simple fractions
    'a/b', comma thousand-separators. Returns None if not purely numeric."""
    if not isinstance(token, str):
        return None
    t = token.strip().rstrip(".")
    t = t.replace(",", "") if re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", t.strip()) else t
    frac = re.fullmatch(r"(-?\d+)\s*/\s*(\d+)", t)
    if frac:
        num, den = int(frac.group(1)), int(frac.group(2))
        if den == 0:
            return None
        return num / den
    if re.fullmatch(r"[+-]?\d+", t):
        return float(int(t))
    if re.fullmatch(r"[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?", t):
        try:
            v = float(t)
        except ValueError:
            return None
        return None if math.isnan(v) or math.isinf(v) else v
    return None


def numbers_equal(a: float, b: float, rel_tol: float = 1e-6, abs_tol: float = 1e-9) -> bool:
    return math.isclose(a, b, rel_tol=rel_tol, abs_tol=abs_tol)


# ---------------------------------------------------------------------------
# scalar normalization for extraction scoring
# ---------------------------------------------------------------------------

_DATE_PATTERNS = [
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})$"), lambda m: (m[1], m[2], m[3])),
    (re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$"), lambda m: (m[3], m[1].zfill(2), m[2].zfill(2))),
    (re.compile(r"^(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})$"), None),  # handled below
    (re.compile(r"^([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})$"), None),  # handled below
]

_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ["january", "february", "march", "april", "may", "june",
         "july", "august", "september", "october", "november", "december"]
    )
}


def normalize_date(s: str) -> Optional[str]:
    """Normalize common date renderings to ISO YYYY-MM-DD; None if unparseable."""
    if not isinstance(s, str):
        return None
    t = s.strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", t)
    if m:  # ambiguous mm/dd vs dd/mm: our generators only emit mm/dd/yyyy
        return f"{m.group(3)}-{m.group(1).zfill(2)}-{m.group(2).zfill(2)}"
    m = re.fullmatch(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", t)
    if m:
        mon = _month_num(m.group(1))
        if mon:
            return f"{m.group(3)}-{str(mon).zfill(2)}-{m.group(2).zfill(2)}"
    m = re.fullmatch(r"(\d{1,2})\.?\s+([A-Za-z]+)\.?\s+(\d{4})", t)
    if m:
        mon = _month_num(m.group(2))
        if mon:
            return f"{m.group(3)}-{str(mon).zfill(2)}-{m.group(1).zfill(2)}"
    return None


def _month_num(name: str) -> Optional[int]:
    n = name.strip().lower()
    if n in _MONTHS:
        return _MONTHS[n]
    for full, i in _MONTHS.items():
        if full.startswith(n) and len(n) >= 3:
            return i
    return None


def normalize_money(s: Any) -> Optional[float]:
    """'$1,234.50' / '1234.5 USD' / 1234.5 -> 1234.5. None if unparseable."""
    if type(s) in (int, float) and type(s) is not bool:
        return round(float(s), 2)
    if not isinstance(s, str):
        return None
    t = re.sub(r"[^\d.,\-]", "", s.strip())
    t = t.replace(",", "")
    try:
        return round(float(t), 2)
    except ValueError:
        return None


def normalize_text(s: Any) -> Optional[str]:
    if not isinstance(s, str):
        return None
    return re.sub(r"\s+", " ", s).strip().casefold()
