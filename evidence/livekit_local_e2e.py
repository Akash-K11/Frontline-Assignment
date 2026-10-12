"""End-to-end checks against a LOCAL open-source LiveKit server (livekit-server --dev, devkey/secret).

No production credentials, no SIP, no phone calls. Uses the PR's real Room2PipelineService,
LiveKitTransferMedia, TransportFactory and bot.run_bot with real Pipecat LiveKitTransport + livekit-rtc.
Run from Frontline-Assignment/voice-agent:  python ../evidence/livekit_local_e2e.py
"""

import asyncio
import os
import sys
import threading
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.getcwd())
os.environ.setdefault("SUPABASE_URL", "http://localhost.invalid")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "k")
for k in ("CARTESIA_API_KEY", "DEEPGRAM_API_KEY", "OPENAI_API_KEY"):
    os.environ.setdefault(k, "dummy")

from loguru import logger  # noqa: E402

logger.remove()
logger.add(sys.stderr, level="WARNING")

from livekit import api, rtc  # noqa: E402
from pipecat.frames.frames import InputAudioRawFrame  # noqa: E402
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
from pipecat.pipeline.task import PipelineTask  # noqa: E402
from pipecat.processors.frame_processor import FrameProcessor  # noqa: E402

from livekit_transfer_media import LiveKitTransferMedia, PrimaryMediaBinding  # noqa: E402
from room2_pipeline_service import Room2PipelineService  # noqa: E402
from transfer_state import TransferState  # noqa: E402
from transport_factory import TransportFactory  # noqa: E402

URL = "ws://127.0.0.1:7880"


def token(room: str, identity: str) -> str:
    return (
        api.AccessToken("devkey", "secret")
        .with_identity(identity)
        .with_grants(api.VideoGrants(room_join=True, room=room))
        .to_jwt()
    )


async def human(room_name: str, identity: str):
    room = rtc.Room()
    await room.connect(URL, token(room_name, identity))
    source = rtc.AudioSource(16000, 1)
    track = rtc.LocalAudioTrack.create_audio_track("mic", source)
    opts = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    await room.local_participant.publish_track(track, opts)

    async def feed():
        frame = rtc.AudioFrame.create(16000, 1, 320)
        while True:
            await source.capture_frame(frame)
            await asyncio.sleep(0.02)

    feeder = asyncio.create_task(feed())
    return room, feeder


class CountAudio(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.by_user = Counter()

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            self.by_user[getattr(frame, "user_id", "?")] += 1
        await self.push_frame(frame, direction)


def run(task):
    return asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))


async def check_room2_identity():
    print("\n[E-A] Room2 broker recognition with real LiveKit participants")
    state = TransferState(human_participant_identity="broker-abc123")
    svc = Room2PipelineService(state, SimpleNamespace(on_broker_left_consultation=AsyncMock()))
    svc._transport = TransportFactory.create(URL, token("room2", "negotiation-agent-room2"), "room2")
    svc._context = SimpleNamespace(session_id=None)
    svc._task = SimpleNamespace(queue_frames=AsyncMock())
    svc._audiobuffer = SimpleNamespace(start_recording=AsyncMock())
    seen = []

    @svc._transport.event_handler("on_audio_track_subscribed")
    async def _log(transport, participant_id):
        seen.append(participant_id)

    svc._setup_room2_events()
    pipeline_run = run(PipelineTask(Pipeline([svc._transport.input(), svc._transport.output()]), idle_timeout_secs=None))
    await asyncio.sleep(1.5)
    await svc.mark_broker_answered()
    broker_room, feeder = await human("room2", "broker-abc123")
    await asyncio.sleep(4)
    keys = list(svc._transport._client.room.remote_participants.keys())
    print(f"  Pipecat callback participant_id values: {seen}")
    print(f"  livekit-rtc remote_participants keys:   {keys}")
    print(f"  broker SID (real):                       {broker_room.local_participant.sid}")
    print(f"  _broker_connected={svc._broker_connected.is_set()}  _broker_media_ready={svc._broker_media_ready.is_set()}  "
          f"briefing LLMRunFrame queued={svc._task.queue_frames.await_count}")
    feeder.cancel()
    await broker_room.disconnect()
    pipeline_run.cancel()


async def check_hold_audio():
    print("\n[E-B] Room1 input while 'held' by the PR's LiveKitTransferMedia")
    transport = TransportFactory.create(URL, token("room1", "negotiation-agent"), "room1")
    counter = CountAudio()
    task = PipelineTask(Pipeline([transport.input(), counter, transport.output()]), idle_timeout_secs=None)
    pipeline_run = run(task)
    await asyncio.sleep(1.5)
    carrier_room, feeder = await human("room1", "carrier-sip")
    await asyncio.sleep(2)
    context = SimpleNamespace(skip_tts=False, call_mode="BOT_ACTIVE")
    media = LiveKitTransferMedia(
        primary=PrimaryMediaBinding("room1", URL, task, context, transport),
        hold_token=token("room1", "hold-observer-test"),
    )
    await media.gate_primary_media(True)
    await media.start_hold()
    await asyncio.sleep(1)
    counter.by_user.clear()
    await asyncio.sleep(4)
    remote = {p.sid: p.identity for p in transport._client.room.remote_participants.values()}
    named = {remote.get(sid, sid): n for sid, n in counter.by_user.items()}
    print(f"  gated={media._primary_media_gated} call_mode={context.call_mode} hold_active={media.hold_active}")
    print(f"  audio frames reaching the processor after transport.input() in 4s while held: {named}")
    await media.stop_hold(retain_observer=False)
    feeder.cancel()
    await carrier_room.disconnect()
    pipeline_run.cancel()


class Passthrough(FrameProcessor):
    """Stands in for Deepgram/Cartesia/OpenAI services so no provider keys or network are needed."""

    class InputParams:
        def __init__(self, **kwargs):
            pass

    def __init__(self, *args, **kwargs):
        super().__init__()

    def register_function(self, *args, **kwargs):
        pass

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


async def check_runner_never_exits():
    print("\n[E-C] bot.run_bot when the expected caller is not in the room")
    import bot
    from server_utils import AgentRequest

    unknown = SimpleNamespace(status="unknown", reason="test", mc_number=None, carrier_name=None)
    with patch.object(bot, "DeepgramSTTService", Passthrough), patch.object(bot, "CartesiaTTSService", Passthrough), \
         patch.object(bot, "OpenAILLMService", Passthrough), \
         patch.object(bot, "resolve_caller_identity", AsyncMock(return_value=unknown)), \
         patch.object(bot, "start_call", lambda **kw: None):
        req = AgentRequest(livekit_url=URL, room_name="room-empty", token=token("room-empty", "negotiation-agent"),
                           caller_phone="+14155550123", participant_sid="PA_caller_already_gone",
                           participant_identity="sip_+14155550123")
        first = await bot.start_bot(req)
        bot_task = bot._active_bot_task
        await asyncio.sleep(20)
        alive = not bot_task.done()
        if bot_task.done() and bot_task.exception():
            print(f"  bot task exited with: {bot_task.exception()!r}")
        async with api.LiveKitAPI(URL.replace("ws", "http"), "devkey", "secret") as lk:
            listed = await lk.room.list_participants(api.ListParticipantsRequest(room="room-empty"))
        print(f"  server-side participants in room-empty: {[p.identity for p in listed.participants]}")
        try:
            await bot.start_bot(req.model_copy(update={"room_name": "another-call"}))
            second = "accepted"
        except Exception as exc:
            second = f"{type(exc).__name__}: {getattr(exc, 'status_code', '')} {getattr(exc, 'detail', '')}"
        print(f"  first /start -> {first}; after 20s bot task still running: {alive}")
        print(f"  second, unrelated /start -> {second}")


async def main():
    await check_room2_identity()
    await check_hold_audio()
    await check_runner_never_exits()


threading.Timer(80, lambda: os._exit(0)).start()
asyncio.run(main())
os._exit(0)
