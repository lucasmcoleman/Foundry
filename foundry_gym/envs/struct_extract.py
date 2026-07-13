"""struct_extract — structured extraction from synthetic documents rendered
from known ground truth. Three document types (invoice, meeting_minutes,
log_summary): the generator builds a ground-truth object FIRST, renders a
prose document from it (with formatting noise and distractor fields that
must NOT be extracted), then derives (a) a JSON schema and (b) a flat field
spec (leaf paths + expected values + comparison types) that the response is
scored against.

``verify()`` is fully generic — it never branches on document type. It only
(1) schema-gates the response (a hard 0.0 on any violation — additionalProperties
is false everywhere, so extracting a distractor field is itself a failure),
then (2) walks the field spec, resolving each leaf path into the response
and comparing with the appropriate normalizer (money/date/text/int/number/
str_list_unordered).
"""

from __future__ import annotations

import datetime
import json
import re
from typing import Any, Optional

from ..core.env import Environment
from ..core.registry import register
from ..core.types import Task, VerifyResult
from ..core import checkers

# ---------------------------------------------------------------------------
# generic leaf-path resolution (dotted keys + [i] indices)
# ---------------------------------------------------------------------------

class _PathError(ValueError):
    pass


_PATH_FULL_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*|\[[0-9]+\])*$"
)
_PATH_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\[[0-9]+\]")


def _tokenize_path(path: Any) -> list:
    if type(path) is not str or not path or len(path) > 500 or not _PATH_FULL_RE.match(path):
        raise _PathError(f"bad field path syntax: {path!r}")
    tokens = []
    for m in _PATH_TOKEN_RE.finditer(path):
        s = m.group(0)
        if s.startswith("["):
            tokens.append(("idx", int(s[1:-1])))
        else:
            tokens.append(("key", s))
    if len(tokens) > 32:
        raise _PathError("field path too deep")
    return tokens


def _get_path(obj: Any, path: str):
    """Returns (value, found: bool). Never raises on a malformed *obj*."""
    tokens = _tokenize_path(path)
    cur = obj
    for kind, val in tokens:
        if kind == "key":
            if type(cur) is not dict or val not in cur:
                return None, False
            cur = cur[val]
        else:
            if type(cur) is not list or val < 0 or val >= len(cur):
                return None, False
            cur = cur[val]
    return cur, True


def _set_path(obj: Any, path: str, value: Any) -> None:
    """Mutates ``obj`` in place. Only used on our own trusted ground-truth
    copies (paths are always valid for that structure by construction)."""
    tokens = _tokenize_path(path)
    cur = obj
    for kind, val in tokens[:-1]:
        cur = cur[val]
    last_kind, last_val = tokens[-1]
    cur[last_val] = value


# ---------------------------------------------------------------------------
# word banks / constants
# ---------------------------------------------------------------------------

_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

_VENDOR_BANK = [
    "Acme Corp", "Globex LLC", "Initech", "Umbrella Industries", "Stark Systems",
    "Wayne Enterprises", "Soylent Co", "Hooli Inc", "Wonka Industries",
    "Cyberdyne Systems", "Massive Dynamic", "Gringotts Consulting",
]
_ITEM_WORDS = [
    "Widget", "Gadget", "Consulting hours", "Shipping", "Support plan",
    "License seat", "Onboarding", "Maintenance", "Hardware unit",
    "Software module", "Training session", "Custom integration",
]
_TAX_RATES = [0, 5, 6, 7, 7.5, 8, 8.5, 9, 10, 12.5]

_PERSON_NAMES = [
    "Dana Kim", "Robin Chen", "Sam Ortiz", "Priya Nair", "Alex Novak",
    "Jordan Lee", "Casey Brooks", "Morgan Diaz", "Taylor Reyes", "Jamie Singh",
    "Riley Owens", "Quinn Patel", "Harper Cole", "Skyler Ford", "Devon Marsh",
    "Emerson Vale",
]
_DECISION_BANK = [
    "Approved the Q3 budget.",
    "Postponed the vendor migration.",
    "Adopted the new onboarding checklist.",
    "Rejected the office relocation proposal.",
    "Approved hiring two contractors.",
    "Deferred the pricing change to next quarter.",
    "Confirmed the launch date.",
    "Approved the security audit budget.",
]
_TASK_BANK = [
    "draft the proposal", "update the roadmap doc", "schedule the follow-up call",
    "circulate meeting notes", "finalize the budget", "reach out to the vendor",
    "prepare the demo", "review the contract",
]

_SERVICE_BANK = [
    "payments-api", "payments-worker", "auth-service", "auth-worker",
    "checkout-api", "search-api", "billing-worker", "notifications-api",
    "inventory-service", "gateway-api",
]
_LEVEL_MSGS = {
    "INFO": ["request handled", "health check ok", "cache warmed", "started worker", "connection established"],
    "WARN": ["slow response", "retrying request", "deprecated field used", "queue backlog growing"],
    "ERROR": ["request failed", "connection refused", "timeout exceeded", "unhandled exception", "database write failed"],
}


def _rand_date(rng):
    return rng.randint(2024, 2026), rng.randint(1, 12), rng.randint(1, 28)


def _render_date_variety(rng, y: int, m: int, d: int) -> str:
    kind = rng.choice(["iso", "long", "mmdd", "dmy"])
    if kind == "iso":
        return f"{y:04d}-{m:02d}-{d:02d}"
    if kind == "long":
        return f"{_MONTHS[m - 1]} {d}, {y}"
    if kind == "mmdd":
        return f"{m:02d}/{d:02d}/{y:04d}"
    return f"{d} {_MONTHS[m - 1]} {y}"


def _fmt_money(x: float) -> str:
    return f"${x:,.2f}"


# ---------------------------------------------------------------------------
# per-doctype generators (generation-only; verify() never branches on this)
# ---------------------------------------------------------------------------

def _gen_invoice(rng, d: float) -> dict:
    n_items = 2 + round(d * 6)
    vendor = rng.choice(_VENDOR_BANK)
    invoice_number = "INV-" + "".join(rng.choice("0123456789") for _ in range(5))
    y, m, dd = _rand_date(rng)
    date_iso = f"{y:04d}-{m:02d}-{dd:02d}"
    rendered_date = _render_date_variety(rng, y, m, dd)
    po_number = "PO-" + "".join(rng.choice("0123456789") for _ in range(6))
    customer_id = "CUST-" + "".join(rng.choice("0123456789") for _ in range(4))
    previous_balance = round(rng.uniform(0, 1000), 2)

    items = []
    for _ in range(n_items):
        desc = rng.choice(_ITEM_WORDS)
        qty = rng.randint(1, 20)
        unit_price = round(rng.uniform(5, 500), 2)
        line_total = round(qty * unit_price, 2)
        items.append({"description": desc, "qty": qty, "unit_price": unit_price, "line_total": line_total})

    subtotal = round(sum(it["line_total"] for it in items), 2)
    tax_rate = rng.choice(_TAX_RATES)
    total_due = round(subtotal * (1 + tax_rate / 100), 2)

    ground_truth = {
        "vendor_name": vendor,
        "invoice_number": invoice_number,
        "invoice_date": date_iso,
        "line_items": items,
        "subtotal": subtotal,
        "tax_rate": tax_rate,
        "total_due": total_due,
    }
    field_spec = [
        {"path": "vendor_name", "type": "text", "expected": vendor, "weight": 1.0},
        {"path": "invoice_number", "type": "text", "expected": invoice_number, "weight": 1.0},
        {"path": "invoice_date", "type": "date", "expected": date_iso, "weight": 1.0},
    ]
    for i, it in enumerate(items):
        field_spec.append({"path": f"line_items[{i}].description", "type": "text", "expected": it["description"], "weight": 1.0})
        field_spec.append({"path": f"line_items[{i}].qty", "type": "int", "expected": it["qty"], "weight": 1.0})
        field_spec.append({"path": f"line_items[{i}].unit_price", "type": "money", "expected": it["unit_price"], "weight": 1.0})
        field_spec.append({"path": f"line_items[{i}].line_total", "type": "money", "expected": it["line_total"], "weight": 1.0})
    field_spec += [
        {"path": "subtotal", "type": "money", "expected": subtotal, "weight": 1.0},
        {"path": "tax_rate", "type": "number", "expected": tax_rate, "weight": 1.0},
        {"path": "total_due", "type": "money", "expected": total_due, "weight": 1.0},
    ]

    item_schema = {
        "type": "object",
        "properties": {
            "description": {"type": "string"},
            "qty": {"type": "integer"},
            "unit_price": {"type": "number"},
            "line_total": {"type": "number"},
        },
        "required": ["description", "qty", "unit_price", "line_total"],
        "additionalProperties": False,
    }
    schema = {
        "type": "object",
        "properties": {
            "vendor_name": {"type": "string"},
            "invoice_number": {"type": "string"},
            "invoice_date": {"type": "string"},
            "line_items": {"type": "array", "items": item_schema},
            "subtotal": {"type": "number"},
            "tax_rate": {"type": "number"},
            "total_due": {"type": "number"},
        },
        "required": ["vendor_name", "invoice_number", "invoice_date", "line_items", "subtotal", "tax_rate", "total_due"],
        "additionalProperties": False,
    }

    lines = [
        "INVOICE", f"Vendor: {vendor}", f"Invoice #: {invoice_number}",
        f"Date: {rendered_date}", f"PO Number: {po_number}", f"Customer ID: {customer_id}",
        "", "Line items:",
    ]
    for i, it in enumerate(items, 1):
        if d < 0.6:
            lines.append(
                f"  {i}. {it['description']} - qty {it['qty']} @ "
                f"{_fmt_money(it['unit_price'])} = {_fmt_money(it['line_total'])}"
            )
        else:
            lines.append(f"  {i}. {it['description']} - qty {it['qty']} @ {_fmt_money(it['unit_price'])}")
    lines += [
        "", f"Subtotal: {_fmt_money(subtotal)}", f"Tax rate: {tax_rate}%",
        f"Total due: {_fmt_money(total_due)}",
        f"Previous balance (unrelated to this invoice): {_fmt_money(previous_balance)}",
    ]
    return {
        "ground_truth": ground_truth, "field_spec": field_spec, "schema": schema,
        "document_text": "\n".join(lines), "doc_type": "invoice",
    }


def _gen_meeting_minutes(rng, d: float) -> dict:
    n_att = 3 + round(d * 5)
    n_dec = 2 + round(d * 3)
    n_ai = 1 + round(d * 3)
    n_apol = rng.randint(0, 2)

    y, m, dd = _rand_date(rng)
    meeting_date_iso = f"{y:04d}-{m:02d}-{dd:02d}"
    rendered_meeting_date = _render_date_variety(rng, y, m, dd)

    picks = rng.sample(_PERSON_NAMES, n_att + n_apol)
    attendees = picks[:n_att]
    apologies = picks[n_att:]

    decisions = rng.sample(_DECISION_BANK, n_dec)

    base_date = datetime.date(y, m, dd)
    action_items = []
    for _ in range(n_ai):
        owner = rng.choice(attendees)
        task = rng.choice(_TASK_BANK)
        offset = rng.randint(1, 20)
        due = base_date + datetime.timedelta(days=offset)
        due_iso = due.isoformat()
        action_items.append({"owner": owner, "task": task, "due_date": due_iso, "_due_obj": due})

    next_meeting = base_date + datetime.timedelta(days=rng.randint(20, 40))

    ground_truth = {
        "meeting_date": meeting_date_iso,
        "attendees": attendees,
        "decisions": decisions,
        "action_items": [
            {"owner": ai["owner"], "task": ai["task"], "due_date": ai["due_date"]}
            for ai in action_items
        ],
    }
    field_spec = [
        {"path": "meeting_date", "type": "date", "expected": meeting_date_iso, "weight": 1.0},
        {"path": "attendees", "type": "str_list_unordered", "expected": list(attendees), "weight": 1.0},
    ]
    for i, dec in enumerate(decisions):
        field_spec.append({"path": f"decisions[{i}]", "type": "text", "expected": dec, "weight": 1.0})
    for i, ai in enumerate(action_items):
        field_spec.append({"path": f"action_items[{i}].owner", "type": "text", "expected": ai["owner"], "weight": 1.0})
        field_spec.append({"path": f"action_items[{i}].task", "type": "text", "expected": ai["task"], "weight": 1.0})
        field_spec.append({"path": f"action_items[{i}].due_date", "type": "date", "expected": ai["due_date"], "weight": 1.0})

    ai_schema = {
        "type": "object",
        "properties": {"owner": {"type": "string"}, "task": {"type": "string"}, "due_date": {"type": "string"}},
        "required": ["owner", "task", "due_date"],
        "additionalProperties": False,
    }
    schema = {
        "type": "object",
        "properties": {
            "meeting_date": {"type": "string"},
            "attendees": {"type": "array", "items": {"type": "string"}},
            "decisions": {"type": "array", "items": {"type": "string"}},
            "action_items": {"type": "array", "items": ai_schema},
        },
        "required": ["meeting_date", "attendees", "decisions", "action_items"],
        "additionalProperties": False,
    }

    lines = ["MEETING MINUTES", f"Date: {rendered_meeting_date}", f"Attendees: {', '.join(attendees)}"]
    if apologies:
        lines.append(f"Apologies/Absent: {', '.join(apologies)}")
    lines.append("")
    lines.append("Decisions:")
    for i, dec in enumerate(decisions, 1):
        lines.append(f"  {i}. {dec}")
    lines.append("")
    lines.append("Action items:")
    for ai in action_items:
        due_rendered = _render_date_variety(rng, ai["_due_obj"].year, ai["_due_obj"].month, ai["_due_obj"].day)
        lines.append(f"  - {ai['owner']}: {ai['task']} (due {due_rendered})")
    lines.append("")
    nm_rendered = _render_date_variety(rng, next_meeting.year, next_meeting.month, next_meeting.day)
    lines.append(f"Next meeting: {nm_rendered}")

    return {
        "ground_truth": ground_truth, "field_spec": field_spec, "schema": schema,
        "document_text": "\n".join(lines), "doc_type": "meeting_minutes",
    }


def _gen_log_summary(rng, d: float) -> dict:
    n_lines = 8 + round(d * 30)
    n_services = rng.randint(3, 6)
    services = rng.sample(_SERVICE_BANK, n_services)

    levels = [rng.choices(["INFO", "WARN", "ERROR"], weights=[70, 20, 10])[0] for _ in range(n_lines)]
    if "ERROR" not in levels:
        levels[rng.randrange(n_lines)] = "ERROR"

    y = rng.randint(2024, 2026)
    m = rng.randint(1, 12)
    dd = rng.randint(1, 28)
    ts = datetime.datetime(y, m, dd, rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59))

    log_lines = []
    first_error_ts = None
    services_with_errors = set()
    for level in levels:
        svc = rng.choice(services)
        msg = rng.choice(_LEVEL_MSGS[level])
        ts_str = ts.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
        log_lines.append(f"{ts_str} [{level}] {svc}: {msg}")
        if level == "ERROR":
            services_with_errors.add(svc)
            if first_error_ts is None:
                first_error_ts = ts_str
        ts = ts + datetime.timedelta(seconds=rng.randint(1, 120))

    error_count = levels.count("ERROR")
    services_with_errors_sorted = sorted(services_with_errors)

    ground_truth = {
        "error_count": error_count,
        "first_error_timestamp": first_error_ts,
        "services_with_errors": services_with_errors_sorted,
    }
    field_spec = [
        {"path": "error_count", "type": "int", "expected": error_count, "weight": 1.0},
        {"path": "first_error_timestamp", "type": "text", "expected": first_error_ts, "weight": 1.0},
        {"path": "services_with_errors", "type": "str_list_unordered", "expected": services_with_errors_sorted, "weight": 1.0},
    ]
    schema = {
        "type": "object",
        "properties": {
            "error_count": {"type": "integer"},
            "first_error_timestamp": {"type": "string"},
            "services_with_errors": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["error_count", "first_error_timestamp", "services_with_errors"],
        "additionalProperties": False,
    }

    document_text = "LOG EXCERPT\n\n" + "\n".join(log_lines)
    return {
        "ground_truth": ground_truth, "field_spec": field_spec, "schema": schema,
        "document_text": document_text, "doc_type": "log_summary",
    }


_GENERATORS = {
    "invoice": _gen_invoice,
    "meeting_minutes": _gen_meeting_minutes,
    "log_summary": _gen_log_summary,
}

_INSTRUCTION = (
    "\n\nReturn exactly one JSON object matching the schema, in a ```json fence, "
    "and nothing else."
)


# ---------------------------------------------------------------------------
# generic comparison (used by verify(); no per-doctype logic here)
# ---------------------------------------------------------------------------

def _compare(ftype: Any, got: Any, expected: Any) -> bool:
    if ftype == "money":
        g, e = checkers.normalize_money(got), checkers.normalize_money(expected)
        return g is not None and e is not None and g == e
    if ftype == "date":
        g, e = checkers.normalize_date(got), checkers.normalize_date(expected)
        return g is not None and e is not None and g == e
    if ftype == "text":
        g, e = checkers.normalize_text(got), checkers.normalize_text(expected)
        return g is not None and e is not None and g == e
    if ftype == "int":
        return checkers.canonical_equal(got, expected)
    if ftype == "number":
        return checkers.canonical_equal(got, expected, rel_tol=1e-6)
    if ftype == "str_list_unordered":
        if type(got) is not list or type(expected) is not list:
            return False
        g_norm = [checkers.normalize_text(v) for v in got]
        e_norm = [checkers.normalize_text(v) for v in expected]
        if any(x is None for x in g_norm) or any(x is None for x in e_norm):
            return False
        return sorted(g_norm) == sorted(e_norm)
    return False


def _truncate(v: Any, limit: int = 80) -> str:
    s = v if type(v) is str else repr(v)
    return s[:limit]


def _corrupt_number(value: float, salt: int) -> float:
    v = float(value)
    if v == 0:
        return 1.0 if salt % 2 == 0 else -1.0
    return round(v + 1, 2) if salt % 2 == 0 else round(v * 1.1, 2)


def _corrupt_value(ftype: Any, value: Any, salt: int) -> Any:
    if ftype in ("money", "number"):
        return _corrupt_number(value, salt)
    if ftype == "int":
        return int(value) + 1
    if ftype == "date":
        try:
            y, m, dd = (int(p) for p in str(value).split("-"))
        except ValueError:
            return value
        new_day = (dd % 27) + 1
        return f"{y:04d}-{m:02d}-{new_day:02d}"
    if ftype == "text":
        suffixes = [" Inc", " (revised)", "-x", " updated"]
        return str(value) + suffixes[salt % len(suffixes)]
    if ftype == "str_list_unordered" and type(value) is list:
        return [str(v) + " Inc" for v in value]
    return value


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------

@register
class StructExtractEnv(Environment):
    name = "struct_extract"
    reference_threshold = 0.95
    corrupted_threshold = 0.2

    def generate(self, task_params: Optional[dict] = None, seed: int = 0) -> Task:
        params = dict(task_params or {})
        d = self.resolve_difficulty(params)
        rng = self.rng(seed, params)
        doc_type = params.get("doc_type") or rng.choice(sorted(_GENERATORS))
        if doc_type not in _GENERATORS:
            doc_type = rng.choice(sorted(_GENERATORS))
        gen = _GENERATORS[doc_type]
        spec = gen(rng, d)

        prompt = (
            f"{spec['document_text']}\n\n"
            "## Schema\n\n```json\n" + json.dumps(spec["schema"], indent=2) + "\n```"
            + _INSTRUCTION
        )
        norm_params = {"difficulty": d, "doc_type": doc_type}
        payload = {
            "schema": spec["schema"],
            "field_spec": spec["field_spec"],
            "ground_truth": spec["ground_truth"],
        }
        metadata = {"doc_type": doc_type, "n_fields": len(spec["field_spec"])}
        return Task(
            env=self.name,
            task_id=self.make_task_id(norm_params, seed, payload),
            seed=seed,
            difficulty=d,
            prompt=prompt,
            task_params=norm_params,
            payload=payload,
            metadata=metadata,
        )

    def verify(self, task: Task, response: str) -> VerifyResult:
        try:
            value, why = checkers.extract_json_response(response)
        except RecursionError:
            return VerifyResult(0.0, {"error": "response too deeply nested"})
        if value is None:
            return VerifyResult(0.0, {"error": why})

        schema = task.payload.get("schema")
        field_spec = task.payload.get("field_spec")
        if type(schema) is not dict or type(field_spec) is not list:
            return VerifyResult(0.0, {"error": "malformed task payload"})

        errs = checkers.schema_check(value, schema)
        if errs:
            return VerifyResult(0.0, {"error": "schema violation", "schema_errors": errs[:5]})

        total_w = 0.0
        matched_w = 0.0
        field_results = []
        for spec in field_spec:
            if type(spec) is not dict:
                continue
            path = spec.get("path")
            ftype = spec.get("type")
            expected = spec.get("expected")
            try:
                weight = float(spec.get("weight", 1.0))
            except (TypeError, ValueError):
                weight = 1.0
            total_w += weight

            got, found = None, False
            if type(path) is str:
                try:
                    got, found = _get_path(value, path)
                except _PathError:
                    got, found = None, False

            ok = found and _compare(ftype, got, expected)
            if ok:
                matched_w += weight
            field_results.append({
                "path": path, "type": ftype, "ok": bool(ok),
                "got": _truncate(got), "expected": _truncate(expected),
            })

        if total_w <= 0:
            return VerifyResult(0.0, {"error": "empty field spec"})

        reward = matched_w / total_w
        return VerifyResult(reward, {
            "fields": field_results,
            "matched_weight": matched_w,
            "total_weight": total_w,
        })

    def reference_solution(self, task: Task) -> str:
        gt = task.payload["ground_truth"]
        return "```json\n" + json.dumps(gt, indent=2) + "\n```"

    def corrupted_solution(self, task: Task) -> str:
        gt = json.loads(json.dumps(task.payload["ground_truth"]))
        base_salt = int(task.task_id[:8], 16)
        for i, spec in enumerate(task.payload["field_spec"]):
            path, ftype, expected = spec["path"], spec["type"], spec["expected"]
            value, found = _get_path(gt, path)
            if not found:
                continue
            new_value = _corrupt_value(ftype, value if value is not None else expected, base_salt + i)
            _set_path(gt, path, new_value)
        return "```json\n" + json.dumps(gt, indent=2) + "\n```"
