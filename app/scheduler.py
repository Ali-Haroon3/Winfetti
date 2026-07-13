"""In-process background scheduler.

The API process runs its own maintenance jobs (cashback maturation, stuck
redemption retries, token purges), so `docker compose up` — or a single
deployed machine — is fully self-operating with no external cron. Every job
is safe under concurrent runs (SKIP LOCKED claims + ledger idempotency +
fulfillment external_id dedupe), so running N replicas just means each due
row is handled by whichever process gets there first.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable

from app import jobs
from app.config import get_settings
from app.fulfillment import FulfillmentClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PeriodicJob:
    name: str
    every_seconds: float
    run: Callable[[], object]  # sync; executed in a worker thread


class Scheduler:
    """Runs each job every `every_seconds`, starting one initial delay after
    start(). A job that raises is logged (Sentry's logging integration turns
    that into an event) and retried on its next tick; one failing job never
    affects another.
    """

    def __init__(self, periodic: list[PeriodicJob], initial_delay_seconds: float = 10.0):
        self._jobs = periodic
        self._initial_delay = initial_delay_seconds
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        for job in self._jobs:
            self._tasks.append(
                asyncio.create_task(self._loop(job), name=f"job:{job.name}")
            )
        logger.info(
            "scheduler started: %s",
            ", ".join(f"{j.name} every {j.every_seconds:g}s" for j in self._jobs),
        )

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _loop(self, job: PeriodicJob) -> None:
        await asyncio.sleep(self._initial_delay)
        while True:
            try:
                await asyncio.to_thread(job.run)
            except Exception:
                logger.exception("scheduled job %s failed; retrying next tick", job.name)
            await asyncio.sleep(job.every_seconds)


def build_scheduler(fulfillment: FulfillmentClient) -> Scheduler | None:
    """Assemble the scheduler from settings; None when disabled or when every
    job's interval is 0 (each job can be switched off individually)."""
    settings = get_settings()
    if not settings.scheduler_enabled:
        return None
    spec: list[tuple[str, float, Callable[[], object]]] = [
        (
            "mature-cashback",
            settings.scheduler_mature_cashback_every_seconds,
            jobs.mature_cashback,
        ),
        (
            "retry-approved",
            settings.scheduler_retry_redemptions_every_seconds,
            lambda: jobs.retry_stuck_redemptions(fulfillment),
        ),
        (
            "purge-email-tokens",
            settings.scheduler_purge_email_tokens_every_seconds,
            jobs.purge_expired_email_verifications,
        ),
    ]
    periodic = [
        PeriodicJob(name, every, run) for name, every, run in spec if every > 0
    ]
    if not periodic:
        return None
    return Scheduler(periodic, initial_delay_seconds=settings.scheduler_initial_delay_seconds)
