"""
cli_runner.py — Non-interactive entrypoint for GitHub Actions CI.

Reads PR metadata from environment variables, runs the PR Gatekeeper ADK
Workflow to completion, prints the final decision, and exits non-zero if
the decision is BLOCK_MERGE (so the GitHub job itself shows red, independent
of the custom Check Run status set by ReviewComposerAgent).

Usage (Actions workflow):
    uv run python -m app.cli_runner

Required environment variables:
    GOOGLE_API_KEY   — Gemini API key
    PR_NUMBER        — Pull request number (int)
    REPO             — Repository in "owner/repo" format
    BASE_SHA         — Base commit SHA
    HEAD_SHA         — Head commit SHA

Optional:
    GATEKEEPER_LIVE  — Set to "true" to enable live GitHub writes
    GEMINI_MODEL     — Override the Gemini model (default: gemini-3.5-flash)
"""

import asyncio
import json
import os
import sys
import uuid

from dotenv import load_dotenv

# Load .env if present (local dev); in CI these come from the workflow env block.
load_dotenv()

# Ensure Vertex AI is disabled (we use Gemini API key only).
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "False")


def _require_env(name: str) -> str:
    """Return env var value or abort with a clear message."""
    val = os.environ.get(name, "").strip()
    if not val:
        print(f"ERROR: Required environment variable '{name}' is not set.", file=sys.stderr)
        sys.exit(2)
    return val


async def main() -> None:
    # 1. Read PR metadata from environment
    pr_number_raw = _require_env("PR_NUMBER")
    try:
        pr_number = int(pr_number_raw)
    except ValueError:
        print(f"ERROR: PR_NUMBER must be an integer, got '{pr_number_raw}'.", file=sys.stderr)
        sys.exit(2)

    repo     = _require_env("REPO")
    base_sha = _require_env("BASE_SHA")
    head_sha = _require_env("HEAD_SHA")

    print(f"[cli_runner] Starting PR Gatekeeper for PR #{pr_number} in {repo}")
    print(f"[cli_runner] base={base_sha[:12]}  head={head_sha[:12]}")
    sys.stdout.flush()

    # 2. Import the ADK app (deferred so env vars are set first)
    from google.adk import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types as genai_types
    from app.agent import app as adk_app

    # 3. Bootstrap a transient session
    session_service = InMemorySessionService()
    user_id    = "ci-runner"
    session_id = f"run-{uuid.uuid4().hex}"

    initial_state = {
        "pr_number": pr_number,
        "repo":      repo,
        "base_sha":  base_sha,
        "head_sha":  head_sha,
        "findings":  [],
        "decision":  "",
        "review_summary": "",
        "repo_context":   "",
    }

    await session_service.create_session(
        app_name=adk_app.name,
        user_id=user_id,
        session_id=session_id,
        state=initial_state,
    )

    # 4. Run the workflow to completion
    runner = Runner(
        app=adk_app,
        session_service=session_service,
    )

    # The initial message passes PR context so context_phase can bootstrap state.
    initial_message = genai_types.Content(
        role="user",
        parts=[
            genai_types.Part(
                text=json.dumps({
                    "pr_number": pr_number,
                    "repo":      repo,
                    "base_sha":  base_sha,
                    "head_sha":  head_sha,
                })
            )
        ],
    )

    print("[cli_runner] Workflow running ...")
    sys.stdout.flush()

    async with runner:
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=initial_message,
        ):
            # Stream events to stdout so the Action log shows real-time progress.
            if hasattr(event, "content") and event.content:
                for part in getattr(event.content, "parts", []):
                    text = getattr(part, "text", None)
                    if text:
                        print(f"[event] {text[:200]}")
            sys.stdout.flush()

    # 5. Read final decision from session state
    session = await session_service.get_session(
        app_name=adk_app.name,
        user_id=user_id,
        session_id=session_id,
    )
    decision = (session.state or {}).get("decision", "UNKNOWN")

    print(f"\n[cli_runner] === FINAL DECISION: {decision} ===")
    sys.stdout.flush()

    # 6. Exit code drives the GitHub job status
    # exit 1 -> job shows red  (BLOCK_MERGE)
    # exit 0 -> job shows green (AUTO_COMMENT)
    if decision == "BLOCK_MERGE":
        print("[cli_runner] Exiting non-zero -- branch protection will block merge.")
        sys.exit(1)

    print("[cli_runner] Exiting zero -- PR is clear to merge (pending human review).")
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
