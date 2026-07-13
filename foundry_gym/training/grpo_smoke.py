#!/usr/bin/env python3
"""GRPO smoke train: prove the foundry_gym reward loop end-to-end.

Loads a small trainable HF checkpoint (safetensors — NOT a GGUF or a served
endpoint; the policy needs trainable weights), builds a mixed-family gym
dataset, and runs a few GRPOTrainer steps with LoRA. This is a plumbing
proof, not a capability run.

Run (GPU-coordinated — always via the machine-wide lock):
    flock /tmp/claude-gpu.lock -c \
      'cd /server/programming/Foundry && .venv/bin/python foundry_gym/training/grpo_smoke.py'
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

# ROCm env (mirrors Foundry core/fast_train_zeroclaw.py) — before torch import.
os.environ.setdefault("HSA_ENABLE_SDMA", "0")
os.environ.setdefault("PYTORCH_HIP_ALLOC_CONF",
                      "backend:native,expandable_segments:True")
os.environ.setdefault("TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

FOUNDRY_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(FOUNDRY_ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="/server/ai/models/source/Qwen2.5-0.5B")
    ap.add_argument("--families", default="math_logic,orchestrator_planning")
    ap.add_argument("--n-per-family", type=int, default=24)
    ap.add_argument("--difficulty", type=float, default=0.2)
    ap.add_argument("--steps", type=int, default=6)
    ap.add_argument("--num-generations", type=int, default=4)
    ap.add_argument("--batch-size", type=int, default=4)
    ap.add_argument("--max-completion", type=int, default=128)
    ap.add_argument("--lr", type=float, default=1e-5)
    ap.add_argument("--out", default=str(FOUNDRY_ROOT / "output" / "gym_grpo_smoke"))
    args = ap.parse_args()

    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from peft import LoraConfig
    from trl import GRPOConfig, GRPOTrainer

    from foundry_gym.training import build_dataset, gym_reward

    families = [f.strip() for f in args.families.split(",") if f.strip()]
    print(f"[gym-smoke] families={families} model={args.model}")
    t0 = time.time()

    ds = build_dataset(families, n_per_family=args.n_per_family,
                       seed_base=0, difficulty=args.difficulty)
    print(f"[gym-smoke] dataset: {len(ds)} prompts "
          f"(cols: {ds.column_names}) in {time.time()-t0:.1f}s")

    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.bfloat16 if device == "cuda" else torch.float32
    print(f"[gym-smoke] device={device} dtype={dtype}")

    tokenizer = AutoTokenizer.from_pretrained(args.model)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        args.model, dtype=dtype, device_map={"": 0} if device == "cuda" else None,
    )

    peft_config = LoraConfig(
        r=8, lora_alpha=16, lora_dropout=0.0, bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
    )

    cfg = GRPOConfig(
        output_dir=args.out,
        max_steps=args.steps,
        per_device_train_batch_size=args.batch_size,
        num_generations=args.num_generations,
        max_completion_length=args.max_completion,
        learning_rate=args.lr,
        logging_steps=1,
        report_to=[],
        save_strategy="no",
        seed=42,
        bf16=(device == "cuda"),
    )

    trainer = GRPOTrainer(
        model=model,
        reward_funcs=gym_reward,
        args=cfg,
        train_dataset=ds,
        processing_class=tokenizer,
        peft_config=peft_config,
    )
    print(f"[gym-smoke] trainer ready in {time.time()-t0:.1f}s — training "
          f"{args.steps} steps")
    result = trainer.train()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    trainer.save_model(str(out / "adapter"))
    history = trainer.state.log_history
    summary = {
        "model": args.model,
        "families": families,
        "steps": args.steps,
        "train_runtime_s": round(result.metrics.get("train_runtime", 0.0), 1),
        "final_loss": next((h["loss"] for h in reversed(history) if "loss" in h), None),
        "reward_by_step": [
            {"step": h.get("step"), "reward": h.get("reward"),
             "reward_std": h.get("reward_std"), "loss": h.get("loss")}
            for h in history if "reward" in h
        ],
    }
    (out / "smoke_summary.json").write_text(json.dumps(summary, indent=2))
    print("[gym-smoke] summary:", json.dumps(summary, indent=2))
    print(f"[gym-smoke] DONE in {time.time()-t0:.1f}s — adapter at {out/'adapter'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
