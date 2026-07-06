# ruff: noqa
# Copyright 2026 Google LLC

import os
import json
import logging
import sys
from typing import Any, Optional, Union
from pydantic import BaseModel, Field

from google.adk import Workflow, Context
from google.adk.apps import App
from google.adk.agents import Agent
from google.adk.models import Gemini
from google.adk.workflow import node, Edge, START, JoinNode
from google.adk.tools import FunctionTool

from app.config import config
from app.mcp_server import (
    get_pr_diff,
    get_pr_files,
    get_file_context,
    run_semgrep,
    run_sca_scan,
    post_pr_comment,
    set_check_run_status,
)

# ----------------------------------------------------------------------
# 1. State Definition and Schemas
# ----------------------------------------------------------------------

class Finding(BaseModel):
    severity: str = Field(description="Severity: INFO, WARNING, or CRITICAL")
    file: str = Field(description="The path of the file containing the finding")
    line: int = Field(description="The line number of the finding (0 if global/no line)")
    rule_or_reasoning: str = Field(description="Reasoning or description of the rule violated")
    suggested_fix: str = Field(description="Concrete suggestion to resolve the issue")

class PRGatekeeperState(BaseModel):
    pr_number: int = 0
    repo: str = ""
    base_sha: str = ""
    head_sha: str = ""
    diff: str = ""
    changed_files: list[str] = []
    repo_context: str = ""
    findings: list[Finding] = []
    decision: str = ""  # AUTO_COMMENT or BLOCK_MERGE
    review_summary: str = ""

# Direct FunctionTools — wraps mcp_server functions without a subprocess
tools = [
    FunctionTool(get_pr_diff),
    FunctionTool(get_pr_files),
    FunctionTool(get_file_context),
    FunctionTool(run_semgrep),
    FunctionTool(run_sca_scan),
    FunctionTool(post_pr_comment),
    FunctionTool(set_check_run_status),
]

# ----------------------------------------------------------------------
# 3. Agent Definitions
# ----------------------------------------------------------------------

model_instance = Gemini(model=config.model)

# 3a. ContextAgent
ContextAgent = Agent(
    name="ContextAgent",
    model=model_instance,
    instruction="""You are ContextAgent. Your task is to gather PR context.
Use your tools to:
1. List changed files in the PR (`get_pr_files`).
2. Fetch the unified diff (`get_pr_diff`).
3. For any modified or newly added source files (especially routes, handlers, or config files), fetch full content of 2-3 sibling/related files at the base ref (`get_file_context`) to help downstream agents ground their decisions in the repository's existing auth and style patterns.
Produce a comprehensive report summarizing the changed files, code changes, and relevant sibling file code patterns.""",
    tools=tools
)

# 3b. SecurityAgent
_FINDING_SCHEMA = (
    '[{"severity": "CRITICAL|WARNING|INFO", "file": "path", '
    '"line": 0, "rule_or_reasoning": "description", "suggested_fix": "fix"}]'
)

SecurityAgent = Agent(
    name="SecurityAgent",
    model=model_instance,
    instruction=f"""You are SecurityAgent. Audit the PR context provided by the user for:
1. Secrets: Hardcoded credentials/keys. Call `run_semgrep` with a list of changed file paths.
2. SCA: Vulnerable dependencies in lockfiles. Call `run_sca_scan` on changed lockfiles.
3. Auth/Authz: Verify new/modified routes check authentication/session, matching sibling route patterns.
4. Injection & SSRF: Dangerous raw input, SQL interpolation, eval, or unvalidated URLs.
5. PII: Ensure personal data is not logged.

You MUST respond ONLY with a valid JSON array (no markdown, no code fences, no explanation).
Schema: {_FINDING_SCHEMA}
If there are no findings, respond with an empty array: []""",
    tools=tools
)

# 3c. QualityAgent
QualityAgent = Agent(
    name="QualityAgent",
    model=model_instance,
    instruction=f"""You are QualityAgent. Audit the PR context provided by the user for:
1. Code quality, styling, and conventions.
2. Error handling: uncaught exceptions, resource leaks.
3. Code smell: dead code, unused imports, leftover debug statements.

You MUST respond ONLY with a valid JSON array (no markdown, no code fences, no explanation).
Schema: {_FINDING_SCHEMA}
If there are no findings, respond with an empty array: []""",
    tools=tools
)

# 3d. ReviewComposerAgent
ReviewComposerAgent = Agent(
    name="ReviewComposerAgent",
    model=model_instance,
    instruction="""You are ReviewComposerAgent. Write a final, comprehensive PR review comment.
Include:
1. A Vibe Diff: plain-English description of what this PR does and why it was flagged (if applicable).
2. A table of findings grouped by severity (CRITICAL, WARNING, INFO).
3. Clear decision: BLOCKED (CRITICAL findings) or AUTO_COMMENT.
Call `post_pr_comment` to publish the review, and `set_check_run_status` to set the check run result.""",
    tools=tools
)

# ----------------------------------------------------------------------
# 4. Workflow Graph Nodes
# ----------------------------------------------------------------------

@node(rerun_on_resume=True)
async def context_phase(ctx: Context):
    # Initialize keys if not already present
    for key, default in [("pr_number", 0), ("repo", ""), ("base_sha", ""), ("head_sha", ""), ("findings", []), ("decision", ""), ("review_summary", ""), ("repo_context", "")]:
        if key not in ctx.state:
            ctx.state[key] = default

    # Initialize state from input if provided
    # ctx.user_content in ADK is a genai Content object with .parts[].text
    raw = ctx.user_content
    inp = {}
    if raw is not None:
        # Extract text from ADK Content object (has .parts with .text)
        text = None
        if hasattr(raw, "parts") and raw.parts:
            text = "".join(p.text for p in raw.parts if hasattr(p, "text") and p.text)
        elif isinstance(raw, str):
            text = raw
        elif isinstance(raw, dict):
            inp = raw

        if text:
            try:
                inp = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                inp = {}

    if inp:
        ctx.state["pr_number"] = inp.get("pr_number", ctx.state["pr_number"])
        ctx.state["repo"] = inp.get("repo", ctx.state["repo"])
        ctx.state["base_sha"] = inp.get("base_sha", ctx.state["base_sha"])
        ctx.state["head_sha"] = inp.get("head_sha", ctx.state["head_sha"])

    pr_num = ctx.state["pr_number"]
    repo = ctx.state["repo"]
    prompt = f"Extract repository and PR context for PR #{pr_num} in repo {repo}. Use get_pr_files to list changed files, get_pr_diff to fetch the diff, and get_file_context for sibling files."
    res = await ctx.run_node(ContextAgent, node_input=prompt)
    ctx.state["repo_context"] = str(res)
    return "ok"

def _normalize_findings(findings: list) -> list[dict]:
    """Convert Finding objects or dicts to plain dicts for state storage."""
    result = []
    for f in findings:
        if isinstance(f, dict):
            result.append(f)
        elif hasattr(f, "model_dump"):
            result.append(f.model_dump())
        else:
            result.append({"severity": "INFO", "file": "", "line": 0, "rule_or_reasoning": str(f), "suggested_fix": ""})
    return result

def _parse_findings(raw) -> list[dict]:
    """Parse agent response into a list of finding dicts. Robust to text, JSON, or Pydantic objects."""
    import re
    if isinstance(raw, list):
        return _normalize_findings(raw)
    if not raw:
        return []
    text = str(raw)
    # Strip markdown code fences if present
    text = re.sub(r"^```[\w]*\n?", "", text.strip(), flags=re.MULTILINE)
    text = re.sub(r"\n?```$", "", text.strip(), flags=re.MULTILINE)
    text = text.strip()
    # Try to parse as JSON array
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return _normalize_findings(parsed)
        if isinstance(parsed, dict):
            return _normalize_findings([parsed])
    except json.JSONDecodeError:
        pass
    # Try to extract a JSON array from the text
    match = re.search(r"(\[.*?\])", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(1))
            if isinstance(parsed, list):
                return _normalize_findings(parsed)
        except json.JSONDecodeError:
            pass
    # Fallback: return empty list (no parseable findings)
    return []

@node(rerun_on_resume=True)
async def security_phase(ctx: Context):
    prompt = f"Run security audit. PR Context:\n{ctx.state.get('repo_context')}"
    raw = await ctx.run_node(SecurityAgent, node_input=prompt)
    findings = _parse_findings(raw)
    current = ctx.state.get("findings", [])
    ctx.state["findings"] = current + findings
    return "ok"

@node(rerun_on_resume=True)
async def quality_phase(ctx: Context):
    prompt = f"Run quality and conventions audit. PR Context:\n{ctx.state.get('repo_context')}"
    raw = await ctx.run_node(QualityAgent, node_input=prompt)
    findings = _parse_findings(raw)
    current = ctx.state.get("findings", [])
    ctx.state["findings"] = current + findings
    return "ok"

join_audits = JoinNode(name="join_audits")

@node(rerun_on_resume=True)
def security_checkpoint(ctx: Context):
    findings_list = _normalize_findings(ctx.state.get("findings", []))
    # Deterministic checkpoint — dict access since state stores plain dicts
    has_critical = any(
        (f.get("severity", "") if isinstance(f, dict) else f.severity).upper() == "CRITICAL"
        for f in findings_list
    )
    decision = "BLOCK_MERGE" if has_critical else "AUTO_COMMENT"
    ctx.state["decision"] = decision

    audit_log = {
        "severity": "CRITICAL" if has_critical else "INFO",
        "pr_number": ctx.state.get("pr_number", 0),
        "repo": ctx.state.get("repo", ""),
        "commit_sha": ctx.state.get("head_sha", ""),
        "findings_count": len(findings_list),
        "decision": decision
    }
    print(f"AUDIT_LOG: {json.dumps(audit_log)}")
    sys.stdout.flush()

    return decision

@node(rerun_on_resume=True)
async def composer_phase(ctx: Context):
    # Findings are already plain dicts in state — no .model_dump() needed
    findings_list = _normalize_findings(ctx.state.get("findings", []))
    findings_json = json.dumps(findings_list, indent=2)
    prompt = (
        f"Compose and post final PR review.\n"
        f"PR Number: {ctx.state.get('pr_number')}\n"
        f"Repository: {ctx.state.get('repo')}\n"
        f"Commit SHA: {ctx.state.get('head_sha')}\n"
        f"Decision: {ctx.state.get('decision')}\n"
        f"Findings:\n{findings_json}"
    )
    res = await ctx.run_node(ReviewComposerAgent, node_input=prompt)
    ctx.state["review_summary"] = str(res)
    return "done"

# ----------------------------------------------------------------------
# 5. Workflow and App Construction
# ----------------------------------------------------------------------

# We use the single edges converging routes pattern
wf = Workflow(
    name="pr_gatekeeper_workflow",
    state_schema=PRGatekeeperState,
    edges=[
        Edge(from_node=START, to_node=context_phase),
        Edge(from_node=context_phase, to_node=security_phase),
        Edge(from_node=context_phase, to_node=quality_phase),
        Edge(from_node=security_phase, to_node=join_audits),
        Edge(from_node=quality_phase, to_node=join_audits),
        # Deterministic checkpoint
        Edge(from_node=join_audits, to_node=security_checkpoint),
        # Routes converge into composer_phase via a single edge
        Edge(from_node=security_checkpoint, to_node=composer_phase)
    ]
)

root_agent = wf

app = App(
    root_agent=root_agent,
    name="app",
)
