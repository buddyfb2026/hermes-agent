import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bizina-hermes-safe-update.sh"


def call_function(home: Path, body: str, **extra: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        HOME=str(home),
        BIZINA_SAFE_UPDATE_LIB_ONLY="1",
        HERMES_AGENT_DIR=str(home / "agent"),
        DESKTOP_DATA_DIR=str(home / "desktop-data"),
        DESKTOP_LOG=str(home / "desktop.log"),
        DESKTOP_ROLLOUT_TIMEOUT="2",
        DESKTOP_STABILITY_SECONDS="0.5",
        **extra,
    )
    return subprocess.run(
        ["bash", "-c", f"source {shlex_quote(str(SCRIPT))}; {body}"],
        text=True,
        capture_output=True,
        env=env,
    )


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


def test_connection_preflight_rejects_unreachable_saved_primary(tmp_path):
    data = tmp_path / "desktop-data"
    data.mkdir()
    (data / "connections.json").write_text(
        '{"primary":"dead","connections":[{"id":"dead","kind":"remote",'
        '"url":"http://127.0.0.1:9","authMode":"oauth"}]}'
    )

    result = call_function(tmp_path, "selected_connection_preflight")

    assert result.returncode != 0
    assert "connection preflight failed" in result.stderr


def fake_desktop(tmp_path: Path) -> subprocess.Popen[bytes]:
    exe = tmp_path / "agent/apps/desktop/release/mac-arm64/Hermes.app/Contents/MacOS/Hermes"
    exe.parent.mkdir(parents=True)
    exe.touch()
    return subprocess.Popen(["/bin/sleep", "30"])


def test_rollout_gate_rejects_the_recorded_ticket_crash_loop(tmp_path):
    process = fake_desktop(tmp_path)
    try:
        (tmp_path / "desktop.log").write_text(
            "[boot] Desktop boot failed: Could not reach the remote Hermes gateway while refreshing its WebSocket ticket.\n"
        )
        result = call_function(
            tmp_path, "verify_desktop_rollout 0", DESKTOP_TEST_PID=str(process.pid)
        )
        assert result.returncode != 0
        assert "Desktop boot failed:" in result.stderr
    finally:
        process.terminate()
        process.wait()


def test_rollout_gate_requires_ready_then_stable_process(tmp_path):
    process = fake_desktop(tmp_path)
    try:
        (tmp_path / "desktop.log").write_text(
            "[boot] Hermes backend is ready. Finalizing desktop startup\n"
        )
        result = call_function(
            tmp_path, "verify_desktop_rollout 0", DESKTOP_TEST_PID=str(process.pid)
        )
        assert result.returncode == 0, result.stderr
        assert "stable_for=0.5s" in result.stdout
    finally:
        process.terminate()
        process.wait()


def test_rollout_gate_rejects_renderer_reference_error_after_ready(tmp_path):
    process = fake_desktop(tmp_path)
    try:
        (tmp_path / "desktop.log").write_text(
            "[boot] Hermes backend is ready. Finalizing desktop startup\n"
            "[renderer console:main] ReferenceError: sessionsWorkspaceName is not defined\n"
        )
        result = call_function(
            tmp_path, "verify_desktop_rollout 0", DESKTOP_TEST_PID=str(process.pid)
        )
        assert result.returncode != 0
        assert "ReferenceError:" in result.stderr
    finally:
        process.terminate()
        process.wait()


def fake_codesign(tmp_path: Path, exit_code: int) -> Path:
    binary = tmp_path / "codesign"
    binary.write_text(f"#!/bin/sh\nexit {exit_code}\n")
    binary.chmod(0o755)
    (tmp_path / "agent/apps/desktop/release/mac-arm64/Hermes.app").mkdir(
        parents=True, exist_ok=True
    )
    return binary


def test_signature_gate_rejects_invalid_promoted_bundle(tmp_path):
    binary = fake_codesign(tmp_path, 1)
    result = call_function(tmp_path, "verify_desktop_bundle", CODESIGN_BIN=str(binary))
    assert result.returncode != 0
    assert "signature is invalid" in result.stderr


def test_signature_gate_accepts_strictly_valid_bundle(tmp_path):
    binary = fake_codesign(tmp_path, 0)
    result = call_function(tmp_path, "verify_desktop_bundle", CODESIGN_BIN=str(binary))
    assert result.returncode == 0, result.stderr
    assert "signature: valid on disk" in result.stdout


def test_rollback_restores_app_bundle_and_both_connection_files(tmp_path):
    backup = tmp_path / "backup"
    (backup / "Hermes.app").mkdir(parents=True)
    (backup / "Hermes.app/version.txt").write_text("known-good\n")
    (backup / "desktop-data").mkdir()
    (backup / "desktop-data/connection.json").write_text('{"mode":"local"}\n')
    (backup / "desktop-data/connections.json").write_text('{"primary":"local"}\n')

    live_app = tmp_path / "agent/apps/desktop/release/mac-arm64/Hermes.app"
    live_app.mkdir(parents=True)
    (live_app / "version.txt").write_text("failed-candidate\n")
    desktop_data = tmp_path / "desktop-data"
    desktop_data.mkdir()
    (desktop_data / "connection.json").write_text('{"mode":"remote"}\n')
    (desktop_data / "connections.json").write_text('{"primary":"dead"}\n')

    result = call_function(
        tmp_path,
        f"restore_app_and_connections {shlex_quote(str(backup))} {shlex_quote(str(live_app))}",
    )

    assert result.returncode == 0, result.stderr
    assert (live_app / "version.txt").read_text() == "known-good\n"
    assert (desktop_data / "connection.json").read_text() == '{"mode":"local"}\n'
    assert (desktop_data / "connections.json").read_text() == '{"primary":"local"}\n'
