"""math_logic — math/logic tasks with exact reference checkers.

Sub-families (chosen so ground truth is computed programmatically, never
approximated): arithmetic expression trees, modular exponentiation, integer
linear systems, propositional model counting, integer sequences.

Verification: the response must end with a single ``Answer: <value>`` line
(last marker wins; ambiguous multi-value markers are rejected). Exact integer
comparison; a wrong-but-well-formed answer earns only the 0.1 format shaping.
"""

from __future__ import annotations

import random
from typing import Optional

from ..core.env import Environment
from ..core.registry import register
from ..core.types import Task, VerifyResult
from ..core import checkers

_INSTRUCTION = (
    "\n\nSolve the problem step by step. End your response with a final line of "
    "the form `Answer: <value>` where <value> is a single integer and nothing else."
)


def _expr_tree(rng: random.Random, depth: int, mag: int) -> tuple:
    """Build (expr_string, value) for an integer arithmetic expression."""
    if depth <= 0:
        v = rng.randint(-mag, mag)
        return (str(v) if v >= 0 else f"({v})"), v
    op = rng.choice(["+", "-", "*", "//", "%"])
    ls, lv = _expr_tree(rng, depth - 1, mag)
    if op in ("//", "%"):
        # positive divisor, modest magnitude, never zero
        dv = rng.randint(2, max(3, mag // 10 or 3))
        rs = str(dv)
        value = lv // dv if op == "//" else lv % dv
        return f"({ls} {op} {rs})", value
    rs, rv = _expr_tree(rng, depth - 1, mag if op != "*" else max(2, mag // 20))
    if op == "*":
        # keep magnitudes sane: shrink one side
        ls2, lv2 = _expr_tree(rng, depth - 1, max(2, mag // 20))
        return f"({ls2} * {rs})", lv2 * rv
    value = lv + rv if op == "+" else lv - rv
    return f"({ls} {op} {rs})", value


def _gen_arithmetic(rng: random.Random, d: float) -> dict:
    depth = 2 + round(d * 3)
    mag = int(10 ** (1 + 2 * d))
    expr, value = _expr_tree(rng, depth, mag)
    problem = (
        "Evaluate the following integer expression exactly. `//` is integer floor "
        f"division and `%` is the (Python-style) modulo:\n\n    {expr}"
    )
    return {"problem": problem, "answer": value, "sub": "arithmetic"}


def _gen_modpow(rng: random.Random, d: float) -> dict:
    a = rng.randint(2, int(10 ** (1 + 2 * d)) + 5)
    b = rng.randint(2, int(10 ** (1 + 3 * d)) + 5)
    m = rng.randint(3, int(10 ** (2 + 2 * d)) + 7)
    problem = f"Compute ({a} ** {b}) mod {m}, i.e. {a} raised to the power {b}, modulo {m}."
    return {"problem": problem, "answer": pow(a, b, m), "sub": "modpow"}


def _gen_linear_system(rng: random.Random, d: float) -> dict:
    n = 2 if d < 0.5 else 3
    lo = -(3 + int(d * 9))
    hi = 3 + int(d * 9)
    sol = [rng.randint(lo, hi) for _ in range(n)]
    names = ["x", "y", "z"][:n]
    # build n independent equations (retry coefficient draws until nonsingular)
    while True:
        rows = [[rng.randint(-6, 6) for _ in range(n)] for _ in range(n)]
        det = _det(rows)
        if det != 0:
            break
    eqs = []
    for row in rows:
        rhs = sum(c * s for c, s in zip(row, sol))
        terms = []
        for c, v in zip(row, names):
            if c == 0:
                continue
            sign = "+ " if c > 0 and terms else ("- " if c < 0 and terms else ("-" if c < 0 else ""))
            mag = abs(c)
            terms.append(f"{sign}{'' if mag == 1 else mag}{v}")
        if not terms:
            terms = ["0"]
        eqs.append(" ".join(terms) + f" = {rhs}")
    target = rng.randrange(n)
    problem = (
        "Solve this system of linear equations (it has a unique integer solution):\n\n    "
        + "\n    ".join(eqs)
        + f"\n\nWhat is the value of {names[target]}?"
    )
    return {"problem": problem, "answer": sol[target], "sub": "linear_system"}


def _det(m: list) -> int:
    n = len(m)
    if n == 2:
        return m[0][0] * m[1][1] - m[0][1] * m[1][0]
    return (
        m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
        - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
        + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
    )


def _gen_logic_count(rng: random.Random, d: float) -> dict:
    nvars = 3 + round(d * 3)
    names = [f"p{i+1}" for i in range(nvars)]

    def formula(depth: int):
        if depth <= 0 or (depth < 2 and rng.random() < 0.4):
            v = rng.randrange(nvars)
            if rng.random() < 0.3:
                return (f"NOT {names[v]}", lambda a, v=v: not a[v])
            return (names[v], lambda a, v=v: a[v])
        op = rng.choice(["AND", "OR", "IMPLIES"])
        ls, lf = formula(depth - 1)
        rs, rf = formula(depth - 1)
        if op == "AND":
            return (f"({ls} AND {rs})", lambda a: lf(a) and rf(a))
        if op == "OR":
            return (f"({ls} OR {rs})", lambda a: lf(a) or rf(a))
        return (f"({ls} IMPLIES {rs})", lambda a: (not lf(a)) or rf(a))

    for _ in range(50):
        text, fn = formula(2 + round(d * 2))
        count = 0
        for bits in range(2 ** nvars):
            assign = [(bits >> i) & 1 == 1 for i in range(nvars)]
            if fn(assign):
                count += 1
        if 0 < count < 2 ** nvars:  # skip tautologies/contradictions (trivial)
            break
    problem = (
        f"Consider boolean variables {', '.join(names)}. How many of the "
        f"{2 ** nvars} truth assignments satisfy the formula below? "
        "(IMPLIES is material implication.)\n\n    " + text
    )
    return {"problem": problem, "answer": count, "sub": "logic_count"}


def _gen_sequence(rng: random.Random, d: float) -> dict:
    kind = rng.choice(["poly", "geo"]) if d >= 0.4 else "poly1"
    if kind == "geo":
        a0 = rng.randint(1, 6)
        r = rng.randint(2, 4)
        terms = [a0 * r ** i for i in range(5)]
        answer = a0 * r ** 5
    else:
        deg = 1 if kind == "poly1" else 2
        coeffs = [rng.randint(-5, 5) for _ in range(deg + 1)]
        if coeffs[-1] == 0:
            coeffs[-1] = rng.randint(1, 5)
        poly = lambda x: sum(c * x ** i for i, c in enumerate(coeffs))
        terms = [poly(i) for i in range(1, 6)]
        answer = poly(6)
    problem = (
        "The following integer sequence follows a simple closed-form rule "
        "(polynomial of degree at most 2, or geometric). What is the next term?\n\n    "
        + ", ".join(str(t) for t in terms) + ", ?"
    )
    return {"problem": problem, "answer": answer, "sub": "sequence"}


_SUBGENS = {
    "arithmetic": _gen_arithmetic,
    "modpow": _gen_modpow,
    "linear_system": _gen_linear_system,
    "logic_count": _gen_logic_count,
    "sequence": _gen_sequence,
}


@register
class MathLogicEnv(Environment):
    name = "math_logic"
    reference_threshold = 0.95
    corrupted_threshold = 0.15

    def generate(self, task_params: Optional[dict] = None, seed: int = 0) -> Task:
        params = dict(task_params or {})
        d = self.resolve_difficulty(params)
        rng = self.rng(seed, params)
        sub = params.get("sub") or rng.choice(sorted(_SUBGENS))
        gen = _SUBGENS[sub]
        spec = gen(rng, d)
        norm_params = {"difficulty": d, "sub": sub}
        payload = {"answer": int(spec["answer"]), "sub": spec["sub"]}
        prompt = spec["problem"] + _INSTRUCTION
        return Task(
            env=self.name,
            task_id=self.make_task_id(norm_params, seed, payload),
            seed=seed,
            difficulty=d,
            prompt=prompt,
            task_params=norm_params,
            payload=payload,
            metadata={"sub": spec["sub"]},
        )

    def verify(self, task: Task, response: str) -> VerifyResult:
        expected = int(task.payload["answer"])
        token, why = checkers.extract_final_answer(response)
        if token is None:
            return VerifyResult(0.0, {"error": why, "expected_format": "Answer: <int>"})
        # Reject multi-value answers ("Answer: 3 or 5"): must parse as one number.
        value = checkers.parse_number(token)
        if value is None:
            return VerifyResult(0.0, {"error": f"unparseable answer token: {token[:80]!r}"})
        if value != int(value):
            return VerifyResult(0.1, {"correct": False, "got": value, "note": "non-integer"})
        correct = int(value) == expected
        return VerifyResult(
            1.0 if correct else 0.1,
            {"correct": correct, "got": int(value), "sub": task.payload.get("sub")},
        )

    def reference_solution(self, task: Task) -> str:
        return f"Working through it carefully.\n\nAnswer: {task.payload['answer']}"

    def corrupted_solution(self, task: Task) -> str:
        # plausible near-miss: off-by-one (deterministic direction from task_id)
        delta = 1 if int(task.task_id[:2], 16) % 2 == 0 else -1
        wrong = int(task.payload["answer"]) + delta
        return f"Working through it carefully.\n\nAnswer: {wrong}"
