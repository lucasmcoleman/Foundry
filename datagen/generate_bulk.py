#!/usr/bin/env python3
"""
Hybrid training data generator for ZeroClaw tool-call scenarios.

Part 1: Programmatic expansion — deterministic scenarios for undertrained tools
         (cron, memory, http_request, web_search/fetch, pushover, delegate, etc.)
Part 2: LLM-powered generation — uses a local LMStudio model to generate
         creative multi-tool chains and diverse user requests.

Output: Python scenario dicts compatible with generate.py's scenario_to_messages().
Run generate.py after this to produce the final JSONL.
"""

import json
import sys
import time
import random
import urllib.request
import urllib.error

LMSTUDIO_URL = "http://localhost:1234/v1/chat/completions"
# Will be auto-detected from loaded models
LMSTUDIO_MODEL = None


def detect_model() -> str:
    """Auto-detect the best loaded LMStudio model for generation."""
    # Preference order: fast, capable models
    preferred = [
        "qwen3.5-27b-claude-4.6-opus-reasoning-distilled-v2",
        "qwen3-14b-claude-4.5-opus-high-reasoning-distill",
        "gpt-oss-20b",
        "hermes-4.3-36b",
        "qwen3.5-4b-claude-4.6-opus-reasoning-distilled",
    ]
    try:
        req = urllib.request.Request(
            "http://localhost:1234/v1/models",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            loaded = {m["id"] for m in data.get("data", [])}
            for p in preferred:
                if p in loaded:
                    return p
            # Fallback: pick any non-embedding model
            for m in data.get("data", []):
                mid = m["id"]
                if "embedding" not in mid:
                    return mid
    except Exception:
        pass
    return "gpt-oss-20b"


def llm_generate(prompt: str, system: str = "", temperature: float = 0.8,
                 max_tokens: int = 4096) -> str:
    """Call the local LMStudio model."""
    global LMSTUDIO_MODEL
    if LMSTUDIO_MODEL is None:
        LMSTUDIO_MODEL = detect_model()
        print(f"  Using model: {LMSTUDIO_MODEL}")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = json.dumps({
        "model": LMSTUDIO_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode()

    req = urllib.request.Request(
        LMSTUDIO_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"]
    except urllib.error.URLError as e:
        print(f"  LLM request failed: {e}", file=sys.stderr)
        return ""


# =============================================================================
# PART 1: Programmatic Scenarios
# =============================================================================

def programmatic_scenarios() -> list[dict]:
    """Generate deterministic scenarios for undertrained tools."""
    scenarios = []

    # --- CRON: add/list/remove cycles ---
    cron_jobs = [
        ("heartbeat-check", "*/5 * * * *", "Check system health and report anomalies", "every 5 minutes"),
        ("notion-sync", "0 */2 * * *", "Sync Notion tasks to local state", "every 2 hours"),
        ("daily-summary", "0 8 * * 1-5", "Generate and send daily work summary", "weekdays at 8am"),
        ("weekly-cleanup", "0 3 * * 0", "Clean up temp files and old logs", "Sundays at 3am"),
        ("memory-snapshot", "0 0 * * *", "Refresh memory snapshot from workspace files", "daily at midnight"),
        ("telegram-digest", "0 18 * * 1-5", "Send end-of-day Telegram digest", "weekdays at 6pm"),
        ("git-backup", "0 4 * * *", "Push workspace changes to git remote", "daily at 4am"),
        ("scriberr-poll", "*/15 * * * *", "Check Scriberr for new completed transcriptions", "every 15 minutes"),
    ]

    for name, schedule, command, human_schedule in cron_jobs:
        # Simple add
        scenarios.append({
            "user": f"Set up a cron job to {command.lower()} {human_schedule}",
            "turns": [
                {"tool": "cron_add", "args": {"name": name, "schedule": schedule, "command": command},
                 "result": f"Cron job '{name}' created successfully."},
            ],
            "final": f"Done — `{name}` is now scheduled to run {human_schedule}.",
        })

    # List + remove flow
    scenarios.append({
        "user": "What cron jobs are currently running?",
        "turns": [
            {"tool": "cron_list", "args": {},
             "result": "Active cron jobs:\n1. heartbeat-check — */5 * * * * — Check system health and report anomalies\n2. notion-sync — 0 */2 * * * — Sync Notion tasks to local state\n3. daily-summary — 0 8 * * 1-5 — Generate and send daily work summary\n4. weekly-cleanup — 0 3 * * 0 — Clean up temp files and old logs"},
        ],
        "final": "You have 4 active cron jobs:\n\n| Job | Schedule | Task |\n|---|---|---|\n| heartbeat-check | Every 5 min | System health checks |\n| notion-sync | Every 2 hours | Notion task sync |\n| daily-summary | Weekdays 8am | Work summary |\n| weekly-cleanup | Sundays 3am | Temp/log cleanup |",
    })

    scenarios.append({
        "user": "Remove the weekly cleanup job",
        "turns": [
            {"tool": "cron_remove", "args": {"name": "weekly-cleanup"},
             "result": "Cron job 'weekly-cleanup' removed."},
        ],
        "final": "Removed `weekly-cleanup`. It won't run anymore.",
    })

    # List then modify
    scenarios.append({
        "user": "Change the heartbeat to run every 10 minutes instead of 5",
        "turns": [
            {"tool": "cron_remove", "args": {"name": "heartbeat-check"},
             "result": "Cron job 'heartbeat-check' removed."},
            {"tool": "cron_add", "args": {"name": "heartbeat-check", "schedule": "*/10 * * * *", "command": "Check system health and report anomalies"},
             "result": "Cron job 'heartbeat-check' created successfully."},
        ],
        "final": "Updated — heartbeat now runs every 10 minutes instead of 5.",
    })

    # Error: duplicate name
    scenarios.append({
        "user": "Add a daily backup cron job",
        "turns": [
            {"tool": "cron_add", "args": {"name": "daily-backup", "schedule": "0 2 * * *", "command": "Backup workspace to remote storage"},
             "result": "Error: A cron job named 'daily-backup' already exists.", "status": "error"},
            {"tool": "cron_list", "args": {},
             "result": "Active cron jobs:\n1. daily-backup — 0 3 * * * — Push database dumps to S3"},
        ],
        "final": "There's already a `daily-backup` job running at 3am (database dumps to S3). Want me to rename the new one, or replace the existing job?",
    })

    # --- MEMORY: store/recall/forget cycles ---
    memory_entries = [
        ("user-timezone", "Eastern Time (ET)", "Remember my timezone is Eastern"),
        ("deploy-branch", "release/v2.4", "The current deploy branch is release/v2.4"),
        ("api-key-location", "Stored in .env under OPENAI_API_KEY", "Where is the OpenAI API key stored?"),
        ("db-migration-status", "Last migration: 2026-03-28, pending: add_user_preferences table", "Track the database migration status"),
        ("project-deadline", "MVP due April 15, 2026", "The MVP deadline is April 15th"),
        ("preferred-model", "qwen3.5-27b for complex tasks, qwen3-4b for simple ones", "My preferred models are qwen3.5-27b for complex and qwen3-4b for simple tasks"),
        ("lucas-schedule", "WFH Mon-Tue-Thu-Fri, office Wed. 9am-5pm ET", "Lucas works from home except Wednesdays"),
        ("notion-workspace", "Tasks tracked in Notion, workspace ID: abc123", "Notion is the task management system"),
    ]

    for key, value, user_msg in memory_entries:
        scenarios.append({
            "user": user_msg,
            "turns": [
                {"tool": "memory_store", "args": {"key": key, "value": value},
                 "result": f"Stored '{key}' in memory."},
            ],
            "final": f"Got it, saved to memory.",
        })

    # Recall patterns
    scenarios.append({
        "user": "What timezone am I in?",
        "turns": [
            {"tool": "memory_recall", "args": {"query": "timezone"},
             "result": "user-timezone: Eastern Time (ET)"},
        ],
        "final": "You're in Eastern Time (ET).",
    })

    scenarios.append({
        "user": "What branch are we deploying from?",
        "turns": [
            {"tool": "memory_recall", "args": {"query": "deploy branch"},
             "result": "deploy-branch: release/v2.4"},
        ],
        "final": "Current deploy branch is `release/v2.4`.",
    })

    scenarios.append({
        "user": "When is the MVP due?",
        "turns": [
            {"tool": "memory_recall", "args": {"query": "deadline"},
             "result": "project-deadline: MVP due April 15, 2026"},
        ],
        "final": "MVP is due April 15, 2026.",
    })

    scenarios.append({
        "user": "What model should I use for a quick question?",
        "turns": [
            {"tool": "memory_recall", "args": {"query": "preferred model"},
             "result": "preferred-model: qwen3.5-27b for complex tasks, qwen3-4b for simple ones"},
        ],
        "final": "For quick questions, use `qwen3-4b`. Save `qwen3.5-27b` for complex tasks.",
    })

    # Recall miss
    scenarios.append({
        "user": "What's the staging server URL?",
        "turns": [
            {"tool": "memory_recall", "args": {"query": "staging server URL"},
             "result": "No matching memories found."},
        ],
        "final": "I don't have the staging server URL stored. Want to tell me so I can save it?",
    })

    # Forget
    scenarios.append({
        "user": "Forget the deploy branch — we switched to trunk-based development",
        "turns": [
            {"tool": "memory_forget", "args": {"key": "deploy-branch"},
             "result": "Memory 'deploy-branch' removed."},
        ],
        "final": "Removed. No more deploy branch tracking — trunk-based it is.",
    })

    # Store + recall in same session
    scenarios.append({
        "user": "The new staging URL is staging.example.com — remember that",
        "turns": [
            {"tool": "memory_store", "args": {"key": "staging-url", "value": "staging.example.com"},
             "result": "Stored 'staging-url' in memory."},
        ],
        "final": "Saved — staging URL is `staging.example.com`.",
    })

    # --- HTTP_REQUEST: API interactions ---
    scenarios.append({
        "user": "Check if the production API is healthy",
        "turns": [
            {"tool": "http_request", "args": {"method": "GET", "url": "https://api.example.com/health"},
             "result": '{"status": "healthy", "version": "2.4.1", "uptime_hours": 142.5, "db_connected": true}'},
        ],
        "final": "Production API is healthy:\n- Version: 2.4.1\n- Uptime: ~142 hours (~6 days)\n- Database: connected",
    })

    scenarios.append({
        "user": "Create a new task in Notion: 'Review PR #42'",
        "think": "I need to create a task in Notion using their API. I'll use the http_request tool to POST to the Notion API.",
        "turns": [
            {"tool": "http_request", "args": {
                "method": "POST",
                "url": "https://api.notion.com/v1/pages",
                "headers": {"Authorization": "Bearer secret_xxx", "Notion-Version": "2022-06-28", "Content-Type": "application/json"},
                "body": '{"parent": {"database_id": "abc123"}, "properties": {"Name": {"title": [{"text": {"content": "Review PR #42"}}]}, "Status": {"select": {"name": "Not started"}}}}'
            }, "result": '{"id": "page_789", "created_time": "2026-03-30T14:00:00Z", "url": "https://notion.so/page_789"}'},
        ],
        "final": "Created in Notion: **Review PR #42** (Not started).",
    })

    scenarios.append({
        "user": "Send a POST to the webhook at hooks.example.com/deploy with branch=main",
        "turns": [
            {"tool": "http_request", "args": {
                "method": "POST",
                "url": "https://hooks.example.com/deploy",
                "headers": {"Content-Type": "application/json"},
                "body": '{"branch": "main"}'
            }, "result": '{"ok": true, "deploy_id": "dep_4521", "message": "Deployment queued"}'},
        ],
        "final": "Deployment queued (ID: `dep_4521`). Branch `main` is being deployed.",
    })

    scenarios.append({
        "user": "Check the status of deploy dep_4521",
        "turns": [
            {"tool": "http_request", "args": {"method": "GET", "url": "https://hooks.example.com/deploy/dep_4521"},
             "result": '{"deploy_id": "dep_4521", "status": "running", "progress": 65, "started_at": "2026-03-30T14:01:00Z"}'},
        ],
        "final": "Deploy `dep_4521` is running — 65% complete.",
    })

    # HTTP error handling
    scenarios.append({
        "user": "Hit the analytics API at analytics.internal/api/v2/stats",
        "turns": [
            {"tool": "http_request", "args": {"method": "GET", "url": "http://analytics.internal/api/v2/stats"},
             "result": "Error: Connection refused (111)", "status": "error"},
        ],
        "final": "Connection refused — the analytics service at `analytics.internal` appears to be down. Is it running?",
    })

    scenarios.append({
        "user": "Send a Telegram message via the bot API: tell the group 'deploy complete'",
        "turns": [
            {"tool": "http_request", "args": {
                "method": "POST",
                "url": "https://api.telegram.org/bot<TOKEN>/sendMessage",
                "headers": {"Content-Type": "application/json"},
                "body": '{"chat_id": "-100123456", "text": "Deploy complete ✅"}'
            }, "result": '{"ok": true, "result": {"message_id": 4521}}'},
        ],
        "final": "Message sent to the group.",
    })

    # --- WEB_SEARCH + WEB_FETCH ---
    scenarios.append({
        "user": "Search for the latest ROCm compatibility list",
        "turns": [
            {"tool": "web_search", "args": {"query": "ROCm 6.x GPU compatibility list 2026"},
             "result": "1. ROCm 6.4 Supported GPUs — AMD Official Documentation\n   https://rocm.docs.amd.com/en/latest/compatibility/compatibility-matrix.html\n2. ROCm 6.4 Release Notes\n   https://github.com/ROCm/ROCm/releases/tag/rocm-6.4\n3. Reddit: ROCm 6.4 adds gfx1151 support\n   https://reddit.com/r/LocalLLaMA/comments/..."},
        ],
        "final": "Found the official compatibility list. Want me to fetch the full page from AMD's docs?",
    })

    scenarios.append({
        "user": "Yeah pull up the AMD docs page",
        "turns": [
            {"tool": "web_fetch", "args": {"url": "https://rocm.docs.amd.com/en/latest/compatibility/compatibility-matrix.html", "prompt": "Extract the list of supported GPUs and architectures"},
             "result": "Supported GPUs for ROCm 6.4:\n\nRDNA 3.5: gfx1150, gfx1151 (Strix Point, Strix Halo)\nRDNA 3: gfx1100, gfx1101, gfx1102 (Navi 31/32/33)\nCDNA 3: gfx940, gfx941, gfx942 (MI300 series)\nCDNA 2: gfx90a (MI250)\n\nNote: RDNA 3.5 support is experimental."},
        ],
        "final": "ROCm 6.4 supported GPUs:\n\n| Architecture | GPUs | Notes |\n|---|---|---|\n| RDNA 3.5 | gfx1150, gfx1151 | Strix Point/Halo — **experimental** |\n| RDNA 3 | gfx1100-1102 | Navi 31/32/33 |\n| CDNA 3 | gfx940-942 | MI300 series |\n| CDNA 2 | gfx90a | MI250 |\n\nYour Strix Halo (gfx1151) is listed under experimental RDNA 3.5 support.",
    })

    scenarios.append({
        "user": "Look up the Python argparse documentation for subparsers",
        "turns": [
            {"tool": "web_search", "args": {"query": "python argparse subparsers documentation"},
             "result": "1. argparse — Parser for command-line options — Python 3.12 docs\n   https://docs.python.org/3/library/argparse.html#sub-commands"},
            {"tool": "web_fetch", "args": {"url": "https://docs.python.org/3/library/argparse.html#sub-commands", "prompt": "Extract the subparsers usage example and key methods"},
             "result": "Sub-commands:\n\nparser.add_subparsers() creates a special action object.\n\nKey methods:\n- add_parser(name) — add a subcommand\n- set_defaults(func=handler) — associate handler function\n\nExample:\nsubparsers = parser.add_subparsers(dest='command')\nparser_a = subparsers.add_parser('checkout')\nparser_a.add_argument('branch')\nparser_a.set_defaults(func=do_checkout)"},
        ],
        "final": "Here's the pattern:\n\n```python\nsubparsers = parser.add_subparsers(dest='command')\n\n# Add subcommands\nparser_a = subparsers.add_parser('checkout')\nparser_a.add_argument('branch')\nparser_a.set_defaults(func=do_checkout)\n```\n\nKey: use `dest='command'` to capture which subcommand was chosen, and `set_defaults(func=...)` to route to the handler.",
    })

    # --- PUSHOVER: notifications ---
    scenarios.append({
        "user": "Send me a notification that the build finished",
        "turns": [
            {"tool": "pushover", "args": {"message": "Build completed successfully.", "title": "Build Complete", "priority": 0},
             "result": "Notification sent successfully."},
        ],
        "final": "Sent you a push notification.",
    })

    scenarios.append({
        "user": "Alert me urgently — the production database is down",
        "turns": [
            {"tool": "pushover", "args": {"message": "ALERT: Production database is unreachable. Connection refused on db.internal:5432.", "title": "🚨 DB Down", "priority": 2},
             "result": "Notification sent successfully (priority: emergency)."},
        ],
        "final": "Emergency notification sent. Priority 2 means it'll keep alerting until you acknowledge it.",
    })

    scenarios.append({
        "user": "Quietly log that the nightly backup succeeded — don't buzz my phone",
        "turns": [
            {"tool": "pushover", "args": {"message": "Nightly backup completed. 12.4 GB synced to remote.", "title": "Backup OK", "priority": -1},
             "result": "Notification sent successfully (priority: low/silent)."},
        ],
        "final": "Logged silently — no buzz, it'll show up next time you check.",
    })

    # --- DELEGATE ---
    scenarios.append({
        "user": "Research the best practices for fine-tuning LLMs with LoRA",
        "turns": [
            {"tool": "delegate", "args": {"agent": "researcher", "task": "Research best practices for fine-tuning LLMs using LoRA/QLoRA", "context": "Focus on: optimal rank values, learning rates, which layers to target, dataset size requirements, and common pitfalls. Include recent findings from 2025-2026."},
             "result": "Research complete. Key findings:\n1. Rank: r=16-64 is the sweet spot for most tasks. Higher ranks give diminishing returns.\n2. Learning rate: 1e-4 to 2e-4 with cosine scheduler works best.\n3. Target modules: q_proj, k_proj, v_proj, o_proj at minimum. Adding gate_proj/up_proj/down_proj helps for style transfer.\n4. Dataset: 500-5000 high-quality examples beats 50k noisy ones.\n5. Pitfalls: Overfitting on small datasets (use dropout=0.05), catastrophic forgetting (keep lr low), broken chat templates after merge."},
        ],
        "final": "Key LoRA fine-tuning best practices:\n\n1. **Rank**: r=16-64 is the sweet spot\n2. **Learning rate**: 1e-4 to 2e-4 with cosine decay\n3. **Target modules**: At minimum Q/K/V/O projections; add gate/up/down for style transfer\n4. **Dataset**: Quality over quantity — 500-5000 clean examples > 50k noisy\n5. **Watch out for**: Overfitting (use dropout=0.05), catastrophic forgetting (keep lr conservative), broken chat templates post-merge",
    })

    scenarios.append({
        "user": "Have an agent summarize today's git activity across all repos",
        "turns": [
            {"tool": "delegate", "args": {"agent": "reporter", "task": "Summarize today's git activity", "context": "Check git log --since='today' in workspace and any known sub-repos. Report: commits, files changed, who committed."},
             "result": "Git activity for 2026-03-30:\n\nWorkspace (main):\n- 3 commits by Lucas\n  - fix: update stale path reference (ca8b236)\n  - feat: add hf_upload module (bb68eb7)\n  - fix: replace Unsloth imports (cc18026)\n- 8 files changed, +420 -85 lines\n\nMagicQuant:\n- 2 commits by Lucas\n  - fix: block-size compatibility check (3f9641f)\n  - fix: llama.cpp compat guards (dfb9c73)\n- 2 files changed, +95 -12 lines"},
        ],
        "final": "**Today's git activity (2026-03-30):**\n\nWorkspace: 3 commits, 8 files changed (+420 -85)\nMagicQuant: 2 commits, 2 files changed (+95 -12)\n\nAll commits by Lucas. Main changes: HF upload module, Unsloth removal, MagicQuant GGUF compatibility fixes.",
    })

    # --- BACKUP_TOOL ---
    scenarios.append({
        "user": "Back up the workspace before I make breaking changes",
        "turns": [
            {"tool": "backup_tool", "args": {"action": "create", "path": ".", "name": "pre-refactor-backup"},
             "result": "Backup 'pre-refactor-backup' created successfully. 142 files, 24.5 MB."},
        ],
        "final": "Backup saved as `pre-refactor-backup` (142 files, 24.5 MB). You can restore with the backup tool if needed.",
    })

    scenarios.append({
        "user": "What backups do I have?",
        "turns": [
            {"tool": "backup_tool", "args": {"action": "list"},
             "result": "Available backups:\n1. pre-refactor-backup — 2026-03-30 14:00 — 24.5 MB\n2. pre-deploy-backup — 2026-03-28 09:00 — 22.1 MB\n3. initial-state — 2026-03-15 10:00 — 18.3 MB"},
        ],
        "final": "3 backups:\n\n| Name | Date | Size |\n|---|---|---|\n| pre-refactor-backup | Mar 30 | 24.5 MB |\n| pre-deploy-backup | Mar 28 | 22.1 MB |\n| initial-state | Mar 15 | 18.3 MB |",
    })

    scenarios.append({
        "user": "The refactor broke everything — roll back",
        "turns": [
            {"tool": "backup_tool", "args": {"action": "restore", "name": "pre-refactor-backup"},
             "result": "Backup 'pre-refactor-backup' restored. 142 files restored to workspace."},
        ],
        "final": "Rolled back to `pre-refactor-backup`. Workspace is restored to the pre-refactor state.",
    })

    # --- MULTI-TOOL CHAINS ---
    # Check health → store result → notify
    scenarios.append({
        "user": "Do a health check on all services and notify me with the results",
        "think": "I need to check the API health, then store the result for tracking, then send a notification.",
        "turns": [
            {"tool": "http_request", "args": {"method": "GET", "url": "http://localhost:8080/health"},
             "result": '{"api": "ok", "db": "ok", "cache": "degraded", "queue": "ok"}'},
            {"tool": "memory_store", "args": {"key": "last-health-check", "value": "2026-03-30 14:00 UTC — api:ok, db:ok, cache:degraded, queue:ok"},
             "result": "Stored 'last-health-check' in memory."},
            {"tool": "pushover", "args": {"message": "Health check: API ✓, DB ✓, Cache ⚠️ (degraded), Queue ✓", "title": "Service Health", "priority": 0},
             "result": "Notification sent successfully."},
        ],
        "final": "Health check complete. Everything is OK except the cache, which is in a degraded state. Results saved to memory and notification sent.\n\n| Service | Status |\n|---|---|\n| API | OK |\n| DB | OK |\n| Cache | ⚠️ Degraded |\n| Queue | OK |",
    })

    # Read file → edit → git commit
    scenarios.append({
        "user": "Bump the version in setup.py to 2.5.0 and commit it",
        "turns": [
            {"tool": "file_read", "args": {"path": "setup.py"},
             "result": '1 | from setuptools import setup\n2 | \n3 | setup(\n4 |     name="myproject",\n5 |     version="2.4.1",\n6 |     packages=["src"],\n7 | )'},
            {"tool": "file_edit", "args": {"path": "setup.py", "old_string": 'version="2.4.1"', "new_string": 'version="2.5.0"'},
             "result": "File edited successfully."},
            {"tool": "git_operations", "args": {"operation": "add", "args": ["setup.py"]},
             "result": ""},
            {"tool": "git_operations", "args": {"operation": "commit", "args": ["-m", "chore: bump version to 2.5.0"]},
             "result": "[main abc1234] chore: bump version to 2.5.0\n 1 file changed, 1 insertion(+), 1 deletion(-)"},
        ],
        "final": "Version bumped from 2.4.1 → 2.5.0 and committed (`abc1234`).",
    })

    # Search → read → explain
    scenarios.append({
        "user": "Where is the database connection configured?",
        "turns": [
            {"tool": "content_search", "args": {"pattern": "DATABASE_URL|db_connect|create_engine", "glob": "*.py"},
             "result": "src/config.py:12: DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///local.db')\nsrc/db.py:8: engine = create_engine(config.DATABASE_URL, pool_size=10)"},
            {"tool": "file_read", "args": {"path": "src/db.py"},
             "result": '1 | from sqlalchemy import create_engine\n2 | from sqlalchemy.orm import sessionmaker\n3 | from src import config\n4 | \n5 | engine = create_engine(\n6 |     config.DATABASE_URL,\n7 |     pool_size=10,\n8 |     max_overflow=20,\n9 |     echo=False,\n10| )\n11| Session = sessionmaker(bind=engine)'},
        ],
        "final": "Database connection is set up in two places:\n\n1. **`src/config.py:12`** — reads `DATABASE_URL` from environment (falls back to `sqlite:///local.db`)\n2. **`src/db.py`** — creates the SQLAlchemy engine with pool_size=10, max_overflow=20\n\nTo change the database, set the `DATABASE_URL` environment variable.",
    })

    # Glob → read → write pattern
    scenarios.append({
        "user": "Find all YAML config files and show me what's in them",
        "turns": [
            {"tool": "glob_search", "args": {"pattern": "**/*.yaml"},
             "result": "configs/training.yaml\nconfigs/export.yaml\nconfigs/deploy.yaml"},
            {"tool": "file_read", "args": {"path": "configs/training.yaml"},
             "result": "1 | model: qwen3.5-27b\n2 | epochs: 3\n3 | lr: 2e-4\n4 | batch_size: 4"},
            {"tool": "file_read", "args": {"path": "configs/export.yaml"},
             "result": "1 | format: safetensors\n2 | quantize: true\n3 | output_dir: ./output"},
            {"tool": "file_read", "args": {"path": "configs/deploy.yaml"},
             "result": "1 | host: 0.0.0.0\n2 | port: 8080\n3 | workers: 4"},
        ],
        "final": "Found 3 YAML configs:\n\n**training.yaml** — qwen3.5-27b, 3 epochs, lr=2e-4, batch=4\n**export.yaml** — safetensors format, quantization on, output to ./output\n**deploy.yaml** — serving on 0.0.0.0:8080 with 4 workers",
    })

    # Error recovery: tool fails → try alternative
    scenarios.append({
        "user": "What version of Node.js is installed?",
        "turns": [
            {"tool": "shell", "args": {"command": "node --version"},
             "result": "bash: node: command not found", "status": "error"},
            {"tool": "shell", "args": {"command": "which nodejs 2>/dev/null || nvm ls 2>/dev/null || echo 'Node.js is not installed'"},
             "result": "Node.js is not installed"},
        ],
        "final": "Node.js isn't installed on this system. Want me to install it? I can use `nvm` or the system package manager.",
    })

    # Security scan + fix
    scenarios.append({
        "user": "Run a security check on the project",
        "turns": [
            {"tool": "security_ops", "args": {"action": "audit", "target": "."},
             "result": "Security Audit Results:\n\nHIGH: Hardcoded API key found in src/config.py:15\nMEDIUM: pip packages with known CVEs: requests==2.28.0 (CVE-2023-32681)\nLOW: .env file not in .gitignore"},
            {"tool": "file_read", "args": {"path": "src/config.py", "offset": 13, "limit": 5},
             "result": "13 | # API Configuration\n14 | API_BASE = 'https://api.example.com'\n15 | API_KEY = 'sk-live-abc123def456'\n16 | \n17 | # Timeouts"},
        ],
        "final": "Found 3 issues:\n\n1. **HIGH** — Hardcoded API key in `src/config.py:15`. This should be in an environment variable.\n2. **MEDIUM** — `requests==2.28.0` has a known CVE. Upgrade to 2.32+.\n3. **LOW** — `.env` isn't in `.gitignore`.\n\nWant me to fix the API key issue first? I'll move it to an env var.",
    })

    # Cron list → remove multiple → add replacement
    scenarios.append({
        "user": "Clear all the old cron jobs and set up a single heartbeat every 10 minutes",
        "turns": [
            {"tool": "cron_list", "args": {},
             "result": "Active cron jobs:\n1. old-heartbeat — */5 * * * * — Legacy health check\n2. stale-sync — 0 */6 * * * — Outdated sync task\n3. test-job — * * * * * — Debug test (every minute)"},
            {"tool": "cron_remove", "args": {"name": "old-heartbeat"},
             "result": "Cron job 'old-heartbeat' removed."},
            {"tool": "cron_remove", "args": {"name": "stale-sync"},
             "result": "Cron job 'stale-sync' removed."},
            {"tool": "cron_remove", "args": {"name": "test-job"},
             "result": "Cron job 'test-job' removed."},
            {"tool": "cron_add", "args": {"name": "heartbeat", "schedule": "*/10 * * * *", "command": "Check system health and report anomalies via Telegram"},
             "result": "Cron job 'heartbeat' created successfully."},
        ],
        "final": "Cleared all 3 old jobs and set up a fresh `heartbeat` running every 10 minutes.",
    })

    # Memory recall → make decision → store update
    scenarios.append({
        "user": "Should I send Lucas a reminder right now?",
        "think": "I need to check what time zone Lucas is in and what his notification preferences are before deciding.",
        "turns": [
            {"tool": "memory_recall", "args": {"query": "lucas schedule notification preferences"},
             "result": "lucas-schedule: WFH Mon-Tue-Thu-Fri, office Wed. 9am-5pm ET\nuser-timezone: Eastern Time (ET)"},
            {"tool": "shell", "args": {"command": "TZ='America/New_York' date '+%A %H:%M'"},
             "result": "Saturday 21:30"},
        ],
        "final": "No — it's Saturday at 9:30 PM ET. Lucas's work reminders are weekdays only (8am-6pm). Personal reminders go out Saturdays at 9am, which has already passed. I'd hold this until Monday morning unless it's urgent.",
    })

    return scenarios


# =============================================================================
# PART 2: LLM-Generated Scenarios
# =============================================================================

LLM_SYSTEM_PROMPT = """You generate training data as a Python list of dicts. Output ONLY the Python list. No markdown, no explanation, no thinking.

CRITICAL: You may ONLY use these exact tool names (nothing else):
shell, file_read, file_write, file_edit, glob_search, content_search, memory_store, memory_recall, memory_forget, cron_add, cron_list, cron_remove, web_fetch, web_search, http_request, git_operations, browser, delegate, pushover, backup_tool, pdf_read, screenshot, project_intel, security_ops, tool_search

Tool argument schemas:
- shell: {"command": str, "approved": bool (optional, for destructive ops)}
- file_read: {"path": str, "offset": int (optional), "limit": int (optional)}
- file_write: {"path": str, "content": str}
- file_edit: {"path": str, "old_string": str, "new_string": str}
- glob_search: {"pattern": str, "path": str (optional)}
- content_search: {"pattern": str, "path": str (optional), "glob": str (optional)}
- memory_store: {"key": str, "value": str}
- memory_recall: {"query": str}
- memory_forget: {"key": str}
- cron_add: {"name": str, "schedule": str (cron expr), "command": str}
- cron_list: {}
- cron_remove: {"name": str}
- web_fetch: {"url": str, "prompt": str (optional)}
- web_search: {"query": str}
- http_request: {"method": str, "url": str, "headers": dict (optional), "body": str (optional)}
- git_operations: {"operation": str, "args": list[str] (optional)}
- browser: {"action": str, "url": str (optional), "selector": str (optional), "text": str (optional), "script": str (optional)}
- delegate: {"agent": str, "task": str, "context": str (optional)}
- pushover: {"message": str, "title": str (optional), "priority": int (optional)}
- backup_tool: {"action": str, "path": str (optional), "name": str (optional)}
- pdf_read: {"path": str, "pages": str (optional)}
- screenshot: {"target": str (optional), "output": str (optional)}
- project_intel: {"action": str}
- security_ops: {"action": str, "target": str (optional)}

Each scenario dict: {"user": str, "turns": [{"tool": str, "args": dict, "result": str, "status": str (optional, "error" for failures)}], "final": str, "think": str (optional)}

The assistant style is direct and concise. No filler."""

LLM_GENERATION_PROMPTS = [
    # Multi-tool chains
    """Generate 3 scenarios where the assistant uses 2-4 different tools in sequence to accomplish a task. Focus on realistic workflows like:
- Searching for something, reading the result, then editing it
- Checking system state, making a decision, then acting on it
- Fetching external data, processing it, then storing/reporting it

Each scenario should use a DIFFERENT combination of tools. Make user requests natural and conversational.""",

    # Error recovery
    """Generate 3 scenarios where a tool call fails and the assistant recovers gracefully. Types of failures:
- Shell command not found → try alternative
- File not found → search for correct path
- HTTP request timeout → retry or report
- Permission denied → ask for approval or find workaround
- Invalid arguments → correct and retry

Use status: "error" for failed tool results. Show the assistant trying a different approach.""",

    # Cron management (advanced)
    """Generate 3 scenarios involving cron job management. Include:
- User wants to reschedule an existing job (remove + add)
- User describes a schedule in natural language, assistant converts to cron expression
- User wants to see what ran recently and whether jobs are working
- User wants conditional cron (e.g., "only on weekdays")
- Debugging a cron job that isn't firing

Make the cron commands be agent prompts (text descriptions), not shell commands.""",

    # Memory patterns
    """Generate 3 scenarios involving memory_store, memory_recall, and memory_forget. Include:
- User tells assistant a preference → store it
- Assistant proactively recalls relevant context before answering
- User corrects outdated information → forget old + store new
- Assistant recalls multiple related memories to make a decision
- Memory miss → assistant asks user for the information

Make the memory keys descriptive (like "project-deadline" or "deploy-process").""",

    # Git workflows
    """Generate 3 scenarios with git_operations combined with other tools. Include:
- Branch + edit + commit flow
- Check diff → review changes → push
- Resolve conflict by reading both versions
- Stash → switch branch → do work → switch back → unstash
- Check log to find when a bug was introduced

Use realistic file names and commit messages.""",

    # Proactive/agentic behavior
    """Generate 3 scenarios where the assistant shows proactive, agentic behavior:
- User asks a vague question, assistant investigates thoroughly before answering
- Assistant notices a problem while doing something else and reports it
- Assistant chains 3+ tools to fully complete a task without asking for intermediate input
- Assistant uses memory to contextualize a request (recall → act → store result)
- Assistant delegates a research task and then uses the results

Show thinking with the "think" key where the assistant reasons about approach.""",

    # HTTP/API interactions
    """Generate 3 scenarios involving http_request for API interactions:
- Checking a service status endpoint
- Creating/updating resources via REST API
- Querying a search API and presenting results
- Handling API authentication (with headers)
- Dealing with rate limiting or API errors

Use realistic API patterns (REST, JSON bodies, auth headers).""",

    # File operations (diverse)
    """Generate 3 scenarios with file operations (read, write, edit, glob, content_search):
- User asks to refactor: search for pattern across codebase → edit multiple files
- Create a new config file based on an existing template
- Find and fix a typo across the project
- Read a log file and extract key information
- Generate a report file from collected data

Use realistic file paths and content.""",

    # --- Additional prompts for higher volume ---

    # Web automation
    """Generate 3 scenarios using the browser tool for web automation:
- Navigate to a page, take a screenshot, report what's there
- Fill out a form by typing into fields and clicking submit
- Evaluate JavaScript on a page to extract data
- Scroll through a page and click on specific elements

Use realistic web pages and CSS selectors.""",

    # Notification + monitoring patterns
    """Generate 3 scenarios combining pushover notifications with other tools:
- Monitor a service (http_request) → alert on failure (pushover)
- Complete a long task (shell/delegate) → notify when done (pushover)
- Check for overdue items (memory_recall) → send reminder (pushover)

Use different priority levels (-2 to 2) appropriately.""",

    # DevOps workflows
    """Generate 3 scenarios for DevOps tasks using shell, git_operations, and http_request:
- Deploy flow: git pull → run tests → restart service → verify health
- Debug: check logs → find error → read relevant code → suggest fix
- Infrastructure: check disk/memory → clean up → verify improvement

Use realistic commands and output.""",

    # Data processing
    """Generate 3 scenarios involving file operations for data processing:
- Read a CSV/JSON data file → process it → write results
- Search logs for errors → aggregate counts → write report
- Read config → validate it → fix issues → write corrected version

Include realistic file contents in tool results.""",

    # Project management / Notion-style
    """Generate 3 scenarios where the assistant manages tasks via http_request to an API:
- List tasks from a project management API and summarize
- Create a new task with metadata (priority, due date, assignee)
- Update task status and add a comment

Use REST API patterns with JSON bodies and realistic responses.""",

    # Security and maintenance
    """Generate 3 scenarios involving security_ops, backup_tool, or project_intel:
- Run security audit → find issues → fix the critical one
- Create backup → make risky change → verify it worked (no restore needed)
- Analyze project health → identify problems → report findings

Use realistic security findings and project analysis output.""",

    # Search + research patterns
    """Generate 3 scenarios using web_search and web_fetch together:
- Research a technical topic → fetch docs → summarize findings
- Look up an error message → find solution → apply fix
- Find latest version of a tool → fetch changelog → report changes

Use realistic search results and web page content.""",

    # Delegation patterns
    """Generate 3 scenarios using the delegate tool:
- Delegate a code review to a sub-agent and present findings
- Delegate data collection to one agent while doing other work
- Delegate a complex research task and then act on the results

Use agent names like "researcher", "reviewer", "analyzer".""",

    # Mixed tool variety (catch-all for coverage)
    """Generate 3 scenarios using tools that are LESS common: pdf_read, screenshot, tool_search, glob_search, project_intel. Each scenario should use a different one of these tools combined with 1-2 other tools.

Make each scenario solve a realistic problem.""",
]


def parse_llm_scenarios(raw: str) -> list[dict]:
    """Parse LLM output into scenario dicts. Handles common formatting issues."""
    raw = raw.strip()

    # Remove markdown fences if present
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) >= 3 else parts[1] if len(parts) >= 2 else raw
        if raw.startswith("python"):
            raw = raw[6:]
        raw = raw.strip()

    # Remove leading think tags
    if "<think>" in raw:
        idx = raw.find("</think>")
        if idx != -1:
            raw = raw[idx + 8:].strip()

    # Try to parse as complete JSON array first
    try:
        result = json.loads(raw)
        if isinstance(result, list):
            return [s for s in result if validate_scenario(s)]
        if isinstance(result, dict):
            return [result] if validate_scenario(result) else []
    except json.JSONDecodeError:
        pass

    # Try eval (handles Python-style True/False/None)
    try:
        result = __import__('ast').literal_eval(raw)
        if isinstance(result, list):
            return [s for s in result if validate_scenario(s)]
        if isinstance(result, dict):
            return [result] if validate_scenario(result) else []
    except Exception:
        pass

    # Extract individual JSON objects using brace matching (handles nesting)
    scenarios = []
    i = 0
    while i < len(raw):
        if raw[i] == '{':
            # Try progressively longer substrings starting from this brace
            depth = 0
            for j in range(i, len(raw)):
                if raw[j] == '{':
                    depth += 1
                elif raw[j] == '}':
                    depth -= 1
                    if depth == 0:
                        candidate = raw[i:j+1]
                        try:
                            s = json.loads(candidate)
                            if validate_scenario(s):
                                scenarios.append(s)
                            i = j + 1
                            break
                        except json.JSONDecodeError:
                            try:
                                s = __import__('ast').literal_eval(candidate)
                                if isinstance(s, dict) and validate_scenario(s):
                                    scenarios.append(s)
                                i = j + 1
                                break
                            except Exception:
                                pass
                                # Continue looking for a larger match
            else:
                i += 1
        else:
            i += 1

    return scenarios


VALID_TOOLS = {
    "shell", "file_read", "file_write", "file_edit", "glob_search",
    "content_search", "memory_store", "memory_recall", "memory_forget",
    "cron_add", "cron_list", "cron_remove", "web_fetch", "web_search",
    "http_request", "git_operations", "browser", "project_intel",
    "security_ops", "delegate", "pushover", "pdf_read", "screenshot",
    "tool_search", "backup_tool",
}


def validate_scenario(s: dict) -> bool:
    """Check that a scenario dict has the required structure and valid tools."""
    if not isinstance(s, dict):
        return False
    if "user" not in s or "final" not in s:
        return False
    if not str(s.get("user", "")).strip():
        return False
    if not str(s.get("final", "")).strip():
        return False
    if "turns" in s:
        if not isinstance(s["turns"], list) or not s["turns"]:
            return False
        for t in s["turns"]:
            if not isinstance(t, dict):
                return False
            if "tool" not in t or "args" not in t or "result" not in t:
                return False
            if t["tool"] not in VALID_TOOLS:
                return False
            if not isinstance(t["result"], str):
                return False
            if not isinstance(t["args"], dict):
                return False
    return True


def generate_llm_scenarios(target_count: int = 200) -> list[dict]:
    """Use the local LLM to generate diverse scenarios."""
    all_scenarios = []
    prompts_used = 0

    print(f"\nGenerating LLM scenarios (target: {target_count})...")

    for prompt in LLM_GENERATION_PROMPTS:
        if len(all_scenarios) >= target_count:
            break

        prompts_used += 1
        print(f"\n  Prompt {prompts_used}/{len(LLM_GENERATION_PROMPTS)}: ", end="", flush=True)

        raw = llm_generate(prompt, system=LLM_SYSTEM_PROMPT, temperature=0.8)
        if not raw:
            print("FAILED (no response)")
            continue

        parsed = parse_llm_scenarios(raw)
        all_scenarios.extend(parsed)
        print(f"got {len(parsed)} scenarios (total: {len(all_scenarios)})")

        # Small delay to avoid hammering the model
        time.sleep(1)

    # If we haven't hit the target, do additional rounds with variation
    round_num = 0
    while len(all_scenarios) < target_count and round_num < 80:
        round_num += 1
        prompt_idx = round_num % len(LLM_GENERATION_PROMPTS)
        base_prompt = LLM_GENERATION_PROMPTS[prompt_idx]
        varied_prompt = base_prompt + f"\n\nIMPORTANT: Generate completely DIFFERENT scenarios from any you've seen. Be creative with user requests. Round {round_num + 1}."

        print(f"\n  Extra round {round_num}: ", end="", flush=True)
        raw = llm_generate(varied_prompt, system=LLM_SYSTEM_PROMPT, temperature=0.9)
        if raw:
            parsed = parse_llm_scenarios(raw)
            all_scenarios.extend(parsed)
            print(f"got {len(parsed)} scenarios (total: {len(all_scenarios)})")
        else:
            print("FAILED")

        time.sleep(1)

    return all_scenarios[:target_count]


# =============================================================================
# MAIN
# =============================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Generate bulk training scenarios")
    parser.add_argument("--output", default="/server/programming/pipeline/datagen/scenarios_generated.py",
                        help="Output Python file with SCENARIOS_GENERATED list")
    parser.add_argument("--append", action="store_true",
                        help="Append to existing generated scenarios instead of overwriting")
    parser.add_argument("--llm-count", type=int, default=200,
                        help="Target number of LLM-generated scenarios")
    parser.add_argument("--programmatic-only", action="store_true",
                        help="Skip LLM generation, only produce programmatic scenarios")
    parser.add_argument("--llm-only", action="store_true",
                        help="Skip programmatic, only produce LLM scenarios")
    parser.add_argument("--model", type=str, default=None,
                        help="Override LMStudio model name")
    parser.add_argument("--stats", action="store_true",
                        help="Print stats about generated scenarios")
    args = parser.parse_args()

    if args.model:
        global LMSTUDIO_MODEL
        LMSTUDIO_MODEL = args.model

    all_scenarios = []

    # Part 1: Programmatic
    if not args.llm_only:
        prog = programmatic_scenarios()
        print(f"Programmatic scenarios: {len(prog)}")
        all_scenarios.extend(prog)

    # Part 2: LLM-generated
    if not args.programmatic_only:
        llm = generate_llm_scenarios(args.llm_count)
        print(f"LLM-generated scenarios: {len(llm)}")
        all_scenarios.extend(llm)

    # If appending, load existing scenarios first
    if args.append:
        import re
        try:
            with open(args.output) as f:
                text = f.read()
            match = re.search(r'SCENARIOS_GENERATED\s*=\s*(\[.*)', text, re.DOTALL)
            if match:
                existing = json.loads(match.group(1))
                print(f"Appending to {len(existing)} existing scenarios")
                all_scenarios = existing + all_scenarios
        except FileNotFoundError:
            pass

    print(f"\nTotal new scenarios: {len(all_scenarios)}")

    # Write output
    output_path = args.output
    with open(output_path, "w") as f:
        f.write('"""\nAuto-generated training scenarios.\n')
        f.write(f"Generated {len(all_scenarios)} scenarios.\n")
        f.write('"""\n\n')
        f.write("SCENARIOS_GENERATED = ")
        # Pretty-print the list
        f.write(json.dumps(all_scenarios, indent=4, ensure_ascii=False))
        f.write("\n")

    print(f"Written to {output_path}")

    if args.stats:
        print_stats(all_scenarios)


def print_stats(scenarios: list[dict]):
    """Print coverage statistics."""
    tool_counts = {}
    no_tool = 0
    multi_tool = 0
    error_recovery = 0
    has_think = 0

    for s in scenarios:
        turns = s.get("turns", [])
        if not turns:
            no_tool += 1
            continue
        if len(turns) > 1:
            multi_tool += 1
        if s.get("think"):
            has_think += 1
        for t in turns:
            tool = t["tool"]
            tool_counts[tool] = tool_counts.get(tool, 0) + 1
            if t.get("status") == "error":
                error_recovery += 1

    print(f"\n--- Stats ---")
    print(f"Total: {len(scenarios)}")
    print(f"Single-tool: {len(scenarios) - no_tool - multi_tool}")
    print(f"Multi-tool chains: {multi_tool}")
    print(f"No-tool (text only): {no_tool}")
    print(f"Error recovery: {error_recovery}")
    print(f"With reasoning (think): {has_think}")
    print(f"\nTool coverage:")
    for tool, count in sorted(tool_counts.items(), key=lambda x: -x[1]):
        print(f"  {tool}: {count}")


if __name__ == "__main__":
    main()
