# PR #1: consolidated findings and adjudication

Pinned head: `f93f7a5`. Every candidate finding from every source has been verified independently. Nothing is accepted on the say-so of its author.

## Sources

| Code | Source | Notes |
|---|---|---|
| ME | My own review | |
| KZ | kzhdanov | Inline comment on the PR |
| TI | tieincred | Inline comments and a review comment |
| PB | prabhi | Inline comments |
| VS | vipul-sharma20 | Inline comments |
| KR | Kishorerh1993 | Reviews |
| RS | ryansann | Conversation comment |
| OA | Another agent's write-up | Supplied by you |

The raw comments are in the PR #1 comment threads.

## Evidence tiers

| Tier | Meaning |
|---|---|
| **A** | Executed end-to-end with real library objects. Either against a **local open-source LiveKit server** (dev mode; no keys, no SIP, no calls), or with real SDK signing and protobuf objects |
| **B** | Executed: the PR's real logic, driven through the PR's own test fakes |
| **C** | Source or code trace only |
| **D** | Vendor documentation only |

## Severity scale

| Level | Meaning |
|---|---|
| **P0** | A core flow is broken for every call of a given type |
| **P1** | Merge blocker: a realistic path strands or misleads a live party, takes the service down, or breaks tenant isolation |
| **P2** | Fix before production, or conditional on deployment |
| **P3** | Minor or hygiene |

## Accepted findings

### F1. Every inbound call is rejected (P0, verified)

**Raised by:** KZ, TI. **Missed by:** ME.

**Evidence (Tier A):** `ingress_real_webhook_check.py` produces a real `participant_joined` event signed with `AccessToken.with_sha256`. The SDK's `WebhookReceiver` decodes `kind=3`, and the PR's `LiveKitWebhookService.accept` then returns `REJECTED NOT_SIP_PARTICIPANT 422`.

**Root cause:**
- `livekit.protocol.models_pb2` doesn't exist (the module is `livekit.protocol.models`), so the import fails.
- The enum name is `SIP`, which doesn't match `endswith("_SIP")`.

**Why the tests miss it:** the fixture uses the string `"SIP"`.

**Anchor:** `livekit_ingress.py:196-205`

### F2. Room2 never recognises the broker, so every warm transfer fails after the broker answers (P0, verified)

**Raised by:** KZ, TI. **Missed by:** ME.

**Evidence (Tier A, local LiveKit):** `livekit_local_e2e.py` E-A shows:
- Pipecat passes the SID `PA_…`;
- the `livekit-rtc` participant map is keyed `['broker-abc123']`;
- the PR's code ends with `_broker_connected=False`, `media_ready=False`, and no briefing frame queued.

**Source:** `livekit/rtc/room.py:988` (keyed by identity); Pipecat `transport.py:400-417, 511/538`.

**Why the tests miss it:** the fixture keys the map by SID.

**Anchor:** `room2_pipeline_service.py:247-266`

### F3. A carrier hangup during transfer setup doesn't stop the attempt (P1, verified)

**Raised by:** ME, KZ, TI.

**Evidence (Tier B):** `test_review_repro.py` shows the phase overwritten `cancelled → consulting`, one dial, and Room2 never stopped.

**Anchors:** `orchestrator.py:171-243`, `transfer_state.py:90`

### F4. "Hold" doesn't gate Room1 audio, and Room1 stays live through hold and after handoff (P1, verified)

**Raised by:** ME, KZ, TI. RS and OA identified the mechanism wrongly (see FP2).

**Evidence (Tier A):**
- `pause_check.py`: paused, 5/5 audio frames still flow.
- Local LiveKit E-B: with the PR's `gate_primary_media(True)` plus `start_hold()`, in 4 s **carrier=55 and hold-observer=61** frames reached the processor right after `transport.input()`, which is where STT sits.

**Consequences:**
- **Corrected after measurement:** `end_call`'s `EndFrame` (a control frame) is **held** while paused and released on resume (`pause_check` 3a/3b). So an `end_call` during hold hangs up on the carrier *right after* they're restored, not mid-consult.
- **Inferred:** tool side effects such as `record_agreement` aren't blocked.
- After a successful handoff, the bot transcribes and records the human-to-human conversation.

This is a regression: the base cancelled Room1 at transfer.

**Anchors:** `livekit_transfer_media.py:64, 69`

### F5. A runner whose caller never appears never exits, so later calls get 409 (P1, verified)

**Raised by:** ME only.

**Evidence (Tier A, local LiveKit, with speech/LLM services stubbed):** E-C shows:
- the bot connected to an empty room (server-side participant list: `['negotiation-agent']`);
- the task was still running after 20 s;
- an unrelated second `/start` got `409 This bot runner is already handling a call`.

The "forever" part rests on code: `idle_timeout_secs=None` and there's no `on_disconnected` handler.

**Harness caveat:** the first run exited because of a bug in my stub (missing `InputParams`), not the PR. It was diagnosed and fixed before I relied on the result.

**Anchors:** `bot.py:365, 577, 693`

### F6. The caller is restored only after Room2 teardown, which includes a synchronous upload with no timeouts (P1, verified)

**Raised by:** ME only.

**Evidence (Tier B plus C):**
- Repro order: `stop → stop_end → restore_primary`.
- supabase-py's synchronous `Client.upload` runs inside async code (`audio_storage.py:51-56`, `shared/src/supabase_service.py`).

**Anchors:** `orchestrator.py:486-488`, `room2_pipeline_service.py:221`

### F7. Outbound tenant context can differ from the actual caller ID (P1, verified)

**Raised by:** KZ, TI, PB, KR, RS, OA. **Missed by:** ME.

**Evidence (Tier C):** `bot_phone=from_phone` (`server.py:175`) drives the org lookup and attribution. Meanwhile `sip_number=config.livekit_outbound_number` (`livekit_telephony_service.py:112`) is always the real caller ID.

**Severity:** P1 (tenant isolation), mitigated by the endpoint requiring an API key.

### F8. Webhook dedupe is racy, process-local and unbounded (P2, verified)

**Raised by:** ME, TI, PB, KR, RS, OA.

**Evidence (Tier C):** the event ID is added at `server.py:141`, after the awaited start at L140.

**Adjudication:** the claim that it "starts two bots" is **overstated for this deployment**. With one runner, the second start gets a 409 (→ 503). It becomes a real double start with more than one runner. Hence P2, or P1 if Q1 says multiple runners.

### F9. A failed inbound start leaves the caller with no cleanup (P2, verified)

**Raised by:** KZ, ME.

**Evidence (Tier C plus D):** there's no cleanup on the `start_agent` exception path at `server.py:140`. The docs say an inbound SIP caller stays in `ringing` until it subscribes to remote audio, so the likely experience is a call that rings and is never answered. Severity depends on Q1.

### F10. Post-arrival media failure skips Room2 cleanup (P2, verified)

**Raised by:** TI. **Missed by:** ME.

**Evidence (Tier B):** `test_review_repro_more.py` shows `room2 stopped=False, deleted=False, cleanup_pending=True`.

**Anchor:** `orchestrator.py:376-383, 428`

### F11. Hard-coded `wait_until_answered=True` blocks `/outbound-call` until answer, and the bot cold-starts after answer (P2, verified)

**Raised by:** PB, KR, RS, OA, ME.

**Evidence (Tier C plus D):** `livekit_telephony_service.py:117`; LiveKit outbound docs.

### F12. Slack notify is awaited before dialing, with aiohttp's default 300 s timeout (P2, verified)

**Raised by:** ME, TI.

**Evidence (Tier C):** `orchestrator.py:215`, `transfer_notifier.py:55`.

### F13. `ringing_timeout` and `max_call_duration` are unset; the 45 s `wait_for` only cancels the HTTP request (P2, verified)

**Raised by:** ME.

**Evidence (Tier C plus D):** `livekit_telephony_service.py:108-118`; SIP API docs.

### F14. Hold has no overall budget or reassurance prompt (P2, risk)

**Raised by:** ME.

**Evidence (Tier C):** the timeouts add up (45 s + 180 s + 10 s), plus F12 and F6.

### F15. Voicemail or a wrong party gets the internal-rate briefing; the bridge is LLM-decided (P2, risk)

**Raised by:** ME.

**Evidence (Tier D plus C):** docs: `wait_until_answered` returns on voicemail. The prompt in `context_builder.py` is unchanged.

**Adjudication:** today this is **masked by F2**, because the briefing never starts. It becomes live as soon as F2 is fixed. The PR defers voicemail detection.

### F16. A media failure after a successful move evicts the broker from a live call (P2, risk)

**Raised by:** ME (related to TI's F10).

**Evidence (Tier C):** `orchestrator.py:376-383`.

### F17. Migration deploy ordering and no rollback; non-unique index (P2, risk)

**Raised by:** ME.

**Evidence (Tier C):** the migration file, plus the dashboard and voice code that both depend on it.

### F18. `provider_call_id` semantics differ: inbound stores the trunk SIP Call-ID (`sip.callIDFull`), outbound stores LiveKit's `sip_call_id` (P3, verified)

**Raised by:** ME, OA.

**Evidence (Tier C plus D):** the SIP participant docs define `sip.callIDFull` as the "Trunk provider SIP call ID", not a Telnyx Call Control ID.

### F19. Whitespace-only phone numbers pass validation and normalise to `None` (P3, verified)

**Raised by:** OA. **Missed by:** ME.

**Evidence (Tier B):** `OutboundCallRequest(to_phone="  ")` passes; `normalize_e164` returns `None`.

### F20. Silence gap: input is gated before the hold publisher connects (up to 10 s), with no telemetry (P3, verified)

**Raised by:** VS. **Missed by:** ME.

**Evidence (Tier C):** `orchestrator.py:197-198`.

### F21. Reliance on Pipecat private attributes (P3, risk)

**Raised by:** ME.

**Evidence (Tier C):** the attributes exist in 0.0.95, so this is brittle rather than broken. `livekit_transfer_media.py:203`

### F22. Cleanup "retry" uses `sleep(0)`; expected non-inbound joins get 4xx; 19 lint findings; leftover `pipecatcloud` dependency (P3, verified)

**Raised by:** ME, RS.

**Evidence (Tier C):** code, plus `ruff --no-fix`.

### F23. The LiveKit SDK adapter and other provider boundaries are only tested with permissive fakes (P2, verified)

**Raised by:** RS, KZ, TI.

**Evidence:** F1 and F2 are both hidden by fixtures shaped unlike the real SDK objects.

## Questions (not defects)

| ID | Question | Raised by | Status |
|---|---|---|---|
| Q1 | Deployment capacity: one isolated runner per call? This decides the severity of F8, F9 and the single-runner design | PB, KR, RS, ME | The PR states the single-call runner is intentional; the deployment contract isn't in the repo |
| Q2 | Is the broker's SID preserved across `MoveParticipant`? If not, post-handoff departure detection misses the broker | VS, RS, OA, ME | Docs describe remove + add without promising the SID; can't be tested locally (`MoveParticipant` is Cloud-only). Suggest matching by identity |
| Q3 | Rollout and rollback plan (Daily code is removed in the same PR) | ME | Open |
| Q4 | Recording consent (consultation, and post-handoff via F4) and broker identity before internal rates are disclosed | ME | Open |

## Rejected (false positive, or not material)

| ID | Claim | Raised by | Verdict and evidence |
|---|---|---|---|
| FP1 | "`pause_processing_frames()`/`resume_processing_frames()` don't exist in Pipecat 0.0.95, so every transfer raises `AttributeError`" | PB (P1), KR (P0) | **False positive.** They are defined on `FrameProcessor` (`frame_processor.py:578, 592`). `pause_check.py` and the local LiveKit E-B call them successfully on a real input transport. The real defect is F4 |
| FP2 | "Held audio is buffered and replayed after a failed transfer / unbounded audio backlog" | RS (blocker 1.4), OA (P1) | **False positive as stated.** `InputAudioRawFrame` is a `SystemFrame` and keeps flowing. `pause_check.py` shows 0 audio frames replayed after resume. Only non-system frames are held (see F4's corrected `end_call` consequence). TI explicitly disclaims the replay effect |
| FP3 | Move the lazy imports to top level (PEP 8) | VS | **Not material.** The lazy imports are deliberate: the module docstring says it keeps "imports optional in tests" at the SDK boundary |
| FP4 | Credential-shaped CI placeholders | KR | **Not material.** The values are inert placeholders in CI env (`test-key`, `*.invalid`) |
| FP5 | Import ordering, `Optional` annotation, constant naming, line length | RS §5 | **Nits.** Folded into F22 and not commented on individually |

## How these map to the final review comments (`../REVIEW.md`)

| Comment | Findings | Comment | Findings |
|---|---|---|---|
| #1 | F1 | #8 | F8 + F9 + 4xx part of F22 |
| #2 | F2 (+ the Room1 lookup note from TI's PR #17 doc) | #9 | F10 + F16 |
| #3 | F3 | #10 | F12 + F14 + F20 |
| #4 | F4 | #11 | F11 + F13 |
| #5 | F5 | #12 | F15 |
| #6 | F6 | #13 | F17 + F18 |
| #7 | F7 + F19 | #14 | Q2 |

- Summary "Tests" paragraph: F23, F21.
- Summary "Minor": rest of F22.
- Questions: Q1, Q3, Q4.
- Sources checked: 19 inline comments, 10 reviews, 3 conversation comments, tieincred's linked `REVIEW.md` (PR #22) and `conversation.md` (PR #17), the other agent's write-up, and my own review.

## Where my original review went wrong (owned)

- **Missed:** F1 and F2, the two P0s. I assumed the ingress and Room2 fixtures matched the real SDK shapes and didn't execute those paths against real objects. Also missed F7, F10, F19 and F20.
- **Overstated:** F4's `end_call` consequence was wrong in its timing. Measured and corrected.
- **Severity raised** after checking real behaviour: F5 (now executed end-to-end).
- **Severity lowered:** the webhook items (F8, F9) after comparing with the base and the single-runner behaviour.
