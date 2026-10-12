"""Review reproductions for PR #1 (local only, not part of the PR)."""

import asyncio

import pytest

from tests.test_livekit_transfer_orchestrator import (
    FakeMedia,
    FakeRoom2,
    FakeTelephony,
    build_transfer,
)
from transfer_state import TransferPhase


class BlockingHoldMedia(FakeMedia):
    release: asyncio.Event

    async def start_hold(self):
        self.calls.append("start_hold")
        await self.release.wait()


@pytest.mark.asyncio
async def test_carrier_hangup_during_hold_start_must_not_dial_broker():
    telephony = FakeTelephony()
    transfer, _, media_instances, room2_instances = build_transfer(telephony=telephony)
    release = asyncio.Event()

    def media_factory(**kwargs):
        media = BlockingHoldMedia(**kwargs)
        media.release = release
        media_instances.append(media)
        return media

    transfer._media_factory = media_factory
    attempt = asyncio.create_task(
        transfer.execute_transfer_to_room2(None, None, None, None, None, None, None, None, None)
    )
    while not media_instances or "start_hold" not in media_instances[0].calls:
        await asyncio.sleep(0)

    await transfer.on_carrier_left_primary()
    assert transfer.state.phase is TransferPhase.CANCELLED

    release.set()
    await asyncio.wait_for(attempt, timeout=1)

    dials = [c for c in telephony.calls if c[0] == "dial"]
    print("\nphase after attempt:", transfer.state.phase, "| dials:", len(dials),
          "| room2 pipelines started:", sum("start" in r.calls for r in room2_instances),
          "| room2 stopped:", [r.calls[-1] for r in room2_instances])
    assert not dials, "broker was dialed after the carrier hung up"
    assert transfer.state.phase is TransferPhase.CANCELLED, "terminal phase was overwritten"


@pytest.mark.asyncio
async def test_failure_restores_caller_only_after_room2_teardown():
    """Ordering: the caller stays on hold until Room2 stop + room delete finish."""
    order = []

    class SlowRoom2(FakeRoom2):
        async def stop(self):
            order.append("room2_stop_begin")
            await asyncio.sleep(0.05)
            order.append("room2_stop_end")

    class OrderedMedia(FakeMedia):
        async def restore_primary(self):
            order.append("restore_primary")

    telephony = FakeTelephony(dial_error=RuntimeError("busy"))
    transfer, _, _, _ = build_transfer(telephony=telephony)
    transfer._room2_factory = SlowRoom2
    transfer._media_factory = lambda **kw: OrderedMedia(**kw)

    await transfer.execute_transfer_to_room2(None, None, None, None, None, None, None, None, None)
    print("\norder:", order)
    assert order.index("restore_primary") < order.index("room2_stop_end")
