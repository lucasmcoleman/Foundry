#!/usr/bin/env python3
"""
Generate ZeroClaw tool-call training data in chat-messages JSONL format.

Converts scenario definitions into multi-turn conversations matching
ZeroClaw's XmlToolDispatcher format:
  - Tool calls:  <tool_call>{"name": "...", "arguments": {...}}</tool_call>
  - Tool results: [Tool results]\n<tool_result name="..." status="ok">...</tool_result>
  - Think tags:   <think>...</think> (optional, for reasoning models)

Output: JSONL with {"messages": [...]} per line, ready for Unsloth SFTTrainer.
"""

import json
import sys
from pathlib import Path

from zeroclaw_tools import TOOLS, format_tools_for_system_prompt
from scenarios import SCENARIOS
from scenarios_extended import SCENARIOS_EXTENDED


def build_system_prompt() -> str:
    """Build a representative ZeroClaw system prompt."""
    tools_section = format_tools_for_system_prompt()

    return f"""## Project Context

You are a capable AI coding assistant running inside ZeroClaw, an autonomous agent runtime. You help users with software engineering tasks including writing code, debugging, managing files, running commands, and more.

## Tools

{tools_section}

## Tool Use Protocol

To use a tool, wrap a JSON object in <tool_call></tool_call> tags:

<tool_call>
{{"name": "tool_name", "arguments": {{"param": "value"}}}}
</tool_call>

You may use multiple tool calls in a single response. Text can appear before, between, or after tool calls.

## Safety

- Do not exfiltrate private data.
- Do not run destructive commands without asking.
- Do not bypass oversight or approval mechanisms.
- Prefer `trash` over `rm`.
- When in doubt, ask before acting externally.

## Workspace

Working directory: /home/user/workspace

## Current Date & Time

2026-03-16 12:00:00 (UTC)

## Runtime

Host: dev-workstation | OS: linux | Model: local/omnicoder-9b"""


def format_tool_call(name: str, arguments: dict) -> str:
    """Format a tool call in ZeroClaw XML format."""
    args_json = json.dumps(arguments, ensure_ascii=False)
    return f'<tool_call>\n{{"name": "{name}", "arguments": {args_json}}}\n</tool_call>'


def format_tool_result(name: str, output: str, status: str = "ok") -> str:
    """Format a tool result in ZeroClaw format."""
    return f'[Tool results]\n<tool_result name="{name}" status="{status}">\n{output}\n</tool_result>'


def scenario_to_messages(scenario: dict, system_prompt: str) -> list[dict]:
    """Convert a scenario into a list of chat messages."""
    messages = [{"role": "system", "content": system_prompt}]
    messages.append({"role": "user", "content": scenario["user"]})

    turns = scenario.get("turns", [])
    final = scenario["final"]
    think = scenario.get("think")

    if not turns:
        # Pure text response, no tools
        content = final
        if think:
            content = f"<think>{think}</think>\n\n{final}"
        messages.append({"role": "assistant", "content": content})
        return messages

    # Process tool call turns
    # Group consecutive tool calls that happen before we get another user message
    i = 0
    while i < len(turns):
        turn = turns[i]
        tool_name = turn["tool"]
        tool_args = turn["args"]
        status = turn.get("status", "ok")
        result = turn["result"]

        # Build assistant message with tool call
        assistant_content = ""
        if i == 0 and think:
            assistant_content += f"<think>{think}</think>\n\n"

        assistant_content += format_tool_call(tool_name, tool_args)
        messages.append({"role": "assistant", "content": assistant_content})

        # Add tool result as user message
        messages.append({
            "role": "user",
            "content": format_tool_result(tool_name, result, status),
        })

        i += 1

    # Final assistant response after all tool calls
    messages.append({"role": "assistant", "content": final})

    return messages


def generate_dataset(output_path: str = "zeroclaw_training_data.jsonl"):
    """Generate the full training dataset."""
    system_prompt = build_system_prompt()

    all_scenarios = SCENARIOS + SCENARIOS_EXTENDED

    examples = []
    for scenario in all_scenarios:
        messages = scenario_to_messages(scenario, system_prompt)
        examples.append({"messages": messages})

    output = Path(output_path)
    with open(output, "w") as f:
        for ex in examples:
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")

    print(f"Generated {len(examples)} training examples -> {output}")

    # Print stats
    tool_counts = {}
    total_turns = 0
    for s in all_scenarios:
        for t in s.get("turns", []):
            tool_counts[t["tool"]] = tool_counts.get(t["tool"], 0) + 1
            total_turns += 1

    no_tool = sum(1 for s in all_scenarios if not s.get("turns"))

    print(f"\nStats:")
    print(f"  Total examples: {len(examples)}")
    print(f"  Tool call turns: {total_turns}")
    print(f"  No-tool responses: {no_tool}")
    print(f"\n  Tool usage breakdown:")
    for tool, count in sorted(tool_counts.items(), key=lambda x: -x[1]):
        print(f"    {tool}: {count}")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "zeroclaw_training_data.jsonl"
    generate_dataset(out)
