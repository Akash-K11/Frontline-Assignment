"""X2 check (local only): Room2 broker readiness with livekit-rtc's real identity-keyed participant map.

livekit-rtc 1.0.25 room.py:988 stores `_remote_participants[participant.identity]`, while
Pipecat 0.0.95 passes `participant.sid` to on_participant_connected / on_audio_track_subscribed
and its own get_participant_metadata looks up `remote_participants.get(sid)` (returns {}).
Copy into voice-agent/tests/ to run.
"""

import pytest

from tests.test_room2_pipeline_service import FakeTransport, build_service


class IdentityKeyedTransport(FakeTransport):
    def __init__(self, identity="broker-expected", sid="PA_broker"):
        super().__init__(identity)
        participant = self._client.room.remote_participants.pop("PA_broker")
        participant.sid = sid
        self._client.room.remote_participants[identity] = participant


@pytest.mark.asyncio
async def test_expected_broker_is_recognised_with_identity_keyed_map():
    service, state, _ = build_service()
    service._transport = IdentityKeyedTransport()
    service._setup_room2_events()

    await service.mark_broker_answered()
    await service._transport.handlers["on_participant_connected"](service._transport, "PA_broker")
    await service._transport.handlers["on_audio_track_subscribed"](service._transport, "PA_broker")

    print("\nconnected:", service._broker_connected.is_set(),
          "| media_ready:", service._broker_media_ready.is_set(),
          "| briefing started:", service._task.queue_frames.await_count)
    assert service._broker_media_ready.is_set(), "real broker rejected: briefing never starts"
