"""orchestrator_planning — delegation plans scored by simulated outcome.

The judgment-call fifth family (mission item 2: "one family you judge
highest-value for training orchestrator behavior"). Justification:

- What it trains: exactly the orchestrator skills that matter for a
  maestro-style stack — goal decomposition into typed subtasks, matching
  subtask -> worker *capability* (not vibes), dependency ordering / data flow,
  parallelism under worker-serial execution (assignment changes makespan), and
  respecting cost budgets. These are the behaviors delegation-heavy agent
  systems live or die by.
- Why it is airtight: the plan is *executed* by a deterministic simulator with
  typed artifact provenance — an artifact of the goal type can only exist if a
  capability-valid, dependency-valid chain actually produced it. Scoring is
  outcome predicates over the simulated end state, never text similarity, and
  no judge model appears anywhere.
- Rejected alternatives: multi-turn agent dialogues (not verifiable without a
  judge), plan-text rubric scoring (gameable), single tool calls (already
  covered by tool_orchestration).

Task: the prompt lists workers (capabilities, cost, duration), action recipes
(input types -> output type), initial artifacts, goal artifact types, a cost
budget, and a deadline. The policy answers with a JSON plan; the simulator
executes it worker-serially and scores:

    reward = 0.6 * goal_types_produced_fraction
           + 0.1 * executed_subtasks_fraction
           + 0.15 * [all goals AND total cost <= budget]
           + 0.15 * [all goals AND makespan <= deadline]

Budget/deadline components are conditioned on all goals being produced so an
empty plan cannot farm them (see docs/verifier-audits.md).
"""

from __future__ import annotations

import json
import random
from typing import Optional

from ..core.env import Environment
from ..core.registry import register
from ..core.types import Task, VerifyResult
from ..core import checkers

_MAX_SUBTASKS = 30

_PLAN_SCHEMA = {
    "type": "object",
    "required": ["subtasks"],
    "additionalProperties": False,
    "properties": {
        "subtasks": {
            "type": "array",
            "maxItems": _MAX_SUBTASKS,
            "items": {
                "type": "object",
                "required": ["id", "worker", "action", "inputs", "output"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "worker": {"type": "string"},
                    "action": {"type": "string"},
                    "inputs": {"type": "array", "items": {"type": "string"},
                               "maxItems": 8},
                    "output": {"type": "string"},
                },
            },
        }
    },
}

# themed name banks (indexed deterministically, never iterated as sets)
_CHAIN_THEMES = [
    ("data", ["raw_data", "clean_data", "analysis", "figures", "data_report"],
     ["ingest", "clean", "analyze", "visualize", "compile"]),
    ("docs", ["source_docs", "doc_index", "doc_notes", "doc_summary", "brief"],
     ["collect", "index", "annotate", "summarize", "brief_up"]),
    ("code", ["spec", "prototype", "reviewed_code", "test_results", "release"],
     ["draft_code", "review_code", "run_tests", "package", "ship"]),
]


@register
class OrchestratorPlanningEnv(Environment):
    name = "orchestrator_planning"
    reference_threshold = 0.95
    corrupted_threshold = 0.2

    # -- generation ---------------------------------------------------------

    def generate(self, task_params: Optional[dict] = None, seed: int = 0) -> Task:
        params = dict(task_params or {})
        d = self.resolve_difficulty(params)
        rng = self.rng(seed, params)

        n_goals = 1 + (1 if d > 0.55 else 0) + (1 if d > 0.85 else 0)
        depth = 2 + round(d * 3)          # actions per goal chain
        theme_ids = rng.sample(range(len(_CHAIN_THEMES)), n_goals)

        recipes = {}       # action -> {"inputs": [types], "output": type,
                           #             "duration_key": action}
        initial = []       # [{"id", "type"}]
        goals = []         # goal artifact types
        chains = []        # per goal: ordered list of action names
        for g, ti in enumerate(theme_ids):
            theme, types, verbs = _CHAIN_THEMES[ti]
            depth_g = min(depth, len(verbs), len(types) - 1)
            t_names = [f"{types[i]}_{g + 1}" for i in range(depth_g + 1)]
            a_names = [f"{verbs[i]}_{g + 1}" for i in range(depth_g)]
            initial.append({"id": f"A{g + 1}", "type": t_names[0]})
            chain = []
            for i, act in enumerate(a_names):
                recipes[act] = {"inputs": [t_names[i]], "output": t_names[i + 1]}
                chain.append(act)
            chains.append(chain)
            goals.append(t_names[depth_g])

        # cross-chain dependency: the last action of chain 2 also needs a
        # mid-chain artifact from chain 1 (forces coordination at high d)
        if n_goals >= 2 and d > 0.7 and len(chains[0]) >= 2:
            final_act = chains[1][-1]
            mid_type = recipes[chains[0][len(chains[0]) // 2 - 1]]["output"] \
                if len(chains[0]) >= 2 else None
            if mid_type:
                recipes[final_act]["inputs"] = sorted(
                    recipes[final_act]["inputs"] + [mid_type]
                )

        # distractor recipe (never needed for any goal)
        recipes[f"audit_{n_goals + 1}"] = {
            "inputs": [initial[0]["type"]], "output": f"audit_log_{n_goals + 1}"
        }

        # workers: every action gets >=1 capable worker; sparsity rises with d
        all_actions = sorted(recipes.keys())
        n_workers = 3 + round(d * 3)
        worker_ids = [f"w{i + 1}" for i in range(n_workers)]
        capabilities = {w: [] for w in worker_ids}
        for i, act in enumerate(all_actions):
            capabilities[worker_ids[i % n_workers]].append(act)
            if rng.random() < (0.6 - 0.4 * d):  # second capable worker
                other = worker_ids[(i + 1 + rng.randrange(n_workers - 1)) % n_workers]
                if act not in capabilities[other]:
                    capabilities[other].append(act)
        costs = {w: rng.randint(3, 9) for w in worker_ids}
        durations = {w: rng.randint(1, 5) for w in worker_ids}
        # the trap: a dirt-cheap "intern" with no useful capabilities
        capabilities["intern"] = []
        costs["intern"] = 1
        durations["intern"] = 1
        worker_ids = worker_ids + ["intern"]

        world = {
            "recipes": {a: recipes[a] for a in sorted(recipes)},
            "workers": {
                w: {"capabilities": sorted(capabilities[w]),
                    "cost": costs[w], "duration": durations[w]}
                for w in sorted(worker_ids)
            },
            "initial": sorted(initial, key=lambda x: x["id"]),
            "goals": sorted(goals),
        }

        # reference plan: per action (topological per chain), cheapest capable
        ref_subtasks = []
        produced = {a["type"]: a["id"] for a in initial}
        k = 0
        remaining = [act for chain in chains for act in chain]
        while remaining:
            progressed = False
            for act in list(remaining):
                need = recipes[act]["inputs"]
                if all(t in produced for t in need):
                    capable = sorted(
                        (w for w in worker_ids if act in capabilities.get(w, [])),
                        key=lambda w: (costs[w], w),
                    )
                    w = capable[0]
                    k += 1
                    out_id = f"out{k}"
                    ref_subtasks.append({
                        "id": f"s{k}", "worker": w, "action": act,
                        "inputs": sorted(produced[t] for t in need),
                        "output": out_id,
                    })
                    produced[recipes[act]["output"]] = out_id
                    remaining.remove(act)
                    progressed = True
            if not progressed:  # pragma: no cover — chains are constructible
                raise RuntimeError("reference plan construction stalled")
        ref_plan = {"subtasks": ref_subtasks}

        # budget/deadline from the reference outcome, slack shrinks with d
        sim = _simulate(ref_plan, world)
        assert sim["goals_done"] == len(goals)
        slack = 1.0 - 0.85 * d
        budget = int(sim["cost"] * (1 + slack)) + 1
        deadline = int(sim["makespan"] * (1 + slack)) + 1
        world["budget"] = budget
        world["deadline"] = deadline

        norm_params = {"difficulty": d}
        payload = {"world": world, "reference_plan": ref_plan}
        prompt = _render_prompt(world)
        return Task(
            env=self.name,
            task_id=self.make_task_id(norm_params, seed, payload),
            seed=seed,
            difficulty=d,
            prompt=prompt,
            task_params=norm_params,
            payload=payload,
            metadata={"n_goals": len(goals), "n_actions": len(all_actions),
                      "ref_cost": sim["cost"], "ref_makespan": sim["makespan"],
                      "budget": budget, "deadline": deadline},
        )

    # -- verification --------------------------------------------------------

    def verify(self, task: Task, response: str) -> VerifyResult:
        world = task.payload["world"]
        value, why = checkers.extract_json_response(response)
        if value is None:
            return VerifyResult(0.0, {"error": why})
        errors = checkers.schema_check(value, _PLAN_SCHEMA)
        if errors:
            return VerifyResult(0.0, {"error": "schema", "detail": errors[:5]})
        subtasks = value["subtasks"]
        ids = [s["id"] for s in subtasks]
        if len(set(ids)) != len(ids):
            return VerifyResult(0.0, {"error": "duplicate subtask ids"})
        outputs = [s["output"] for s in subtasks]
        initial_ids = {a["id"] for a in world["initial"]}
        if len(set(outputs)) != len(outputs) or set(outputs) & initial_ids:
            return VerifyResult(0.0, {"error": "output ids must be unique and "
                                               "distinct from initial artifacts"})

        sim = _simulate(value, world)
        n_goals = len(world["goals"])
        goals_frac = sim["goals_done"] / n_goals if n_goals else 0.0
        total = len(subtasks)
        valid_frac = (sim["executed"] / total) if total else 0.0
        all_goals = sim["goals_done"] == n_goals and n_goals > 0
        budget_ok = all_goals and sim["cost"] <= world["budget"]
        deadline_ok = all_goals and sim["makespan"] <= world["deadline"]
        reward = (0.6 * goals_frac + 0.1 * valid_frac
                  + (0.15 if budget_ok else 0.0)
                  + (0.15 if deadline_ok else 0.0))
        return VerifyResult(reward, {
            "goals_done": sim["goals_done"], "n_goals": n_goals,
            "executed": sim["executed"], "planned": total,
            "failed_steps": sim["failed"][:6],
            "cost": sim["cost"], "budget": world["budget"],
            "makespan": sim["makespan"], "deadline": world["deadline"],
            "budget_ok": budget_ok, "deadline_ok": deadline_ok,
        })

    # -- controls -------------------------------------------------------------

    def reference_solution(self, task: Task) -> str:
        return "```json\n" + json.dumps(task.payload["reference_plan"], indent=1) + "\n```"

    def corrupted_solution(self, task: Task) -> str:
        # The classic naive delegation mistake: assign everything to the
        # cheapest worker (the capability-free intern). Plausible, parses,
        # schema-valid — and the simulator executes none of it.
        plan = json.loads(json.dumps(task.payload["reference_plan"]))
        for s in plan["subtasks"]:
            s["worker"] = "intern"
        return "```json\n" + json.dumps(plan, indent=1) + "\n```"


# ---------------------------------------------------------------------------
# simulator (deterministic; worker-serial list scheduling)
# ---------------------------------------------------------------------------

def _simulate(plan: dict, world: dict) -> dict:
    recipes = world["recipes"]
    workers = world["workers"]
    initial_types = {a["id"]: a["type"] for a in world["initial"]}
    subtasks = {s["id"]: s for s in plan["subtasks"]}

    artifact_type = dict(initial_types)   # artifact id -> type (only REAL ones)
    artifact_time = {a: 0 for a in initial_types}
    worker_free = {w: 0 for w in sorted(workers)}
    done: dict = {}
    failed: list = []
    cost = 0
    makespan = 0

    pending = sorted(subtasks.keys())
    # bounded rounds: each round executes every ready subtask (deterministic
    # order); cycles/unsatisfiable steps simply never become ready.
    for _ in range(len(pending) + 1):
        progressed = False
        for sid in list(pending):
            s = subtasks[sid]
            # inputs ready? (initial artifacts or outputs of *executed* steps)
            producer_pending = False
            for inp in s["inputs"]:
                if inp in artifact_type:
                    continue
                if any(t != sid and subtasks[t]["output"] == inp
                       for t in pending):
                    producer_pending = True
                    break
            if producer_pending:
                continue
            pending.remove(sid)
            progressed = True
            w = s["worker"]
            act = s["action"]
            reason = None
            if w not in workers:
                reason = f"unknown worker {w!r}"
            elif act not in recipes:
                reason = f"unknown action {act!r}"
            elif act not in workers[w]["capabilities"]:
                reason = f"worker {w} lacks capability {act!r}"
            else:
                missing = [i for i in s["inputs"] if i not in artifact_type]
                if missing:
                    reason = f"missing inputs {missing}"
                else:
                    have = sorted(artifact_type[i] for i in s["inputs"])
                    need = sorted(recipes[act]["inputs"])
                    if have != need:
                        reason = f"recipe {act!r} needs types {need}, got {have}"
            if reason is not None:
                failed.append({"id": sid, "reason": reason})
                continue
            start = max(worker_free[w],
                        max((artifact_time[i] for i in s["inputs"]), default=0))
            end = start + workers[w]["duration"]
            worker_free[w] = end
            cost += workers[w]["cost"]
            artifact_type[s["output"]] = recipes[act]["output"]
            artifact_time[s["output"]] = end
            makespan = max(makespan, end)
            done[sid] = True
        if not progressed:
            break
    # anything still pending is part of a cycle / waits on a failed producer
    for sid in pending:
        failed.append({"id": sid, "reason": "never became ready (cycle or "
                                            "failed/missing producer)"})

    produced_types = set(artifact_type.values())
    goals_done = sum(1 for g in world["goals"] if g in produced_types)
    return {"goals_done": goals_done, "executed": len(done), "failed": failed,
            "cost": cost, "makespan": makespan}


def _render_prompt(world: dict) -> str:
    lines = [
        "You are an orchestrator. Delegate work to workers so that all GOAL "
        "artifacts get produced, within the cost budget and deadline.",
        "",
        "WORKERS (cost and duration are per subtask):",
    ]
    for w, info in world["workers"].items():
        caps = ", ".join(info["capabilities"]) if info["capabilities"] else "(none)"
        lines.append(f"  - {w}: capabilities [{caps}], cost {info['cost']}, "
                     f"duration {info['duration']}")
    lines.append("")
    lines.append("ACTION RECIPES (an action consumes artifact TYPES and produces a TYPE):")
    for a, r in world["recipes"].items():
        lines.append(f"  - {a}: {' + '.join(r['inputs'])} -> {r['output']}")
    lines.append("")
    lines.append("INITIAL ARTIFACTS (id: type):")
    for a in world["initial"]:
        lines.append(f"  - {a['id']}: {a['type']}")
    lines.append("")
    lines.append("GOALS (produce at least one artifact of each type): "
                 + ", ".join(world["goals"]))
    lines.append(f"COST BUDGET: {world['budget']}    DEADLINE (makespan): "
                 f"{world['deadline']}")
    lines.append("""
Rules:
- Each subtask runs one action on one worker. The worker must have the action
  in its capabilities. Subtask inputs are artifact IDs: either initial artifact
  ids or the `output` id of another subtask. Input artifact types must match
  the recipe exactly. Workers execute their own subtasks one at a time;
  independent subtasks on different workers run in parallel.
- Makespan = time when the last subtask finishes. Cost = sum of worker costs
  over executed subtasks.

Reply with ONLY one fenced ```json block containing a plan object:
{"subtasks": [{"id": "s1", "worker": "w1", "action": "clean_1",
               "inputs": ["A1"], "output": "out1"}, ...]}""")
    return "\n".join(lines)
