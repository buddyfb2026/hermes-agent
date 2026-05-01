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
    ORDER BY priority DESC, queued_at
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
        adapters: Optional[dict] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        poll_interval_seconds: float = 3.0,
        poll_jitter_seconds: float = 2.0,
        lease_renewal_interval_seconds: float = 300.0,
        runner=None,
    ):
        self.callsign = callsign
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
        self._stop_event = asyncio.Event()
        self._pool = None  # asyncpg.Pool
        self._current_row_id = None  # set while processing

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

        renewal_task = asyncio.create_task(
            self._renew_lease_loop(row_id),
            name=f"hermes-jobs-renew-{row_id}",
        )
        self._current_row_id = row_id

        success = False
        error_msg: Optional[str] = None
        try:
            success, error_msg = await self._run_packet_authoring(row)
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
        """Run the packet-authoring pipeline via ``cron.scheduler.run_job``.

        Runs the synchronous ``run_job`` in an executor so the asyncio
        event loop stays responsive (lease renewal, shutdown handler,
        platform adapters).
        """
        runner = self._runner
        if runner is None:
            from cron.scheduler import run_job as runner  # type: ignore[no-redef]

        job_dict = build_runner_job_from_row(row, callsign=self.callsign)

        # Don't even hand the runner an empty prompt — central-api should
        # never enqueue one, but if it ever does we'd just spin the agent
        # for nothing.
        if not (job_dict.get("prompt") or "").strip():
            return False, "empty packet_message in payload"

        loop = self.loop or asyncio.get_running_loop()
        try:
            success, _output_doc, final_response, error_msg = await loop.run_in_executor(
                None, runner, job_dict,
            )
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

        # Post-run delivery (mirrors cron.scheduler.tick._process_job):
        # the agent's final response is delivered to the configured target
        # if any was resolved. The Linear-side packet_created /
        # review_requested events are emitted by the agent's tool calls
        # during the run, independent of this delivery.
        if success and final_response and job_dict.get("deliver", "local") != "local":
            try:
                from cron.scheduler import _deliver_result, SILENT_MARKER
                if SILENT_MARKER not in (final_response or "").strip().upper():
                    delivery_err = _deliver_result(
                        job_dict, final_response,
                        adapters=self.adapters, loop=self.loop,
                    )
                    if delivery_err:
                        logger.warning(
                            "HermesJobsWorker (%s) delivery error for %s: %s",
                            self.callsign, job_dict.get("name", "?"), delivery_err,
                        )
            except Exception as e:
                # Delivery failure must not flip the row to failed — the
                # packet itself was authored successfully (Linear events
                # already posted by the agent's tool calls). Log and
                # continue.
                logger.warning(
                    "HermesJobsWorker (%s) delivery raised for %s: %s",
                    self.callsign, job_dict.get("name", "?"), e,
                )

        return bool(success), error_msg
