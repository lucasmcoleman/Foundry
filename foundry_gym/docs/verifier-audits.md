# Verifier Adversarial Audits

Mission item 4: after building each verifier, adversarially attack it — generate
responses that maximize reward while violating intent, patch the verifier, and
document every exploit. The guiding principle: **a reward function is a security
boundary.** A policy under GRPO will find any gap between "scores well" and "is
correct," and that gap becomes the trained behavior.

Every exploit below has a regression test (see `tests/` — `test_sandbox.py`,
`test_checkers.py`, and the generic robustness cases in `test_envs_generic.py`)
and/or is demonstrated by the adversarial battery reproduced at the end of this
file. Status legend: **PATCHED** = fixed and regression-tested; **BY DESIGN** =
structurally impossible given the architecture, retained here as a documented
guarantee; **RESIDUAL** = accepted risk with rationale.

---

## Cross-cutting defenses (core/)

These live in `core/checkers.py` and `core/sandbox.py` because more than one
family relies on them.

### X-1. `__eq__` / type-coercion spoofing — PATCHED
**Attack.** Return an object whose `__eq__` always returns `True` (or exploit
`True == 1`, `1 == 1.0` in a context that wants a specific type) so any
comparison the verifier makes passes.
**Where it bit.** Any verifier comparing extracted values to ground truth with
`==`.
**Fix.** `checkers.canonical()` rebuilds a value using `type(x) is T` (never
`isinstance`), so subclasses — the vector for overriding `__eq__` — are rejected
with `CanonicalError`; comparisons go through `canonical_equal`, which also keeps
`bool` from cross-comparing with `int`/`float` (`True` != `1`). The sandbox
runner canonicalizes candidate return values **child-side** with the same rules,
so a spoof object can't even cross the process boundary — it becomes an
`ok: False` error record.
**Regression.** `test_checkers.py::test_canonical_*`, `test_sandbox.py` spoof case,
and the live battery (`code_repair` "spoof `__eq__`" → 0.00).

### X-2. Answer-shotgunning — PATCHED
**Attack.** Emit many candidate answers (`Answer: 3` / `Answer: 4` / `Answer: 5`,
or several JSON objects) so at least one matches and the verifier "helpfully"
picks the matching one.
**Fix.** `extract_final_answer` takes only the **last** answer marker (models
restate their final answer last); `extract_json_response` uses the last *fenced*
JSON block, and when reading unfenced prose it **refuses** (returns
`ambiguous`) if more than one top-level JSON value parses. Ambiguity earns
nothing.
**Regression.** `test_checkers.py::test_extract_json_response_ambiguous`,
`test_math_logic.py` multi-answer case, battery (`math_logic` shotgun → 0.10,
i.e. scored on the wrong last line).

### X-3. Hedged / multi-value answers — PATCHED
**Attack.** `Answer: 4 or 5` to cover a near-miss.
**Fix.** `parse_number` requires a single numeric token; `math_logic` scores a
hedge as unparseable → 0.0.

### X-4. Resource exhaustion (DoS as reward-hack) — PATCHED
**Attack.** Return a 5 MB answer / infinite loop / fork bomb / giant int so the
verifier hangs or OOMs and the trainer skips or mis-scores the sample.
**Fix.** All extractors enforce `max_len`. The sandbox sets `RLIMIT_CPU`,
`RLIMIT_AS`, `RLIMIT_FSIZE`, `RLIMIT_NOFILE`, and `RLIMIT_NPROC=0` (fork fails),
a per-call `SIGALRM`, a parent-side wall-clock kill of the whole process group,
and a 2 MB cap on the result stream. Giant ints are rejected child-side
(`abs > 10**60`).
**Regression.** `test_sandbox.py` (import-timeout, per-call-timeout),
battery robustness (`"a"*100000` → 0.00 everywhere).

### X-5. Forged sandbox result — BY DESIGN
**Attack.** Candidate writes a fake `{"status":"ok","results":[...]}` line to the
result fd to report passing values it never computed.
**Why it fails.** The parent generates a fresh 128-bit `nonce` per run and only
trusts result lines prefixed with it. The nonce is delivered through a pipe the
runner drains *before* importing the candidate, and candidate stdout/stderr are
redirected to `/dev/null`. A candidate cannot learn the nonce, so forged lines
are ignored.
**Regression.** `test_sandbox.py` forged-fd-2 case (returns the real wrong value
4, not the forged 5); battery.

### X-6. Reading expected outputs from the verifier — BY DESIGN
**Attack.** From inside the sandbox, walk `__globals__` / `sys._getframe` /
`gc.get_objects()` to read the hidden expected outputs and echo them.
**Why it fails.** **Expected outputs are never sent to the child.** The child
reports the *actual* return value of each call; the parent compares. The most a
candidate can introspect is the test **inputs** — and computing correct outputs
from inputs is just solving the task.
**Regression.** battery (`code_repair` "read expected via `__globals__`" → 0.00:
the returned dict fails canonicalization).

### SBX-1. Candidate file writes into the caller's working tree — PATCHED
**Attack.** Candidate opens `../../core/env.py` (or any repo file) for write, or
litters the cwd. `RLIMIT_FSIZE` caps size but not *location*.
**Fix.** The child runs with `cwd=` a fresh `tempfile.mkdtemp()` scratch dir that
is `shutil.rmtree`'d on every exit path (wrapped in try/finally). Relative writes
land in throwaway space; the repo tree is untouched.
**Regression.** live check in this audit: a candidate writing `pwned.txt` leaves
no file in the repo cwd and the scratch dir is cleaned. (Absolute-path writes are
still bounded by filesystem permissions — see RESIDUAL below.)

### X-7. Network side-channel — PATCHED (best-effort)
**Attack.** Exfiltrate inputs / fetch an oracle over the network mid-verify.
**Fix.** `socket.socket`, `create_connection`, and `getaddrinfo` are stubbed to
raise before the candidate imports. Combined with X-6 (nothing secret to
exfiltrate) this closes the reward-hack.
**Residual.** A determined `ctypes` syscall bypass is possible; documented under
RESIDUAL. Not reachable through the pure-Python surface a policy model emits.

---

## Family: math_logic

### ML-1. Format-shaping farm — PATCHED
**Attack.** Emit only `Answer: 0` on every task to farm the 0.1 format-credit.
**Analysis.** Intended: format credit (0.1) is deliberately << outcome credit
(1.0) and identical across right/wrong, so GRPO's group-relative advantage for a
correct-vs-incorrect pair is 0.9. A constant 0.1 gives zero advantage signal, so
it can't be reinforced. Kept at 0.1.
**Regression.** `test_math_logic.py` (wrong-but-formatted → 0.1, correct → 1.0).

### ML-2. Boolean/point spoof — PATCHED (via X-1).
`Answer: True` on a task whose answer is 1 → 0.0 (bool never equals int here).

---

## Family: code_repair

### CR-1. Bypass hidden tests by redefining behavior trivially — PATCHED
**Attack.** Return a stub (`def f(*a, **k): return 1`) hoping partial-credit
lands high, or raise to look "safe."
**Analysis.** Reward is strictly the fraction of hidden tests whose canonical
return value matches — a stub passes only tests that happen to expect its
constant. With 8–12 diverse generated tests this is near zero. Raising → all
tests error → 0.0.
**Regression.** battery ("hardcode" → 0.00), soundness (corrupted buggy module
≤ 0.6 by construction).

### CR-2. Static-guard evasion — PATCHED
**Attack.** `import os; os.system(...)`, `eval`, dunder attribute walking to reach
`__builtins__`/`__globals__`.
**Fix.** `static_guard` runs before execution: import allowlist (stdlib compute
modules only), banned identifiers (`eval`/`exec`/`open`/`getattr`/…), and a ban
on `__dunder__` attribute access. Rejection → 0.0. (Execution is *also* sandboxed;
the guard is defense-in-depth and gives a clean diagnostic.)
**Regression.** battery ("exec/eval injection" and "`__globals__`" → 0.00),
`test_envs_generic.py` robustness.

### CR-3. Negative-control soundness — PATCHED (generation-time invariant)
The corrupted solution is the *actual injected-bug module*, and `generate()`
**rejects any mutation that fails < 40 % of the hidden tests**, so every emitted
task has a corrupted score ≤ 0.6 (`corrupted_threshold`). This makes the
negative control a property of the data, not a hope. Verified over the 100-task
soundness sweep (zero violations).

---

## Family: tool_orchestration  (built by Sonnet lane A to my spec; audited by me)

### TO-1. Hardcode the incident code without the read hop — PATCHED (generation invariant)
**Attack.** Skip `docs.read` and put a literal `INC-####` straight into the
ticket.
**Fix.** The generator guarantees the incident code appears **only** in the
document body/fields, never in the prompt. A plan that doesn't actually read the
doc has no way to name the correct code, so the `ticket_with_code` predicate
fails. (Enforced in the generator; asserted in the family smoke check.)

### TO-2. Predicate pre-satisfaction — PATCHED (generation invariant)
**Attack.** Hope the goal predicates are already true in the initial state.
**Fix.** Initial state has no tickets/emails/kv by construction, so zero
predicates hold at t=0; all credit requires real state transitions.

### TO-3. Invalid-step farming — PATCHED
**Attack.** Emit dozens of junk/distractor steps around a partial solution.
**Fix.** Reward subtracts `0.05·min(invalid_steps, 4)` and is floored at 0;
credit is predicate-driven, so padding can only lose points.

### TO-4. `$from` path traversal / malformed refs — PATCHED
Bad data-flow references resolve to an invalid step (no effect), never an
exception; `verify` never raises on malformed plans (generic robustness test).

---

## Family: struct_extract  (built by Sonnet lane A to my spec; audited by me)

### SE-1. Schema-gate bypass — PATCHED
**Attack.** Dump prose or a superset object hoping field matches are counted.
**Fix.** `schema_check` is a hard gate: any schema error → 0.0 before field
scoring, and `additionalProperties: false` blocks kitchen-sink objects.

### SE-2. Distractor capture — PATCHED (generation invariant)
Every document embeds distractors (PO number, previous balance, absent-list,
`payments-worker` vs `payments-api`, WARN vs ERROR). Ground truth excludes them;
extracting a distractor scores that field 0. This is what forces genuine reading.

### SE-3. Type-coercion credit — PATCHED (via X-1)
`"3"` for an integer field, `True` for a count → no credit (`canonical_equal`
is type-exact for int/text; money/date/number go through explicit normalizers).

---

## Family: orchestrator_planning

### OP-1. Budget/deadline farming with an empty plan — PATCHED
**Attack.** Submit `{"subtasks": []}`: zero cost ≤ budget and zero makespan ≤
deadline, farming the 0.30 of reward tied to those constraints.
**Fix.** The budget and deadline reward terms are **conditioned on all goals
being produced** (`budget_ok = all_goals and cost <= budget`). No goals → no
constraint credit. An empty plan scores exactly 0.0.
**Regression.** battery ("empty plan" → 0.00), `test_envs_generic.py` (`{}`/`[]`
robustness).

### OP-2. Phantom artifacts / capability bypass — PATCHED (simulator invariant)
**Attack.** Claim a goal-typed `output` from a subtask with fabricated inputs, a
worker that lacks the capability, or the free "intern."
**Fix.** The simulator only materializes an artifact when the worker **has** the
action capability, the recipe input **types** match exactly, and every input
artifact was really produced upstream. Goal credit counts only artifact **types
that exist in the simulated end state** — provenance can't be faked. The
all-intern corrupted control executes zero subtasks → 0.0.
**Regression.** battery ("phantom artifact", "all-intern" → 0.00).

### OP-3. ID collisions to double-count / overwrite — PATCHED
**Attack.** Reuse an initial artifact id as a subtask `output`, or emit duplicate
output ids, to alias a goal artifact into existence.
**Fix.** `verify` rejects (0.0) plans whose subtask ids or output ids are not
unique, or whose outputs collide with initial artifact ids, before simulation.
**Regression.** battery ("reuse initial id", "duplicate output ids" → 0.00).

### OP-4. Cyclic dependencies / unsatisfiable steps — PATCHED
The simulator runs bounded rounds; steps whose inputs never become available are
recorded as failed rather than executed, and never produce artifacts. No hang, no
credit.

---

## RESIDUAL risks (accepted, with rationale)

1. **ctypes/syscall sandbox escape.** The sandbox targets a reward-hacking policy
   emitting Python, not weaponized malware. A hand-crafted `ctypes` payload could
   bypass the socket stub or issue raw syscalls. Rationale for acceptance: (a) the
   policy models here don't emit such payloads; (b) X-6 means there is nothing
   secret in the child to steal; (c) the correct next hardening step is an OS-level
   jail (seccomp/nsjail/container), noted as a follow-up for a hostile-input
   deployment, out of scope for a training-signal generator on a trusted box.
2. **Absolute-path writes.** SBX-1 redirects *relative* writes to scratch;
   `RLIMIT_FSIZE` caps size, but an absolute path the process user can write is
   still writable. Same rationale as (1); a container is the real fix.
3. **`math_logic` linear-system / sequence uniqueness.** Generators guarantee a
   unique integer solution / a degree-≤2-or-geometric rule, but a response could
   in principle satisfy an unintended alternative rule for a sequence. Mitigated by
   drawing the answer from the generator's own closed form; residual probability is
   negligible and any hit would score as "wrong," never as a false pass.

---

## Reproducing the battery

```
cd /server/programming/Foundry
.venv/bin/python foundry_gym/scripts/adversarial_audit.py
```

Every line must read `[ok]` (no `[LEAK]`). Last run: all 16 attacks across the
five families blocked (rewards 0.00–0.10, none ≥ the family's reference
threshold); the two `(control)` lines show the intended negative-control scores.
