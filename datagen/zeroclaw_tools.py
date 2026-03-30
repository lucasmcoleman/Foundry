"""
ZeroClaw tool definitions scraped from source.

Each tool has: name, description, parameters (JSON Schema), and example invocations.
"""

TOOLS = [
    # === Core tools (always registered) ===
    {
        "name": "shell",
        "description": "Execute a shell command in the workspace directory",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to execute"},
                "approved": {"type": "boolean", "default": False, "description": "Set true to approve medium/high-risk commands in supervised mode"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "file_read",
        "description": "Read file contents with line numbers. Supports partial reading via offset and limit. Extracts text from PDF; other binary files are read with lossy UTF-8 conversion.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to file; relative resolves from workspace"},
                "offset": {"type": "integer", "description": "Starting line number, 1-based, default 1"},
                "limit": {"type": "integer", "description": "Maximum lines to return, default all"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "file_write",
        "description": "Write content to a file, creating it if it doesn't exist or overwriting if it does.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to file"},
                "content": {"type": "string", "description": "Content to write"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "file_edit",
        "description": "Perform exact string replacements in a file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to file"},
                "old_string": {"type": "string", "description": "Exact string to find"},
                "new_string": {"type": "string", "description": "Replacement string"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "glob_search",
        "description": "Search for files matching a glob pattern.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern (e.g. **/*.rs)"},
                "path": {"type": "string", "description": "Base directory to search from"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "content_search",
        "description": "Search file contents using ripgrep-style regex patterns.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
                "path": {"type": "string", "description": "Directory or file to search in"},
                "glob": {"type": "string", "description": "File glob filter (e.g. *.py)"},
            },
            "required": ["pattern"],
        },
    },
    # === Memory tools ===
    {
        "name": "memory_store",
        "description": "Store a key-value pair in persistent memory.",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Memory key"},
                "value": {"type": "string", "description": "Value to store"},
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "memory_recall",
        "description": "Recall a value from persistent memory by key or search query.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Key or search query"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_forget",
        "description": "Remove a key-value pair from persistent memory.",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Memory key to forget"},
            },
            "required": ["key"],
        },
    },
    # === Cron tools ===
    {
        "name": "cron_add",
        "description": "Create a new scheduled cron job.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Job name"},
                "schedule": {"type": "string", "description": "Cron expression (e.g. '0 */6 * * *')"},
                "command": {"type": "string", "description": "Command or prompt to execute"},
            },
            "required": ["name", "schedule", "command"],
        },
    },
    {
        "name": "cron_list",
        "description": "List all scheduled cron jobs.",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "cron_remove",
        "description": "Remove a scheduled cron job by name.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Job name to remove"},
            },
            "required": ["name"],
        },
    },
    # === Web tools ===
    {
        "name": "web_fetch",
        "description": "Fetch and extract content from a URL.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to fetch"},
                "prompt": {"type": "string", "description": "Extraction prompt for the content"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "web_search",
        "description": "Search the web and return results.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "http_request",
        "description": "Make an HTTP request to a URL.",
        "parameters": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "description": "HTTP method (GET, POST, PUT, DELETE, etc.)"},
                "url": {"type": "string", "description": "Request URL"},
                "headers": {"type": "object", "description": "Request headers"},
                "body": {"type": "string", "description": "Request body"},
            },
            "required": ["method", "url"],
        },
    },
    # === Git tools ===
    {
        "name": "git_operations",
        "description": "Perform git operations in the workspace repository.",
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "description": "Git operation (status, diff, log, add, commit, branch, checkout, push, pull, stash)"},
                "args": {"type": "array", "items": {"type": "string"}, "description": "Additional arguments"},
            },
            "required": ["operation"],
        },
    },
    # === Browser tools ===
    {
        "name": "browser",
        "description": "Control a headless browser for web automation.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "Browser action (navigate, click, type, screenshot, evaluate, scroll)"},
                "url": {"type": "string", "description": "URL to navigate to"},
                "selector": {"type": "string", "description": "CSS selector for element interaction"},
                "text": {"type": "string", "description": "Text to type"},
                "script": {"type": "string", "description": "JavaScript to evaluate"},
            },
            "required": ["action"],
        },
    },
    # === Project tools ===
    {
        "name": "project_intel",
        "description": "Analyze project structure, dependencies, and provide insights.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "Analysis action (overview, dependencies, structure, health)"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "security_ops",
        "description": "Run security analysis on the project.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "Security action (audit, scan, check)"},
                "target": {"type": "string", "description": "Target path or scope"},
            },
            "required": ["action"],
        },
    },
    # === Delegation tools ===
    {
        "name": "delegate",
        "description": "Delegate a task to a specialized sub-agent.",
        "parameters": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "Agent name or role to delegate to"},
                "task": {"type": "string", "description": "Task description"},
                "context": {"type": "string", "description": "Additional context for the agent"},
            },
            "required": ["agent", "task"],
        },
    },
    # === Notification tools ===
    {
        "name": "pushover",
        "description": "Send a push notification via Pushover.",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Notification message"},
                "title": {"type": "string", "description": "Notification title"},
                "priority": {"type": "integer", "description": "Priority level (-2 to 2)"},
            },
            "required": ["message"],
        },
    },
    # === Utility tools ===
    {
        "name": "pdf_read",
        "description": "Read and extract text from a PDF file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to PDF file"},
                "pages": {"type": "string", "description": "Page range (e.g. '1-5', '3')"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "screenshot",
        "description": "Take a screenshot of the screen or a window.",
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Target (screen, window name, or display)"},
                "output": {"type": "string", "description": "Output file path"},
            },
            "required": [],
        },
    },
    {
        "name": "tool_search",
        "description": "Search available tools by name or description.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query for tools"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "backup_tool",
        "description": "Create or restore backups of files and directories.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "Backup action (create, restore, list)"},
                "path": {"type": "string", "description": "Path to backup or restore"},
                "name": {"type": "string", "description": "Backup name"},
            },
            "required": ["action"],
        },
    },
]


def get_tool_by_name(name: str) -> dict | None:
    for t in TOOLS:
        if t["name"] == name:
            return t
    return None


def format_tools_for_system_prompt() -> str:
    """Format tools list as it appears in ZeroClaw's system prompt."""
    lines = []
    for t in TOOLS:
        import json
        lines.append(f"- **{t['name']}**: {t['description']}")
        lines.append(f"  Parameters: `{json.dumps(t['parameters'])}`")
    return "\n".join(lines)
