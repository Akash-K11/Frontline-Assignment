# Review of #1: COR-4078 Migrate voice calls and warm transfers to LiveKit/Telnyx

Head reviewed: `f93f7a5` (base `5a98a66`)

## Summary

**Request changes.**

The structure of the migration is good. Provider calls sit behind `LiveKitTelephonyService`, webhook signatures are verified before the payload is used, the transfer has an explicit state machine, and the move into Room1 is reconciled rather than assumed. I'd keep that shape.

It can't merge yet, though, because the two main flows don't work against the real SDK objects:

- **Inbound calls never start.** Every real SIP `participant_joined` webhook is rejected with 422 (#1).
- **Warm transfers never brief the broker.** Room2 rejects the broker it just dialled, so the attempt times out after the broker has answered (#2).

Both are hidden by test fixtures that don't match what the SDKs return.

Behind those, the transfer lifecycle has paths that strand or mislead people on a live call:
- a carrier hangup during setup still dials the broker (#3);
- the "hold" doesn't stop the bot listening and acting (#4);
- a bot that never sees its caller blocks the runner indefinitely (#5);
- a failed transfer leaves the carrier on hold until cleanup, including a blocking upload, has finished (#6).

`/outbound-call` can also load one tenant's context while dialling from another number (#7).

**Must fix before merge:** #1–#7.
**Should fix before production:** #8–#13.
**Question:** #14 could add one more.

### What changes in the call flow

**Before (Daily):**
1. The webhook takes a pooled room and starts the bot through the Pipecat runner.
2. At transfer, a separate hold-music bot joins the carrier's room, and the main bot's Room1 pipeline is cancelled, so the bot leaves.
3. Room2 dials the broker, and the broker is SIP-REFERed into Room1.

**After (LiveKit/Telnyx):**
1. A signed LiveKit `participant_joined` webhook starts a single-call bot runner.
2. At transfer, the Room1 pipeline stays alive with its input "paused" and TTS skipped, while a hold publisher joins Room1.
3. Room2 dials the broker with `wait_until_answered`, briefs them, and moves them into Room1 with `MoveParticipant`.
4. Room1 then stays up, with an observer, until either human hangs up.

Most of the lifecycle findings come from that last change: Room1 is no longer torn down at transfer, so its pipeline, recording and tools stay live through the transfer.

### What I ran

- **Test suites:** voice (127 passed, 17 subtests) and shared (91 passed) at this head, Python 3.11 with the pinned dependencies.
- **Unit-level reproductions:** #3, #6, #9 and the whitespace case in #7, using the existing orchestrator and Room2 fakes plus small changes.
- **Against a real local LiveKit server** (`livekit-server --dev`; no SIP, no phone calls): #2, #4 and #5, with the real Pipecat `LiveKitTransport` and `livekit-rtc` participants. For #5, the speech-to-text, text-to-speech and LLM services were replaced with pass-through stubs.
- **#1:** a webhook signed and decoded with the real `livekit-api` `WebhookReceiver`, then passed through `LiveKitWebhookService.accept`.
- **Not exercised:** anything needing Telnyx, LiveKit Cloud (`MoveParticipant`), speech providers or Supabase. Those points are phrased as risks or questions below.

### Tests

#1 and #2 slipped through because the fixtures model the SDKs differently from reality:
- `kind="SIP"` as a string, where the SDK gives the integer enum `3`;
- `remote_participants` keyed by SID, where `livekit-rtc` keys it by identity.

It would be worth adding a few contract tests that build the real `livekit.protocol` and `livekit.rtc` types, and that use the Pipecat attributes the media code depends on (`_client._audio_track`, `room.local_participant`). That would keep these from regressing on a dependency bump.

### Minor (no reply needed)

- `orchestrator.py:532`: `_retry_cleanup` retries after `sleep(0)`, so both attempts happen back to back. A short backoff would make the second attempt useful.
- `requirements.txt` still pulls in `pipecatcloud` with a "production deployment" comment, though nothing uses it now.
- `voice-agent/ruff.toml` has `fix = true`, so `ruff check` rewrites files. With `--no-fix`, the changed files report 19 findings, mostly existing ones.

---

## Inline comments

Each comment is marked with a priority (P0–P3) and with one of:
- **verified defect:** reproduced, or traced through this code and the pinned library source;
- **risk:** reasoned from code and docs, not observed;
- **question.**

### 1. `voice-agent/livekit_ingress.py:201` · P0 · Verified defect · Every inbound SIP call is rejected

`WebhookReceiver.receive()` returns a protobuf, so `participant.kind` arrives as the integer `3`. That integer never matches the string branch above.

The fallback then has two problems:
- `livekit.protocol.models_pb2` doesn't exist in the pinned `livekit-protocol` (the module is `livekit.protocol.models`), so the import fails and the function returns `False`.
- Even with the import fixed, `Kind.Name(3)` is `"SIP"`, which doesn't satisfy `endswith("_SIP")`.

The result is that `parse_verified_sip_event` returns `422 NOT_SIP_PARTICIPANT` for every real inbound call, and no bot is started.

```python
from livekit.protocol import models
_participant_is_sip(models.ParticipantInfo(kind=models.ParticipantInfo.Kind.SIP))  # -> False
```

The same thing happens end to end: a correctly signed `participant_joined` event through `LiveKitWebhookService.accept()` returns `REJECTED NOT_SIP_PARTICIPANT 422`.

Suggest comparing directly against `models.ParticipantInfo.Kind.SIP`, and changing the ingress tests to build a real `WebhookEvent` instead of the string fixture.

### 2. `voice-agent/room2_pipeline_service.py:254` · P0 · Verified defect · Room2 never recognises the broker it dialled

Pipecat 0.0.95 passes `participant.sid` to `on_participant_connected` and `on_audio_track_subscribed`. But `livekit-rtc` stores `room.remote_participants` keyed by **identity** (`rtc/room.py:988`). So both this lookup and Pipecat's own `get_participant_metadata()` find nothing, and `identity` stays `None`.

The `expected_identity` check on L266 then rejects the real broker. Neither readiness event fires, and `wait_for_broker()` times out. Every transfer fails after the broker has answered and heard silence.

I checked this against a local LiveKit server with a participant joining as `broker-abc123`:
- the callback received `PA_vDLCWFUgtf7M`;
- `remote_participants` keys were `['broker-abc123']`;
- `_broker_connected` and `_broker_media_ready` both stayed `False`, and no `LLMRunFrame` was queued.

Suggest resolving the participant with `next(p for p in room.remote_participants.values() if p.sid == participant_id)`, then comparing `p.identity`. The test should use an identity-keyed map.

`bot.py:484-495` has the same lookup. It's harmless today only because `_matches_caller` checks the SID first, but it does overwrite `carrier_participant_identity` with `None` at L530.

### 3. `voice-agent/orchestrator.py:216` · P1 · Verified defect · A carrier hangup during setup doesn't stop the transfer

`execute_transfer_to_room2` awaits several steps without re-checking that the attempt is still live:
- `create_room` (L171)
- `start_hold` (L198), up to 10 s
- `room2_pipeline.start` (L204)
- `_notify_best_effort` (L215)
- `dial_phone` (L220), up to 45 s

If the carrier hangs up during any of these, `on_carrier_left_primary()` calls `_terminal_failure()`, which marks the attempt `CANCELLED`, deletes Room2 and sets `_transfer_complete_event`. This coroutine then carries on:
- it starts a new Room2 pipeline;
- it dials the broker;
- it sets the phase back to `CONSULTING` (L243);
- it returns at once, because the completion event is already set.

Room2 and the broker leg are left with no owner. Meanwhile `bot.py` saves the call and frees the runner. `TransferState.transition()` (`transfer_state.py:90`) accepts any phase, so the terminal state is silently overwritten, and the stored audit says `consulting`.

**Repro:** with the existing fakes, make `FakeMedia.start_hold` wait on an event, call `on_carrier_left_primary()`, then release it. You get `phase=consulting`, one `dial`, and `FakeRoom2.stop` is never called.

**Suggested fix:**
- Keep the attempt as a task that `_terminal_failure` cancels. Alternatively, compare `attempt_id` after each await and dispose of anything acquired late.
- Make `transition()` refuse to leave a terminal phase.
- Cancel the attempt when `run_bot` exits.

**Tests:** a hangup in each phase before `CONSULTING`.

### 4. `voice-agent/livekit_transfer_media.py:64` · P1 · Verified defect · "Hold" doesn't stop Room1 from hearing or acting

`pause_processing_frames()` only pauses the processor's queue of non-system frames. Caller audio is `InputAudioRawFrame`, which is a `SystemFrame`. `BaseInputTransport._audio_task_handler` pushes it downstream with `push_frame()`, outside the paused queue. The LiveKit input also takes audio from every subscribed participant, which now includes the hold-music publisher and, after the move, the broker.

On a local LiveKit server, with `gate_primary_media(True)` and `start_hold()` active, 55 carrier frames and 61 hold-music frames reached the processor after `transport.input()` in 4 s. That's the position where STT sits.

Room1 is now also kept alive through the hold and after the handoff, whereas the base cancelled it at transfer. So STT and the LLM keep running on that audio, and `skip_tts` only mutes the output.

**Consequences:**
- **Tool calls aren't gated.** `record_agreement` can write whatever the humans are discussing. This follows from the pipeline wiring; I haven't observed it with a live LLM.
- **`end_call` still fires, just late.** Its guard (`call_helpers.py:792`) checks `call_mode == "HUMAN_ONLY"`, but this code sets `"TRANSFER_HOLD"`. An `end_call` made during hold doesn't take effect straight away: its `EndFrame` is a non-system frame, so it waits in the paused queue. It's released on `resume_processing_frames()`, which hangs up on the carrier just after they're told "you are back with me".
- **The human-to-human conversation is transcribed and recorded** by Room1 after the handoff.

**Suggested fix:**
- Add a gate processor right after `transport.input()` that drops audio, VAD and transcription frames while held, and ignores the hold-observer identity.
- Refuse tool calls unless `call_mode == "BOT_ACTIVE"`.
- Cancel the Room1 pipeline (and stop its recording) once the broker is confirmed in Room1, keeping only the observer.

### 5. `voice-agent/bot.py:365` · P1 · Verified defect · A bot that never sees its caller never exits, and the runner rejects every later call

With `idle_timeout_secs=None`, the task only ends through `end_call` or the matched caller's `on_participant_disconnected` (L577). There's no `on_disconnected` handler and no deadline for the caller to join. So a caller who hangs up before the bot connects leaves `runner.run()` waiting forever. The same goes for a SID mismatch or the bot losing its own connection. Because of the single-task guard at L693, this runner then answers every new call with 409.

On a local LiveKit server (with speech and LLM services stubbed):
- the bot was in an empty room and still running after 20 s;
- an unrelated second `/start` returned `409 This bot runner is already handling a call`.

**Suggested fix:**
- Add a join deadline after which the task ends.
- Add an `on_disconnected` handler.
- Keep an idle timeout outside the transfer phases rather than turning it off globally.

### 6. `voice-agent/orchestrator.py:486` · P1 · Verified defect · A failed transfer keeps the carrier on hold until Room2 teardown finishes

`_terminal_failure` runs `_cleanup_room2()` before `restore_primary()` (L488). Cleanup awaits `pipeline.stop()`, which calls `stop_recording()`. That fires `on_audio_data` (`room2_pipeline_service.py:221`), which uploads through the synchronous supabase-py `Client`, blocking the event loop for every pipeline in the process. Then the room is deleted, with retries.

None of these steps has a timeout, and all of them run under `_cleanup_lock`. A slow storage call keeps the carrier on hold music, and blocks every other terminal path.

**Repro:** with the existing fakes and a `Room2.stop` that takes 50 ms, `restore_primary()` runs only after `stop()` has returned.

**Suggested fix:**
- Restore the caller and play the announcement first, then tear down Room2.
- Wrap each cleanup step in `asyncio.wait_for`.
- Move the upload off the event loop (`asyncio.to_thread`) and out of the lock.

### 7. `voice-agent/server.py:175` · P1 · Verified defect · `/outbound-call` can run under one tenant while dialling from another number

`from_phone` becomes `bot_phone`, which picks the organisation, the greeting, the carrier-lookup scope and the call-record attribution. But the SIP request always sets `sip_number=self._config.livekit_outbound_number` (`livekit_telephony_service.py:112`). An API-key holder can therefore place a call from the configured number while loading another organisation's context and recording the call under it.

Related, in the same handler: `OutboundCallRequest` accepts `"  "` for both numbers (`min_length=2`), and `normalize_e164("  ")` returns `None` (L157-158). A required number can therefore reach `dial_phone` and `AgentRequest` as `None`.

**Suggested fix:**
- If only one outbound number is supported, drop `from_phone` (or require it to equal the configured number) and derive the tenant from config.
- Otherwise, map allowed numbers to tenants, and pass the same number to LiveKit.
- Reject `None` after normalising.
- Add tests for a mismatched `from_phone` and for whitespace-only input.

### 8. `voice-agent/server.py:123` · P2 · Verified defect · Webhook admission: racy dedupe, and no cleanup when the bot can't start

The event ID is checked at L123 but only recorded at L141, after `await start_agent()`. Two overlapping deliveries both pass the check. With a single runner, the second one gets a 409 (returned to LiveKit as 503) rather than a second bot. With more than one runner it becomes a double start. The set is also per-process, unbounded and lost on restart.

When `start_agent()` fails (runner busy or unavailable), nothing removes the SIP participant or deletes the room. LiveKit retries a few times and then drops the webhook. Its docs say delivery has "no guarantees", and that an inbound SIP participant stays `ringing` until it subscribes to remote audio, so the caller most likely rings out unanswered. The outbound path already deletes its room on failure; the inbound path should do the same.

Also, the outbound legs (carrier, broker) and the broker after its move into Room1 produce `participant_joined` events that come back as 403/422. That makes LiveKit retry. They should get a 2xx "ignored".

**Suggested fix:**
- Reserve the ID before starting, in a TTL store (shared if ingress is replicated).
- Decide what a retry should do after a failed start.
- Clean up the inbound room on failure.
- Add a concurrent-delivery test.

### 9. `voice-agent/orchestrator.py:381` · P2 · Verified defect · A media error after the broker arrives evicts them and leaks Room2

If `stop_hold()` or `detach_primary_publisher()` raises after the move succeeded, `_recover_after_arrived_broker()` removes the broker from a live conversation with the carrier. It then returns without calling `_cleanup_room2()`.

**Repro:** with a `detach_primary_publisher` that raises, Room2 is never stopped or deleted, and the audit shows `cleanup_pending=True`.

A cosmetic media failure shouldn't end a connected call. Suggest retrying the unpublish, or falling back to `end_primary_pipeline()`, before evicting anyone. In every branch, run `_cleanup_room2()`. That includes `_cleanup_after_carrier_departure`, which skips it too.

### 10. `voice-agent/orchestrator.py:215` · P2 · Verified defect · Time on hold isn't bounded

The carrier is already on hold when the Slack notification is awaited. `TransferNotifier` uses an `aiohttp.ClientSession()` with the default 300 s total timeout, and the broker deadline only starts after this returns. After that, the timeouts add up: 45 s to answer, 180 s of consultation, then 10 s for arrival, plus the teardown in #6.

Separately, primary media is gated (L197) before the hold publisher connects (L198, up to 10 s), so the carrier may hear silence first.

**Suggested fix:**
- Fire the notification in the background with a short timeout.
- Start hold music before gating.
- Add a single hold budget that triggers the fallback, ideally with a short "still connecting you" message.

### 11. `voice-agent/livekit_telephony_service.py:117` · P2 · Verified defect · The SIP request needs per-call options and server-side limits

`CreateSIPParticipantRequest` hard-codes `wait_until_answered=True` and sets neither `ringing_timeout` (max 80 s) nor `max_call_duration`.

**Effects:**
- `/outbound-call` holds the HTTP request open until the callee answers or rejects, and it's documented as returning 202.
- The bot process is only started after the answer, so the callee hears silence while it boots.
- `asyncio.wait_for(..., 45)` in the orchestrator cancels the client request but not the dial.
- Neither leg has a server-side cap if cleanup fails.

**Suggested fix:**
- Make "wait for answer" a `dial_phone()` argument: keep it for the broker, and drop it for the async outbound endpoint (or document a bounded synchronous contract).
- Set `ringing_timeout` below the application deadline, and set a `max_call_duration`.

### 12. `voice-agent/room2_pipeline_service.py:320` · P2 · Risk · Once #2 is fixed, voicemail will get the internal-rate briefing

LiveKit documents that `wait_until_answered` returns "including if it goes to voicemail" (200 OK). The briefing starts as soon as the call is answered and audio is subscribed. The Room2 prompt (`context_builder.py`) tells the bot to share target and ceiling rates with "the broker". After that, the move into Room1 happens whenever the LLM calls `transfer_human_to_carrier`.

#2 hides this today. After that fix, a broker's voicemail, or whoever picks up that line, would hear internal pricing, and the carrier could be bridged into a voicemail box.

The PR defers voicemail detection, which is fine. Even so, please gate the opening briefing on at least one utterance from the broker, and gate the handoff on an explicit accept (DTMF or a spoken yes).

### 13. `web/supabase/migrations/20260907000000_add_livekit_call_attribution.sql:15` · P2 · Verified defect · Call attribution and rollout

- **`provider_call_id` means different things by direction.** For inbound it's `sip.callIDFull` (`livekit_ingress.py:183`), which LiveKit documents as the trunk provider's SIP Call-ID rather than a Telnyx Call Control ID. For outbound it's LiveKit's `sip_call_id` (`livekit_telephony_service.py:123`). Please pick one meaning, and rename `telnyx_call_id` to match.
- **The index isn't unique,** so it can't guard against duplicate call rows from duplicate webhooks (#8).
- **The migration must run first.** The voice service writes these columns and the dashboard selects them. If either deploys before the migration, inserts fail silently (`start_call` returns `None`) or dashboard queries break. Please note that in the PR, along with a rollback plan, since the Daily code is removed here.

### 14. `voice-agent/bot.py:580` · Question · Is the broker's SID the same after `MoveParticipant`?

Departure detection after the handoff matches on `human_participant_id`, which is the SID from Room2. The LiveKit docs describe a move as removing the participant from the source room and adding them to the destination, and they don't say whether the SID survives. If it changes, a broker hangup in Room1 is ignored and the carrier sits alone until they hang up.

I couldn't check this locally, because `MoveParticipant` is Cloud-only. If it isn't guaranteed, matching on identity (already tracked) would avoid the question.

---

## Assumptions and open questions

**Assumptions:**
- **LiveKit Cloud is the target.** `MoveParticipant` is Cloud-only, and `.env.example` points at `*.livekit.cloud`.
- **One call per BotRunner process,** as the PR states and `LOCAL_BOT_URL` implies.
- **Voicemail detection is out of scope,** as the PR states. #12 asks only for minimal gating.

**Questions:**
1. **How are concurrent calls handled in production?** Is there one runner per call, with routing? This decides whether #8 is P1 (multiple runners double-start bots) or stays P2. The runner hang in #5 is a blocker either way.
2. **Is the SID preserved across `MoveParticipant`?** (#14)
3. **What's the rollout and rollback plan for numbers moving from Daily to Telnyx?**
4. **Is recording the broker consultation covered by consent?** Because of #4, so is recording the human-to-human part. Is anything meant to confirm the answering party is the broker before internal rates are shared?

None of these answers changes #1–#7.

---

## Evidence

All checks ran locally at `f93f7a5` (Python 3.11, pinned `requirements.txt` plus `tests/requirements-test.txt`), with the env vars from the CI workflow. No production credentials were used and no calls were placed. The LiveKit checks used the open-source server in dev mode (`brew install livekit && livekit-server --dev --bind 127.0.0.1 --node-ip 127.0.0.1`, keys `devkey`/`secret`).

| # | Check | Result |
|---|---|---|
| — | `python -m pytest tests -q` / `python -m pytest ../shared/tests -q` | 127 passed (17 subtests) / 91 passed |
| — | `git diff --check 5a98a66 f93f7a5`; `ruff check --no-fix` on the changed files | Clean; 19 findings, mostly carried over |
| 1 | `livekit.api.AccessToken(...).with_sha256(...)` signs a `WebhookEvent(participant_joined, kind=SIP, full sip.* attributes)`. `WebhookReceiver.receive()` decodes it, then `LiveKitWebhookService.accept()` | Decoded `kind=3`, result `REJECTED NOT_SIP_PARTICIPANT 422`. `import livekit.protocol.models_pb2` raises `ModuleNotFoundError` (`livekit-protocol` 1.1.27) |
| 2 | Local LiveKit: `Room2PipelineService._setup_room2_events()` on a real `TransportFactory.create(...)` transport; a `livekit-rtc` participant `broker-abc123` publishes audio; `mark_broker_answered()` | Callback got `PA_…`, `remote_participants` keys `['broker-abc123']`, `_broker_connected=False`, `_broker_media_ready=False`, 0 `LLMRunFrame`. Source: `livekit/rtc/room.py:988`; Pipecat `transports/livekit/transport.py:400-417, 508-538` |
| 3 | Existing orchestrator fakes; `FakeMedia.start_hold` blocked on an event; `on_carrier_left_primary()`; release | Phase `cancelled`, then `consulting`; telephony calls `create, token, delete, dial`; `FakeRoom2.stop` never called |
| 4 | (a) A real `PipelineTask([BaseInputTransport, sink])`: pause, push audio, queue a non-system frame, resume. (b) Local LiveKit: Room1 `TransportFactory.create(...)` plus a counting processor; `LiveKitTransferMedia.gate_primary_media(True)` and `start_hold()` with a real hold transport | (a) 5/5 audio frames pass while paused; 0 replayed on resume; queued frame held, then released on resume. (b) In 4 s while held: carrier 55 frames, hold-observer 61 frames reached the processor after `transport.input()` |
| 5 | Local LiveKit: `bot.start_bot()` for a room where `participant_sid` never joins (STT/TTS/LLM classes replaced by pass-through processors; `resolve_caller_identity`/`start_call` stubbed) | After 20 s the bot task is running; server lists `['negotiation-agent']` in the room; a second `start_bot()` returns `409 This bot runner is already handling a call` |
| 6 | Existing fakes; `dial_error=RuntimeError("busy")`; a `Room2.stop` that sleeps 50 ms; record call order | `room2_stop_begin, room2_stop_end, restore_primary`. Upload: `audio_storage.py:51-56` uses the synchronous supabase-py `Client` (`shared/src/supabase_service.py`) |
| 7 | Code path `server.py:175` vs `livekit_telephony_service.py:112`; `OutboundCallRequest(to_phone="  ", from_phone="  ")` then `normalize_e164` | Validation passes; normalises to `None` |
| 9 | Existing fakes; `detach_primary_publisher` raises after the move | Phase `failed`; Room2 not stopped, not deleted; `cleanup_pending=True` |
| 11, 12, 14 | LiveKit docs: [SIP API](https://docs.livekit.io/reference/telephony/sip-api/) (`wait_until_answered` returns on voicemail; `ringing_timeout` ≤ 80 s); [outbound calls](https://docs.livekit.io/telephony/making-calls/outbound-calls/); [managing participants](https://docs.livekit.io/home/server/managing-participants/#move-participant) (`MoveParticipant` Cloud-only) | As cited in the comments |
| 8, 13 | [Webhooks: delivery and retries](https://docs.livekit.io/home/server/webhooks/#delivery-and-retries); [SIP participant attributes](https://docs.livekit.io/reference/telephony/sip-participant/) (`sip.callIDFull`, inbound `ringing` until audio subscription) | As cited in the comments |
