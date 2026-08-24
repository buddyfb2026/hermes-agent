"""
BIZ-208 — Hermes Avengers gateway poll loop.

Each Hermes profile (base ``~/.hermes`` + ``hermes2..hermes5``) runs an idle
poll loop that atomically claims one row at a time from the central
``hermes_jobs`` queue (BIZ-202 / db-central migration ``030_hermes_jobs.sql``),
runs the existing packet-authoring path through ``cron.scheduler.run_job``,
and writes back ``completed_at`` / ``failed_at`` on the row.

Lifecycle (called from ``gateway/run.py`` ``start_gateway``):

    worker = HermesJobsWorker.from_env(adapters=runner.adapters, loop=loop)
    if worker is not None:
        worker_task = asyncio.create_task(worker.run(), name="hermes-jobs-worker")
    ...
    if worker is not None:
        await worker.stop()
        await worker_task

Off-ramp: when ``HERMES_JOBS_DATABASE_URL`` is unset, ``from_env`` returns
``None`` after a single info log. The gateway boots normally on machines that
are not part of the Avengers pool. ``asyncpg`` is the only required new
runtime dependency.

Pinning notes:
  - ``CLAIM_NEXT_HERMES_JOB_SQL`` is copied from
    ``services/central-api/src/linear/agent-router.ts`` (BIZ-202). Keep the
    single-statement ``UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP
    LOCKED) ... RETURNING *`` shape — central-api unit tests pin this.
  - Callsign strings (``"Hermes"``, ``"Iron Man"``, ``"Captain America"``,
    ``"Black Widow"``, ``"Spiderman"``) must match exactly. central-api's
    ``lookupHermesPoolCallsign`` ``.trim()``s whitespace but does NOT
    case-fold. Wrong spelling = wrong attribution in Linear comments.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pinned SQL — see module docstring.
# ---------------------------------------------------------------------------

CLAIM_NEXT_HERMES_JOB_SQL = """
UPDATE hermes_jobs
   SET claimed_by = $1,
       claimed_at = now(),
       lease_expires_at = now() + interval '30 minutes'
 WHERE id IN (
   SELECT id FROM hermes_jobs
      WHERE completed_at IS NULL
        AND failed_at IS NULL
        AND (claimed_by IS NULL OR lease_expires_at < now())
        AND (
          metadata->'retry_after_epoch' IS NULL
          OR jsonb_typeof(metadata->'retry_after_epoch') <> 'number'
          OR (metadata->>'retry_after_epoch')::double precision <= extract(epoch FROM now())
        )
      AND (
        metadata->>'exclude_callsign' IS NULL
        OR metadata->>'exclude_callsign' = ''
        OR metadata->>'exclude_callsign' != $1
      )
      AND (
        metadata->>'preferred_callsign' IS NULL
        OR metadata->>'preferred_callsign' = ''
        OR metadata->>'preferred_callsign' = $1
        OR queued_at <= now() - interval '30 seconds'
      )
    ORDER BY
      CASE
        WHEN metadata->>'preferred_callsign' IS NULL THEN 0
        WHEN metadata->>'preferred_callsign' = '' THEN 0
        WHEN metadata->>'preferred_callsign' = $1 THEN 0
        WHEN queued_at <= now() - interval '30 seconds' THEN 0
        ELSE 1
      END,
      priority DESC,
      queued_at
    LIMIT 1 FOR UPDATE SKIP LOCKED
 )
 RETURNING *
""".strip()

# Lease renewal: extend the lease on the same row+callsign while work runs.
# Guarded by id AND claimed_by so a stolen lease (post-expiry reclaim by
# another Avenger) cannot be silently re-extended by the original owner —
# instead, ``fetchval`` returns NULL and the worker stops processing.
RENEW_LEASE_SQL = """
UPDATE hermes_jobs
   SET lease_expires_at = now() + interval '30 minutes'
 WHERE id = $1
   AND claimed_by = $2
   AND completed_at IS NULL
   AND failed_at IS NULL
 RETURNING id
""".strip()

COMPLETE_JOB_SQL = """
UPDATE hermes_jobs
   SET completed_at = now()
 WHERE id = $1
   AND claimed_by = $2
""".strip()

FAIL_JOB_SQL = """
UPDATE hermes_jobs
   SET failed_at = now(),
       last_error = $3
 WHERE id = $1
   AND claimed_by = $2
""".strip()

# BIZ-265 fix (2026-05-06): transient-failure release path. When the
# worker hits a TRANSIENT failure (e.g., the chat already has an active
# session, the lease was stolen mid-flight, etc.) we MUST NOT set
# failed_at — that permanently disqualifies the row from re-claim and
# Spencer's autonomous PR review/merge never runs. Instead, RELEASE the
# claim by clearing claimed_by/claimed_at/lease_expires_at so the row
# returns to the unclaimed pool and the next idle Avenger picks it up
# on the very next poll tick.
#
# Rationale: the original docstring at line 601-605 says "the lease will
# expire and another idle Avenger reclaims" — but that only works if
# failed_at stays NULL. The pre-fix code path called FAIL_JOB_SQL
# unconditionally, contradicting that intent and producing the BIZ-265
# failure mode where Black Widow claimed a pr_review row, hit the
# active-session pre-condition 3ms later, and the row was permanently
# dead even though no real work had happened.
RELEASE_JOB_SQL = """
UPDATE hermes_jobs
   SET claimed_by = NULL,
       claimed_at = NULL,
       lease_expires_at = NULL,
       last_error = $3,
       metadata = jsonb_set(
         jsonb_set(
           COALESCE(metadata, '{}'::jsonb),
           '{transient_attempts}',
           to_jsonb(
             (CASE
               WHEN COALESCE(metadata->>'transient_attempts', '') ~ '^[0-9]+$'
               THEN (metadata->>'transient_attempts')::integer
               ELSE 0
             END) + 1
           ),
           true
         ),
         '{retry_after_epoch}',
         to_jsonb(
           extract(epoch FROM now()) + LEAST(
             300,
             15 * power(
               2,
               LEAST(
                 5,
                 CASE
                   WHEN COALESCE(metadata->>'transient_attempts', '') ~ '^[0-9]+$'
                   THEN (metadata->>'transient_attempts')::integer
                   ELSE 0
                 END
               )
             )
           )
         ),
         true
       )
 WHERE id = $1
   AND claimed_by = $2
   AND completed_at IS NULL
   AND failed_at IS NULL
""".strip()

# BIZ-352 (2026-05-29) — rate-limit / auth no-op detection.
#
# ``ZERO_API_CALLS_MARKER`` is the substring that flags a packet-authoring
# run which exited with zero *successful* model API calls (the signature of
# a 429 / auth-dead window where the agent loop bailed before producing any
# output). It is embedded in ``ZERO_API_CALLS_ERROR`` and also listed in
# ``TRANSIENT_FAILURE_SUBSTRINGS`` so ``_writeback_completion`` RELEASES the
# row (returns it to the unclaimed pool) instead of recording a fake
# completed_at with no artifact.
ZERO_API_CALLS_MARKER = "zero successful api calls"
ZERO_API_CALLS_ERROR = (
    "rate-limit/auth no-op: packet-authoring run completed with "
    "zero successful api calls before producing output — releasing row for retry"
)

PACKET_EVIDENCE_MISSING_MARKER = "packet completion evidence missing"
PACKET_EVIDENCE_MISSING_ERROR = (
    "packet completion evidence missing: no post-queue canonical packet handoff "
    "was observed for the expected version — "
    "releasing row for retry"
)
PACKET_EVIDENCE_POLL_SECONDS = 30.0
PACKET_EVIDENCE_POLL_INTERVAL_SECONDS = 2.0

# Bounded exponential backoff applied AFTER a zero-api-calls release, so a
# sustained 429 / auth-dead window doesn't spin every idle Avenger re-claiming
# and re-tripping the same wall every poll tick. Reset to 0 on the next
# successful run. Interruptible via ``_stop_event`` so shutdown stays prompt.
RATE_LIMIT_BACKOFF_BASE_SECONDS = 30.0
RATE_LIMIT_BACKOFF_CAP_SECONDS = 600.0

# BIZ-265 fix: treat these substrings in error_msg as TRANSIENT — the
# worker releases the claim without setting failed_at. These represent
# pre-conditions that would resolve on the next poll (Spencer's manual
# DM finishes, the renewal lease re-syncs, etc.) and are not real
# implementation failures.
TRANSIENT_FAILURE_SUBSTRINGS = (
    "already has an active session",
    # 2026-05-19 — when the gateway's invalidate_generation() fires
    # mid-run (new_command from a fresh DM, manual session_reset, or
    # another Avenger touching the same session_key), the agent's
    # result is "Discarded as stale" before reaching Telegram and no
    # packet artifact lands. Release the row so another idle Avenger
    # reclaims on the next poll rather than burning the hermes_jobs
    # row as completed-without-output. Live repro: CQI-45 v1
    # 2026-05-19 14:02 CDT (Spiderman) — see project_hermes_update_safety.md.
    "session interrupted",
    # BIZ-352 (2026-05-29) — a 429 / auth-failure during packet authoring
    # makes the agent loop exit cleanly with ZERO successful API calls and
    # no artifact. _process_message_background still returns None, so the
    # pre-fix path recorded the row completed_at with api_calls=0. Treat a
    # confidently-observed zero-successful-call run as transient: RELEASE so
    # the next idle Avenger retries once the rate-limit / auth window clears.
    ZERO_API_CALLS_MARKER,
    PACKET_EVIDENCE_MISSING_MARKER,
)


# ---------------------------------------------------------------------------
# Profile → callsign mapping.
# ---------------------------------------------------------------------------
#
# ``"default"`` is the base ``~/.hermes`` profile, where
# ``hermes_cli.profiles.get_active_profile_name`` returns ``"default"``
# (HERMES_HOME unset or = ~/.hermes). ``"hermes"`` is reserved for forward
# compatibility if a future ``~/.hermes/profiles/hermes`` is created.

PROFILE_TO_CALLSIGN: Mapping[str, str] = {
    "default": "Hermes",
    "hermes": "Hermes",
    "hermes2": "Iron Man",
    "hermes3": "Captain America",
    "hermes4": "Black Widow",
    "hermes5": "Spiderman",
}

DEFAULT_ISSUE_ROOM_ROOT = Path.home() / ".hermes" / "issue-rooms" / "active"


def profile_for_callsign(callsign: str) -> Optional[str]:
    """Return the canonical local profile for an Avenger callsign."""
    for profile, mapped in PROFILE_TO_CALLSIGN.items():
        if mapped == callsign and profile != "hermes":
            return profile
    return None


def _issue_room_path(root: Path, issue_key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", str(issue_key or "unknown")).strip("-")
    return root / f"{safe or 'unknown'}.json"


def write_issue_room_state(
    root: Path,
    row: Mapping,
    *,
    profile_name: str,
    callsign: str,
    status: str,
    session_key: str = "",
    session_id: str = "",
    baseline_message_count: int = 0,
    error: str = "",
) -> Path:
    """Atomically publish one issue's authoring-room state for Desktop.

    Only routing/lifecycle metadata is copied. The transcript remains
    authoritative in the profile SessionDB and is read through the read-only
    ``session.messages`` RPC. Existing members are unioned so a retry claimed
    by another Avenger never erases the first participant.
    """
    root.mkdir(parents=True, exist_ok=True)
    issue_key = str(row.get("issue_key") or "?")
    path = _issue_room_path(root, issue_key)
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        existing = {}
    if not isinstance(existing, dict):
        existing = {}

    now = time.time()
    members = [m for m in existing.get("members", []) if isinstance(m, dict)]
    # A claim is tentative and may be released before any model turn starts.
    # Seat an Avenger only once a real authoring session exists; otherwise a
    # busy-chat retry storm paints every poller as a participant.
    if status == "authoring" and not any(m.get("profile") == profile_name for m in members):
        members.append({"profile": profile_name, "callsign": callsign, "joined_at": now})

    previous = existing.get("authoring")
    if not isinstance(previous, dict) or str(existing.get("job_id") or "") != str(row.get("id") or ""):
        previous = {}
    record = {
        "schema": 1,
        "issue_key": issue_key,
        "issue_id": str(row.get("issue_id") or existing.get("issue_id") or ""),
        "job_id": str(row.get("id") or ""),
        "room_state": "active",
        "members": members,
        "authoring": {
            "profile": profile_name,
            "callsign": callsign,
            "status": status,
            "session_key": session_key or previous.get("session_key") or "",
            "session_id": session_id or previous.get("session_id") or "",
            "baseline_message_count": int(
                baseline_message_count
                if session_key or session_id
                else previous.get("baseline_message_count") or 0
            ),
            "started_at": previous.get("started_at") or now,
            "updated_at": now,
            "error": str(error or "")[:1000],
        },
        "updated_at": now,
    }
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{profile_name}.tmp")
    tmp.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def resolve_callsign(
    profile_name: Optional[str],
    env: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """Resolve the callsign for a profile.

    Precedence:
      1. ``HERMES_AVENGER_CALLSIGN`` env override (whitespace-trimmed).
      2. ``PROFILE_TO_CALLSIGN`` table lookup.
      3. ``None`` if neither matches — caller should disable the worker
         rather than claim under an unknown callsign.
    """
    env = env if env is not None else os.environ
    override = (env.get("HERMES_AVENGER_CALLSIGN") or "").strip()
    if override:
        return override
    if not profile_name:
        return None
    return PROFILE_TO_CALLSIGN.get(profile_name)


_DSN_PASSWORD_RE = re.compile(r"(://[^:/@]+:)([^@/]+)(@)")


def redact_dsn(dsn: str) -> str:
    """Replace the password segment of a DSN with ``***`` for safe logging."""
    if not dsn:
        return ""
    return _DSN_PASSWORD_RE.sub(r"\1***\3", dsn)


# ---------------------------------------------------------------------------
# Row → cron-job-dict adapter.
# ---------------------------------------------------------------------------

def build_runner_job_from_row(row: Mapping, *, callsign: str) -> dict:
    """Convert a ``hermes_jobs`` row into the dict shape ``cron.scheduler.run_job`` expects.

    The packet-authoring prompt is read from ``payload["packet_message"]``,
    which central-api populates in ``enqueueHermesJob`` (BIZ-202). The
    returned dict carries ``deliver="telegram"`` so ``run_job`` resolves the
    profile's ``TELEGRAM_HOME_CHANNEL`` for live tool-stream visibility, and
    falls back to ``"local"`` (no delivery) when no home channel is set.
    """
    payload = row.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}

    metadata = row.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}

    issue_key = row.get("issue_key") or "?"
    issue_id = row.get("issue_id") or ""
    packet_version = (
        payload.get("packet_version")
        or metadata.get("packet_version")
        or 1
    )
    prompt = (
        payload.get("packet_message")
        or payload.get("prompt")
        or ""
    )

    # Stable but unique id so cron's session-store + output-save paths don't
    # collide across runs of the same Linear issue/version.
    row_id_str = str(row.get("id") or "")
    job_id = f"hermes_jobs_{row_id_str.replace('-', '')[:24]}"
    job_name = f"hermes-pool-{issue_key}-v{packet_version}"

    deliver = "telegram" if os.environ.get("TELEGRAM_HOME_CHANNEL") else "local"

    return {
        "id": job_id,
        "name": job_name,
        "prompt": prompt,
        "deliver": deliver,
        # Surface enough metadata for downstream logs/diagnostics without
        # bloating the prompt or persisting secrets.
        "metadata": {
            "hermes_jobs_row_id": row_id_str,
            "issue_id": str(issue_id),
            "issue_key": issue_key,
            "packet_version": packet_version,
            "claimed_by": callsign,
        },
    }


# ---------------------------------------------------------------------------
# Worker.
# ---------------------------------------------------------------------------

class HermesJobsWorker:
    """Idle poll loop that claims one ``hermes_jobs`` row at a time."""

    def __init__(
        self,
        *,
        callsign: str,
        dsn: str,
        profile_name: Optional[str] = None,
        issue_room_root: Optional[Path] = None,
        adapters: Optional[dict] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        poll_interval_seconds: float = 3.0,
        poll_jitter_seconds: float = 2.0,
        lease_renewal_interval_seconds: float = 300.0,
        runner=None,
        packet_event_fetcher=None,
        packet_evidence_poll_seconds: float = PACKET_EVIDENCE_POLL_SECONDS,
        packet_evidence_poll_interval_seconds: float = PACKET_EVIDENCE_POLL_INTERVAL_SECONDS,
    ):
        self.callsign = callsign
        self.profile_name = profile_name or profile_for_callsign(callsign) or "default"
        self.issue_room_root = Path(issue_room_root) if issue_room_root is not None else None
        self.dsn = dsn
        self.adapters = adapters or {}
        self.loop = loop
        self.poll_interval_seconds = poll_interval_seconds
        self.poll_jitter_seconds = poll_jitter_seconds
        self.lease_renewal_interval_seconds = lease_renewal_interval_seconds
        # Allow test injection of a fake runner; default to the real
        # cron.scheduler.run_job (lazy-imported inside _process_row to
        # avoid pulling cron at module import time).
        self._runner = runner
        self._packet_event_fetcher = packet_event_fetcher
        self.packet_evidence_poll_seconds = max(0.0, packet_evidence_poll_seconds)
        self.packet_evidence_poll_interval_seconds = max(
            0.01, packet_evidence_poll_interval_seconds,
        )
        self._stop_event = asyncio.Event()
        self._pool = None  # asyncpg.Pool
        self._current_row_id = None  # set while processing
        # BIZ-352: bounded exponential backoff after a zero-api-calls
        # (rate-limit / auth no-op) release. 0.0 = no backoff pending.
        self._rate_limit_backoff_seconds = 0.0

    # -- Construction ---------------------------------------------------

    @classmethod
    def from_env(
        cls,
        *,
        adapters: Optional[dict] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> Optional["HermesJobsWorker"]:
        """Construct a worker from env, or return ``None`` (worker disabled).

        Returns ``None`` (with a log entry) when:
          - ``HERMES_JOBS_DATABASE_URL`` is unset.
          - The active profile cannot be mapped to a callsign and no
            ``HERMES_AVENGER_CALLSIGN`` override is set.
        """
        dsn = (os.environ.get("HERMES_JOBS_DATABASE_URL") or "").strip()
        if not dsn:
            logger.info(
                "HermesJobsWorker disabled: HERMES_JOBS_DATABASE_URL not set "
                "(this profile is not part of the Avengers pool)."
            )
            return None

        try:
            from hermes_cli.profiles import get_active_profile_name
            profile_name = get_active_profile_name()
        except Exception:
            profile_name = None

        callsign = resolve_callsign(profile_name)
        if not callsign:
            logger.warning(
                "HermesJobsWorker disabled: profile %r has no callsign mapping. "
                "Set HERMES_AVENGER_CALLSIGN explicitly to opt this profile in.",
                profile_name,
            )
            return None

        try:
            poll = float(os.environ.get("HERMES_AVENGER_POLL_INTERVAL_SECONDS", "3"))
        except ValueError:
            poll = 3.0

        return cls(
            callsign=callsign,
            profile_name=profile_name,
            issue_room_root=DEFAULT_ISSUE_ROOM_ROOT,
            dsn=dsn,
            adapters=adapters,
            loop=loop,
            poll_interval_seconds=max(0.5, poll),
        )

    # -- Lifecycle ------------------------------------------------------

    async def run(self) -> None:
        """Main poll loop. Returns when ``stop()`` is set."""
        try:
            import asyncpg  # type: ignore[import-not-found]
        except ImportError:
            logger.error(
                "HermesJobsWorker disabled: asyncpg not installed. "
                "Install with `pip install 'hermes-agent[matrix]'` or "
                "pin asyncpg in the active environment."
            )
            return

        try:
            self._pool = await asyncpg.create_pool(
                self.dsn,
                min_size=1,
                max_size=2,
                command_timeout=30,
            )
        except Exception as e:
            logger.error(
                "HermesJobsWorker (%s) failed to connect to %s: %s",
                self.callsign, redact_dsn(self.dsn), e,
            )
            return

        logger.info(
            "HermesJobsWorker started — callsign=%r dsn=%s poll=%.1fs",
            self.callsign, redact_dsn(self.dsn), self.poll_interval_seconds,
        )

        try:
            while not self._stop_event.is_set():
                row = None
                try:
                    row = await self._claim_one()
                except Exception as e:
                    logger.exception(
                        "HermesJobsWorker (%s) claim attempt failed: %s",
                        self.callsign, e,
                    )

                if row is None:
                    # Idle — sleep with jitter unless shutdown is requested.
                    sleep_for = self.poll_interval_seconds + random.uniform(
                        0, self.poll_jitter_seconds,
                    )
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(), timeout=sleep_for,
                        )
                        break  # stop_event was set during sleep
                    except asyncio.TimeoutError:
                        continue

                # We hold a row — run it through the packet-authoring
                # pipeline with lease renewal in the background.
                await self._process_row(row)

                # BIZ-352: if the run we just finished was a rate-limit /
                # auth no-op (zero successful API calls), back off before
                # claiming the next row so a sustained 429 window doesn't
                # spin every idle Avenger re-tripping the same wall. The
                # sleep is interruptible via _stop_event for prompt shutdown.
                backoff_for = self._rate_limit_backoff_seconds
                if backoff_for > 0:
                    logger.warning(
                        "HermesJobsWorker (%s) rate-limit backoff: sleeping %.0fs before next claim",
                        self.callsign, backoff_for,
                    )
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(), timeout=backoff_for,
                        )
                        break  # stop_event was set during backoff
                    except asyncio.TimeoutError:
                        pass

        finally:
            if self._pool is not None:
                try:
                    await asyncio.wait_for(self._pool.close(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning(
                        "HermesJobsWorker (%s) pool close timed out",
                        self.callsign,
                    )
                self._pool = None
            logger.info("HermesJobsWorker (%s) stopped", self.callsign)

    async def stop(self) -> None:
        """Signal the loop to exit. Does not interrupt an in-flight job.

        Letting the in-flight ``run_job`` finish keeps the row from being
        left half-processed and visible to another Avenger on the next
        poll. If the gateway is hard-killed mid-processing, the lease
        protects the row until ``lease_expires_at`` passes — the standard
        crash-recovery path.
        """
        self._stop_event.set()

    # -- DB helpers (one connection acquire each; safe with pool size 2) -

    async def _claim_one(self) -> Optional[dict]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(CLAIM_NEXT_HERMES_JOB_SQL, self.callsign)
        if row is None:
            return None
        return dict(row)

    async def _renew_lease_loop(self, row_id) -> None:
        """Renew the row's lease every ``lease_renewal_interval_seconds``.

        Stops on ``stop_event`` or when the row is no longer ours
        (``RENEW_LEASE_SQL`` returns NULL — surfaces loudly so the agent
        run is recognised as duplicate work).
        """
        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self.lease_renewal_interval_seconds,
                    )
                    return  # stop requested
                except asyncio.TimeoutError:
                    pass

                try:
                    async with self._pool.acquire() as conn:
                        renewed = await conn.fetchval(
                            RENEW_LEASE_SQL, row_id, self.callsign,
                        )
                    if renewed is None:
                        logger.error(
                            "HermesJobsWorker (%s) lease lost on row %s — "
                            "another Avenger reclaimed it. Stopping renewal.",
                            self.callsign, row_id,
                        )
                        return
                    logger.debug(
                        "HermesJobsWorker (%s) renewed lease on row %s",
                        self.callsign, row_id,
                    )
                except Exception as e:
                    logger.warning(
                        "HermesJobsWorker (%s) lease renewal failed for row %s: %s",
                        self.callsign, row_id, e,
                    )
        except asyncio.CancelledError:
            return

    def _publish_issue_room(self, row: Mapping, *, status: str, **kwargs) -> None:
        if self.issue_room_root is None:
            return
        try:
            write_issue_room_state(
                self.issue_room_root,
                row,
                profile_name=self.profile_name,
                callsign=self.callsign,
                status=status,
                **kwargs,
            )
        except Exception:
            # Observability must never burn or interrupt a real queue job.
            logger.exception(
                "HermesJobsWorker (%s) could not publish issue-room state for %s",
                self.callsign,
                row.get("issue_key", "?"),
            )

    @staticmethod
    def _session_transcript_snapshot(adapter, session_key: str) -> tuple[str, int]:
        """Read the durable session id + message baseline without mutating it."""
        store = getattr(adapter, "_session_store", None)
        if store is None:
            return "", 0
        try:
            peek = getattr(store, "peek_session_id", None)
            session_id = str(peek(session_key) or "") if callable(peek) else ""
            load = getattr(store, "load_transcript", None)
            transcript = load(session_id or session_key) if callable(load) else []
            return session_id, len(transcript) if isinstance(transcript, (list, tuple)) else 0
        except Exception:
            return "", 0

    async def _publish_session_id_when_ready(
        self,
        row: Mapping,
        adapter,
        session_key: str,
        baseline_message_count: int,
    ) -> None:
        """Publish a freshly-created session id while authoring is still live."""
        last_session_id = ""
        try:
            while True:
                session_id, _ = self._session_transcript_snapshot(adapter, session_key)
                if session_id and session_id != last_session_id:
                    last_session_id = session_id
                    self._publish_issue_room(
                        row,
                        status="authoring",
                        session_key=session_key,
                        session_id=session_id,
                        baseline_message_count=baseline_message_count,
                    )
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            return

    async def _process_row(self, row: dict) -> None:
        row_id = row["id"]
        issue_key = row.get("issue_key", "?")
        payload_raw = row.get("payload") or {}
        if isinstance(payload_raw, str):
            try:
                payload_raw = json.loads(payload_raw)
            except Exception:
                payload_raw = {}
        packet_version = (
            payload_raw.get("packet_version") if isinstance(payload_raw, dict) else None
        ) or 1

        logger.info("🦸 %s claimed %s v%s", self.callsign, issue_key, packet_version)
        self._publish_issue_room(row, status="claimed")

        renewal_task = asyncio.create_task(
            self._renew_lease_loop(row_id),
            name=f"hermes-jobs-renew-{row_id}",
        )
        self._current_row_id = row_id

        success = False
        error_msg: Optional[str] = None
        try:
            success, error_msg = await self._run_packet_authoring(row)
            if success and row.get("dispatch_lane") == "packet_authoring":
                if not await self._wait_for_packet_completion_evidence(row):
                    success = False
                    error_msg = PACKET_EVIDENCE_MISSING_ERROR
        except Exception as e:
            success = False
            error_msg = f"{type(e).__name__}: {e}"
            logger.exception(
                "HermesJobsWorker (%s) packet-authoring crashed for row %s (%s): %s",
                self.callsign, row_id, issue_key, e,
            )
        finally:
            renewal_task.cancel()
            try:
                await renewal_task
            except (asyncio.CancelledError, Exception):
                pass
            self._current_row_id = None

        await self._writeback_completion(
            row_id, issue_key, packet_version, success, error_msg,
        )
        err_lower = (error_msg or "").lower()
        transient = (not success) and any(
            marker in err_lower for marker in TRANSIENT_FAILURE_SUBSTRINGS
        )
        room_status = "packet_ready" if success else ("released" if transient else "authoring_failed")
        self._publish_issue_room(row, status=room_status, error=error_msg or "")

        # BIZ-352: grow/reset the rate-limit backoff based on this run's
        # outcome. Done after writeback so the row state is already settled.
        self._update_rate_limit_backoff(success, error_msg)

    def _update_rate_limit_backoff(
        self, success: bool, error_msg: Optional[str],
    ) -> None:
        """Grow the rate-limit backoff on a zero-api-calls release, reset it
        otherwise.

        A *successful* run, or any failure that is NOT the zero-api-calls
        no-op, clears the backoff — we only want to throttle re-claims while
        the rate-limit / auth window is actively biting. Growth is bounded
        exponential: BASE, 2×BASE, 4×BASE, … capped at CAP.
        """
        if not success and error_msg and ZERO_API_CALLS_MARKER in error_msg.lower():
            if self._rate_limit_backoff_seconds <= 0:
                self._rate_limit_backoff_seconds = RATE_LIMIT_BACKOFF_BASE_SECONDS
            else:
                self._rate_limit_backoff_seconds = min(
                    self._rate_limit_backoff_seconds * 2.0,
                    RATE_LIMIT_BACKOFF_CAP_SECONDS,
                )
        else:
            self._rate_limit_backoff_seconds = 0.0

    @staticmethod
    def _parse_timestamp(value) -> Optional[datetime]:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str) and value.strip():
            try:
                parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError:
                return None
        else:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _has_packet_completion_evidence(cls, row: Mapping, events) -> bool:
        """Require the canonical v1 or rework handoff for this queue run."""
        queued_at = cls._parse_timestamp(row.get("queued_at"))
        if queued_at is None:
            return False
        payload = row.get("payload") or {}
        metadata = row.get("metadata") or {}
        for name, value in (("payload", payload), ("metadata", metadata)):
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                except (TypeError, ValueError):
                    parsed = {}
                if name == "payload":
                    payload = parsed
                else:
                    metadata = parsed
        try:
            version = int(
                (payload if isinstance(payload, dict) else {}).get("packet_version")
                or (metadata if isinstance(metadata, dict) else {}).get(
                    "packet_version_target"
                )
                or (metadata if isinstance(metadata, dict) else {}).get("packet_version")
                or 1
            )
        except (TypeError, ValueError):
            return False
        issue_id = str(row.get("issue_id") or "")
        issue_key = str(row.get("issue_key") or "")
        observed = set()
        for event in events or ():
            if not isinstance(event, Mapping):
                continue
            event_issue_id = str(event.get("issue_id") or "")
            event_issue_key = str(event.get("issue_key") or "")
            if issue_id and event_issue_id and event_issue_id != issue_id:
                continue
            if issue_key and event_issue_key and event_issue_key != issue_key:
                continue
            raw_event_version = event.get("packet_version")
            if raw_event_version is None:
                continue
            try:
                event_version = int(raw_event_version)
            except (TypeError, ValueError):
                continue
            if event_version != version:
                continue
            created_at = cls._parse_timestamp(event.get("created_at"))
            if created_at is None or created_at < queued_at:
                continue
            event_type = str(event.get("type") or event.get("event_type") or "")
            if event_type in {"packet_created", "packet_resubmitted", "review_requested"}:
                observed.add(event_type)
        if version == 1:
            return {"packet_created", "review_requested"}.issubset(observed)
        if version >= 2:
            # BIZ-176's CCD review emitter consumes packet_resubmitted itself;
            # canonical rework rows do not emit a second review_requested.
            #
            # BIZ-1308 review: this MUST NOT enumerate specific versions. An
            # earlier form matched only {2, 3}, so any higher version fell
            # through to the unconditional `return False` below and could never
            # be corroborated — and because missing evidence RELEASES the row
            # rather than failing it, such a job re-authored and released
            # forever. That is reachable, not theoretical: BIZ-421 ran at
            # packet_version 4 (hermes_jobs, queued 2026-06-12). The rework
            # contract is "not v1", so bound it that way and it stays correct
            # as the version ceiling moves.
            return "packet_resubmitted" in observed
        return False

    async def _fetch_packet_events(self, row: Mapping):
        if self._packet_event_fetcher is not None:
            result = self._packet_event_fetcher(row)
            if asyncio.iscoroutine(result):
                result = await result
            return result
        issue_id = str(row.get("issue_id") or "").strip()
        if not issue_id or self._pool is None:
            return []
        try:
            async with self._pool.acquire() as conn:
                records = await conn.fetch(
                    """SELECT issue_id,
                              issue_key,
                              event_type AS type,
                              packet_version,
                              occurred_at AS created_at
                         FROM linear_packet_events
                        WHERE issue_id = $1
                          AND event_type IN (
                            'packet_created',
                            'packet_resubmitted',
                            'review_requested'
                          )
                        ORDER BY occurred_at ASC""",
                    issue_id,
                )
            return [dict(record) for record in records]
        except Exception as exc:
            logger.warning(
                "HermesJobsWorker (%s) could not query durable packet evidence for %s: %s",
                self.callsign, row.get("issue_key", "?"), exc,
            )
            return []

    async def _wait_for_packet_completion_evidence(self, row: Mapping) -> bool:
        """Boundedly poll durable packet events to absorb propagation lag."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.packet_evidence_poll_seconds
        while True:
            if self._has_packet_completion_evidence(row, await self._fetch_packet_events(row)):
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=min(self.packet_evidence_poll_interval_seconds, remaining),
                )
                return False
            except asyncio.TimeoutError:
                pass

    @staticmethod
    def _resolve_session_agent(adapter, session_key: str):
        """Return the cached ``AIAgent`` for ``session_key``, or ``None``.

        The agent lives in the GatewayRunner's ``_agent_cache`` (an
        OrderedDict mapping session_key → ``(agent, config_sig)``). We reach
        the runner via the bound message handler the runner registered on the
        adapter (``adapter.set_message_handler(self._handle_message)`` →
        ``adapter._message_handler.__self__`` is the runner). Every hop is
        getattr-guarded; ANY missing link returns ``None`` (caller treats a
        ``None`` agent as "can't tell" and fails safe to success, never to a
        false release).

        Read under ``_agent_cache_lock`` and mirror run.py's own tuple-unwrap
        idiom (``_cached[0] if isinstance(_cached, tuple) else ...``).
        """
        handler = getattr(adapter, "_message_handler", None)
        runner = getattr(handler, "__self__", None)
        if runner is None:
            return None
        cache = getattr(runner, "_agent_cache", None)
        if cache is None:
            return None
        lock = getattr(runner, "_agent_cache_lock", None)
        try:
            if lock is not None:
                with lock:
                    cached = cache.get(session_key)
            else:
                cached = cache.get(session_key)
        except Exception:
            return None
        if not cached:
            return None
        if isinstance(cached, tuple):
            return cached[0] if cached else None
        return cached

    @staticmethod
    def _run_made_successful_calls(pre_agent, pre_calls, post_agent, post_calls):
        """Tri-state: did the just-finished run make ≥1 *successful* API call?

        Returns:
          * ``True``  — confident the run made progress (positive delta, or
            counters unreadable in a way that should NOT trigger a release).
          * ``False`` — CONFIDENT zero: the SAME agent object spanned the run
            and its ``session_api_calls`` did not advance, OR a fresh agent
            ended the run at exactly zero. This is the ONLY path that releases
            the row, so it must never fire on ambiguity.
          * ``None``  — can't tell (agent object swapped mid-run, or a counter
            was unreadable). Caller fails safe to success.

        Signal rationale (BIZ-352): ``session_api_calls`` increments ONLY after
        a successful provider response (run_agent.py:11210), so a first-call
        429 leaves it at 0. (We deliberately do NOT use ``_api_call_count`` —
        that is bumped at the loop TOP, *before* the call, so a 429 leaves it
        at 1 and would mask the bug.)
        """
        # Same agent spanned the whole run → trust the delta.
        if (
            pre_agent is not None
            and post_agent is not None
            and pre_agent is post_agent
            and isinstance(pre_calls, int)
            and isinstance(post_calls, int)
        ):
            return (post_calls - pre_calls) > 0

        # Cold start: the agent wasn't cached when we snapshotted (pre_agent
        # is None) but exists afterward. The post agent is fresh for this
        # session, so its counter reflects THIS run in full — a readable 0 is
        # a confident zero. (A mid-run swap to a *different* real agent is NOT
        # handled here: pre_agent being a distinct live object means the
        # pre-swap agent may have made calls we can't see, so we fall through
        # to None and fail safe to success.)
        if pre_agent is None and post_agent is not None and isinstance(post_calls, int):
            return post_calls > 0

        # Anything else — unreadable counters, missing post agent, or a mid-run
        # agent swap — is ambiguous. Fail safe to success (never a false release).
        return None

    async def _writeback_completion(
        self,
        row_id,
        issue_key: str,
        packet_version,
        success: bool,
        error_msg: Optional[str],
    ) -> None:
        try:
            if success:
                async with self._pool.acquire() as conn:
                    await conn.execute(COMPLETE_JOB_SQL, row_id, self.callsign)
                logger.info(
                    "🦸 %s completed %s v%s",
                    self.callsign, issue_key, packet_version,
                )
            else:
                # BIZ-265 fix: detect transient failures and RELEASE the
                # claim instead of marking failed. Releasing returns the
                # row to the unclaimed pool so the next idle Avenger
                # picks it up on the next poll — identical queue
                # semantics to packet-authoring's "all-Avengers-busy
                # auto-routes-to-next-available" behavior. This path
                # specifically protects pr_review and pr_merge dispatch
                # lanes from being permanently lost when an Avenger
                # claims them while its chat session is busy.
                err_lower = (error_msg or "").lower()
                is_transient = any(
                    pattern in err_lower
                    for pattern in TRANSIENT_FAILURE_SUBSTRINGS
                )
                if is_transient:
                    async with self._pool.acquire() as conn:
                        await conn.execute(
                            RELEASE_JOB_SQL,
                            row_id,
                            self.callsign,
                            (error_msg or "transient failure")[:8000],
                        )
                    logger.warning(
                        "HermesJobsWorker (%s) released %s v%s (transient: %s) — row deferred with fleet-wide exponential backoff",
                        self.callsign, issue_key, packet_version, error_msg,
                    )
                else:
                    async with self._pool.acquire() as conn:
                        await conn.execute(
                            FAIL_JOB_SQL,
                            row_id,
                            self.callsign,
                            (error_msg or "unknown failure")[:8000],
                        )
                    logger.error(
                        "HermesJobsWorker (%s) marked %s v%s failed: %s",
                        self.callsign, issue_key, packet_version, error_msg,
                    )
        except Exception as e:
            logger.exception(
                "HermesJobsWorker (%s) failed to write completion for row %s: %s",
                self.callsign, row_id, e,
            )

    async def _run_packet_authoring(self, row: dict) -> tuple[bool, Optional[str]]:
        """Run the packet-authoring pipeline through the gateway's INTERACTIVE
        path so typing indicator, skill-load notifications, and tool-call
        streaming all surface live in the claiming Avenger's Telegram DM.

        We synthesize a ``MessageEvent`` for the bot's home channel and call
        ``adapter._process_message_background`` directly (the same code path
        invoked when Spencer DMs the bot manually). The agent runs in
        non-quiet, non-cron mode — ``send_typing`` fires every ~2s,
        skill-load events surface, and every tool call streams as it happens.

        Reuse of ``cron.scheduler.run_job`` was the v1 design but it sets
        ``quiet_mode=True, platform="cron"`` which suppresses streaming.
        Visibility is non-negotiable per BIZ-208 packet §5 step 4 and the
        operator's standing requirement.
        """
        prompt = (build_runner_job_from_row(row, callsign=self.callsign).get("prompt") or "").strip()
        if not prompt:
            return False, "empty packet_message in payload"

        # Test injection path: when a fake runner is provided, run it
        # synchronously in an executor and skip the live-Telegram path.
        if self._runner is not None:
            loop = self.loop or asyncio.get_running_loop()
            try:
                success, _doc, _final, error_msg = await loop.run_in_executor(
                    None, self._runner, build_runner_job_from_row(row, callsign=self.callsign),
                )
                return bool(success), error_msg
            except Exception as e:
                return False, f"{type(e).__name__}: {e}"

        # Resolve the Telegram adapter + home channel for this profile.
        try:
            from gateway.config import Platform
        except Exception as e:
            return False, f"failed to import Platform enum: {e}"

        adapter = (self.adapters or {}).get(Platform.TELEGRAM)
        home_chat_id = (os.environ.get("TELEGRAM_HOME_CHANNEL") or "").strip()

        if adapter is None:
            return False, "Telegram adapter unavailable for this profile (cannot run with visibility)"
        if not home_chat_id:
            return False, "TELEGRAM_HOME_CHANNEL not set (cannot run with visibility)"
        if not hasattr(adapter, "_process_message_background") or not hasattr(adapter, "_active_sessions"):
            return False, f"Telegram adapter API mismatch ({type(adapter).__name__})"

        # Build the synthetic event the gateway's interactive path expects.
        try:
            from gateway.session import SessionSource, build_session_key
            from gateway.platforms.base import MessageEvent, MessageType
        except Exception as e:
            return False, f"failed to import gateway primitives: {e}"

        row_id_str = str(row.get("id") or "")
        # Use None so Telegram adapter doesn't try to reply_to a non-numeric ID.
        # The UUID was causing int() parse failures in telegram.py line 1101.
        synthetic_message_id = None

        source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id=str(home_chat_id),
            chat_type="dm",
            # In a Telegram DM, the user_id equals the chat_id (Spencer's
            # Telegram numeric ID). Setting it makes the synthetic event
            # pass the TELEGRAM_ALLOWED_USERS auth filter, which is
            # gating the message_handler when user_id is None.
            user_id=str(home_chat_id),
            user_name=f"hermes-jobs/{self.callsign}",
            message_id=synthetic_message_id,
        )
        event = MessageEvent(
            text=prompt,
            message_type=MessageType.TEXT,
            source=source,
            message_id=synthetic_message_id,
        )
        session_key = build_session_key(source)

        # Pre-register the session lock the same way ``handle_message`` does
        # before spawning the background task. ``_process_message_background``
        # reuses this entry and cleans it up on exit.
        if session_key in adapter._active_sessions:
            # Another run is already active in this chat (Spencer manually
            # DM'ing the bot, or a previous run hadn't cleaned up). Bail —
            # the lease will expire and another idle Avenger reclaims, OR
            # the user finishes their interaction first.
            return False, f"chat {home_chat_id} already has an active session — try later"

        interrupt_event = asyncio.Event()
        adapter._active_sessions[session_key] = interrupt_event
        # Modern Hermes gateways release a session guard only when the running
        # task is registered as that session's owner. The queue worker invokes
        # _process_message_background directly rather than through
        # _start_session_processing, so it must perform that ownership handoff
        # itself; without it, every completed packet leaves a permanent lock.
        owner_task = asyncio.current_task()
        session_tasks = getattr(adapter, "_session_tasks", None)
        if owner_task is not None and isinstance(session_tasks, dict):
            session_tasks[session_key] = owner_task

        logger.info(
            "HermesJobsWorker (%s) routing %s through interactive Telegram path (session_key=%s)",
            self.callsign, row.get("issue_key", "?"), session_key,
        )

        session_id, baseline_message_count = self._session_transcript_snapshot(adapter, session_key)
        self._publish_issue_room(
            row,
            status="authoring",
            session_key=session_key,
            session_id=session_id,
            baseline_message_count=baseline_message_count,
        )
        session_id_task = asyncio.create_task(
            self._publish_session_id_when_ready(
                row, adapter, session_key, baseline_message_count,
            ),
            name=f"hermes-issue-room-session-{row_id_str}",
        )

        # BIZ-352: snapshot the session's successful-API-call counter BEFORE
        # the run so we can detect a 429 / auth no-op (zero successful calls)
        # afterward. Best-effort: if the agent isn't cached yet (cold start)
        # both snapshots are None and we fail safe to success.
        pre_agent = self._resolve_session_agent(adapter, session_key)
        pre_calls = getattr(pre_agent, "session_api_calls", None)

        try:
            await adapter._process_message_background(event, session_key)
            # 2026-05-19 fix — _process_message_background returns cleanly even
            # when the gateway's invalidate_generation() invalidated our run
            # mid-flight (new_command from a fresh DM, session_reset, etc.).
            # The pre-fix code returned (True, None) here, which marked the
            # hermes_jobs row completed_at — burning it without producing a
            # packet artifact. Live repro: CQI-45 v1 at 14:02 CDT (Spiderman):
            #   "Invalidated run generation ... (new_command)"
            #   "Invalidated run generation ... (session_reset)"
            #   "Discarding stale agent result ... generation 1 is no longer current"
            #   followed by "Spiderman completed CQI-45 v1" — but no artifact landed.
            #
            # When the gateway's invalidate_generation() fires for a session_key
            # we own, it sets the interrupt_event we registered above. After
            # _process_message_background returns we check that event: if set,
            # signal transient failure so _writeback_completion releases the
            # row (via TRANSIENT_FAILURE_SUBSTRINGS match on "session interrupted")
            # rather than marking it completed_at.
            if interrupt_event.is_set():
                logger.warning(
                    "HermesJobsWorker (%s) %s: session interrupted before agent result reached Telegram — releasing row for retry",
                    self.callsign, row.get("issue_key", "?"),
                )
                return False, "session interrupted: invalidate_generation fired mid-run, agent result was discarded as stale before reaching Telegram"

            # BIZ-352: detect a rate-limit / auth no-op. _process_message_background
            # returns None whether the agent did real work OR bailed on a first-call
            # 429 / auth-dead window, so the pre-fix `return True, None` recorded a
            # fake completed_at with no artifact. Compare the session's successful-
            # API-call counter before/after on the SAME agent object: a confident
            # zero (delta == 0, or a fresh agent that ended at 0) means no model
            # call ever succeeded — release the row for retry instead of completing
            # it. Anything ambiguous (agent swapped, counters unreadable) fails safe
            # to success so we NEVER release a row that actually produced output.
            post_agent = self._resolve_session_agent(adapter, session_key)
            post_calls = getattr(post_agent, "session_api_calls", None)
            made_calls = self._run_made_successful_calls(
                pre_agent, pre_calls, post_agent, post_calls,
            )
            if made_calls is False:
                logger.warning(
                    "HermesJobsWorker (%s) %s: run made zero successful API calls "
                    "(pre=%r post=%r) — rate-limit/auth no-op, releasing row for retry",
                    self.callsign, row.get("issue_key", "?"), pre_calls, post_calls,
                )
                return False, ZERO_API_CALLS_ERROR
            return True, None
        except Exception as e:
            logger.exception(
                "HermesJobsWorker (%s) interactive run failed for row %s: %s",
                self.callsign, row_id_str, e,
            )
            return False, f"{type(e).__name__}: {e}"
        finally:
            final_session_id, _ = self._session_transcript_snapshot(adapter, session_key)
            if final_session_id:
                self._publish_issue_room(
                    row,
                    status="authoring",
                    session_key=session_key,
                    session_id=final_session_id,
                    baseline_message_count=baseline_message_count,
                )
            session_id_task.cancel()
            try:
                await session_id_task
            except (asyncio.CancelledError, Exception):
                pass
            # Compatibility safety net: modern BasePlatformAdapter normally
            # clears these in _process_message_background's owner-aware finally.
            # Clear only entries we still own so a concurrent /new or queued
            # handoff can never be erased by this unwind.
            if adapter._active_sessions.get(session_key) is interrupt_event:
                adapter._active_sessions.pop(session_key, None)
            if isinstance(session_tasks, dict) and session_tasks.get(session_key) is owner_task:
                session_tasks.pop(session_key, None)
