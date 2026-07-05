import os
import subprocess
import httpx
import json
from mcp.server.fastmcp import FastMCP
from github import Github

mcp = FastMCP("pr-gatekeeper")

def get_github_client():
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return None
    return Github(token)

# ----------------------------------------------------------------------
# Mock / Fixture Data for Offline/Local Testing
# ----------------------------------------------------------------------

MOCK_DIFFS = {
    1: """diff --git a/src/app.py b/src/app.py
index 1234567..89abcde 100644
--- a/src/app.py
+++ b/src/app.py
@@ -10,3 +10,10 @@
 def home():
     return "Hello World"
+
+@app.route("/api/health", methods=["GET"])
+def health():
+    # safe health check route
+    return {"status": "ok"}
""",
    2: """diff --git a/config/settings.py b/config/settings.py
--- a/config/settings.py
+++ b/config/settings.py
@@ -5,2 +5,3 @@
 DB_PORT = 5432
+STRIPE_API_KEY = "sk_live_REDACTED_EXAMPLE_NOT_REAL"
""",
    3: """diff --git a/src/routes/users.py b/src/routes/users.py
--- a/src/routes/users.py
+++ b/src/routes/users.py
@@ -20,3 +20,7 @@
+@app.route("/api/users/delete", methods=["POST"])
+def delete_user():
+    # Missing session/auth check!
+    db.delete_user(request.json['id'])
+    return {"status": "deleted"}
"""
}

MOCK_FILES = {
    1: ["src/app.py"],
    2: ["config/settings.py"],
    3: ["src/routes/users.py"]
}

MOCK_CONTEXTS = {
    "src/app.py": """from flask import Flask, session
app = Flask(__name__)
@app.route("/")
def home():
    return "Hello World"
""",
    "config/settings.py": """DB_HOST = "localhost"
DB_PORT = 5432
""",
    "src/routes/users.py": """from flask import Flask, session, request
app = Flask(__name__)

@app.route("/api/users/profile", methods=["GET"])
def get_profile():
    if "user_id" not in session:
        return {"error": "unauthorized"}, 401
    return {"user": session["user_id"]}

@app.route("/api/users/update", methods=["POST"])
def update_profile():
    if "user_id" not in session:
        return {"error": "unauthorized"}, 401
    db.update_user(session["user_id"], request.json)
    return {"status": "success"}
"""
}

# ----------------------------------------------------------------------
# MCP Tool Implementations
# ----------------------------------------------------------------------

@mcp.tool()
def get_pr_diff(pr_number: int) -> str:
    """Fetch the unified diff of a pull request from GitHub."""
    # Check if mock exists first
    if pr_number in MOCK_DIFFS:
        return MOCK_DIFFS[pr_number]
        
    token = os.environ.get("GITHUB_TOKEN")
    repo_name = os.environ.get("REPO", "dummy/repo")
    if not token:
        return "Warning: GITHUB_TOKEN is not set and no mock data for this PR. Cannot fetch PR diff."
    
    url = f"https://api.github.com/repos/{repo_name}/pulls/{pr_number}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3.diff",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    try:
        res = httpx.get(url, headers=headers)
        if res.status_code == 200:
            return res.text
        return f"Error fetching PR diff: HTTP {res.status_code} - {res.text}"
    except Exception as e:
        return f"Error calling GitHub API: {str(e)}"

@mcp.tool()
def get_pr_files(pr_number: int) -> list[str]:
    """List changed files in a pull request."""
    if pr_number in MOCK_FILES:
        return MOCK_FILES[pr_number]
        
    client = get_github_client()
    if not client:
        return []
    repo_name = os.environ.get("REPO", "dummy/repo")
    try:
        repo = client.get_repo(repo_name)
        pr = repo.get_pull(pr_number)
        return [f.filename for f in pr.get_files()]
    except Exception as e:
        print(f"Error fetching PR files: {e}")
        return []

@mcp.tool()
def get_file_context(path: str, ref: str) -> str:
    """Fetch the content of a file at a specific git ref (branch or commit SHA)."""
    # Check if we have mock context for this path
    if path in MOCK_CONTEXTS:
        return MOCK_CONTEXTS[path]
        
    client = get_github_client()
    if not client:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        return f"Error: Local file {path} not found and GITHUB_TOKEN/Mock context not set."
    
    repo_name = os.environ.get("REPO", "dummy/repo")
    try:
        repo = client.get_repo(repo_name)
        content = repo.get_contents(path, ref=ref)
        return content.decoded_content.decode("utf-8")
    except Exception as e:
        return f"Error fetching file context: {str(e)}"

@mcp.tool()
def run_semgrep(diff_paths: list[str]) -> str:
    """Run semgrep security analysis on specific file paths using p/secrets ruleset."""
    # Check if we're scanning config/settings.py (Mock PR 2)
    if any("config/settings.py" in p for p in diff_paths):
        # Return a mocked Semgrep finding for STRIPE_API_KEY
        return json.dumps({
            "results": [{
                "path": "config/settings.py",
                "start": {"line": 7},
                "end": {"line": 7},
                "extra": {
                    "message": "Detected hardcoded Stripe API Key",
                    "severity": "ERROR",
                    "lines": "STRIPE_API_KEY = \"sk_live_REDACTED_EXAMPLE_NOT_REAL\""
                }
            }]
        })

    if not diff_paths:
        return "No files to scan."
    existing_paths = [p for p in diff_paths if os.path.exists(p)]
    if not existing_paths:
        return "None of the specified files exist locally."
    
    cmd = ["semgrep", "--config=p/secrets", "--json"] + existing_paths
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return res.stdout
    except Exception as e:
        return f"Error running semgrep: {str(e)}"

@mcp.tool()
def run_sca_scan(lockfile_path: str) -> str:
    """Run Software Composition Analysis (SCA) scan against a lockfile."""
    if not os.path.exists(lockfile_path):
        return f"Lockfile {lockfile_path} not found."
    
    if "package-lock.json" in lockfile_path:
        cmd = ["npm", "audit", "--json"]
    elif "requirements.txt" in lockfile_path or "uv.lock" in lockfile_path:
        cmd = ["pip-audit", "--format", "json"]
    else:
        return f"Unsupported lockfile format: {lockfile_path}"
        
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return res.stdout
    except Exception as e:
        return f"Error running SCA scan: {str(e)}"

@mcp.tool()
def post_pr_comment(pr_number: int, body: str) -> str:
    """Post the review comment to the GitHub PR."""
    live = os.environ.get("GATEKEEPER_LIVE", "").lower() == "true"
    if not live:
        print(f"[DRY-RUN] post_pr_comment on PR #{pr_number}:\n{body}")
        return f"DRY-RUN success: Review comment logged for PR #{pr_number}."
        
    client = get_github_client()
    if not client:
        return "Error: GITHUB_TOKEN not set, cannot post comment."
    repo_name = os.environ.get("REPO")
    try:
        repo = client.get_repo(repo_name)
        pr = repo.get_pull(pr_number)
        pr.create_issue_comment(body)
        return "Successfully posted PR comment."
    except Exception as e:
        return f"Error posting comment: {str(e)}"

@mcp.tool()
def set_check_run_status(sha: str, conclusion: str, summary: str) -> str:
    """Set the GitHub Check Run status for a commit SHA."""
    live = os.environ.get("GATEKEEPER_LIVE", "").lower() == "true"
    if not live:
        print(f"[DRY-RUN] set_check_run_status for SHA {sha}: conclusion={conclusion}, summary={summary}")
        return f"DRY-RUN success: Check run status logged."
        
    client = get_github_client()
    if not client:
        return "Error: GITHUB_TOKEN not set, cannot update check run."
    repo_name = os.environ.get("REPO")
    try:
        repo = client.get_repo(repo_name)
        repo.create_check_run(
            name="PR Gatekeeper",
            head_sha=sha,
            status="completed",
            conclusion=conclusion,
            output={
                "title": "PR Gatekeeper Security Scan",
                "summary": summary
            }
        )
        return "Successfully created check run status."
    except Exception as e:
        return f"Error creating check run: {str(e)}"

if __name__ == "__main__":
    mcp.run()
