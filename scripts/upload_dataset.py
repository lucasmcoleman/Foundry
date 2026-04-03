#!/usr/bin/env python3
"""Upload the ZeroClaw training dataset to HuggingFace as a standalone dataset."""

import os
import sys
from pathlib import Path

def main():
    from huggingface_hub import HfApi

    token = os.environ.get("HF_TOKEN")
    if not token:
        token_file = Path.home() / ".cache" / "huggingface" / "token"
        if token_file.exists():
            token = token_file.read_text().strip()
    if not token:
        print("ERROR: No HF_TOKEN found")
        sys.exit(1)

    api = HfApi(token=token)
    repo_id = "lmcoleman/zeroclaw-tool-use-training"
    dataset_path = Path(__file__).resolve().parent.parent / "data" / "zeroclaw_training_data.jsonl"

    if not dataset_path.exists():
        print(f"ERROR: Dataset not found at {dataset_path}")
        sys.exit(1)

    # Count stats
    import json
    n_examples = 0
    n_tool_calls = 0
    with open(dataset_path) as f:
        for line in f:
            ex = json.loads(line)
            n_examples += 1
            for msg in ex.get("messages", []):
                if "<tool_call>" in msg.get("content", ""):
                    n_tool_calls += 1

    print(f"Dataset: {n_examples} examples, {n_tool_calls} tool-call turns")

    # Create repo
    api.create_repo(repo_id=repo_id, repo_type="dataset", private=False, exist_ok=True)
    print(f"Repo ready: https://huggingface.co/datasets/{repo_id}")

    # Upload dataset file
    api.upload_file(
        path_or_fileobj=str(dataset_path),
        path_in_repo="zeroclaw_training_data.jsonl",
        repo_id=repo_id,
        repo_type="dataset",
        commit_message=f"Upload ZeroClaw tool-use training data ({n_examples} examples, {n_tool_calls} tool-call turns)",
    )
    print("Dataset uploaded")

    # Create dataset card
    card = f"""---
license: apache-2.0
task_categories:
  - text-generation
language:
  - en
tags:
  - tool-use
  - function-calling
  - zeroclaw
  - agent
  - synthetic
size_categories:
  - n<1K
---

# ZeroClaw Tool-Use Training Data

Training dataset for teaching LLMs to use tools in the [ZeroClaw](https://github.com/lucasmcoleman/zeroclaw) autonomous agent runtime.

## Format

Standard chat-messages JSONL. Each line is a complete multi-turn conversation:

```json
{{"messages": [{{"role": "system", "content": "..."}}, {{"role": "user", "content": "..."}}, {{"role": "assistant", "content": "<tool_call>...</tool_call>"}}]}}
```

## Stats

- **{n_examples} examples** with {n_tool_calls} tool-call turns
- **25 tools** covered: shell, file_read, file_write, file_edit, glob_search, content_search, memory_store/recall/forget, cron_add/list/remove, web_fetch, web_search, http_request, git_operations, browser, delegate, pushover, backup_tool, pdf_read, screenshot, project_intel, security_ops, tool_search
- **Tool call format**: XML-wrapped JSON (`<tool_call>{{"name": "...", "arguments": {{...}}}}</tool_call>`)
- Includes multi-tool chains, error recovery scenarios, and reasoning traces

## Generation

- 108 hand-crafted scenarios covering core tool patterns
- LLM-generated scenarios (via local models) for diversity
- Validated: no empty user messages, all results are strings, all tools are valid
- Completion-only loss masking: only assistant turns contribute to training loss

## Usage

```python
from datasets import load_dataset
dataset = load_dataset("lmcoleman/zeroclaw-tool-use-training", split="train")
```
"""

    api.upload_file(
        path_or_fileobj=card.encode(),
        path_in_repo="README.md",
        repo_id=repo_id,
        repo_type="dataset",
        commit_message="Add dataset card",
    )
    print("Dataset card uploaded")
    print(f"URL: https://huggingface.co/datasets/{repo_id}")


if __name__ == "__main__":
    main()
