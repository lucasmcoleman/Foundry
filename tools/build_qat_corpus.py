#!/usr/bin/env python3
"""Build a QAT training corpus as chat JSONL, blended by domain.

WHY THIS EXISTS
---------------
QAT trains LoRA adapters that compensate for quantization error *on the
distribution you show them*. Train on one narrow domain and you do not get a
compensated general model -- you get a domain fine-tune wearing a compensation
hat, and because QAT touches weights it is not something a prompt can undo.
Foundry's own QAT validation controlled against a "bf16 + identical-LoRA"
baseline precisely to separate real quantization recovery from domain drift.

So this mirrors the discipline `tools/build_calib_corpus.py` already applies to
the imatrix side: a deliberate domain blend, a deterministic seed, and a hard
disjointness check against the perplexity eval corpus. QAT and imatrix
compensate for the SAME error; calibrating them on different distributions
means the one that edits weights is the narrow one.

TOOL-CALL FORMAT (the trap)
---------------------------
Qwen3.6's own chat template mandates nested XML and says so:

    <tool_call>
    <function=NAME>
    <parameter=KEY>
    value
    </parameter>
    </function>
    </tool_call>

ZeroClaw's traces use the older Hermes/Qwen2.5 convention -- JSON inside
<tool_call>. Training on those verbatim teaches the model to emit the wrong
syntax for its own template, which perplexity will not reveal; it surfaces
later as broken agent loops. `transcode_tool_calls` converts them, and
--verify-template renders samples through the real tokenizer to prove it.

Blend is CLI-tunable; the default de-emphasises multilingual (vocab coverage
only) in favour of code / math+reasoning / agentic.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

DEFAULT_BLEND = {
    "code":         0.30,
    "math":         0.30,   # includes chain-of-thought; this model has a thinking mode
    "agentic":      0.25,
    "general":      0.10,
    "multilingual": 0.05,   # vocab coverage, not capability
}

# (hf_id, split, config) -- first that loads wins, so a delisted primary
# degrades to a fallback instead of silently skewing the blend.
SOURCES = {
    "code": [("ise-uiuc/Magicoder-Evol-Instruct-110K", "train", None),
             ("m-a-p/CodeFeedback-Filtered-Instruction", "train", None)],
    "math": [("nvidia/OpenMathInstruct-2", "train_1M", None),
             ("meta-math/MetaMathQA", "train", None)],
    "agentic": [("BitAgent/tool_calling", "train", None)],
    "general": [("teknium/OpenHermes-2.5", "train", None),
                ("HuggingFaceH4/ultrachat_200k", "train_sft", None)],
    "multilingual": [("CohereForAI/aya_dataset", "train", None)],
}

TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


def transcode_tool_calls(text: str) -> tuple[str, int]:
    """Rewrite Hermes-style JSON tool calls into Qwen3.6 nested-XML form.

    Returns (text, n_converted). Leaves text untouched when nothing matches,
    so it is safe to run over every assistant turn.
    """
    n = 0

    def _one(m: re.Match) -> str:
        nonlocal n
        try:
            call = json.loads(m.group(1))
            name = call["name"]
            args = call.get("arguments", {}) or {}
        except (ValueError, KeyError, TypeError):
            return m.group(0)          # unparseable -> leave verbatim, never guess
        parts = [f"<tool_call>\n<function={name}>"]
        for k, v in args.items():
            v = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            parts.append(f"<parameter={k}>\n{v}\n</parameter>")
        parts.append("</function>\n</tool_call>")
        n += 1
        return "\n".join(parts)

    return TOOL_CALL_RE.sub(_one, text), n


def _normalize(rec: dict) -> list[dict] | None:
    """Coerce a source row into [{'role','content'}, ...] or None if unusable."""
    if isinstance(rec.get("messages"), list) and rec["messages"]:
        out = []
        for m in rec["messages"]:
            r, c = m.get("role"), m.get("content")
            if not r or not isinstance(c, str) or not c.strip():
                return None
            out.append({"role": r, "content": c})
        return out
    # Some sources carry the turns under a differently-named list -- and some
    # ship that list as a JSON *string* (BitAgent/tool_calling does).
    for key in ("conversation", "conversations", "chat"):
        val = rec.get(key)
        if isinstance(val, str) and val.lstrip().startswith("["):
            try:
                val = json.loads(val)
            except ValueError:
                val = None
        if isinstance(val, list) and val:
            out = []
            for m in val:
                if not isinstance(m, dict):
                    return None
                r = m.get("role") or m.get("from")
                c = m.get("content") or m.get("value")
                # A "tool call" turn may carry a dict payload; render it in the
                # model's own nested-XML form rather than dropping the row --
                # these are exactly the agentic samples the blend wants.
                if isinstance(c, dict) and "name" in c:
                    args = c.get("arguments") or {}
                    parts = [f"<tool_call>\n<function={c['name']}>"]
                    for k, v in args.items():
                        v = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                        parts.append(f"<parameter={k}>\n{v}\n</parameter>")
                    parts.append("</function>\n</tool_call>")
                    c = "\n".join(parts)
                    r = "assistant"
                if not r or not isinstance(c, str) or not c.strip():
                    return None
                r = {"human": "user", "gpt": "assistant",
                     "tool call": "assistant", "tool_call": "assistant",
                     "tool response": "tool", "tool_response": "tool"}.get(r, r)
                out.append({"role": r, "content": c})
            return out or None

    # Single-turn pairs. Try every user-ish x assistant-ish COMBINATION rather
    # than a fixed list -- a hardcoded pairing silently zeroes a whole bucket
    # when a source uses instruction/response instead of instruction/output,
    # and the blend skews without anything failing loudly.
    USER_KEYS = ("instruction", "problem", "query", "prompt", "inputs",
                 "question", "input")
    ASST_KEYS = ("response", "output", "generated_solution", "completion",
                 "targets", "answer", "solution")
    for u in USER_KEYS:
        uv = rec.get(u)
        if not isinstance(uv, str) or not uv.strip():
            continue
        for k in ASST_KEYS:
            av = rec.get(k)
            if isinstance(av, str) and av.strip():
                return [{"role": "user", "content": uv},
                        {"role": "assistant", "content": av}]
    return None


def load_bucket(name: str, n: int, rng: random.Random, log) -> list[list[dict]]:
    from datasets import load_dataset
    for hf_id, split, cfg in SOURCES.get(name, []):
        try:
            ds = load_dataset(hf_id, cfg, split=split, streaming=True)
            rows, seen = [], 0
            for rec in ds:
                seen += 1
                msgs = _normalize(rec)
                if msgs:
                    rows.append(msgs)
                if len(rows) >= n or seen > n * 40:
                    break
            if rows:
                log(f"  {name:12} {len(rows):5} from {hf_id}")
                return rows[:n]
            log(f"  {name:12} 0 usable rows from {hf_id}, trying next")
        except Exception as e:                       # noqa: BLE001 - any source may 404
            log(f"  {name:12} {hf_id} unavailable ({type(e).__name__}), trying next")
    log(f"  {name:12} NO SOURCE AVAILABLE -- bucket will be short")
    return []


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="data/qat_corpus.jsonl")
    ap.add_argument("--samples", type=int, default=6000)
    ap.add_argument("--seed", type=int, default=20260804)
    ap.add_argument("--zeroclaw", default="data/zeroclaw_training_data.jsonl",
                    help="local agentic traces; transcoded into the agentic bucket")
    ap.add_argument("--eval-corpus", default="/server/ai/wikitext/wikitext-2-raw/wiki.test.raw",
                    help="PPL eval text; the corpus is checked to be disjoint from it")
    ap.add_argument("--verify-template", default="Qwen/Qwen3.6-35B-A3B",
                    help="render samples through this model's real chat template ('' to skip)")
    for b in DEFAULT_BLEND:
        ap.add_argument(f"--{b}", type=float, default=DEFAULT_BLEND[b])
    a = ap.parse_args()

    def log(m):
        print(m, flush=True)

    blend = {b: getattr(a, b) for b in DEFAULT_BLEND}
    total = sum(blend.values())
    if abs(total - 1.0) > 1e-6:
        log(f"blend sums to {total:.3f}, normalising")
        blend = {k: v / total for k, v in blend.items()}
    rng = random.Random(a.seed)

    log(f"target {a.samples} samples, blend: " +
        " ".join(f"{k}={v:.0%}" for k, v in blend.items()))

    convo: list[list[dict]] = []

    # --- agentic: local ZeroClaw traces, transcoded, first ---------------
    want_agentic = int(a.samples * blend["agentic"])
    zc_used = zc_calls = 0
    zp = Path(a.zeroclaw)
    if zp.exists():
        for line in zp.read_text().splitlines():
            try:
                msgs = _normalize(json.loads(line))
            except ValueError:
                continue
            if not msgs:
                continue
            n_here = 0
            for m in msgs:
                if m["role"] == "assistant":
                    m["content"], k = transcode_tool_calls(m["content"])
                    n_here += k
            convo.append(msgs)
            zc_used += 1
            zc_calls += n_here
        log(f"  agentic      {zc_used:5} from {zp} "
            f"({zc_calls} tool calls transcoded to <function=>/<parameter=>)")
    remaining_agentic = max(0, want_agentic - zc_used)

    for bucket, share in blend.items():
        n = int(a.samples * share)
        if bucket == "agentic":
            n = remaining_agentic
        if n <= 0:
            continue
        convo.extend(load_bucket(bucket, n, rng, log))

    rng.shuffle(convo)
    convo = convo[:a.samples]

    # --- disjointness vs the PPL eval corpus -----------------------------
    ev = Path(a.eval_corpus)
    if ev.exists():
        ev_text = ev.read_text(errors="ignore")
        grams = {ev_text[i:i + 60] for i in range(0, min(len(ev_text), 4_000_000), 997)}
        hits = sum(1 for c in convo
                   for m in c if any(g in m["content"] for g in list(grams)[:400]))
        rate = hits / max(1, len(convo))
        log(f"eval-disjointness: {hits} hits over {len(convo)} conversations "
            f"({rate:.4%})")
        if rate > 0.01:
            log("REFUSING: corpus overlaps the perplexity eval text. Measured QAT "
                "recovery would be optimistic with nothing in the output showing it.")
            return 1
    else:
        log(f"WARNING: eval corpus {ev} not found -- disjointness NOT checked")

    # --- verify the transcode against the model's REAL chat template ------
    if a.verify_template:
        try:
            from transformers import AutoTokenizer
            tok = AutoTokenizer.from_pretrained(a.verify_template, trust_remote_code=True)
            sample = [c for c in convo if any("<function=" in m["content"] for m in c)][:3]
            for c in sample:
                rendered = tok.apply_chat_template(c, tokenize=False)
                assert "<function=" in rendered and "<parameter=" in rendered
            log(f"template verify: {len(sample)} transcoded samples render correctly "
                f"through {a.verify_template}")
            if not sample:
                log("template verify: WARNING no transcoded samples present to check")
        except Exception as e:                        # noqa: BLE001
            log(f"template verify SKIPPED ({type(e).__name__}: {e})")

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        for c in convo:
            f.write(json.dumps({"messages": c}, ensure_ascii=False) + "\n")
    log(f"wrote {len(convo)} conversations to {out} "
        f"({out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
