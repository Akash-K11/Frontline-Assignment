"""Additional local-only checks against the PR's own fakes (copy into voice-agent/tests/ to run)."""

import pytest

from server_utils import OutboundCallRequest
from telephony_config import normalize_e164
from tests.test_livekit_transfer_orchestrator import FakeMedia, FakeTelephony, build_transfer, start_consultation


@pytest.mark.asyncio
async def test_post_arrival_media_failure_still_cleans_room2():
    """tieincred #5: _recover_after_arrived_broker never calls _cleanup_room2()."""

    class FailingDetachMedia(FakeMedia):
        async def detach_primary_publisher(self):
            raise RuntimeError("unpublish failed")

    telephony = FakeTelephony()
    transfer, _, media, room2 = build_transfer(telephony=telephony)
    transfer._media_factory = lambda **kw: media.append(FailingDetachMedia(**kw)) or media[-1]
    consultation = await start_consultation(transfer)
    await transfer.execute_transfer_human_to_room1(None)
    await consultation

    room2_deleted = ("delete", "briefing-room") in telephony.calls
    print("\nphase:", transfer.state.phase, "| room2 stopped:", "stop" in room2[0].calls,
          "| room2 deleted:", room2_deleted, "| cleanup_pending:", transfer.state.audit_snapshot()["cleanup_pending"])
    assert room2_deleted and "stop" in room2[0].calls


def test_whitespace_phone_passes_validation_then_normalizes_to_none():
    """Other-agent claim: min_length=2 accepts whitespace; normalize_e164 returns None."""
    req = OutboundCallRequest(to_phone="  ", from_phone="  ")
    print("\nvalidated:", repr(req.to_phone), "-> normalize_e164:", repr(normalize_e164(req.to_phone)))
    assert normalize_e164(req.to_phone) is not None
