"""D2 check: what pause_processing_frames() on an input transport actually blocks (Pipecat 0.0.95).

Measures, in a real PipelineTask:
  1. caller audio (InputAudioRawFrame, a SystemFrame) pushed while paused
  2. whether held audio is replayed after resume (backlog claim)
  3. a control frame queued into the task while paused (how end_call's EndFrame travels)
"""

import asyncio
import os
import threading

from loguru import logger

logger.remove()

from pipecat.frames.frames import InputAudioRawFrame, StartFrame, TextFrame  # noqa: E402
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
from pipecat.pipeline.task import PipelineParams, PipelineTask  # noqa: E402
from pipecat.processors.frame_processor import FrameProcessor  # noqa: E402
from pipecat.transports.base_input import BaseInputTransport  # noqa: E402
from pipecat.transports.base_transport import TransportParams  # noqa: E402


class Input(BaseInputTransport):
    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self.set_transport_ready(frame)


class Sink(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.audio = 0
        self.text = 0

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            self.audio += 1
        elif isinstance(frame, TextFrame):
            self.text += 1
        await self.push_frame(frame, direction)


async def main():
    inp, sink = Input(TransportParams(audio_in_enabled=True)), Sink()
    task = PipelineTask(
        Pipeline([inp, sink]), params=PipelineParams(audio_in_sample_rate=8000), idle_timeout_secs=None
    )
    asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))
    await asyncio.sleep(0.3)

    async def push_audio(n=5):
        for _ in range(n):
            await inp.push_audio_frame(
                InputAudioRawFrame(audio=b"\x00\x00" * 160, sample_rate=8000, num_channels=1)
            )
        await asyncio.sleep(0.3)

    a0 = sink.audio
    await push_audio()
    print(f"1a unpaused: {sink.audio - a0}/5 audio frames reached the next processor", flush=True)

    await inp.pause_processing_frames()
    a1 = sink.audio
    await push_audio()
    print(f"1b paused:   {sink.audio - a1}/5 audio frames reached the next processor", flush=True)

    t0 = sink.text
    await task.queue_frames([TextFrame("control-path probe")])
    await asyncio.sleep(0.3)
    print(f"3a paused:   {sink.text - t0}/1 frame queued into the task reached the next processor", flush=True)

    a2 = sink.audio
    await inp.resume_processing_frames()
    await asyncio.sleep(0.3)
    print(f"2  resume:   {sink.audio - a2} held audio frames replayed after resume", flush=True)
    print(f"3b resume:   {sink.text - t0}/1 queued frame delivered after resume", flush=True)


threading.Timer(8, lambda: os._exit(0)).start()
asyncio.run(main())
os._exit(0)
