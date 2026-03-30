#!/usr/bin/env python3
"""
Synthetic training data generator for the NC Master Gardener model.

Uses the `claude` CLI (Claude Code) as a "teacher model" to generate diverse,
high-quality gardening conversations. Runs through your Claude Code Max
subscription — no API key needed.

Usage:
    # Generate ~3000 examples (default)
    python generate.py

    # Generate a specific number
    python generate.py --count 5000

    # Generate with reference material from ebooks
    python generate.py --references /path/to/extracted_texts/

    # Resume from a partial run
    python generate.py --resume

    # Dry run (show what would be generated without calling Claude)
    python generate.py --dry-run
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from dataclasses import dataclass

# Ensure the gardener package is importable
sys.path.insert(0, str(Path(__file__).parent))
from system_prompt import SYSTEM_PROMPT
from topics import TOPICS


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class GeneratorConfig:
    output_path: str = "gardener_training_data.jsonl"
    target_count: int = 3000
    max_concurrent: int = 5           # parallel claude CLI calls
    batch_size: int = 5               # examples per CLI call
    model: str = "sonnet"             # claude CLI model flag
    reference_dir: str | None = None  # optional dir with .txt reference files
    resume: bool = False
    multi_turn_ratio: float = 0.35    # fraction of examples with follow-up turns
    seed: int = 42


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

def fill_template(template: str, context_vars: dict) -> str:
    """Fill {placeholders} in a prompt template with random values from context_vars."""
    result = template
    for key, values in context_vars.items():
        placeholder = "{" + key + "}"
        if placeholder in result and values:
            result = result.replace(placeholder, random.choice(values))
    return result


def build_user_question(topic: dict) -> str:
    """Pick a random prompt template from a topic and fill it with random context."""
    template = random.choice(topic["prompts"])
    return fill_template(template, topic.get("context_vars", {}))


def load_reference_chunks(ref_dir: str, chunk_size: int = 2000) -> list[str]:
    """Load reference material from text files, split into chunks."""
    chunks = []
    ref_path = Path(ref_dir)
    if not ref_path.exists():
        print(f"Warning: reference directory {ref_dir} not found, skipping")
        return chunks

    for txt_file in sorted(ref_path.glob("*.txt")):
        text = txt_file.read_text(errors="replace")
        words = text.split()
        for i in range(0, len(words), chunk_size - 200):
            chunk = " ".join(words[i:i + chunk_size])
            if len(chunk) > 200:
                chunks.append(f"[From: {txt_file.stem}]\n{chunk}")

    print(f"Loaded {len(chunks)} reference chunks from {ref_dir}")
    return chunks


# ---------------------------------------------------------------------------
# Teacher model prompt (batched)
# ---------------------------------------------------------------------------

TEACHER_SYSTEM = """You are an expert training data generator. Your job is to produce realistic, high-quality conversations between a user and "Carolina Ground Truth," an NC gardening assistant.

RULES:
1. Responses must be specific to North Carolina (zones 6b-8a), actionable, and grounded in real horticultural science.
2. Always default to organic/natural methods. Mention conventional options only when asked.
3. Include specific numbers: spacing in inches, amendment rates per sq ft, gallons of water, days to maturity, etc.
4. Reference NC-specific resources when relevant: NC State Extension, NCDA&CS soil lab, local variety trials.
5. Vary response length naturally: simple questions get concise answers, complex questions get detailed ones.
6. Never be preachy. Be practical and meet the gardener where they are.
7. Use plain language but don't dumb things down. Explain the science behind recommendations.
8. When appropriate, mention related considerations the gardener might not have thought of.
9. Make each response feel like it comes from a real person who has gardened in NC for decades.

OUTPUT FORMAT: You MUST output valid JSON — an array of objects. No markdown fences, no commentary outside the JSON."""


def build_batch_prompt(items: list[dict], references: list[str]) -> str:
    """Build a prompt asking Claude to generate multiple examples at once.

    Each item has: topic, user_question, multi_turn (bool)
    """
    examples_desc = []
    for i, item in enumerate(items):
        topic = item["topic"]
        q = item["user_question"]
        mt = item["multi_turn"]

        ref_note = ""
        if references and random.random() < 0.3:
            ref = random.choice(references)
            # Truncate reference to keep prompt manageable
            ref_note = f'\n  "reference": "{ref[:800]}..."'

        mode = "multi-turn (include 1-2 follow-up USER/ASSISTANT exchanges)" if mt else "single-turn"
        examples_desc.append(
            f'  {{\n'
            f'    "index": {i},\n'
            f'    "topic": "{topic["category"]} > {topic["subcategory"]}",\n'
            f'    "difficulty": "{topic["difficulty"]}",\n'
            f'    "mode": "{mode}",\n'
            f'    "user_message": "{q}"{ref_note}\n'
            f'  }}'
        )

    items_json = ",\n".join(examples_desc)

    return f"""Generate training data for a North Carolina gardening assistant called "Carolina Ground Truth."

For each item below, produce the conversation. Output a JSON array where each element is:
{{
  "index": <matching index>,
  "messages": [
    {{"role": "user", "content": "<the user message from the item>"}},
    {{"role": "assistant", "content": "<your generated response>"}},
    // For multi-turn: add more user/assistant pairs
  ]
}}

IMPORTANT:
- Do NOT include system messages — I will add those.
- For multi-turn items, generate realistic follow-up questions from the user and corresponding assistant responses (2-3 exchanges total).
- For single-turn items, just one user message and one assistant response.
- Output ONLY the JSON array. No markdown code fences. No extra text.

Items to generate:
[
{items_json}
]"""


# ---------------------------------------------------------------------------
# Claude CLI invocation
# ---------------------------------------------------------------------------

def call_claude_cli(prompt: str, system: str, config: GeneratorConfig) -> str | None:
    """Call the claude CLI with -p (print mode) and return stdout."""
    cmd = [
        "claude",
        "-p", prompt,
        "--model", config.model,
        "--output-format", "text",
    ]

    # Pass system prompt via --system-prompt flag
    cmd.extend(["--system-prompt", system])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,  # 5 min per batch
            env={**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()[:200] if result.stderr else "unknown"
            print(f"  CLI error (rc={result.returncode}): {stderr}")
            return None
        return result.stdout
    except subprocess.TimeoutExpired:
        print("  CLI timeout (300s)")
        return None
    except Exception as e:
        print(f"  CLI exception: {e}")
        return None


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def extract_json_array(text: str) -> list[dict] | None:
    """Extract a JSON array from Claude's response, handling markdown fences."""
    text = text.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        # Remove opening fence (with optional language tag)
        text = re.sub(r'^```\w*\n?', '', text)
        text = re.sub(r'\n?```\s*$', '', text)
        text = text.strip()

    # Try to find the JSON array
    # First try: parse the whole thing
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass

    # Second try: find array brackets
    start = text.find('[')
    end = text.rfind(']')
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass

    return None


def parse_batch_response(response_text: str) -> list[dict]:
    """Parse a batch response into training examples.

    Returns list of {"messages": [...]} dicts with system prompt prepended.
    """
    if not response_text:
        return []

    items = extract_json_array(response_text)
    if not items:
        # Fallback: try to parse as individual conversation
        # (sometimes Claude outputs a single object instead of array)
        try:
            obj = json.loads(response_text.strip())
            if isinstance(obj, dict) and "messages" in obj:
                items = [obj]
        except json.JSONDecodeError:
            return []

    if not items:
        return []

    results = []
    for item in items:
        msgs = item.get("messages", [])
        if not msgs:
            continue

        # Validate message structure
        valid = True
        for m in msgs:
            if not isinstance(m, dict) or "role" not in m or "content" not in m:
                valid = False
                break
            if m["role"] not in ("user", "assistant", "system"):
                valid = False
                break
        if not valid:
            continue

        # Remove any system messages Claude may have included
        msgs = [m for m in msgs if m["role"] != "system"]

        # Must have at least user + assistant
        if len(msgs) < 2:
            continue

        # Must start with user and end with assistant
        if msgs[0]["role"] != "user" or msgs[-1]["role"] != "assistant":
            continue

        # Prepend our system prompt
        full_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + msgs
        results.append({"messages": full_messages})

    return results


# ---------------------------------------------------------------------------
# Batch generation with progress
# ---------------------------------------------------------------------------

def generate_all(
    config: GeneratorConfig,
    references: list[str],
    existing_count: int = 0,
) -> int:
    """Generate training examples using parallel claude CLI calls.

    Streams results to disk as they arrive so progress is never lost.
    Returns the number of new examples generated.
    """
    remaining = config.target_count - existing_count
    completed = 0
    failed_batches = 0
    start_time = time.time()
    write_lock = __import__("threading").Lock()

    # Build weighted topic pool
    weighted_topics = []
    for t in TOPICS:
        weighted_topics.extend([t] * len(t["prompts"]))

    # Create batches of items
    batches = []
    batch = []
    for i in range(remaining):
        topic = random.choice(weighted_topics)
        multi_turn = random.random() < config.multi_turn_ratio
        user_question = build_user_question(topic)

        batch.append({
            "topic": topic,
            "user_question": user_question,
            "multi_turn": multi_turn,
        })

        if len(batch) >= config.batch_size:
            batches.append(batch)
            batch = []

    if batch:
        batches.append(batch)

    total_batches = len(batches)
    print(f"  {total_batches} batches of ~{config.batch_size} examples each")
    print(f"  {config.max_concurrent} concurrent CLI processes\n", flush=True)

    def process_batch(batch_idx, batch_items):
        prompt = build_batch_prompt(batch_items, references)
        response = call_claude_cli(prompt, TEACHER_SYSTEM, config)
        examples = parse_batch_response(response) if response else []
        return batch_idx, examples

    # Open output file in append mode for streaming writes
    output_path = Path(config.output_path)
    with open(output_path, "a") as out_f:
        with ThreadPoolExecutor(max_workers=config.max_concurrent) as executor:
            futures = {
                executor.submit(process_batch, idx, batch): idx
                for idx, batch in enumerate(batches)
            }

            for future in as_completed(futures):
                batch_idx, examples = future.result()

                if examples:
                    # Stream results to disk immediately (thread-safe)
                    with write_lock:
                        for ex in examples:
                            out_f.write(json.dumps(ex, ensure_ascii=False) + "\n")
                        out_f.flush()
                    completed += len(examples)
                else:
                    failed_batches += 1

                elapsed = time.time() - start_time
                total = existing_count + completed
                rate = completed / elapsed if elapsed > 0 else 0
                remaining_now = config.target_count - total
                eta = remaining_now / rate if rate > 0 else 0

                print(
                    f"  [{total}/{config.target_count}] "
                    f"batch {batch_idx+1}/{total_batches} | "
                    f"{rate:.1f} ex/sec | "
                    f"ETA: {eta/60:.0f}min | "
                    f"{failed_batches} failed batches",
                    flush=True,
                )

    return completed


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def load_existing(path: str) -> list[dict]:
    """Load existing examples from a JSONL file."""
    if not Path(path).exists():
        return []
    examples = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    examples.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return examples


def save_examples(examples: list[dict], path: str):
    """Write examples to JSONL."""
    with open(path, "w") as f:
        for ex in examples:
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

def dry_run(config: GeneratorConfig):
    """Show what would be generated without calling Claude."""
    random.seed(config.seed)
    print("DRY RUN — showing sample prompts that would be generated:\n")

    topic_counts = {}
    for i in range(min(config.target_count, 20)):
        topic = random.choice(TOPICS)
        key = f"{topic['category']} > {topic['subcategory']}"
        topic_counts[key] = topic_counts.get(key, 0) + 1
        question = build_user_question(topic)
        multi = random.random() < config.multi_turn_ratio
        print(f"  [{i+1}] {'[multi]' if multi else '[single]'} ({key})")
        print(f"      {question}\n")

    if config.target_count > 20:
        print(f"  ... and {config.target_count - 20} more\n")

    num_batches = (config.target_count + config.batch_size - 1) // config.batch_size
    print(f"Execution plan:")
    print(f"  {config.target_count} examples in {num_batches} batches of {config.batch_size}")
    print(f"  {config.max_concurrent} concurrent CLI processes")
    print(f"  Model: {config.model}")
    print()

    print("Topic distribution (full run):")
    random.seed(config.seed)
    topic_counts = {}
    for i in range(config.target_count):
        topic = random.choice(TOPICS)
        key = f"{topic['category']} > {topic['subcategory']}"
        topic_counts[key] = topic_counts.get(key, 0) + 1
    for key, count in sorted(topic_counts.items(), key=lambda x: -x[1]):
        print(f"  {count:4d}  {key}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate NC gardener training data")
    parser.add_argument("--count", type=int, default=3000, help="Target number of examples")
    parser.add_argument("--output", type=str, default="gardener_training_data.jsonl")
    parser.add_argument("--references", type=str, help="Directory with .txt reference files (from ebooks)")
    parser.add_argument("--resume", action="store_true", help="Continue from existing output file")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be generated")
    parser.add_argument("--model", type=str, default="sonnet", help="Claude CLI model (sonnet, opus, haiku)")
    parser.add_argument("--concurrent", type=int, default=5, help="Max parallel CLI processes")
    parser.add_argument("--batch-size", type=int, default=5, help="Examples per CLI call")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    config = GeneratorConfig(
        output_path=args.output,
        target_count=args.count,
        model=args.model,
        max_concurrent=args.concurrent,
        batch_size=args.batch_size,
        reference_dir=args.references,
        resume=args.resume,
        seed=args.seed,
    )

    random.seed(config.seed)

    if args.dry_run:
        dry_run(config)
        return

    # Verify claude CLI is available
    try:
        result = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            print("Error: claude CLI not working properly")
            sys.exit(1)
        print(f"Using Claude Code CLI: {result.stdout.strip()}")
    except FileNotFoundError:
        print("Error: claude CLI not found. Install Claude Code first.")
        sys.exit(1)

    # Load references if provided
    references = []
    if config.reference_dir:
        references = load_reference_chunks(config.reference_dir)

    # Resume or start fresh
    existing = []
    if config.resume:
        existing = load_existing(config.output_path)
        print(f"Resuming: {len(existing)} existing examples")

    if len(existing) >= config.target_count:
        print(f"Already have {len(existing)} examples (target: {config.target_count})")
        return

    remaining = config.target_count - len(existing)
    print(f"\nGenerating {remaining} examples via Claude Code CLI...")
    print(f"  Model: {config.model}")
    print(f"  Batch size: {config.batch_size}")
    print(f"  Concurrent: {config.max_concurrent}")
    print(f"  Topics: {len(TOPICS)} categories")
    print(f"  References: {len(references)} chunks")
    print()

    # Ensure the file exists (generate_all opens in append mode)
    if not existing:
        Path(config.output_path).touch()

    new_count = generate_all(config, references, existing_count=len(existing))

    # Read back for stats
    all_examples = load_existing(config.output_path)
    total = len(all_examples)
    size_kb = Path(config.output_path).stat().st_size / 1024

    multi = sum(1 for ex in all_examples if len([m for m in ex["messages"] if m["role"] == "assistant"]) > 1)

    print(f"\nDone! {config.output_path} has {total} examples")
    print(f"\nDataset Statistics:")
    print(f"  Total examples: {total}")
    if total:
        print(f"  Multi-turn: {multi} ({100*multi/total:.0f}%)")
        print(f"  Single-turn: {total - multi}")
    print(f"  File size: {size_kb:.0f} KB")

    if total < config.target_count:
        shortfall = config.target_count - total
        print(f"\n  Note: {shortfall} examples short of target.")
        print(f"  Run with --resume to generate the remaining examples.")


if __name__ == "__main__":
    main()
