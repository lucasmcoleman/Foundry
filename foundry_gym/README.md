# Foundry Gym

RL environments with **programmatic verifiers** for GRPO/RLVR training in the
[Foundry](../README.md) pipeline. Each environment generates unlimited verified
training signal: rewards are **deterministic functions of the policy model's own
outputs**, computed entirely by code. No frontier-model outputs are stored as
training targets anywhere in this system — this is a ToS-clean, infinitely
refreshable alternative to distillation.

```
policy model  --response-->  env.verify(task, response)  --reward-->  GRPOTrainer
                              (pure code, deterministic)
```

## Quickstart

```python
from foundry_gym import registry

env = registry.get("math_logic")
task = env.generate({"difficulty": 0.6}, seed=7)   # deterministic given (params, seed)
print(task.prompt)                                  # what the policy sees
result = env.verify(task, "... Answer: 42")         # -> VerifyResult
print(result.reward, result.diagnostics)            # float in [0,1], JSON-safe dict
```

Train with TRL GRPO (see `training/grpo_smoke.py` for a runnable end-to-end example):

```python
from foundry_gym.training import build_dataset, gym_reward
from trl import GRPOTrainer, GRPOConfig

ds = build_dataset(["math_logic", "code_repair"], n_per_family=64, seed_base=0)
trainer = GRPOTrainer(model=model, reward_funcs=gym_reward, args=GRPOConfig(...),
                      train_dataset=ds, processing_class=tokenizer)
trainer.train()
```

`build_dataset` puts the serialized `Task` in a `task_json` column; `gym_reward`
is a TRL `reward_funcs` callable `(prompts, completions, task_json=...) -> list[float]`
that reconstructs each task and returns `env.verify(...).reward`. It is stateless
and never raises (a raising reward function would kill a multi-hour run).

## The interface

Every family subclasses `foundry_gym.core.env.Environment` and implements four
methods. The full contract is in that class's docstring; the essentials:

| method | signature | contract |
| --- | --- | --- |
| `generate` | `(task_params: dict | None, seed: int) -> Task` | **Deterministic**: same `(task_params, seed)` → byte-identical `task.to_json()`. All randomness from `self.rng(seed, task_params)`. |
| `verify` | `(task: Task, response: str) -> VerifyResult` | Pure function of `(task, response)`. **Never raises** on any string. `reward` in `[0,1]`. |
| `reference_solution` | `(task) -> str` | A response scoring `>= self.reference_threshold`. Reconstructed from `task.payload` alone (envs are stateless). |
| `corrupted_solution` | `(task) -> str` | A *plausible wrong* response scoring `<= self.corrupted_threshold`. Deterministic. |

`Task` (frozen, JSON-serializable) carries `env`, `task_id` (content hash),
`seed`, `difficulty`, `prompt` (shown to the policy), `task_params`, `payload`
(everything `verify` needs — never shown to the policy), and `metadata`.
`VerifyResult` clamps `reward` to `[0,1]` (NaN → 0) and holds a JSON-safe
`diagnostics` dict.

### Difficulty

`task_params["difficulty"]` is a float in `[0,1]` resolved via
`self.resolve_difficulty(params)`. Each family scales task hardness continuously
from it (more faults, deeper chains, sparser worker capabilities, longer
documents…) so a curriculum can supply a gradient of solvable-but-hard tasks.

## The five families

| name | task | reward |
| --- | --- | --- |
| `math_logic` | arithmetic / modular exponentiation / integer linear systems / propositional model counting / integer sequences | exact integer match of the final `Answer:` (0.1 format shaping) |
| `code_repair` | fix 1–3 AST-injected faults in a self-contained Python module | fraction of **hidden tests** passing, run in a hardened sandbox |
| `tool_orchestration` | emit a JSON plan of MCP-style tool calls (with `$from` data-flow refs) to drive an ops console to a goal | fraction of goal-state **predicates** satisfied after simulation, minus an invalid-step penalty |
| `struct_extract` | extract a schema-conformant JSON object from a synthetic document (invoice / minutes / log summary) | schema-gated, then weighted **field-match** fraction with type-aware normalizers |
| `orchestrator_planning` | write a delegation plan (subtasks → typed workers, dependencies, budget) | outcome predicates over a **simulated** execution: goals produced (provenance-checked) + budget/deadline, conditioned on all goals |

`orchestrator_planning` is the chosen judgment-call family (mission item 2): it
trains decomposition, capability-matching, dependency ordering and parallelism
under a cost budget, while staying airtight because plans are scored by simulated
outcome with typed artifact provenance — never by text similarity, and with no
judge model anywhere. Rationale in `docs/WORKLOG.md`.

## Soundness & anti-reward-hacking

Every verifier was adversarially attacked; exploits and fixes are documented in
`docs/verifier-audits.md`. Two load-bearing defenses:

- **`checkers.canonical`** rebuilds values with `type(x) is T` (never
  `isinstance`), so a candidate returning an object with a permissive `__eq__`
  cannot spoof comparisons; `True` never equals `1`.
- **`core/sandbox.py`** executes untrusted repair code in a subprocess where
  *expected outputs never enter the child* — the child reports actual return
  values, the parent compares. Plus a per-run nonce (forged result lines
  ignored), rlimits (`NPROC=0`, CPU/AS/FSIZE), a socket stub, a scratch cwd, and
  a wall-clock process-group kill.

Reproduce the guarantees:

```bash
cd /server/programming/Foundry
.venv/bin/python -m pytest foundry_gym/ -q                 # 177 tests
.venv/bin/python foundry_gym/scripts/adversarial_audit.py  # 21 attacks, 0 leaks
.venv/bin/python foundry_gym/scripts/demo_soundness.py --n 100   # 0 violations
```

`demo_soundness.py` prints, per family over N tasks, that every reference
solution scores `>= reference_threshold` and every corrupted solution scores
`<= corrupted_threshold` (exit 0 iff zero violations). `generate_samples.py`
writes 100 scored sample tasks/family to `samples/*.jsonl`.

## GRPO smoke train

```bash
# GPU-coordinated (other agents may share the box):
flock /tmp/claude-gpu.lock -c \
  'cd /server/programming/Foundry && .venv/bin/python foundry_gym/training/grpo_smoke.py'
```

Loads a small **safetensors HF checkpoint** (default
`/server/ai/models/source/Qwen2.5-0.5B` — a GGUF or a served endpoint has no
trainable weights and cannot be the policy), builds a mixed-family dataset, and
runs a few LoRA GRPO steps to prove the loop end-to-end. Writes a LoRA adapter +
`smoke_summary.json` to `output/gym_grpo_smoke/`. (Rewards are mostly 0 for the
0.5B base — the tasks are hard by design; this is a plumbing proof, not a
capability run.)

## Layout

```
foundry_gym/
  core/           types.py  env.py  registry.py  checkers.py  sandbox.py
  envs/           math_logic  code_repair  tool_orchestration
                  struct_extract  orchestrator_planning
  training/       reward_adapter.py (build_dataset, gym_reward)  grpo_smoke.py
  scripts/        generate_samples.py  demo_soundness.py  adversarial_audit.py
  tests/          177 tests (generic contract suite + per-family + core)
  docs/           WORKLOG.md  verifier-audits.md
  samples/        <family>.jsonl (100 scored tasks each)  soundness_report.json
```

## Adding a sixth family

1. Create `envs/<name>.py`. Subclass `Environment`, set `name`,
   `reference_threshold`, `corrupted_threshold`, and decorate the class with
   `@register` (from `foundry_gym.core.registry`).
2. Implement the four methods above. Reuse `core/checkers.py` for extraction and
   comparison (`extract_json_response`, `extract_final_answer`, `schema_check`,
   `canonical_equal`, the `normalize_*` helpers) and `core/sandbox.run_calls` if
   you execute candidate code. **Never** compare with a bare `==` where a
   candidate controls a type — go through `canonical_equal`.
3. Draw all randomness from `self.rng(seed, task_params)`; `sorted()` any set/dict
   iteration; keep `payload`/`task_params` JSON-serializable.
4. Add one import line to `envs/__init__.py`.
5. Run `pytest foundry_gym/` — the **generic contract suite**
   (`tests/test_envs_generic.py`) auto-parametrizes over `registry.names()` and
   will immediately test your family for determinism, JSON round-trip,
   soundness (reference vs corrupted), robustness (no raise on garbage), and
   reward bounds. Then `demo_soundness.py --families <name>` and
   `adversarial_audit.py`. No edits to core, tests, or other families needed.
```
