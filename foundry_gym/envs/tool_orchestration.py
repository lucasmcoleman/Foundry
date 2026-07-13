"""tool_orchestration — mock MCP-style tool servers over a deterministic state
machine. The policy emits a JSON *plan* (array of tool calls, optionally
wired together with ``{"$from": "<step_id>.result.<path>"}`` data-flow
references) and the verifier *simulates* the plan against a small in-memory
"ops console" (auth, docs, tickets, email, kv, plus a few no-op distractor
tools), then scores the resulting world state against goal predicates.

Design notes:
- ``docs.search`` matches by "any query word appears among the title's
  words" (OR semantics, casefolded, word-tokenized). To keep "exactly one
  document matches" a *guaranteed* property of generation (not a hopeful
  empirical one), the search anchor embedded in the scenario is always a
  single distinctive codename token (e.g. "zephyr47") that appears in no
  other document's title — a multi-word AND-implied query would not be safe
  under OR-match semantics.
- The incident code and doc owner never appear in the prompt text, only
  inside the simulated ``docs.read`` result — this forces the read (and the
  ``$from`` hop) rather than allowing the code to be hardcoded from the
  prompt.
- All tool call outcomes are one of: *invalid* (bad $from ref, unknown tool,
  or schema violation — structural problem, counts against the 0.05/step
  penalty), *failed* (well-formed call whose precondition wasn't met, e.g.
  not authenticated, unknown doc id, closing a non-open ticket — legal but
  inert), or *ok* (effect applied).
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from ..core.env import Environment
from ..core.registry import register
from ..core.types import Task, VerifyResult
from ..core import checkers

# ---------------------------------------------------------------------------
# static world knowledge: tool schemas / metadata (fixed regardless of task)
# ---------------------------------------------------------------------------

_TOOL_META: dict = {
    "auth.login": {
        "schema": {
            "type": "object",
            "properties": {"token": {"type": "string"}},
            "required": ["token"],
            "additionalProperties": False,
        },
        "requires_auth": False,
        "desc": "Authenticate with the console using your API token.",
    },
    "docs.search": {
        "schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        "requires_auth": True,
        "desc": (
            "Search internal documents by keyword; returns matching "
            "{doc_id, title} pairs (any query word appearing in a title "
            "counts as a match), sorted by doc_id."
        ),
    },
    "docs.read": {
        "schema": {
            "type": "object",
            "properties": {"doc_id": {"type": "string"}},
            "required": ["doc_id"],
            "additionalProperties": False,
        },
        "requires_auth": True,
        "desc": "Read a document by id; returns its title, body and structured fields.",
    },
    "tickets.create": {
        "schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
                "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"]},
            },
            "required": ["title", "body", "priority"],
            "additionalProperties": False,
        },
        "requires_auth": True,
        "desc": "Create a new support ticket; returns its ticket_id.",
    },
    "tickets.close": {
        "schema": {
            "type": "object",
            "properties": {"ticket_id": {"type": "string"}},
            "required": ["ticket_id"],
            "additionalProperties": False,
        },
        "requires_auth": True,
        "desc": "Close an existing open ticket.",
    },
    "email.send": {
        "schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["to", "subject", "body"],
            "additionalProperties": False,
        },
        "requires_auth": True,
        "desc": "Send an email.",
    },
    "kv.set": {
        "schema": {
            "type": "object",
            "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
            "required": ["key", "value"],
            "additionalProperties": False,
        },
        "requires_auth": True,
        "desc": "Store a key/value pair.",
    },
    "kv.get": {
        "schema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
            "additionalProperties": False,
        },
        "requires_auth": True,
        "desc": "Retrieve a stored value by key.",
    },
    # distractors: schema-valid, callable, never needed to satisfy any goal.
    "calendar.create_event": {
        "schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
            },
            "required": ["title", "start", "end"],
            "additionalProperties": False,
        },
        "requires_auth": True,
        "desc": "Create a calendar event (unrelated utility tool).",
    },
    "inventory.reserve": {
        "schema": {
            "type": "object",
            "properties": {"item": {"type": "string"}, "qty": {"type": "integer"}},
            "required": ["item", "qty"],
            "additionalProperties": False,
        },
        "requires_auth": True,
        "desc": "Reserve inventory stock for an item (unrelated utility tool).",
    },
    "metrics.query": {
        "schema": {
            "type": "object",
            "properties": {"metric": {"type": "string"}, "range": {"type": "string"}},
            "required": ["metric", "range"],
            "additionalProperties": False,
        },
        "requires_auth": True,
        "desc": "Query a metrics time series (unrelated utility tool).",
    },
}

_BASE_TOOLS = [
    "auth.login", "docs.search", "docs.read", "tickets.create",
    "tickets.close", "email.send", "kv.set", "kv.get",
]
_DISTRACTOR_TOOLS = ["calendar.create_event", "inventory.reserve", "metrics.query"]

_PRIORITIES = ["low", "normal", "high", "urgent"]

_CODENAME_BANK = [
    "zephyr", "nova", "comet", "atlas", "triton", "orion", "vega", "lyra",
    "juno", "krypton", "phoenix", "quasar", "nebula", "pulsar", "corona", "meridian",
]
_NAME_BANK = [
    ("Dana", "Kim"), ("Robin", "Chen"), ("Sam", "Ortiz"), ("Priya", "Nair"),
    ("Alex", "Novak"), ("Jordan", "Lee"), ("Casey", "Brooks"), ("Morgan", "Diaz"),
    ("Taylor", "Reyes"), ("Jamie", "Singh"), ("Riley", "Owens"), ("Quinn", "Patel"),
]
_RECIPIENT_BANK = [
    "sre@corp.example", "support@corp.example", "oncall@corp.example",
    "security@corp.example", "ops-team@corp.example",
]
_TITLE_TEMPLATES = [
    "{w} Incident Report", "Postmortem: {w}", "{w} Outage Summary",
    "Runbook: {w} Response", "{w} Status Update", "Root Cause: {w}",
]
_FILLER_SENTENCES = [
    "Initial triage is complete.",
    "Customer impact appears limited to a subset of regions.",
    "Mitigation steps have been documented in the runbook.",
    "A follow-up review is scheduled once the incident is resolved.",
    "Logs have been attached to the internal tracker.",
    "The on-call engineer acknowledged the page within five minutes.",
    "No data loss has been observed so far.",
    "A rollback was considered but not required.",
]

_WORD_RE = re.compile(r"[a-z0-9]+")


def _words(text: str) -> set:
    return set(_WORD_RE.findall(text.casefold()))


_PLAN_FORMAT_SPEC = """
## Plan format

Respond with a single JSON array inside one ```json fenced code block, and
nothing else. Each element is a step object:

  {"id": "s1", "tool": "<tool name>", "args": {"<arg name>": <value>, ...}}

- "id" must be a unique string per step.
- "tool" must be one of the tool names from the catalog above.
- "args" must match that tool's argument schema exactly (no extra fields).
- An argument value may be a literal, OR a data-flow reference object of the
  form {"$from": "<step_id>.result.<dotted.path>"} that pulls a value out of
  an earlier step's result. Use "[i]" to index into an array in the path,
  e.g. {"$from": "s2.result.docs[0].doc_id"}.
- Plans may contain at most 30 steps.

Tiny generic example (illustrative only, unrelated to this task):

```json
[
  {"id": "s1", "tool": "auth.login", "args": {"token": "TOK-0000"}},
  {"id": "s2", "tool": "docs.search", "args": {"query": "example"}},
  {"id": "s3", "tool": "docs.read", "args": {"doc_id": {"$from": "s2.result.docs[0].doc_id"}}}
]
```

Output ONLY one ```json fenced array with your plan for THIS task — no other
text inside or outside the fence beyond the array itself.
"""


def _render_catalog(tool_names: list) -> str:
    lines = []
    for name in sorted(tool_names):
        meta = _TOOL_META[name]
        props = meta["schema"].get("properties", {})
        args_desc = ", ".join(f"{k}: {v.get('type')}" for k, v in sorted(props.items()))
        auth_note = "requires auth" if meta["requires_auth"] else "no auth required"
        lines.append(f"- {name}({args_desc}) [{auth_note}]: {meta['desc']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# $from path resolution
# ---------------------------------------------------------------------------

class _RefError(ValueError):
    pass


_PATH_FULL_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*|\[[0-9]+\])*$"
)
_PATH_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\[[0-9]+\]")


def _tokenize_path(path: Any) -> list:
    if type(path) is not str or not path or len(path) > 500 or not _PATH_FULL_RE.match(path):
        raise _RefError(f"bad $from path syntax: {path!r}")
    tokens = []
    for m in _PATH_TOKEN_RE.finditer(path):
        s = m.group(0)
        if s.startswith("["):
            tokens.append(("idx", int(s[1:-1])))
        else:
            tokens.append(("key", s))
    if len(tokens) > 32:
        raise _RefError("$from path too deep")
    return tokens


def _resolve_ref(path: str, results: dict) -> Any:
    tokens = _tokenize_path(path)
    step_id = tokens[0][1]
    step_res = results.get(step_id)
    if step_res is None or not step_res.get("ok"):
        raise _RefError(f"$from references missing/failed step {step_id!r}")
    cur: Any = results
    for kind, val in tokens:
        if kind == "key":
            if type(cur) is not dict or val not in cur:
                raise _RefError(f"bad $from path segment {val!r} in {path!r}")
            cur = cur[val]
        else:
            if type(cur) is not list or val < 0 or val >= len(cur):
                raise _RefError(f"bad $from index [{val}] in {path!r}")
            cur = cur[val]
    return cur


def _resolve_value(v: Any, results: dict) -> Any:
    if type(v) is dict:
        if set(v.keys()) != {"$from"} or type(v.get("$from")) is not str:
            raise _RefError("malformed $from reference object")
        return _resolve_ref(v["$from"], results)
    return v


def _resolve_args(raw_args: dict, results: dict) -> dict:
    out = {}
    for k, v in raw_args.items():
        if type(k) is not str:
            raise _RefError("arg keys must be strings")
        out[k] = _resolve_value(v, results)
    return out


# ---------------------------------------------------------------------------
# structural plan validation
# ---------------------------------------------------------------------------

def _validate_plan_structure(value: Any):
    if type(value) is not list:
        return None, "plan must be a JSON array"
    if len(value) == 0:
        return None, "plan must contain at least one step"
    if len(value) > 30:
        return None, "plan exceeds 30 steps"
    seen_ids = set()
    for i, step in enumerate(value):
        if type(step) is not dict:
            return None, f"step {i} is not an object"
        sid = step.get("id")
        if type(sid) is not str or not sid:
            return None, f"step {i} missing/invalid string id"
        if sid in seen_ids:
            return None, f"duplicate step id {sid!r}"
        seen_ids.add(sid)
        if type(step.get("tool")) is not str:
            return None, f"step {i} missing/invalid string tool"
        if type(step.get("args")) is not dict:
            return None, f"step {i} missing/invalid args object"
    return value, "ok"


# ---------------------------------------------------------------------------
# simulator
# ---------------------------------------------------------------------------

def _execute_tool(tool: str, args: dict, state: dict, world: dict):
    """Returns (ok: bool, result: Any, error: Optional[str])."""
    if tool == "auth.login":
        if args["token"] == world["token"]:
            state["authenticated"] = True
            return True, {"ok": True}, None
        return False, None, "bad token"
    meta = _TOOL_META[tool]
    if meta["requires_auth"] and not state["authenticated"]:
        return False, None, "not authenticated"
    if tool == "docs.search":
        q_words = _words(args["query"])
        matches = []
        for doc_id in sorted(world["docs"]):
            title = world["docs"][doc_id]["title"]
            if q_words & _words(title):
                matches.append({"doc_id": doc_id, "title": title})
        return True, {"docs": matches}, None
    if tool == "docs.read":
        doc = world["docs"].get(args["doc_id"])
        if doc is None:
            return False, None, "unknown doc_id"
        return True, {
            "doc_id": args["doc_id"],
            "title": doc["title"],
            "body": doc["body"],
            "fields": dict(doc["fields"]),
        }, None
    if tool == "tickets.create":
        tid = f"T{state['_next_ticket']}"
        state["_next_ticket"] += 1
        state["tickets"][tid] = {
            "title": args["title"], "body": args["body"],
            "priority": args["priority"], "status": "open",
        }
        return True, {"ticket_id": tid}, None
    if tool == "tickets.close":
        t = state["tickets"].get(args["ticket_id"])
        if t is None or t["status"] != "open":
            return False, None, "ticket not open"
        t["status"] = "closed"
        return True, {"ok": True}, None
    if tool == "email.send":
        state["emails"].append({"to": args["to"], "subject": args["subject"], "body": args["body"]})
        return True, {"ok": True}, None
    if tool == "kv.set":
        state["kv"][args["key"]] = args["value"]
        return True, {"ok": True}, None
    if tool == "kv.get":
        return True, {"value": state["kv"].get(args["key"])}, None
    if tool == "calendar.create_event":
        return True, {"event_id": "EV1"}, None
    if tool == "inventory.reserve":
        return True, {"reservation_id": "R1"}, None
    if tool == "metrics.query":
        return True, {"value": 0}, None
    return False, None, "unhandled tool"  # unreachable (unknown tools filtered earlier)


def _simulate(steps: list, world: dict):
    state = {"authenticated": False, "tickets": {}, "emails": [], "kv": {}, "_next_ticket": 1}
    results: dict = {}
    step_log = []
    invalid_count = 0
    for step in steps:
        sid, tool, raw_args = step["id"], step["tool"], step["args"]
        try:
            resolved = _resolve_args(raw_args, results)
        except _RefError as e:
            invalid_count += 1
            results[sid] = {"ok": False, "result": None, "error": str(e)}
            step_log.append({"id": sid, "tool": tool, "status": "invalid", "reason": str(e)})
            continue
        schema = _TOOL_META.get(tool, {}).get("schema")
        if schema is None:
            invalid_count += 1
            results[sid] = {"ok": False, "result": None, "error": "unknown tool"}
            step_log.append({"id": sid, "tool": tool, "status": "invalid", "reason": "unknown tool"})
            continue
        errs = checkers.schema_check(resolved, schema)
        if errs:
            invalid_count += 1
            reason = "; ".join(errs[:3])
            results[sid] = {"ok": False, "result": None, "error": "schema: " + reason}
            step_log.append({"id": sid, "tool": tool, "status": "invalid", "reason": reason})
            continue
        ok, result, error = _execute_tool(tool, resolved, state, world)
        results[sid] = {"ok": ok, "result": result, "error": error}
        step_log.append({"id": sid, "tool": tool, "status": "ok" if ok else "failed", "reason": error})
    return state, results, step_log, invalid_count


# ---------------------------------------------------------------------------
# goal predicates
# ---------------------------------------------------------------------------

def _eval_predicate(pred: Any, state: dict) -> bool:
    if type(pred) is not dict:
        return False
    kind = pred.get("kind")
    if kind == "ticket_with_code":
        code, priority = pred.get("code"), pred.get("priority")
        if type(code) is not str or type(priority) is not str:
            return False
        return any(
            t.get("priority") == priority and code in t.get("body", "")
            for t in state["tickets"].values()
        )
    if kind == "email_with_ticket_ref":
        to = pred.get("to")
        if type(to) is not str:
            return False
        ticket_ids = list(state["tickets"].keys())
        for email in state["emails"]:
            if email.get("to") != to:
                continue
            subj, body = email.get("subject", ""), email.get("body", "")
            if any(tid in subj or tid in body for tid in ticket_ids):
                return True
        return False
    if kind == "kv_equals":
        key, expected = pred.get("key"), pred.get("expected")
        if type(key) is not str:
            return False
        return state["kv"].get(key) == expected
    return False


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------

@register
class ToolOrchestrationEnv(Environment):
    name = "tool_orchestration"
    reference_threshold = 0.95
    corrupted_threshold = 0.35

    def generate(self, task_params: Optional[dict] = None, seed: int = 0) -> Task:
        params = dict(task_params or {})
        d = self.resolve_difficulty(params)
        rng = self.rng(seed, params)

        token = "TOK-" + "".join(rng.choice("0123456789abcdef") for _ in range(6))

        n_distractor_docs = 2 + round(d * 6)
        n_distractor_tools = round(d * 3)
        doc_id_given_directly = d < 0.35
        want_email = d >= 0.55
        want_kv = d >= 0.75

        codenames = rng.sample(sorted(_CODENAME_BANK), 1 + n_distractor_docs)
        target_word = codenames[0] + str(rng.randrange(10, 99))
        distractor_words = [c + str(rng.randrange(10, 99)) for c in codenames[1:]]

        def _make_doc(word: str):
            title = rng.choice(_TITLE_TEMPLATES).format(w=word.title())
            first, last = rng.choice(_NAME_BANK)
            owner = f"{first} {last}"
            code = f"INC-{rng.randrange(10000):04d}"
            filler = " ".join(rng.sample(_FILLER_SENTENCES, k=3))
            body = f"{title}\n\nOwner: {owner}\nIncident code: {code}\nStatus: investigating\n\n{filler}"
            return {"title": title, "body": body, "fields": {"incident_code": code, "owner": owner}}

        target_doc = _make_doc(target_word)
        distractor_docs = [_make_doc(w) for w in distractor_words]

        doc_records = [target_doc] + distractor_docs
        order = list(range(len(doc_records)))
        rng.shuffle(order)
        world_docs = {}
        target_doc_id = None
        for slot, orig_idx in enumerate(order):
            doc_id = f"D{slot + 1}"
            world_docs[doc_id] = doc_records[orig_idx]
            if orig_idx == 0:
                target_doc_id = doc_id

        # Defensive check: construction must guarantee the search anchor
        # (the target codename word) matches exactly one document title.
        n_matches = sum(1 for rec in world_docs.values() if target_word in _words(rec["title"]))
        if n_matches != 1:
            raise ValueError("generator invariant violated: search anchor is not unique")

        priority = _PRIORITIES[rng.randrange(len(_PRIORITIES))]
        email_to = rng.choice(_RECIPIENT_BANK) if want_email else None
        kv_key = "incident.owner" if want_kv else None

        catalog_tools = list(_BASE_TOOLS)
        if n_distractor_tools:
            catalog_tools += rng.sample(_DISTRACTOR_TOOLS, n_distractor_tools)

        predicates = [{
            "kind": "ticket_with_code",
            "code": target_doc["fields"]["incident_code"],
            "priority": priority,
        }]
        if email_to:
            predicates.append({"kind": "email_with_ticket_ref", "to": email_to})
        if kv_key:
            predicates.append({
                "kind": "kv_equals",
                "key": kv_key,
                "expected": target_doc["fields"]["owner"],
            })

        # -- prompt ------------------------------------------------------
        goal_lines = [f"Your API token is {token}."]
        if doc_id_given_directly:
            goal_lines.append(f"There is an open incident documented in {target_doc_id}.")
        else:
            goal_lines.append(
                f'There is an open incident referenced internally by the codename '
                f'"{target_word}"; you will need to find the corresponding document.'
            )
        goal_lines.append(
            f"Open a {priority}-priority support ticket whose body includes the "
            "incident code found in that document."
        )
        if email_to:
            goal_lines.append(
                f"Also email {email_to} with a subject or body that references the "
                "id of the ticket you created."
            )
        if kv_key:
            goal_lines.append(
                "Also record the incident owner (found in the document's fields) by "
                f'setting the key-value pair "{kv_key}" to that owner\'s name.'
            )
        scenario = "\n".join(goal_lines)
        catalog_text = _render_catalog(catalog_tools)
        prompt = (
            "You are operating an internal ops console through a set of tool calls; "
            "there is no other way to interact with the system.\n\n"
            f"{scenario}\n\n"
            "## Tool catalog\n\n" + catalog_text + "\n" + _PLAN_FORMAT_SPEC
        )

        norm_params = {"difficulty": d}
        payload = {
            "token": token,
            "docs": world_docs,
            "target_doc_id": target_doc_id,
            "doc_id_given_directly": doc_id_given_directly,
            "search_query": target_word,
            "priority": priority,
            "email_to": email_to,
            "kv_key": kv_key,
            "predicates": predicates,
        }
        metadata = {
            "n_docs": len(world_docs),
            "n_predicates": len(predicates),
            "has_email": bool(email_to),
            "has_kv": bool(kv_key),
            "catalog_tools": sorted(catalog_tools),
        }
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
        plan, why2 = _validate_plan_structure(value)
        if plan is None:
            return VerifyResult(0.0, {"error": why2})

        token = task.payload.get("token")
        docs = task.payload.get("docs")
        predicates = task.payload.get("predicates")
        if type(token) is not str or type(docs) is not dict or type(predicates) is not list:
            return VerifyResult(0.0, {"error": "malformed task payload"})
        world = {"token": token, "docs": docs}

        state, results, step_log, invalid_count = _simulate(plan, world)

        pred_results = []
        n_passed = 0
        for pred in predicates:
            ok = _eval_predicate(pred, state)
            pred_results.append({"kind": pred.get("kind") if type(pred) is dict else None, "ok": ok})
            if ok:
                n_passed += 1

        frac = (n_passed / len(predicates)) if predicates else 1.0
        reward = max(0.0, frac - 0.05 * min(invalid_count, 4))
        diagnostics = {
            "predicates": pred_results,
            "n_predicates": len(predicates),
            "n_predicates_passed": n_passed,
            "invalid_steps": invalid_count,
            "steps": step_log,
        }
        return VerifyResult(reward, diagnostics)

    # -- solution builders ----------------------------------------------

    def _build_reference_plan(self, task: Task) -> list:
        payload = task.payload
        token = payload["token"]
        target_doc_id = payload["target_doc_id"]
        doc_id_given = bool(payload["doc_id_given_directly"])
        search_query = payload["search_query"]
        priority = payload["priority"]
        email_to = payload.get("email_to")
        kv_key = payload.get("kv_key")

        steps = [{"id": "s1", "tool": "auth.login", "args": {"token": token}}]
        if doc_id_given:
            steps.append({"id": "s2", "tool": "docs.read", "args": {"doc_id": target_doc_id}})
            read_id, n = "s2", 2
        else:
            steps.append({"id": "s2", "tool": "docs.search", "args": {"query": search_query}})
            steps.append({
                "id": "s3", "tool": "docs.read",
                "args": {"doc_id": {"$from": "s2.result.docs[0].doc_id"}},
            })
            read_id, n = "s3", 3

        n += 1
        ticket_step_id = f"s{n}"
        steps.append({
            "id": ticket_step_id,
            "tool": "tickets.create",
            "args": {
                "title": "Incident ticket",
                "body": {"$from": f"{read_id}.result.fields.incident_code"},
                "priority": priority,
            },
        })
        if email_to:
            n += 1
            steps.append({
                "id": f"s{n}",
                "tool": "email.send",
                "args": {
                    "to": email_to,
                    "subject": {"$from": f"{ticket_step_id}.result.ticket_id"},
                    "body": "See ticket for details.",
                },
            })
        if kv_key:
            n += 1
            steps.append({
                "id": f"s{n}",
                "tool": "kv.set",
                "args": {
                    "key": kv_key,
                    "value": {"$from": f"{read_id}.result.fields.owner"},
                },
            })
        return steps

    def reference_solution(self, task: Task) -> str:
        steps = self._build_reference_plan(task)
        return "```json\n" + json.dumps(steps, indent=2) + "\n```"

    def corrupted_solution(self, task: Task) -> str:
        plan = json.loads(json.dumps(self._build_reference_plan(task)))
        real_code = task.payload["docs"][task.payload["target_doc_id"]]["fields"]["incident_code"]
        real_num = int(real_code.split("-")[1])
        wrong_num = int(task.task_id[:8], 16) % 10000
        if wrong_num == real_num:
            wrong_num = (wrong_num + 1) % 10000
        wrong_code = f"INC-{wrong_num:04d}"

        new_plan = []
        for step in plan:
            if step["tool"] == "email.send":
                continue
            if step["tool"] == "tickets.create":
                step["args"]["body"] = wrong_code
            new_plan.append(step)
        return "```json\n" + json.dumps(new_plan, indent=2) + "\n```"
