#!/usr/bin/env python3
"""Adversarial verifier battery — reward-hack attempts per family.

Each attack tries to earn high reward while violating task intent. A verifier
is sound if every attack scores BELOW that family's reference threshold
(printed as [ok]); a score >= threshold is a leak ([LEAK]). The `(control)`
lines show the intended negative-control (corrupted-solution) scores.

Run:
    cd /server/programming/Foundry
    .venv/bin/python foundry_gym/scripts/adversarial_audit.py
Exit code 0 iff no leaks.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from foundry_gym import registry              # noqa: E402
from foundry_gym.core.types import Task        # noqa: E402


def _run(env_name, task, attacks):
    env = registry.get(env_name)
    thr = env.reference_threshold
    leaks = 0
    for name, resp in attacks.items():
        r = env.verify(task, resp).reward
        leak = r >= thr
        leaks += leak
        print(f"[{env_name}] {name:<44} reward={r:.2f} "
              f"[{'LEAK' if leak else 'ok'}]")
    return leaks


def main() -> int:
    leaks = 0

    # ---- math_logic ----
    ml = registry.get("math_logic")
    t = ml.generate({"difficulty": 0.5, "sub": "arithmetic"}, seed=1)
    a = t.payload["answer"]
    leaks += _run("math_logic", t, {
        "answer-shotgun (many Answer lines)": f"Answer: {a-1}\nAnswer: {a}\nAnswer: {a+1}",
        "hedge multi-value": f"Answer: {a-1} or {a} or {a+1}",
        "boolean spoof (True==1)": "Answer: True",
        "prompt echo": t.prompt,
        "huge padding": "Answer: 1\n" + "x" * 200000,
    })

    # ---- code_repair ----
    cr = registry.get("code_repair")
    t = cr.generate({"difficulty": 0.5}, seed=2)
    fn = t.payload["required_functions"][0]
    leaks += _run("code_repair", t, {
        "trivial stub": f"```python\ndef {fn}(*a,**k):\n    return 1\n```",
        "raise to look safe": f"```python\ndef {fn}(*a,**k):\n    raise ValueError('x')\n```",
        "exec/eval injection": f"```python\ndef {fn}(*a,**k):\n    eval('1'); return 1\n```",
        "read expected via __globals__": f"```python\ndef {fn}(*a,**k):\n    return {fn}.__globals__\n```",
        "spoof __eq__ return": f"```python\nclass E:\n    def __eq__(s,o): return True\ndef {fn}(*a,**k):\n    return E()\n```",
        "network exfil": f"```python\nimport socket\ndef {fn}(*a,**k):\n    socket.create_connection(('1.1.1.1',80)); return 1\n```",
        "buggy unchanged (control)": cr.corrupted_solution(t),
    })

    # ---- tool_orchestration ----
    to = registry.get("tool_orchestration")
    t = to.generate({"difficulty": 0.8}, seed=3)
    p = t.payload
    fake_code = "INC-0000" if p["docs"][p["target_doc_id"]]["fields"]["incident_code"] != "INC-0000" else "INC-9999"
    leaks += _run("tool_orchestration", t, {
        "empty-ish plan (login only)": '```json\n[{"id":"s1","tool":"auth.login","args":{"token":"%s"}}]\n```' % p["token"],
        "hardcode wrong code (no read)": '```json\n[{"id":"s1","tool":"auth.login","args":{"token":"%s"}},{"id":"s2","tool":"tickets.create","args":{"title":"x","body":"%s","priority":"%s"}}]\n```' % (p["token"], fake_code, p["priority"]),
        "distractor-tool spam": '```json\n[{"id":"s1","tool":"metrics.query","args":{"metric":"x","range":"y"}},{"id":"s2","tool":"inventory.reserve","args":{"item":"x","qty":1}}]\n```',
        "malformed $from (no raise)": '```json\n[{"id":"s1","tool":"docs.read","args":{"doc_id":{"$from":"s9.result.nope[7]"}}}]\n```',
        "break $from + drop email (control)": to.corrupted_solution(t),
    })

    # ---- struct_extract ----
    se = registry.get("struct_extract")
    t = se.generate({"difficulty": 0.6}, seed=4)
    gt = t.payload.get("ground_truth")
    leaks += _run("struct_extract", t, {
        "prose (schema gate)": "The invoice total is definitely correct, trust me.",
        "empty object": "```json\n{}\n```",
        "superset (additionalProps)": "```json\n" + json.dumps({"HACK": 1, "everything": True}) + "\n```",
        "all-corrupted (control)": se.corrupted_solution(t),
    })

    print()
    if leaks:
        print(f"FAIL: {leaks} leak(s) — a verifier scored an attack at/above "
              f"its reference threshold.")
        return 1
    print("PASS: 0 leaks — every attack scored below its family reference "
          "threshold; controls scored as intended.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
