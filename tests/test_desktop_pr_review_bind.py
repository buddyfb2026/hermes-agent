import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "desktop_pr_review_bind.py"
SPEC = importlib.util.spec_from_file_location("desktop_pr_review_bind", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

ISSUE = "BIZ-1285"
URL = "https://github.com/buddyfb2026/AI-in-a-BOX/pull/1285"


def test_claude_discovery_uses_exact_structured_pr_metadata(tmp_path, capsys):
    meta = tmp_path / "meta" / "account" / "org"
    meta.mkdir(parents=True)
    (meta / "session.json").write_text(json.dumps({
        "sessionId": "claude-thread-1",
        "title": "misleading BIZ-9999 title",
        "prs": [{"prNumber": 1285, "url": URL}],
    }))
    bindings = tmp_path / "bindings"
    assert MODULE.main([
        "--provider", "claude-desktop", "--discover",
        "--issue-key", ISSUE, "--pr-url", URL, "--pr-number", "1285",
        "--bindings-root", str(bindings), "--claude-meta-root", str(tmp_path / "meta"),
    ]) == 0
    files = list(bindings.glob("*.json"))
    assert len(files) == 1
    record = json.loads(files[0].read_text())
    assert record["provider_thread_id"] == "claude-thread-1"
    assert record["issue_key"] == ISSUE
    assert files[0].stat().st_mode & 0o777 == 0o600
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_claude_discovery_refuses_ambiguous_exact_matches(tmp_path):
    meta = tmp_path / "meta"
    meta.mkdir()
    for index in (1, 2):
        (meta / f"{index}.json").write_text(json.dumps({
            "sessionId": f"thread-{index}", "prs": [{"prNumber": 1285, "url": URL}],
        }))
    with pytest.raises(ValueError, match="found 2"):
        MODULE.main([
            "--provider", "claude-desktop", "--discover", "--issue-key", ISSUE,
            "--pr-url", URL, "--pr-number", "1285", "--claude-meta-root", str(meta),
            "--bindings-root", str(tmp_path / "bindings"),
        ])
    assert not (tmp_path / "bindings").exists()


def test_chatgpt_requires_explicit_verified_codex_desktop_thread(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {"originator": "Codex Desktop"}}) + "\n")
    db_path = tmp_path / "state.sqlite"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT)")
        db.execute("INSERT INTO threads VALUES(?,?)", ("chatgpt-thread-1", str(rollout)))
    bindings = tmp_path / "bindings"
    assert MODULE.main([
        "--provider", "chatgpt-desktop", "--thread-id", "chatgpt-thread-1",
        "--issue-key", ISSUE, "--pr-url", URL, "--pr-number", "1285",
        "--codex-db", str(db_path), "--bindings-root", str(bindings),
    ]) == 0
    record = json.loads(next(bindings.glob("*.json")).read_text())
    assert record["provider"] == "chatgpt-desktop"
    assert record["provider_thread_id"] == "chatgpt-thread-1"


def test_chatgpt_never_discovers_or_accepts_non_desktop_thread(tmp_path):
    with pytest.raises(ValueError, match="discovery is forbidden"):
        MODULE.main([
            "--provider", "chatgpt-desktop", "--discover", "--issue-key", ISSUE,
            "--pr-url", URL, "--pr-number", "1285", "--bindings-root", str(tmp_path / "bindings"),
        ])

    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {"originator": "codex_cli_rs"}}) + "\n")
    db_path = tmp_path / "state.sqlite"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT)")
        db.execute("INSERT INTO threads VALUES(?,?)", ("headless-thread", str(rollout)))
    with pytest.raises(ValueError, match="not owned"):
        MODULE.main([
            "--provider", "chatgpt-desktop", "--thread-id", "headless-thread",
            "--issue-key", ISSUE, "--pr-url", URL, "--pr-number", "1285",
            "--codex-db", str(db_path), "--bindings-root", str(tmp_path / "bindings"),
        ])


def test_pr_url_number_mismatch_refuses_before_write(tmp_path):
    with pytest.raises(ValueError, match="do not match"):
        MODULE.main([
            "--provider", "claude-desktop", "--thread-id", "thread",
            "--issue-key", ISSUE, "--pr-url", URL, "--pr-number", "9",
            "--bindings-root", str(tmp_path / "bindings"),
        ])
    assert not (tmp_path / "bindings").exists()
