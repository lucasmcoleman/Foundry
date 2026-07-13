#!/usr/bin/env python
"""Soundness demonstration for foundry_gym environment families.

For every requested family, scores the reference and corrupted solutions on
``--n`` generated tasks and reports whether every task respects the family's
soundness contract:

    reference_reward >= env.reference_threshold
    corrupted_reward <= env.corrupted_threshold

Prints a summary table and writes a JSON report (``--report``). Exit code is
0 iff there are zero violations across all requested families; nonzero
otherwise. This script is the mission's done-criterion evidence.

Usage:
    python foundry_gym/scripts/demo_soundness.py --families all --n 100
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from foundry_gym.core import registry  # noqa: E402

_DIFFICULTY_CYCLE = [0.1, 0.3, 0.5, 0.7, 0.9]


def _default_out_dir() -> str:
    pkg_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(pkg_dir, "samples")


def _resolve_families(spec: str) -> list:
    if spec.strip().lower() == "all":
        return registry.names()
    names = [f.strip() for f in spec.split(",") if f.strip()]
    known = set(registry.names())
    unknown = [n for n in names if n not in known]
    if unknown:
        raise SystemExit(f"unknown families: {unknown}; available: {sorted(known)}")
    return names


def score_family(family: str, n: int, seed_base: int) -> dict:
    env = registry.get(family)
    ref_rewards = []
    cor_rewards = []
    violations = []
    for i in range(n):
        seed = seed_base + i
        difficulty = _DIFFICULTY_CYCLE[i % len(_DIFFICULTY_CYCLE)]
        task = env.generate({"difficulty": difficulty}, seed=seed)
        ref_reward = env.verify(task, env.reference_solution(task)).reward
        cor_reward = env.verify(task, env.corrupted_solution(task)).reward
        ref_rewards.append(ref_reward)
        cor_rewards.append(cor_reward)
        bad_ref = ref_reward < env.reference_threshold
        bad_cor = cor_reward > env.corrupted_threshold
        if bad_ref or bad_cor:
            violations.append({
                "task_id": task.task_id,
                "seed": seed,
                "difficulty": difficulty,
                "reference_reward": ref_reward,
                "corrupted_reward": cor_reward,
                "reference_threshold": env.reference_threshold,
                "corrupted_threshold": env.corrupted_threshold,
                "bad_reference": bad_ref,
                "bad_corrupted": bad_cor,
            })

    n_ref_ok = sum(1 for r in ref_rewards if r >= env.reference_threshold)
    n_cor_ok = sum(1 for r in cor_rewards if r <= env.corrupted_threshold)
    return {
        "family": family,
        "n": n,
        "reference_threshold": env.reference_threshold,
        "corrupted_threshold": env.corrupted_threshold,
        "ref_mean": sum(ref_rewards) / n if n else 0.0,
        "ref_min": min(ref_rewards) if ref_rewards else 0.0,
        "ref_ok_count": n_ref_ok,
        "cor_mean": sum(cor_rewards) / n if n else 0.0,
        "cor_max": max(cor_rewards) if cor_rewards else 0.0,
        "cor_ok_count": n_cor_ok,
        "violation_count": len(violations),
        "violations": violations,
    }


def _print_table(stats: list) -> None:
    headers = [
        "family", "n", "ref_mean", "ref_min", "ref>=thr",
        "cor_mean", "cor_max", "cor<=thr", "violations",
    ]
    rows = []
    for s in stats:
        rows.append([
            s["family"],
            str(s["n"]),
            f"{s['ref_mean']:.3f}",
            f"{s['ref_min']:.3f}",
            f"{s['ref_ok_count']}/{s['n']}",
            f"{s['cor_mean']:.3f}",
            f"{s['cor_max']:.3f}",
            f"{s['cor_ok_count']}/{s['n']}",
            str(s["violation_count"]),
        ])
    widths = [
        max(len(headers[i]), *(len(r[i]) for r in rows)) if rows else len(headers[i])
        for i in range(len(headers))
    ]

    def fmt_row(cells):
        return "  ".join(c.ljust(w) for c, w in zip(cells, widths))

    print(fmt_row(headers))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print(fmt_row(r))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--families", default="all", help="comma list or 'all' (default: all)")
    parser.add_argument("--n", type=int, default=100, help="tasks per family (default: 100)")
    parser.add_argument("--seed-base", type=int, default=1000, help="starting seed (default: 1000)")
    parser.add_argument(
        "--report", default=None,
        help="path for JSON report (default: <out-dir>/soundness_report.json)",
    )
    parser.add_argument(
        "--out-dir", default=None,
        help="directory for default report path (default: foundry_gym/samples)",
    )
    args = parser.parse_args(argv)

    out_dir = args.out_dir or _default_out_dir()
    os.makedirs(out_dir, exist_ok=True)
    report_path = args.report or os.path.join(out_dir, "soundness_report.json")

    families = _resolve_families(args.families)
    print(f"foundry_gym soundness demo: families={families} n={args.n} seed_base={args.seed_base}\n")

    stats = [score_family(family, args.n, args.seed_base) for family in families]
    _print_table(stats)

    total_violations = sum(s["violation_count"] for s in stats)
    print()
    if total_violations == 0:
        print(f"PASS: 0 violations across {len(stats)} family(ies), {args.n} task(s) each.")
    else:
        print(f"FAIL: {total_violations} violation(s) found. See {report_path} for details.")

    report = {
        "n": args.n,
        "seed_base": args.seed_base,
        "families": stats,
        "total_violations": total_violations,
    }
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"Report written to {report_path}")

    return 0 if total_violations == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
