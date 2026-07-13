"""The in-process scheduler: ticking, error isolation, and config gating."""

import asyncio

from app.scheduler import PeriodicJob, Scheduler, build_scheduler


def _drive(scheduler, until, timeout=5.0):
    """Run the scheduler on a fresh event loop until `until()` or timeout."""

    async def main():
        scheduler.start()
        deadline = asyncio.get_running_loop().time() + timeout
        while not until() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.005)
        await scheduler.stop()

    asyncio.run(main())


def test_jobs_tick_and_an_exception_does_not_kill_the_loop():
    calls = {"flaky": 0, "steady": 0}

    def flaky():
        calls["flaky"] += 1
        if calls["flaky"] == 1:
            raise RuntimeError("boom")

    def steady():
        calls["steady"] += 1

    scheduler = Scheduler(
        [PeriodicJob("flaky", 0.01, flaky), PeriodicJob("steady", 0.01, steady)],
        initial_delay_seconds=0,
    )
    _drive(scheduler, until=lambda: calls["flaky"] >= 3 and calls["steady"] >= 3)

    # the first flaky run raised, yet both jobs kept ticking
    assert calls["flaky"] >= 3
    assert calls["steady"] >= 3


def test_stop_cancels_before_first_run_with_long_delay():
    calls = []
    scheduler = Scheduler(
        [PeriodicJob("later", 0.01, lambda: calls.append(1))],
        initial_delay_seconds=60,
    )

    async def main():
        scheduler.start()
        await asyncio.sleep(0.05)
        await scheduler.stop()

    asyncio.run(main())
    assert calls == []


def test_build_scheduler_respects_master_switch(settings, stub_fulfillment):
    settings.scheduler_enabled = False
    assert build_scheduler(stub_fulfillment) is None


def test_build_scheduler_drops_jobs_with_zero_interval(settings, stub_fulfillment):
    settings.scheduler_enabled = True
    settings.scheduler_mature_cashback_every_seconds = 0
    settings.scheduler_retry_redemptions_every_seconds = 300
    settings.scheduler_purge_email_tokens_every_seconds = 0

    scheduler = build_scheduler(stub_fulfillment)
    assert [j.name for j in scheduler._jobs] == ["retry-approved"]

    settings.scheduler_retry_redemptions_every_seconds = 0
    assert build_scheduler(stub_fulfillment) is None
