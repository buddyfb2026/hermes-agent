#!/usr/bin/env python3
"""Project explicitly bound Claude/ChatGPT Desktop PR reviews for Hermes.

Bindings are the routing authority. Vendor titles, branches, CWDs and transcript
text are never used to infer an issue or PR. The observer is local/read-only and
publishes only sanitized assistant-visible output into 0600 JSON projections.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

HOME = Path.home()
BINDINGS = HOME / ".hermes/external-workers/pr-reviews/bindings"
OUTPUT = HOME / ".hermes/external-workers/pr-reviews/active"
CLAUDE_META = HOME / "Library/Application Support/Claude/claude-code-sessions"
CLAUDE_TRANSCRIPTS = HOME / ".claude/projects"
CODEX_DB = HOME / ".codex/state_5.sqlite"
ISSUE_RE = re.compile(r"^[A-Z]{2,10}-\d+$")
PR_RE = re.compile(r"^https://github\.com/[^/]+/[^/]+/pull/(\d+)$")
SECRET_RE = re.compile(r"(?i)(token|secret|password|authorization|api[_-]?key)(\s*[:=]\s*)(\S+)")


def sanitize(text: str) -> str:
    clean = SECRET_RE.sub(r"\1\2[REDACTED]", str(text).strip())
    return clean[:1800]


def validate_binding(raw: dict[str, Any]) -> dict[str, Any] | None:
    provider = str(raw.get("provider") or "").lower().strip()
    issue = str(raw.get("issue_key") or "").upper().strip()
    url = str(raw.get("pr_url") or "").strip()
    thread = str(raw.get("provider_thread_id") or "").strip()
    match = PR_RE.fullmatch(url)
    number = int(raw.get("pr_number") or 0)
    if provider not in {"claude-desktop", "chatgpt-desktop"} or not ISSUE_RE.fullmatch(issue):
        return None
    if not thread or not match or number != int(match.group(1)):
        return None
    return {**raw, "provider": provider, "issue_key": issue, "pr_url": url, "pr_number": number,
            "provider_thread_id": thread, "review_id": str(raw.get("review_id") or thread)}


def text_parts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    out: list[str] = []
    for part in content:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part, dict) and part.get("type") in {"text", "output_text"} and part.get("text"):
            out.append(str(part["text"]))
    return out


def claude_projection(binding: dict[str, Any]) -> tuple[str, list[str], float]:
    target = binding["provider_thread_id"]
    metadata = None
    for path in CLAUDE_META.glob("**/*.json"):
        try:
            raw = json.loads(path.read_text())
            if target in {str(raw.get("sessionId") or ""), str(raw.get("cliSessionId") or "")}:
                metadata = raw
                break
        except Exception:
            continue
    if not metadata:
        return "unavailable", [], 0
    # Structured PR metadata must corroborate the binding.
    prs = metadata.get("prs") if isinstance(metadata.get("prs"), list) else []
    if not any(int(pr.get("prNumber") or 0) == binding["pr_number"] and str(pr.get("url") or "") == binding["pr_url"] for pr in prs):
        return "unbound", [], 0
    cli_id = str(metadata.get("cliSessionId") or target)
    transcript = next(iter(CLAUDE_TRANSCRIPTS.glob(f"**/{cli_id}.jsonl")), None)
    lines: list[str] = []
    updated = 0.0
    if transcript:
        updated = transcript.stat().st_mtime
        for raw_line in transcript.read_text(errors="ignore").splitlines():
            try:
                event = json.loads(raw_line)
                if event.get("type") != "assistant":
                    continue
                message = event.get("message") or {}
                for text in text_parts(message.get("content")):
                    if text.strip():
                        lines.append(sanitize(text))
            except Exception:
                continue
    explicit = str(binding.get("state") or "").lower()
    state = explicit if explicit else ("merged" if any(str(pr.get("state") or "").upper() == "MERGED" and int(pr.get("prNumber") or 0) == binding["pr_number"] for pr in prs) else "reviewing")
    return state, lines[-100:], updated


def chatgpt_projection(binding: dict[str, Any]) -> tuple[str, list[str], float]:
    if not CODEX_DB.exists():
        return "unavailable", [], 0
    with sqlite3.connect(f"file:{CODEX_DB}?mode=ro", uri=True) as db:
        row = db.execute("SELECT rollout_path, updated_at FROM threads WHERE id=?", (binding["provider_thread_id"],)).fetchone()
    if not row or not row[0]:
        return "unavailable", [], 0
    path = Path(row[0])
    lines: list[str] = []
    originator = ""
    complete = False
    for raw_line in path.read_text(errors="ignore").splitlines():
        try:
            event = json.loads(raw_line)
            payload = event.get("payload") or {}
            if event.get("type") == "session_meta":
                originator = str(payload.get("originator") or "")
            if event.get("type") == "event_msg" and payload.get("type") == "agent_message":
                text = payload.get("message") or payload.get("text")
                if text:
                    lines.append(sanitize(text))
            if event.get("type") == "event_msg" and payload.get("type") == "task_complete":
                complete = True
        except Exception:
            continue
    if originator != "Codex Desktop":
        return "unbound", [], 0
    explicit = str(binding.get("state") or "").lower()
    return explicit or ("complete" if complete else "reviewing"), lines[-100:], path.stat().st_mtime


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        try: os.unlink(temp_name)
        except FileNotFoundError: pass


def project(binding: dict[str, Any]) -> dict[str, Any]:
    state, lines, updated = claude_projection(binding) if binding["provider"] == "claude-desktop" else chatgpt_projection(binding)
    label = "Claude Desktop" if binding["provider"] == "claude-desktop" else "ChatGPT Desktop"
    return {"schema": 1, "provider": label, "provider_kind": binding["provider"],
            "thread_id": binding["provider_thread_id"], "review_id": binding["review_id"],
            "issue_key": binding["issue_key"], "pr_number": binding["pr_number"],
            "pr_url": binding["pr_url"], "url": binding["pr_url"], "state": state,
            "lines": lines, "source_note": "Observed from local Desktop transcript",
            "source_updated_at": updated, "updated_at": time.time()}


def main() -> int:
    BINDINGS.mkdir(parents=True, exist_ok=True, mode=0o700)
    OUTPUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    for path in sorted(BINDINGS.glob("*.json")):
        try:
            binding = validate_binding(json.loads(path.read_text()))
            if not binding:
                continue
            atomic_write(OUTPUT / path.name, project(binding))
        except Exception:
            continue
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
