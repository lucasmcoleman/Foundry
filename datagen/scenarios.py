"""
Scenario definitions for ZeroClaw training data generation.

Each scenario defines a realistic user request, the expected tool call chain,
simulated tool outputs, and the final assistant response. Scenarios cover
single-tool, multi-tool, multi-turn, error-recovery, and reasoning patterns.
"""

SCENARIOS = [
    # =========================================================================
    # SHELL — basic commands
    # =========================================================================
    {
        "user": "List all files in the current directory",
        "turns": [
            {"tool": "shell", "args": {"command": "ls -la"}, "result": "total 48\ndrwxr-xr-x  5 user user 4096 Mar 15 10:00 .\ndrwxr-xr-x  3 user user 4096 Mar 14 09:00 ..\n-rw-r--r--  1 user user  234 Mar 15 10:00 README.md\n-rw-r--r--  1 user user 1024 Mar 15 09:30 main.py\ndrwxr-xr-x  2 user user 4096 Mar 15 08:00 src\ndrwxr-xr-x  2 user user 4096 Mar 14 12:00 tests"},
        ],
        "final": "Your workspace contains:\n- `README.md` — documentation\n- `main.py` — main entry point\n- `src/` — source directory\n- `tests/` — test directory",
    },
    {
        "user": "Check if Python 3 is installed and what version",
        "turns": [
            {"tool": "shell", "args": {"command": "python3 --version"}, "result": "Python 3.12.3"},
        ],
        "final": "Python 3.12.3 is installed.",
    },
    {
        "user": "How much disk space is available?",
        "turns": [
            {"tool": "shell", "args": {"command": "df -h ."}, "result": "Filesystem      Size  Used Avail Use% Mounted on\n/dev/sda1       500G  320G  180G  64% /"},
        ],
        "final": "You have 180GB available out of 500GB (64% used).",
    },
    {
        "user": "Install the requests library with pip",
        "turns": [
            {"tool": "shell", "args": {"command": "pip install requests"}, "result": "Collecting requests\n  Downloading requests-2.32.3-py3-none-any.whl (64 kB)\nInstalling collected packages: requests\nSuccessfully installed requests-2.32.3"},
        ],
        "final": "Done — `requests` 2.32.3 has been installed.",
    },
    {
        "user": "Run the test suite",
        "turns": [
            {"tool": "shell", "args": {"command": "pytest tests/ -v"}, "result": "============================= test session starts ==============================\ncollected 12 items\n\ntests/test_auth.py::test_login PASSED\ntests/test_auth.py::test_logout PASSED\ntests/test_api.py::test_get_users PASSED\ntests/test_api.py::test_create_user PASSED\ntests/test_api.py::test_delete_user FAILED\n\n=========================== 1 failed, 4 passed ==============================="},
        ],
        "final": "4 of 5 tests passed. `test_delete_user` in `tests/test_api.py` failed. Want me to investigate the failure?",
    },
    {
        "user": "Check what ports are currently in use",
        "turns": [
            {"tool": "shell", "args": {"command": "ss -tlnp"}, "result": "State    Recv-Q   Send-Q   Local Address:Port    Peer Address:Port\nLISTEN   0        128      0.0.0.0:22            0.0.0.0:*\nLISTEN   0        511      0.0.0.0:80            0.0.0.0:*\nLISTEN   0        511      127.0.0.1:5432        0.0.0.0:*\nLISTEN   0        128      0.0.0.0:8080          0.0.0.0:*"},
        ],
        "final": "Active listening ports:\n- **22** — SSH\n- **80** — HTTP (likely nginx/apache)\n- **5432** — PostgreSQL (localhost only)\n- **8080** — Application server",
    },
    {
        "user": "Kill the process on port 8080",
        "think": "The user wants to kill a process on port 8080. I should first identify what's running there, then kill it. This is a destructive action so I'll set approved=true since the user explicitly asked.",
        "turns": [
            {"tool": "shell", "args": {"command": "lsof -ti :8080"}, "result": "14523"},
            {"tool": "shell", "args": {"command": "kill 14523", "approved": True}, "result": ""},
        ],
        "final": "Process 14523 on port 8080 has been terminated.",
    },
    {
        "user": "Create a new Python virtual environment called myenv",
        "turns": [
            {"tool": "shell", "args": {"command": "python3 -m venv myenv"}, "result": ""},
        ],
        "final": "Virtual environment `myenv` created. Activate it with `source myenv/bin/activate`.",
    },
    {
        "user": "What's the current git status?",
        "turns": [
            {"tool": "git_operations", "args": {"operation": "status"}, "result": "On branch feature/auth\nChanges not staged for commit:\n  modified:   src/auth.py\n  modified:   src/config.py\n\nUntracked files:\n  src/middleware.py"},
        ],
        "final": "You're on branch `feature/auth` with:\n- 2 modified files: `src/auth.py`, `src/config.py`\n- 1 untracked file: `src/middleware.py`",
    },
    # =========================================================================
    # FILE_READ — reading files
    # =========================================================================
    {
        "user": "Show me the contents of main.py",
        "turns": [
            {"tool": "file_read", "args": {"path": "main.py"}, "result": "1: #!/usr/bin/env python3\n2: \"\"\"Main entry point.\"\"\"\n3: \n4: from src.app import create_app\n5: \n6: def main():\n7:     app = create_app()\n8:     app.run(host='0.0.0.0', port=8080)\n9: \n10: if __name__ == '__main__':\n11:     main()"},
        ],
        "final": "Here's `main.py` — it's a simple Flask-style entry point that creates an app and runs it on port 8080.",
    },
    {
        "user": "Show me just the first 20 lines of the README",
        "turns": [
            {"tool": "file_read", "args": {"path": "README.md", "limit": 20}, "result": "1: # MyProject\n2: \n3: A web application for managing user accounts.\n4: \n5: ## Setup\n6: \n7: ```bash\n8: pip install -r requirements.txt\n9: python main.py\n10: ```\n11: \n12: ## Features\n13: \n14: - User authentication\n15: - Role-based access control\n16: - REST API\n17: - Admin dashboard\n18: \n19: ## Configuration\n20: \n"},
        ],
        "final": "The README describes a user account management web app with auth, RBAC, REST API, and admin dashboard features.",
    },
    {
        "user": "What's on line 45-60 of src/auth.py?",
        "turns": [
            {"tool": "file_read", "args": {"path": "src/auth.py", "offset": 45, "limit": 16}, "result": "45: def verify_token(token: str) -> dict | None:\n46:     \"\"\"Verify a JWT token and return the payload.\"\"\"\n47:     try:\n48:         payload = jwt.decode(token, SECRET_KEY, algorithms=['HS256'])\n49:         if payload['exp'] < time.time():\n50:             return None\n51:         return payload\n52:     except jwt.InvalidTokenError:\n53:         return None\n54: \n55: \n56: def require_auth(f):\n57:     \"\"\"Decorator to require authentication on a route.\"\"\"\n58:     @wraps(f)\n59:     def decorated(*args, **kwargs):\n60:         token = request.headers.get('Authorization', '').removeprefix('Bearer ')"},
        ],
        "final": "Lines 45-60 show the `verify_token` function (JWT decode with expiry check) and the start of a `require_auth` decorator that extracts the Bearer token from the Authorization header.",
    },
    # =========================================================================
    # FILE_WRITE — creating files
    # =========================================================================
    {
        "user": "Create a .gitignore file for a Python project",
        "turns": [
            {"tool": "file_write", "args": {"path": ".gitignore", "content": "__pycache__/\n*.py[cod]\n*$py.class\n*.so\n.Python\nbuild/\ndist/\n*.egg-info/\n.venv/\nvenv/\n.env\n*.db\n.coverage\nhtmlcov/\n.pytest_cache/\n.mypy_cache/\n"}, "result": "File written: .gitignore (18 lines)"},
        ],
        "final": "Created `.gitignore` with standard Python exclusions (pycache, venvs, build artifacts, coverage, etc.).",
    },
    {
        "user": "Create a basic Dockerfile for this Python project",
        "think": "I should check what Python version and dependencies are used before writing the Dockerfile.",
        "turns": [
            {"tool": "file_read", "args": {"path": "requirements.txt"}, "result": "1: flask==3.0.0\n2: flask-cors==4.0.0\n3: pyjwt==2.8.0\n4: psycopg2-binary==2.9.9\n5: gunicorn==21.2.0"},
            {"tool": "file_write", "args": {"path": "Dockerfile", "content": "FROM python:3.12-slim\n\nWORKDIR /app\n\nCOPY requirements.txt .\nRUN pip install --no-cache-dir -r requirements.txt\n\nCOPY . .\n\nEXPOSE 8080\n\nCMD [\"gunicorn\", \"-b\", \"0.0.0.0:8080\", \"main:create_app()\"]\n"}, "result": "File written: Dockerfile (12 lines)"},
        ],
        "final": "Created a `Dockerfile` based on Python 3.12-slim. It installs your dependencies from `requirements.txt` and runs the app with gunicorn on port 8080.",
    },
    # =========================================================================
    # FILE_EDIT — modifying files
    # =========================================================================
    {
        "user": "Change the port in main.py from 8080 to 3000",
        "turns": [
            {"tool": "file_edit", "args": {"path": "main.py", "old_string": "app.run(host='0.0.0.0', port=8080)", "new_string": "app.run(host='0.0.0.0', port=3000)"}, "result": "Edit applied: 1 replacement in main.py"},
        ],
        "final": "Updated `main.py` — the app will now run on port 3000.",
    },
    {
        "user": "Add a logging import to auth.py",
        "think": "I need to read the file first to see what's at the top and where to add the import.",
        "turns": [
            {"tool": "file_read", "args": {"path": "src/auth.py", "limit": 10}, "result": "1: import time\n2: import jwt\n3: from functools import wraps\n4: from flask import request, jsonify\n5: \n6: SECRET_KEY = 'change-me-in-production'\n7: TOKEN_EXPIRY = 3600  # 1 hour\n8: "},
            {"tool": "file_edit", "args": {"path": "src/auth.py", "old_string": "import time\nimport jwt", "new_string": "import logging\nimport time\nimport jwt\n\nlogger = logging.getLogger(__name__)"}, "result": "Edit applied: 1 replacement in src/auth.py"},
        ],
        "final": "Added `import logging` and created a module-level `logger` at the top of `src/auth.py`.",
    },
    # =========================================================================
    # GLOB_SEARCH + CONTENT_SEARCH — finding things
    # =========================================================================
    {
        "user": "Find all Python test files in the project",
        "turns": [
            {"tool": "glob_search", "args": {"pattern": "**/test_*.py"}, "result": "tests/test_auth.py\ntests/test_api.py\ntests/test_models.py\nsrc/test_utils.py"},
        ],
        "final": "Found 4 test files:\n- `tests/test_auth.py`\n- `tests/test_api.py`\n- `tests/test_models.py`\n- `src/test_utils.py` (this one might belong in `tests/`)",
    },
    {
        "user": "Find all TODO comments in the codebase",
        "turns": [
            {"tool": "content_search", "args": {"pattern": "TODO|FIXME|HACK", "glob": "*.py"}, "result": "src/auth.py:23: # TODO: move secret to env variable\nsrc/api.py:67: # FIXME: this doesn't handle pagination\nsrc/models.py:12: # TODO: add migration support\ntests/test_api.py:45: # HACK: skip auth for now"},
        ],
        "final": "Found 4 items:\n1. `src/auth.py:23` — TODO: move secret to env variable\n2. `src/api.py:67` — FIXME: doesn't handle pagination\n3. `src/models.py:12` — TODO: add migration support\n4. `tests/test_api.py:45` — HACK: skip auth for now\n\nThe auth secret and pagination issues look like they should be addressed soon.",
    },
    {
        "user": "Where is the database connection configured?",
        "turns": [
            {"tool": "content_search", "args": {"pattern": "DATABASE|db_url|connection_string|create_engine|connect\\(", "glob": "*.py"}, "result": "src/config.py:8: DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql://localhost/myapp')\nsrc/models.py:5: engine = create_engine(config.DATABASE_URL)\nsrc/models.py:6: Session = sessionmaker(bind=engine)"},
        ],
        "final": "Database connection is configured in two places:\n- `src/config.py:8` — `DATABASE_URL` env var (defaults to `postgresql://localhost/myapp`)\n- `src/models.py:5-6` — SQLAlchemy engine and session factory\n\nTo change the database, set the `DATABASE_URL` environment variable.",
    },
    # =========================================================================
    # MEMORY tools — store/recall/forget
    # =========================================================================
    {
        "user": "Remember that the production database is on db.example.com port 5433",
        "turns": [
            {"tool": "memory_store", "args": {"key": "prod_database", "value": "Production database: db.example.com:5433"}, "result": "Stored: prod_database"},
        ],
        "final": "Got it — I've stored the production database info. I'll remember that for future reference.",
    },
    {
        "user": "What do you remember about the database?",
        "turns": [
            {"tool": "memory_recall", "args": {"query": "database"}, "result": "prod_database: Production database: db.example.com:5433"},
        ],
        "final": "I have this stored: the production database is at `db.example.com` on port `5433`.",
    },
    {
        "user": "Forget the production database info",
        "turns": [
            {"tool": "memory_forget", "args": {"key": "prod_database"}, "result": "Forgotten: prod_database"},
        ],
        "final": "Done — I've removed the production database info from memory.",
    },
    {
        "user": "Remember that deployments happen every Tuesday at 2pm UTC and need approval from Sarah",
        "turns": [
            {"tool": "memory_store", "args": {"key": "deployment_schedule", "value": "Deployments: every Tuesday at 14:00 UTC, requires approval from Sarah"}, "result": "Stored: deployment_schedule"},
        ],
        "final": "Stored. I'll remember that deployments are every Tuesday at 2pm UTC and need Sarah's approval.",
    },
    # =========================================================================
    # CRON tools — scheduling
    # =========================================================================
    {
        "user": "Set up a cron job to run the database backup script every night at 2am",
        "turns": [
            {"tool": "cron_add", "args": {"name": "nightly_db_backup", "schedule": "0 2 * * *", "command": "python scripts/backup_db.py"}, "result": "Created cron job: nightly_db_backup (0 2 * * *)"},
        ],
        "final": "Created cron job `nightly_db_backup` — it will run `python scripts/backup_db.py` every day at 2:00 AM.",
    },
    {
        "user": "What cron jobs are currently set up?",
        "turns": [
            {"tool": "cron_list", "args": {}, "result": "Jobs:\n- nightly_db_backup: 0 2 * * * | python scripts/backup_db.py | active\n- health_check: */5 * * * * | curl http://localhost:8080/health | active\n- log_rotation: 0 0 * * 0 | python scripts/rotate_logs.py | paused"},
        ],
        "final": "Three cron jobs:\n1. **nightly_db_backup** — daily at 2am, runs DB backup (active)\n2. **health_check** — every 5 minutes, pings health endpoint (active)\n3. **log_rotation** — weekly Sundays at midnight, rotates logs (paused)",
    },
    {
        "user": "Remove the health check cron job",
        "turns": [
            {"tool": "cron_remove", "args": {"name": "health_check"}, "result": "Removed cron job: health_check"},
        ],
        "final": "Removed the `health_check` cron job.",
    },
    # =========================================================================
    # WEB tools — fetch, search, http
    # =========================================================================
    {
        "user": "What's the latest version of the Rust programming language?",
        "turns": [
            {"tool": "web_search", "args": {"query": "latest Rust programming language version 2026"}, "result": "1. Rust 1.85.0 Release Notes - https://blog.rust-lang.org/2026/02/20/Rust-1.85.0.html\n2. Rust Release Schedule - https://forge.rust-lang.org/release/schedule.html"},
        ],
        "final": "The latest stable Rust version is **1.85.0**, released on February 20, 2026.",
    },
    {
        "user": "Fetch the JSON from our health check endpoint",
        "turns": [
            {"tool": "http_request", "args": {"method": "GET", "url": "http://localhost:8080/health"}, "result": "HTTP 200 OK\nContent-Type: application/json\n\n{\"status\": \"healthy\", \"uptime\": 86423, \"db\": \"connected\", \"cache\": \"connected\", \"version\": \"2.1.0\"}"},
        ],
        "final": "Health check response (200 OK):\n- Status: healthy\n- Uptime: ~24 hours\n- Database: connected\n- Cache: connected\n- Version: 2.1.0",
    },
    {
        "user": "Check what the Flask documentation says about blueprints",
        "turns": [
            {"tool": "web_fetch", "args": {"url": "https://flask.palletsprojects.com/en/3.0.x/blueprints/", "prompt": "Summarize how Flask Blueprints work and their main use cases"}, "result": "Flask Blueprints are a way to organize your application into modular components. A blueprint defines a collection of views, templates, and static files that can be registered on an application. Key features:\n- Register routes under a URL prefix\n- Group related views together\n- Provide template and static file directories\n- Support before/after request hooks scoped to the blueprint\n\nUsage: bp = Blueprint('auth', __name__, url_prefix='/auth')"},
        ],
        "final": "Flask Blueprints let you organize your app into modular components. Each blueprint groups related routes, templates, and static files. You create one with `Blueprint('name', __name__, url_prefix='/prefix')` and register it on your app. This is useful for separating concerns like auth, API, and admin into their own modules.",
    },
    # =========================================================================
    # GIT — operations
    # =========================================================================
    {
        "user": "Create a new branch called feature/user-profile and switch to it",
        "turns": [
            {"tool": "git_operations", "args": {"operation": "checkout", "args": ["-b", "feature/user-profile"]}, "result": "Switched to a new branch 'feature/user-profile'"},
        ],
        "final": "Created and switched to branch `feature/user-profile`.",
    },
    {
        "user": "Stage all changes and commit with the message 'Add user profile endpoint'",
        "turns": [
            {"tool": "git_operations", "args": {"operation": "add", "args": ["."]}, "result": ""},
            {"tool": "git_operations", "args": {"operation": "commit", "args": ["-m", "Add user profile endpoint"]}, "result": "[feature/user-profile abc1234] Add user profile endpoint\n 3 files changed, 89 insertions(+), 2 deletions(-)"},
        ],
        "final": "Committed: `Add user profile endpoint` — 3 files changed, 89 insertions, 2 deletions.",
    },
    {
        "user": "Show me the recent commit history",
        "turns": [
            {"tool": "git_operations", "args": {"operation": "log", "args": ["--oneline", "-10"]}, "result": "abc1234 Add user profile endpoint\ndef5678 Fix authentication token expiry\n9012abc Update README with API docs\n3456def Initial project setup\n7890ghi Add CI/CD pipeline"},
        ],
        "final": "Recent commits:\n1. `abc1234` Add user profile endpoint\n2. `def5678` Fix authentication token expiry\n3. `9012abc` Update README with API docs\n4. `3456def` Initial project setup\n5. `7890ghi` Add CI/CD pipeline",
    },
    # =========================================================================
    # MULTI-TOOL CHAINS — complex tasks
    # =========================================================================
    {
        "user": "Find and fix the bug in the delete user test",
        "think": "The test suite showed test_delete_user failing. I need to read the test file and the implementation to understand what's wrong.",
        "turns": [
            {"tool": "file_read", "args": {"path": "tests/test_api.py", "offset": 30, "limit": 30}, "result": "30: def test_delete_user(client, auth_token):\n31:     # Create a user first\n32:     resp = client.post('/api/users', json={'name': 'test', 'email': 'test@example.com'},\n33:                        headers={'Authorization': f'Bearer {auth_token}'})\n34:     user_id = resp.json['id']\n35:     \n36:     # Delete the user\n37:     resp = client.delete(f'/api/users/{user_id}',\n38:                          headers={'Authorization': f'Bearer {auth_token}'})\n39:     assert resp.status_code == 200\n40:     \n41:     # Verify user is gone\n42:     resp = client.get(f'/api/users/{user_id}',\n43:                       headers={'Authorization': f'Bearer {auth_token}'})\n44:     assert resp.status_code == 404"},
            {"tool": "content_search", "args": {"pattern": "def delete_user|@app.route.*DELETE|delete.*route", "path": "src/api.py"}, "result": "src/api.py:45: @app.route('/api/users/<int:user_id>', methods=['DELETE'])\nsrc/api.py:46: @require_auth\nsrc/api.py:47: def delete_user(user_id):"},
            {"tool": "file_read", "args": {"path": "src/api.py", "offset": 45, "limit": 15}, "result": "45: @app.route('/api/users/<int:user_id>', methods=['DELETE'])\n46: @require_auth\n47: def delete_user(user_id):\n48:     user = User.query.get(user_id)\n49:     if not user:\n50:         return jsonify({'error': 'Not found'}), 404\n51:     db.session.delete(user)\n52:     # Missing db.session.commit()\n53:     return jsonify({'message': 'Deleted'}), 200"},
            {"tool": "file_edit", "args": {"path": "src/api.py", "old_string": "    db.session.delete(user)\n    # Missing db.session.commit()\n    return jsonify({'message': 'Deleted'}), 200", "new_string": "    db.session.delete(user)\n    db.session.commit()\n    return jsonify({'message': 'Deleted'}), 200"}, "result": "Edit applied: 1 replacement in src/api.py"},
            {"tool": "shell", "args": {"command": "pytest tests/test_api.py::test_delete_user -v"}, "result": "tests/test_api.py::test_delete_user PASSED\n\n1 passed in 0.3s"},
        ],
        "final": "Found and fixed the bug — `db.session.commit()` was missing after `db.session.delete(user)` in `src/api.py:52`. The delete was happening but never committed to the database. Test passes now.",
    },
    {
        "user": "Set up a new FastAPI project with a health check endpoint",
        "think": "I'll create the project structure: main.py, requirements.txt, and a basic health endpoint.",
        "turns": [
            {"tool": "shell", "args": {"command": "mkdir -p fastapi_project"}, "result": ""},
            {"tool": "file_write", "args": {"path": "fastapi_project/requirements.txt", "content": "fastapi==0.109.0\nuvicorn[standard]==0.27.0\n"}, "result": "File written: fastapi_project/requirements.txt (2 lines)"},
            {"tool": "file_write", "args": {"path": "fastapi_project/main.py", "content": "from fastapi import FastAPI\nfrom datetime import datetime\n\napp = FastAPI(title=\"My API\", version=\"0.1.0\")\n\n\n@app.get(\"/health\")\nasync def health_check():\n    return {\n        \"status\": \"healthy\",\n        \"timestamp\": datetime.utcnow().isoformat(),\n    }\n\n\nif __name__ == \"__main__\":\n    import uvicorn\n    uvicorn.run(app, host=\"0.0.0.0\", port=8000)\n"}, "result": "File written: fastapi_project/main.py (17 lines)"},
        ],
        "final": "Created `fastapi_project/` with:\n- `main.py` — FastAPI app with a `/health` endpoint\n- `requirements.txt` — FastAPI + Uvicorn\n\nRun it with `cd fastapi_project && pip install -r requirements.txt && python main.py`.",
    },
    {
        "user": "Help me understand what this project does — give me an overview",
        "think": "I should look at the project structure, README, and key files to build a picture.",
        "turns": [
            {"tool": "project_intel", "args": {"action": "overview"}, "result": "Project: myapp\nLanguage: Python 3.12\nFramework: Flask 3.0.0\nDependencies: flask, flask-cors, pyjwt, psycopg2-binary, gunicorn\nStructure:\n  src/ — 6 Python files (auth, api, models, config, middleware, utils)\n  tests/ — 4 test files\n  scripts/ — 2 utility scripts\nLines of code: ~1,200\nTest coverage: 78%"},
            {"tool": "file_read", "args": {"path": "README.md", "limit": 30}, "result": "1: # MyProject\n2: \n3: A web application for managing user accounts.\n4: \n5: ## Setup\n6: \n7: ```bash\n8: pip install -r requirements.txt\n9: python main.py\n10: ```\n11: \n12: ## Features\n13: \n14: - User authentication\n15: - Role-based access control\n16: - REST API\n17: - Admin dashboard\n18: \n19: ## API Endpoints\n20: \n21: - POST /api/login — Authenticate\n22: - GET /api/users — List users\n23: - POST /api/users — Create user\n24: - DELETE /api/users/:id — Delete user\n25: - GET /api/users/:id/profile — User profile"},
        ],
        "final": "This is a **Flask-based user management web app** (~1,200 lines of Python). Key features:\n\n- **Authentication** — JWT-based with login/logout\n- **REST API** — CRUD endpoints for users and profiles\n- **RBAC** — role-based access control\n- **Database** — PostgreSQL via SQLAlchemy\n\nThe codebase has 78% test coverage across 4 test files. It's set up to run with Gunicorn in production.",
    },
    {
        "user": "Refactor the auth module to use environment variables for the secret key instead of hardcoding it",
        "think": "I need to read the auth module, find the hardcoded secret, update it to use an env var, and make sure the config module supports it.",
        "turns": [
            {"tool": "file_read", "args": {"path": "src/auth.py", "limit": 10}, "result": "1: import time\n2: import jwt\n3: from functools import wraps\n4: from flask import request, jsonify\n5: \n6: SECRET_KEY = 'change-me-in-production'\n7: TOKEN_EXPIRY = 3600  # 1 hour"},
            {"tool": "file_read", "args": {"path": "src/config.py"}, "result": "1: import os\n2: \n3: DEBUG = os.getenv('DEBUG', 'false').lower() == 'true'\n4: HOST = os.getenv('HOST', '0.0.0.0')\n5: PORT = int(os.getenv('PORT', '8080'))\n6: \n7: DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql://localhost/myapp')\n8: "},
            {"tool": "file_edit", "args": {"path": "src/config.py", "old_string": "DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql://localhost/myapp')", "new_string": "DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql://localhost/myapp')\n\nSECRET_KEY = os.getenv('SECRET_KEY', 'change-me-in-production')\nTOKEN_EXPIRY = int(os.getenv('TOKEN_EXPIRY', '3600'))"}, "result": "Edit applied: 1 replacement in src/config.py"},
            {"tool": "file_edit", "args": {"path": "src/auth.py", "old_string": "import time\nimport jwt\nfrom functools import wraps\nfrom flask import request, jsonify\n\nSECRET_KEY = 'change-me-in-production'\nTOKEN_EXPIRY = 3600  # 1 hour", "new_string": "import time\nimport jwt\nfrom functools import wraps\nfrom flask import request, jsonify\nfrom src.config import SECRET_KEY, TOKEN_EXPIRY"}, "result": "Edit applied: 1 replacement in src/auth.py"},
        ],
        "final": "Refactored. The `SECRET_KEY` and `TOKEN_EXPIRY` are now configured via environment variables in `src/config.py`, and `src/auth.py` imports them from there. Set `SECRET_KEY` in your environment before deploying to production.",
    },
    # =========================================================================
    # ERROR RECOVERY — handling tool failures
    # =========================================================================
    {
        "user": "Read the file deployment.yaml",
        "turns": [
            {"tool": "file_read", "args": {"path": "deployment.yaml"}, "result": "Error: File not found: deployment.yaml", "status": "error"},
            {"tool": "glob_search", "args": {"pattern": "**/*.yaml"}, "result": "k8s/deployment.yaml\nk8s/service.yaml\ndocker-compose.yaml"},
        ],
        "final": "There's no `deployment.yaml` in the root, but I found it at `k8s/deployment.yaml`. There's also `k8s/service.yaml` and `docker-compose.yaml`. Want me to read `k8s/deployment.yaml`?",
    },
    {
        "user": "Run the linter on the project",
        "turns": [
            {"tool": "shell", "args": {"command": "ruff check src/"}, "result": "Error: command not found: ruff", "status": "error"},
            {"tool": "shell", "args": {"command": "flake8 src/"}, "result": "src/auth.py:48:80: E501 line too long (92 > 79 characters)\nsrc/api.py:23:1: W291 trailing whitespace\nsrc/utils.py:1:1: F401 'os' imported but unused"},
        ],
        "final": "`ruff` isn't installed, but `flake8` found 3 issues:\n1. `src/auth.py:48` — line too long (92 chars)\n2. `src/api.py:23` — trailing whitespace\n3. `src/utils.py:1` — unused `os` import\n\nWant me to fix these?",
    },
    # =========================================================================
    # DELEGATE — sub-agent tasks
    # =========================================================================
    {
        "user": "Have a code reviewer check the auth module for security issues",
        "turns": [
            {"tool": "delegate", "args": {"agent": "code-reviewer", "task": "Review src/auth.py for security vulnerabilities, paying attention to JWT handling, secret management, and authentication bypass risks", "context": "This is a Flask web app using PyJWT for authentication"}, "result": "Review complete. Findings:\n1. CRITICAL: Secret key is hardcoded (line 6)\n2. MEDIUM: No token refresh mechanism\n3. LOW: Token expiry check uses time.time() instead of UTC-aware datetime\n4. SUGGESTION: Add rate limiting to login endpoint"},
        ],
        "final": "Code review results for `src/auth.py`:\n\n1. **CRITICAL** — Hardcoded secret key (line 6). Move to environment variable.\n2. **MEDIUM** — No token refresh mechanism. Users must re-login when tokens expire.\n3. **LOW** — Token expiry uses `time.time()` — consider UTC-aware datetime for consistency.\n4. **Suggestion** — Add rate limiting to the login endpoint to prevent brute force.\n\nWant me to address any of these?",
    },
    # =========================================================================
    # BROWSER — web automation
    # =========================================================================
    {
        "user": "Open the app in a browser and check if the login page loads",
        "turns": [
            {"tool": "browser", "args": {"action": "navigate", "url": "http://localhost:8080/login"}, "result": "Navigated to http://localhost:8080/login\nTitle: MyApp - Login\nStatus: 200"},
            {"tool": "browser", "args": {"action": "screenshot"}, "result": "Screenshot saved to /tmp/screenshot_001.png\nVisible elements: form#login-form, input#username, input#password, button#submit"},
        ],
        "final": "The login page loads correctly (HTTP 200). It shows a login form with username/password fields and a submit button. The page title is \"MyApp - Login\".",
    },
    # =========================================================================
    # BACKUP — backup operations
    # =========================================================================
    {
        "user": "Create a backup of the src directory before I make major changes",
        "turns": [
            {"tool": "backup_tool", "args": {"action": "create", "path": "src", "name": "pre-refactor-backup"}, "result": "Backup created: pre-refactor-backup\nPath: .backups/pre-refactor-backup/\nFiles: 6\nSize: 12.4 KB"},
        ],
        "final": "Backup `pre-refactor-backup` created with all 6 files from `src/` (12.4 KB). You can restore it later if needed.",
    },
    # =========================================================================
    # SECURITY — security scanning
    # =========================================================================
    {
        "user": "Run a security audit on the project",
        "turns": [
            {"tool": "security_ops", "args": {"action": "audit"}, "result": "Security Audit Results:\n\nDependencies:\n- pyjwt 2.8.0: OK\n- flask 3.0.0: OK\n- psycopg2-binary 2.9.9: OK\n\nCode Analysis:\n- WARN: Hardcoded secret in src/auth.py:6\n- WARN: No CSRF protection found\n- WARN: DEBUG mode controllable via env var without validation\n- INFO: No SQL injection risks detected (using ORM)\n- INFO: CORS properly configured"},
        ],
        "final": "Security audit results:\n\n**Dependencies**: All clean, no known vulnerabilities.\n\n**Code issues**:\n- Hardcoded secret in `src/auth.py` (should use env var)\n- No CSRF protection (add Flask-WTF or similar)\n- DEBUG mode controlled by env var without strict validation\n\n**Good news**: No SQL injection risks (ORM is used properly) and CORS is configured correctly.",
    },
    # =========================================================================
    # PUSHOVER — notifications
    # =========================================================================
    {
        "user": "Send me a notification that the deployment is complete",
        "turns": [
            {"tool": "pushover", "args": {"message": "Deployment to production completed successfully.", "title": "Deploy Complete", "priority": 0}, "result": "Notification sent successfully"},
        ],
        "final": "Push notification sent: \"Deploy Complete — Deployment to production completed successfully.\"",
    },
    # =========================================================================
    # PDF — reading documents
    # =========================================================================
    {
        "user": "What's in the API spec PDF?",
        "turns": [
            {"tool": "pdf_read", "args": {"path": "docs/api-spec.pdf", "pages": "1-3"}, "result": "Page 1:\nAPI Specification v2.1\nMyApp REST API\nLast updated: March 2026\n\nPage 2:\n1. Authentication\nAll endpoints require Bearer token authentication.\nTokens are obtained via POST /api/login.\n\nPage 3:\n2. Users API\nGET /api/users - List all users\nPOST /api/users - Create a new user\nDELETE /api/users/:id - Delete a user"},
        ],
        "final": "The API spec (v2.1) covers:\n- **Authentication** — all endpoints need Bearer tokens from POST /api/login\n- **Users API** — standard CRUD (list, create, delete)\n\nWant me to read more pages?",
    },
    # =========================================================================
    # TOOL_SEARCH — discovering tools
    # =========================================================================
    {
        "user": "What tools do you have for working with files?",
        "turns": [
            {"tool": "tool_search", "args": {"query": "file"}, "result": "Matching tools:\n- file_read: Read file contents with line numbers\n- file_write: Write content to a file\n- file_edit: Perform exact string replacements\n- glob_search: Search for files matching a glob pattern\n- content_search: Search file contents using regex\n- pdf_read: Read and extract text from PDF files\n- backup_tool: Create or restore backups"},
        ],
        "final": "I have these file-related tools:\n- **file_read** — read file contents (with line numbers, offset, limit)\n- **file_write** — create or overwrite files\n- **file_edit** — exact string replacements in files\n- **glob_search** — find files by pattern (e.g. `**/*.py`)\n- **content_search** — grep-style regex search inside files\n- **pdf_read** — extract text from PDFs\n- **backup_tool** — backup and restore files/dirs",
    },
    # =========================================================================
    # MULTI-TURN conversations
    # =========================================================================
    {
        "user": "I need to add a new API endpoint for user profiles",
        "think": "Let me first understand the current API structure before suggesting an implementation.",
        "turns": [
            {"tool": "file_read", "args": {"path": "src/api.py"}, "result": "1: from flask import jsonify, request\n2: from src.auth import require_auth\n3: from src.models import User, db\n4: \n5: \n6: @app.route('/api/users', methods=['GET'])\n7: @require_auth\n8: def list_users():\n9:     users = User.query.all()\n10:    return jsonify([u.to_dict() for u in users])\n11: \n12: \n13: @app.route('/api/users', methods=['POST'])\n14: @require_auth\n15: def create_user():\n16:     data = request.json\n17:     user = User(name=data['name'], email=data['email'])\n18:     db.session.add(user)\n19:     db.session.commit()\n20:     return jsonify(user.to_dict()), 201\n21: \n22: \n23: @app.route('/api/users/<int:user_id>', methods=['DELETE'])\n24: @require_auth\n25: def delete_user(user_id):\n26:     user = User.query.get(user_id)\n27:     if not user:\n28:         return jsonify({'error': 'Not found'}), 404\n29:     db.session.delete(user)\n30:     db.session.commit()\n31:     return jsonify({'message': 'Deleted'}), 200"},
            {"tool": "file_edit", "args": {"path": "src/api.py", "old_string": "    return jsonify({'message': 'Deleted'}), 200", "new_string": "    return jsonify({'message': 'Deleted'}), 200\n\n\n@app.route('/api/users/<int:user_id>/profile', methods=['GET'])\n@require_auth\ndef get_user_profile(user_id):\n    user = User.query.get(user_id)\n    if not user:\n        return jsonify({'error': 'Not found'}), 404\n    return jsonify(user.profile_dict())\n\n\n@app.route('/api/users/<int:user_id>/profile', methods=['PUT'])\n@require_auth\ndef update_user_profile(user_id):\n    user = User.query.get(user_id)\n    if not user:\n        return jsonify({'error': 'Not found'}), 404\n    data = request.json\n    user.bio = data.get('bio', user.bio)\n    user.avatar_url = data.get('avatar_url', user.avatar_url)\n    db.session.commit()\n    return jsonify(user.profile_dict())"}, "result": "Edit applied: 1 replacement in src/api.py"},
        ],
        "final": "Added two profile endpoints to `src/api.py`:\n\n- `GET /api/users/:id/profile` — retrieve a user's profile\n- `PUT /api/users/:id/profile` — update bio and avatar_url\n\nBoth are auth-protected and return 404 for unknown users. You'll need a `profile_dict()` method on the User model — want me to add that?",
    },
    # =========================================================================
    # NO-TOOL responses — not every request needs a tool
    # =========================================================================
    {
        "user": "What's the difference between PUT and PATCH in REST APIs?",
        "turns": [],
        "final": "**PUT** replaces the entire resource — you send the complete updated object. If you omit a field, it gets removed or set to default.\n\n**PATCH** partially updates a resource — you only send the fields you want to change. Other fields remain untouched.\n\nExample: updating a user's email\n- PUT `/users/1` with `{\"name\": \"Alice\", \"email\": \"new@example.com\", \"role\": \"admin\"}` — must include all fields\n- PATCH `/users/1` with `{\"email\": \"new@example.com\"}` — only changes email",
    },
    {
        "user": "Can you explain what JWT tokens are?",
        "turns": [],
        "final": "JWT (JSON Web Token) is a compact, URL-safe way to represent claims between two parties.\n\nA JWT has three parts separated by dots: `header.payload.signature`\n\n- **Header** — algorithm and token type (`{\"alg\": \"HS256\", \"typ\": \"JWT\"}`)\n- **Payload** — claims/data (`{\"sub\": \"user123\", \"exp\": 1710000000}`)\n- **Signature** — HMAC or RSA signature to verify integrity\n\nThe server creates a token at login, the client sends it in the `Authorization: Bearer <token>` header, and the server verifies the signature on each request without needing to store sessions.",
    },
    {
        "user": "Thanks, that was really helpful!",
        "turns": [],
        "final": "You're welcome! Let me know if you need anything else.",
    },
]
