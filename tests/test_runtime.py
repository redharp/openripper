import asyncio

import pytest

from openripper.runtime import RipSlots, TransferMeter


@pytest.mark.asyncio
async def test_capacity_increase_wakes_waiters_and_decrease_does_not_cancel():
    slots = RipSlots(1)
    await slots.__aenter__()
    waiting = asyncio.create_task(slots.__aenter__())
    await asyncio.sleep(0)
    assert not waiting.done()
    await slots.set_limit(2)
    await asyncio.wait_for(waiting, 1)
    assert slots.active == 2
    await slots.set_limit(1)
    third = asyncio.create_task(slots.__aenter__())
    await slots.__aexit__()
    await asyncio.sleep(0)
    assert not third.done()
    await slots.__aexit__()
    await asyncio.wait_for(third, 1)
    assert slots.active == 1
    await slots.__aexit__()


def test_transfer_measurements_handle_warmup_stalls_and_completion():
    meter = TransferMeter(0)
    assert meter.sample(2, 200, 1000)["eta_seconds"] is None
    measured = meter.sample(10, 500, 1000)
    assert measured["bytes_per_second"] == 50
    assert measured["eta_seconds"] == 10
    meter.sample(50, 500, 1000)
    stalled = meter.sample(80, 500, 1000)
    assert stalled["eta_seconds"] is None
    assert stalled["seconds_since_write"] == 70
