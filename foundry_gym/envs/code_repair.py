"""code_repair — fix fault-injected Python modules; reward = hidden tests pass.

Pipeline per task:
  1. pick a corpus module (self-contained, deterministic, order-stable outputs);
  2. generate hidden I/O test calls from per-function argument generators;
  3. compute expected outputs by running the CLEAN source in the sandbox
     (same canonicalization path as verification — no in-process exec at all);
  4. inject 1–3 AST-level faults (operator swaps, off-by-one constants,
     boolean flips) and keep only mutations that fail >= 40% of hidden tests
     (so the buggy module — the negative control — scores <= 0.6);
  5. the policy must return the corrected module in a ```python fence.

Verification: static guard (import allowlist, banned callables, no dunder
attribute access), required functions present, then hidden tests run in the
hardened sandbox (core/sandbox.py); reward = fraction of tests passing with
parent-side canonical comparison (expected outputs never enter the child).
"""

from __future__ import annotations

import ast
import random
import re as _re
from dataclasses import dataclass
from typing import Callable, List, Optional

from ..core.env import Environment
from ..core.registry import register
from ..core.types import Task, VerifyResult
from ..core import checkers
from ..core.sandbox import run_calls

# ---------------------------------------------------------------------------
# corpus
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CorpusEntry:
    name: str
    source: str
    functions: tuple  # required function names
    # arggen(rng, difficulty) -> list of expr strings referencing `m.`
    arggen: Callable[[random.Random, float, int], List[str]]
    rel_tol: float = 0.0  # numeric tolerance for comparisons


def _src(s: str) -> str:
    return s.strip("\n") + "\n"


_MERGE_INTERVALS = _src('''
def merge_intervals(intervals):
    """Merge overlapping [start, end] intervals and return them sorted.

    Example: merge_intervals([[1, 3], [2, 6], [8, 10]]) -> [[1, 6], [8, 10]]
    """
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return merged
''')


def _args_merge_intervals(rng: random.Random, d: float, n: int) -> List[str]:
    out = []
    for _ in range(n):
        k = rng.randint(0, 3 + round(d * 5))
        iv = []
        for _ in range(k):
            a = rng.randint(-20, 40)
            iv.append([a, a + rng.randint(0, 12)])
        out.append(f"m.merge_intervals({iv!r})")
    return out


_BALANCED = _src('''
def balanced_brackets(s):
    """Return True iff every (, [, { is properly closed and nested.

    Example: balanced_brackets("a(b[c]{d})") -> True
    """
    pairs = {")": "(", "]": "[", "}": "{"}
    stack = []
    for ch in s:
        if ch in "([{":
            stack.append(ch)
        elif ch in pairs:
            if not stack or stack[-1] != pairs[ch]:
                return False
            stack.pop()
    return not stack
''')


def _args_balanced(rng: random.Random, d: float, n: int) -> List[str]:
    out = []
    brackets = "()[]{}"
    for _ in range(n):
        k = rng.randint(0, 6 + round(d * 10))
        s = "".join(rng.choice(brackets + "abc") for _ in range(k))
        if rng.random() < 0.4:  # sometimes force a balanced string
            openers = "([{"
            t = ""
            stack = []
            for _ in range(k // 2):
                o = rng.choice(openers)
                t += o
                stack.append({"(": ")", "[": "]", "{": "}"}[o])
            s = t + "".join(reversed(stack))
        out.append(f"m.balanced_brackets({s!r})")
    return out


_BSEARCH = _src('''
def binary_search_left(arr, target):
    """Return the leftmost index where target could be inserted to keep
    the sorted list arr sorted (like bisect_left).

    Example: binary_search_left([1, 3, 3, 5], 3) -> 1
    """
    lo, hi = 0, len(arr)
    while lo < hi:
        mid = (lo + hi) // 2
        if arr[mid] < target:
            lo = mid + 1
        else:
            hi = mid
    return lo
''')


def _args_bsearch(rng: random.Random, d: float, n: int) -> List[str]:
    out = []
    for _ in range(n):
        k = rng.randint(0, 8 + round(d * 12))
        arr = sorted(rng.randint(-15, 30) for _ in range(k))
        target = rng.randint(-16, 31)
        if arr and rng.random() < 0.5:
            target = rng.choice(arr)
        out.append(f"m.binary_search_left({arr!r}, {target!r})")
    return out


_ROMAN = _src('''
def roman_to_int(s):
    """Convert a roman numeral (I V X L C D M) to an integer.

    Example: roman_to_int("MCMXCIV") -> 1994
    """
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    for i, ch in enumerate(s):
        v = values[ch]
        if i + 1 < len(s) and v < values[s[i + 1]]:
            total -= v
        else:
            total += v
    return total
''')

_INT_TO_ROMAN = [
    (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
    (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
]


def _to_roman(n: int) -> str:
    out = ""
    for v, sym in _INT_TO_ROMAN:
        while n >= v:
            out += sym
            n -= v
    return out


def _args_roman(rng: random.Random, d: float, n: int) -> List[str]:
    out = []
    for _ in range(n):
        v = rng.randint(1, 500 + round(d * 3000))
        out.append(f"m.roman_to_int({_to_roman(v)!r})")
    return out


_CAESAR = _src('''
def caesar_shift(s, k):
    """Shift letters by k positions (wrapping), preserving case; other
    characters unchanged.

    Example: caesar_shift("abz", 2) -> "cdb"
    """
    out = []
    for ch in s:
        if "a" <= ch <= "z":
            out.append(chr((ord(ch) - ord("a") + k) % 26 + ord("a")))
        elif "A" <= ch <= "Z":
            out.append(chr((ord(ch) - ord("A") + k) % 26 + ord("A")))
        else:
            out.append(ch)
    return "".join(out)
''')


def _args_caesar(rng: random.Random, d: float, n: int) -> List[str]:
    alphabet = "abcXYZ hij! QRz."
    out = []
    for _ in range(n):
        k = rng.randint(1, 6 + round(d * 12))
        s = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 14)))
        out.append(f"m.caesar_shift({s!r}, {rng.randint(-3, 40)!r})")
    return out


_FLATTEN = _src('''
def flatten(nested):
    """Flatten arbitrarily nested lists into a single flat list, preserving
    left-to-right order.

    Example: flatten([1, [2, [3, 4]], 5]) -> [1, 2, 3, 4, 5]
    """
    flat = []
    for item in nested:
        if isinstance(item, list):
            flat.extend(flatten(item))
        else:
            flat.append(item)
    return flat
''')


def _nested_list(rng: random.Random, depth: int, width: int):
    out = []
    for _ in range(rng.randint(0, width)):
        if depth > 0 and rng.random() < 0.4:
            out.append(_nested_list(rng, depth - 1, width))
        else:
            out.append(rng.randint(-9, 9))
    return out


def _args_flatten(rng: random.Random, d: float, n: int) -> List[str]:
    return [
        f"m.flatten({_nested_list(rng, 1 + round(d * 3), 4)!r})" for _ in range(n)
    ]


_MOVAVG = _src('''
def moving_average(xs, window):
    """Return the list of means over each sliding window of length `window`,
    each rounded to 6 decimal places. Empty list if window is larger than xs
    or window < 1.

    Example: moving_average([1, 2, 3, 4], 2) -> [1.5, 2.5, 3.5]
    """
    if window < 1 or window > len(xs):
        return []
    out = []
    total = sum(xs[:window])
    out.append(round(total / window, 6))
    for i in range(window, len(xs)):
        total += xs[i] - xs[i - window]
        out.append(round(total / window, 6))
    return out
''')


def _args_movavg(rng: random.Random, d: float, n: int) -> List[str]:
    out = []
    for _ in range(n):
        k = rng.randint(0, 6 + round(d * 10))
        xs = [rng.randint(-30, 60) for _ in range(k)]
        w = rng.randint(1, max(1, k + 1))
        out.append(f"m.moving_average({xs!r}, {w!r})")
    return out


_CSV_ROW = _src('''
def parse_csv_row(line):
    """Parse one CSV row: fields separated by commas; a field may be quoted
    with double quotes, inside which commas are literal and "" is an escaped
    quote. Returns the list of field strings.

    Example: parse_csv_row('a,"b,c",d') -> ['a', 'b,c', 'd']
    """
    fields = []
    cur = []
    in_quotes = False
    i = 0
    while i < len(line):
        ch = line[i]
        if in_quotes:
            if ch == '"':
                if i + 1 < len(line) and line[i + 1] == '"':
                    cur.append('"')
                    i += 1
                else:
                    in_quotes = False
            else:
                cur.append(ch)
        elif ch == '"':
            in_quotes = True
        elif ch == ",":
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    fields.append("".join(cur))
    return fields
''')


def _args_csv(rng: random.Random, d: float, n: int) -> List[str]:
    out = []
    for _ in range(n):
        nf = rng.randint(1, 3 + round(d * 4))
        fields = []
        for _ in range(nf):
            base = "".join(rng.choice("xyz12 ") for _ in range(rng.randint(0, 5)))
            if rng.random() < 0.4:
                inner = base + rng.choice(["", ",", '""', ",q"])
                fields.append('"' + inner + '"')
            else:
                fields.append(base)
        out.append(f"m.parse_csv_row({','.join(fields)!r})")
    return out


_ADD_DAYS = _src('''
def add_days(year, month, day, n):
    """Add n (>= 0) days to a Gregorian calendar date, returning
    [year, month, day]. Handles leap years (divisible by 4, except centuries
    unless divisible by 400).

    Example: add_days(2024, 2, 28, 1) -> [2024, 2, 29]
    """
    lengths = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    for _ in range(n):
        leap = (year % 4 == 0 and year % 100 != 0) or year % 400 == 0
        month_len = lengths[month - 1]
        if month == 2 and leap:
            month_len = 29
        day += 1
        if day > month_len:
            day = 1
            month += 1
            if month > 12:
                month = 1
                year += 1
    return [year, month, day]
''')


def _args_add_days(rng: random.Random, d: float, n: int) -> List[str]:
    out = []
    for _ in range(n):
        y = rng.randint(1999, 2101)
        m = rng.randint(1, 12)
        day = rng.randint(1, 28)
        k = rng.randint(0, 40 + round(d * 400))
        out.append(f"m.add_days({y}, {m}, {day}, {k})")
    return out


_LCP = _src('''
def longest_common_prefix(strs):
    """Return the longest common prefix of a list of strings ("" for an
    empty list).

    Example: longest_common_prefix(["flower", "flow", "flight"]) -> "fl"
    """
    if not strs:
        return ""
    prefix = strs[0]
    for s in strs[1:]:
        while not s.startswith(prefix):
            prefix = prefix[:-1]
            if not prefix:
                return ""
    return prefix
''')


def _args_lcp(rng: random.Random, d: float, n: int) -> List[str]:
    words = ["flow", "flower", "flight", "flat", "flare", "glow", "flowchart", ""]
    out = []
    for _ in range(n):
        k = rng.randint(0, 3 + round(d * 3))
        strs = [rng.choice(words) + rng.choice(["", "x", "er"]) for _ in range(k)]
        out.append(f"m.longest_common_prefix({strs!r})")
    return out


_FRACTION = _src('''
def reduce_fraction(num, den):
    """Reduce num/den to lowest terms with a positive denominator, returned
    as [num, den]. den is never 0.

    Example: reduce_fraction(6, -8) -> [-3, 4]
    """
    a, b = abs(num), abs(den)
    while b:
        a, b = b, a % b
    g = a if a else 1
    num //= g
    den //= g
    if den < 0:
        num, den = -num, -den
    return [num, den]
''')


def _args_fraction(rng: random.Random, d: float, n: int) -> List[str]:
    out = []
    for _ in range(n):
        num = rng.randint(-60 - round(d * 400), 60 + round(d * 400))
        den = rng.choice([i for i in range(-40, 41) if i != 0])
        out.append(f"m.reduce_fraction({num}, {den})")
    return out


_CORPUS: List[CorpusEntry] = [
    CorpusEntry("merge_intervals", _MERGE_INTERVALS, ("merge_intervals",), _args_merge_intervals),
    CorpusEntry("balanced_brackets", _BALANCED, ("balanced_brackets",), _args_balanced),
    CorpusEntry("binary_search_left", _BSEARCH, ("binary_search_left",), _args_bsearch),
    CorpusEntry("roman_to_int", _ROMAN, ("roman_to_int",), _args_roman),
    CorpusEntry("caesar_shift", _CAESAR, ("caesar_shift",), _args_caesar),
    CorpusEntry("flatten", _FLATTEN, ("flatten",), _args_flatten),
    CorpusEntry("moving_average", _MOVAVG, ("moving_average",), _args_movavg, rel_tol=1e-9),
    CorpusEntry("parse_csv_row", _CSV_ROW, ("parse_csv_row",), _args_csv),
    CorpusEntry("add_days", _ADD_DAYS, ("add_days",), _args_add_days),
    CorpusEntry("longest_common_prefix", _LCP, ("longest_common_prefix",), _args_lcp),
    CorpusEntry("reduce_fraction", _FRACTION, ("reduce_fraction",), _args_fraction),
]

_CORPUS_BY_NAME = {e.name: e for e in _CORPUS}


# ---------------------------------------------------------------------------
# AST fault injection
# ---------------------------------------------------------------------------

_CMP_SWAP = {
    ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
}
_BIN_SWAP = {
    ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Add,
    ast.FloorDiv: ast.Mod, ast.Mod: ast.FloorDiv,
}


class _Mutator(ast.NodeTransformer):
    """Single traversal used for BOTH site counting and mutation, so site
    indices are stable. When ``targets`` is None it only counts."""

    def __init__(self, targets: Optional[dict] = None):
        self.count = 0
        self.targets = targets  # site_index -> param (delta for consts)
        self.applied = 0

    def _site(self) -> Optional[object]:
        idx = self.count
        self.count += 1
        if self.targets is not None and idx in self.targets:
            self.applied += 1
            return self.targets[idx]
        return None

    def visit_Compare(self, node: ast.Compare):
        self.generic_visit(node)
        if len(node.ops) == 1 and type(node.ops[0]) in _CMP_SWAP:
            hit = self._site()
            if hit is not None:
                node.ops[0] = _CMP_SWAP[type(node.ops[0])]()
        return node

    def visit_BinOp(self, node: ast.BinOp):
        self.generic_visit(node)
        if type(node.op) in _BIN_SWAP:
            hit = self._site()
            if hit is not None:
                node.op = _BIN_SWAP[type(node.op)]()
        return node

    def visit_BoolOp(self, node: ast.BoolOp):
        self.generic_visit(node)
        hit = self._site()
        if hit is not None:
            node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
        return node

    def visit_Constant(self, node: ast.Constant):
        if type(node.value) is int and abs(node.value) < 1000:
            hit = self._site()
            if hit is not None:
                return ast.copy_location(
                    ast.Constant(value=node.value + int(hit)), node
                )
        return node


def _count_sites(source: str) -> int:
    tree = ast.parse(source)
    m = _Mutator(None)
    m.visit(tree)
    return m.count


def _apply_mutations(source: str, targets: dict) -> Optional[str]:
    tree = ast.parse(source)
    m = _Mutator(dict(targets))
    tree = m.visit(tree)
    if m.applied != len(targets):
        return None
    ast.fix_missing_locations(tree)
    try:
        return ast.unparse(tree) + "\n"
    except (ValueError, RecursionError):
        return None


# ---------------------------------------------------------------------------
# response extraction + static guard
# ---------------------------------------------------------------------------

_PY_FENCE_RE = _re.compile(r"```(?:python|py)?\s*\n(.*?)```", _re.DOTALL)

_ALLOWED_IMPORTS = {
    "math", "re", "itertools", "functools", "collections", "string",
    "heapq", "bisect", "json", "datetime", "typing",
}
_BANNED_NAMES = {
    "eval", "exec", "open", "__import__", "compile", "input", "breakpoint",
    "globals", "locals", "vars", "getattr", "setattr", "delattr",
}


def extract_python_module(text: str, max_len: int = 40_000):
    """Return (source, 'ok') for the last syntactically valid fenced python
    block (or the whole text if it parses). (None, reason) otherwise."""
    if not isinstance(text, str):
        return None, "response is not text"
    if len(text) > max_len:
        return None, "response too long"
    blocks = _PY_FENCE_RE.findall(text)
    for block in reversed(blocks):
        src = block.strip("\n") + "\n"
        try:
            ast.parse(src)
            return src, "ok"
        except (SyntaxError, ValueError):
            continue
    stripped = text.strip()
    if stripped:
        src = stripped + "\n"
        try:
            ast.parse(src)
            return src, "ok"
        except (SyntaxError, ValueError):
            pass
    if blocks:
        return None, "no fenced block parses as Python"
    return None, "no Python code found (expected a ```python fence)"


def static_guard(source: str, required_functions: tuple) -> Optional[str]:
    """Return a rejection reason, or None if the module passes the guard."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as e:
        return f"syntax error: {e}"
    defined = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in _ALLOWED_IMPORTS:
                    return f"disallowed import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] not in _ALLOWED_IMPORTS:
                return f"disallowed import: from {node.module}"
        elif isinstance(node, ast.Name) and node.id in _BANNED_NAMES:
            return f"banned identifier: {node.id}"
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__"):
                return f"banned dunder attribute access: .{node.attr}"
            if node.attr in _BANNED_NAMES:
                return f"banned attribute: .{node.attr}"
        elif isinstance(node, ast.FunctionDef):
            defined.add(node.name)
    missing = [f for f in required_functions if f not in defined]
    if missing:
        return f"required function(s) missing: {', '.join(missing)}"
    return None


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------

_MIN_FAIL_FRACTION = 0.4  # injected faults must break >= 40% of hidden tests

_PROMPT_TEMPLATE = """The following Python module contains {n_faults} injected bug(s). \
The docstrings describe the CORRECT behavior; the code deviates from them.

```python
{buggy}
```

Fix the bug(s). Reply with the COMPLETE corrected module in a single \
```python fenced code block. Keep the same function name(s) and signature(s). \
Use only the Python standard library. Your module will be checked against a \
hidden test suite."""


@register
class CodeRepairEnv(Environment):
    name = "code_repair"
    reference_threshold = 0.95
    corrupted_threshold = 0.6  # buggy module scores <= 1 - _MIN_FAIL_FRACTION

    def generate(self, task_params: Optional[dict] = None, seed: int = 0) -> Task:
        params = dict(task_params or {})
        d = self.resolve_difficulty(params)
        rng = self.rng(seed, params)
        forced = params.get("module")
        n_tests = 8 + round(d * 4)
        n_faults = 1 + round(d * 2)

        last_error = "no viable mutation found"
        for attempt in range(60):
            if forced:
                entry = _CORPUS_BY_NAME[forced]
            else:
                entry = _CORPUS[rng.randrange(len(_CORPUS))]
            exprs = entry.arggen(rng, d, n_tests)
            calls = [{"id": f"t{i}", "expr": e} for i, e in enumerate(exprs)]

            expected_run = run_calls(entry.source, calls, per_call_timeout=1.0)
            if expected_run.status != "ok" or any(
                not expected_run.results.get(c["id"], {}).get("ok") for c in calls
            ):
                last_error = f"clean-source run failed ({expected_run.status})"
                continue
            expected = {
                c["id"]: expected_run.results[c["id"]]["value"] for c in calls
            }

            n_sites = _count_sites(entry.source)
            if n_sites < n_faults:
                continue
            site_ids = rng.sample(range(n_sites), n_faults)
            targets = {
                s: (rng.choice([-1, 1]) if rng.random() < 0.5 else 1)
                for s in site_ids
            }
            buggy = _apply_mutations(entry.source, targets)
            if buggy is None or buggy.strip() == entry.source.strip():
                continue

            buggy_run = run_calls(buggy, calls, per_call_timeout=1.0)
            if buggy_run.status != "ok":
                # import-breaking mutation: too broken to be a repair task
                continue
            fails = 0
            for c in calls:
                rec = buggy_run.results.get(c["id"], {})
                if not rec.get("ok") or not checkers.canonical_equal(
                    rec.get("value"), expected[c["id"]], rel_tol=entry.rel_tol
                ):
                    fails += 1
            fail_frac = fails / len(calls)
            if fail_frac < _MIN_FAIL_FRACTION:
                last_error = f"mutation too subtle (fail_frac={fail_frac:.2f})"
                continue

            norm_params = {"difficulty": d}
            if forced:
                norm_params["module"] = forced
            payload = {
                "corpus_name": entry.name,
                "clean_source": entry.source,
                "buggy_source": buggy,
                "required_functions": list(entry.functions),
                "rel_tol": entry.rel_tol,
                "n_faults": n_faults,
                "buggy_fail_fraction": round(fail_frac, 4),
                "tests": [
                    {"id": c["id"], "expr": c["expr"], "expected": expected[c["id"]]}
                    for c in calls
                ],
            }
            prompt = _PROMPT_TEMPLATE.format(n_faults=n_faults, buggy=buggy.rstrip())
            return Task(
                env=self.name,
                task_id=self.make_task_id(norm_params, seed, payload),
                seed=seed,
                difficulty=d,
                prompt=prompt,
                task_params=norm_params,
                payload=payload,
                metadata={"corpus": entry.name, "n_tests": len(calls),
                          "buggy_fail_fraction": round(fail_frac, 4)},
            )
        raise RuntimeError(
            f"code_repair.generate could not build a task (seed={seed}): {last_error}"
        )

    def verify(self, task: Task, response: str) -> VerifyResult:
        p = task.payload
        source, why = extract_python_module(response if isinstance(response, str) else "")
        if source is None:
            return VerifyResult(0.0, {"error": why})
        reason = static_guard(source, tuple(p["required_functions"]))
        if reason is not None:
            return VerifyResult(0.0, {"error": f"static guard: {reason}"})

        calls = [{"id": t["id"], "expr": t["expr"]} for t in p["tests"]]
        outcome = run_calls(source, calls, per_call_timeout=1.0)
        if outcome.status != "ok":
            return VerifyResult(
                0.0, {"error": f"sandbox: {outcome.status}", "detail": outcome.error}
            )
        rel_tol = float(p.get("rel_tol", 0.0))
        passed, detail = 0, []
        for t in p["tests"]:
            rec = outcome.results.get(t["id"], {})
            ok = bool(rec.get("ok")) and checkers.canonical_equal(
                rec.get("value"), t["expected"], rel_tol=rel_tol
            )
            passed += ok
            if not ok and len(detail) < 5:
                detail.append({
                    "expr": t["expr"][:120],
                    "expected": t["expected"],
                    "got": rec.get("value", rec.get("error", "<missing>")),
                })
        total = len(p["tests"])
        return VerifyResult(
            passed / total if total else 0.0,
            {"passed": passed, "total": total, "failures": detail,
             "corpus": p["corpus_name"]},
        )

    def reference_solution(self, task: Task) -> str:
        return f"```python\n{task.payload['clean_source']}```"

    def corrupted_solution(self, task: Task) -> str:
        # The honest negative control: the buggy module returned unchanged
        # (guaranteed by generation to fail >= 40% of the hidden tests).
        return f"```python\n{task.payload['buggy_source']}```"
