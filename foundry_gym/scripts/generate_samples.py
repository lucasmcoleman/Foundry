#!/usr/bin/env python
"""Generate sample tasks (+ reference/corrupted rewards) for one or more
registered foundry_gym environment families.

Usage:
    python foundry_gym/scripts/generate_samples.py --families all --n 100
    python foundry_gym/scripts/generate_samples.py --families math_logic --n 20

Writes ``<out-dir>/<family>.jsonl``, one JSON object per line:
    {"task": <task dict>, "reference_reward": <float>, "corrupted_reward": <float>}
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
    # foundry_gym/samples, resolved relative to this file's package, not cwd.
    pkg_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(pkg_dir, "samples")


def _resolve_families(spec: str) -> list:
    if spec.strip().lower() == "all":
        return registry.names()
    names = [f.strip() for f in spec.split(",") if f.strip()]
    known = set(registry.names())
    unknown = [n for n in names if n not in known]
    if unknown:
        raise SystemExit(
            f"unknown families: {unknown}; available: {sorted(known)}"
        )
    return names


def generate_family(family: str, n: int, seed_base: int, out_dir: str) -> dict:
    env = registry.get(family)
    out_path = os.path.join(out_dir, f"{family}.jsonl")
    count = 0
    ref_sum = 0.0
    cor_sum = 0.0
    with open(out_path, "w", encoding="utf-8") as f:
        for i in range(n):
            seed = seed_base + i
            difficulty = _DIFFICULTY_CYCLE[i % len(_DIFFICULTY_CYCLE)]
            task = env.generate({"difficulty": difficulty}, seed=seed)
            ref_reward = env.verify(task, env.reference_solution(task)).reward
            cor_reward = env.verify(task, env.corrupted_solution(task)).reward
            record = {
                "task": json.loads(task.to_json()),
                "reference_reward": ref_reward,
                "corrupted_reward": cor_reward,
            }
            f.write(json.dumps(record) + "\n")
            count += 1
            ref_sum += ref_reward
            cor_sum += cor_reward
            if (i + 1) % max(1, n // 10) == 0 or (i + 1) == n:
                print(f"  [{family}] {i + 1}/{n} generated")
    return {
        "family": family,
        "n": count,
        "out_path": out_path,
        "ref_mean": ref_sum / count if count else 0.0,
        "cor_mean": cor_sum / count if count else 0.0,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--families", default="all",
        help="comma-separated family names, or 'all' (default: all)",
    )
    parser.add_argument("--n", type=int, default=100, help="tasks per family (default: 100)")
    parser.add_argument(
        "--seed-base", type=int, default=1000,
        help="starting seed; task i uses seed_base + i (default: 1000)",
    )
    parser.add_argument(
        "--out-dir", default=None,
        help="output directory (default: foundry_gym/samples next to this package)",
    )
    args = parser.parse_args(argv)

    out_dir = args.out_dir or _default_out_dir()
    os.makedirs(out_dir, exist_ok=True)

    families = _resolve_families(args.families)
    print(f"Generating samples for families: {families}")
    print(f"n={args.n} seed_base={args.seed_base} out_dir={out_dir}")

    summaries = []
    for family in families:
        print(f"== {family} ==")
        summaries.append(generate_family(family, args.n, args.seed_base, out_dir))

    print("\nSummary:")
    for s in summaries:
        print(
            f"  {s['family']}: n={s['n']} ref_mean={s['ref_mean']:.3f} "
            f"cor_mean={s['cor_mean']:.3f} -> {s['out_path']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
