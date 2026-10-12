# PR #1 review ledger: strategy, evidence, priorities

This is the reviewer's working record behind `../REVIEW.md`. **Final verdicts, severities and evidence tiers are in `ADJUDICATION.md`, which supersedes §3 below wherever they differ.** Each claim lists its evidence, its confidence level, and how to re-check it. Nothing was posted to PR #1, and neither of its branches was modified.

**ID mapping to the final review:** this ledger uses the working IDs. The final review's IDs map as follows:

| Ledger | Final review |
|---|---|
| B1 | D1 |
| B2a/b/d | D2 |
| B2c | D2 part 2 (plausible consequence) |
| B3 | D3 |
| B4 | D4 |
| B5a/c | R2 |
| B5b | D6 |
| H1 | R1 |
| H3 | Q2 |
| H4 | D7 |

## 0. Working with the coding agent: what was delegated and what was challenged

| # | Agent output or first hypothesis | How it was checked | Outcome |
|---|---|---|---|
| 1 | Pre-assessment subagent research reports (the pre-assessment research notes (not included)) | Used only as leads. Every borrowed claim was re-checked against pinned source or primary docs | `MoveParticipant` being Cloud-only, voicemail counting as answered, and webhook best-effort delivery were all confirmed in LiveKit docs or SDK source |
| 2 | "Pipecat private attributes may not exist, so the handoff always fails" | Unpacked the pinned 0.0.95 wheel and read `LiveKitTransportClient` | The attributes exist → **downgraded** from possible blocker to maintainability risk (R6) |
| 3 | "Starting the outbound bot only after answer is wrong" | LiveKit's outbound-calls guide says to wait for answer before greeting | **Reframed** as a cold-start latency risk (R5) |
| 4 | D2 claim based on reading source | Built an empirical check. The first pytest harness was mis-wired and then hung in teardown; replaced it with a standalone script with a watchdog | **Confirmed**: 5/5 frames forwarded while paused |
| 5 | Lint run | The repo's `ruff.toml` has `fix = true`, so the first run rewrote 61 files. Caught via `git status`, reverted with `git checkout -- .`, re-ran with `--no-fix` | Branch content untouched; 19 findings reported honestly |
| 6 | "Broker SID changes after move" | Docs describe remove + add and don't promise a SID either way | **Downgraded** to a question (Q2) |
| 7 | Webhook issues as a blocker | Compared with the base: the old code already had single-call global state; with one runner, a duplicate gets 409 rather than a second bot | **Downgraded** to R2 (pending Q1) plus a Medium defect (D6) |
| 8 | Voicemail exposure as a blocker | The PR explicitly defers voicemail, and the over-sharing prompt is unchanged from base | Kept as a **High risk, not blocking**, with a concrete gating ask |
| 9 | Inline comment anchors | Checked every anchor against `git diff -U0 base head` with a script | Three anchors on unchanged context lines were moved to changed lines |

## 1. Strategy: what I investigated and why

The product is a real-time phone agent, and the PR changes both the transport and the transfer lifecycle. The most expensive failures are the ones a live caller or broker experiences (dead air, being stranded on hold, being bridged to the wrong place), wrong business writes, and anything that takes the whole service down. So I spent the effort in this order:

1. **The baseline.** How the Daily version handled the same moments: room pool, runner, and the bot leaving Room1 at transfer. Without this, regressions can't be told apart from bugs that were already there.
2. **Lifecycle and concurrency in the new transfer orchestration.** Every await is a point where the outside world can change. I walked carrier hangup, broker hangup, timeouts, duplicates and failures against each phase.
3. **Assumptions about the libraries.** The PR depends on Pipecat 0.0.95 and LiveKit behaviour, so I checked those against the pinned source and the primary docs rather than taking the PR's description on trust.
4. **The call-entry path.** Webhook delivery, dedupe, the single-call runner, and outbound calls.
5. **Data, rollout and privacy.** The migration, the dashboard, recordings, and what the broker briefing discloses.

Deliberately lighter: dashboard UI details, prompt wording, style. Not done: live LiveKit/Telnyx calls (no provisioned credentials) and the dashboard `tsc` (needs the private `@e3-solutions/ui` package).

## 2. Context given to AI helpers

- **Before the assessment:** subagents researched public documentation only (LiveKit, Twilio/SIP, Pipecat/Vapi/Retell/OpenAI, eval methods). The output is in the pre-assessment research notes (not included). I treated it as leads, not evidence.
- **During the review:** no repository code or PR content went to any subagent. I did all the PR analysis, test runs and reproductions directly.
- **Re-checking:** every claim the review takes from that research was checked again against the pinned library source or the primary LiveKit docs (see the ledger below). Research claims I couldn't re-check aren't used.

## 3. Claim ledger

Evidence types:
- **R**: reproduced by a test or script.
- **S**: read from the PR or library source at the pinned version.
- **D**: primary vendor documentation.
- **I**: inferred from S or D, not observed.

| ID | Claim | Evidence | Confidence | How to re-check |
|---|---|---|---|---|
| B1 | A carrier hangup during setup doesn't stop the attempt: broker dialled, phase overwritten, Room2 not stopped | R + S (`orchestrator.py:157-253`, `transfer_state.py:90`) | High | `test_review_repro.py::test_carrier_hangup_during_hold_start_must_not_dial_broker` |
| B2a | `pause_processing_frames()` doesn't stop input audio | R + S (`frame_processor.py:578`, `base_input.py:257-480`) | High | `python pause_check.py`: "after pause … 5/5" |
| B2b | Room1 input receives every remote participant's audio, including the hold publisher and the broker after the move | S (`livekit/transport.py:286-289, 576-589, 727-748`) | High | Read the cited lines: `auto_subscribe=True`, no per-participant filter |
| B2c | The Room1 LLM can take turns and call tools during hold and after the handoff. **Corrected:** `end_call`'s `EndFrame` is held while paused and fires on resume, hanging up on the carrier just after restore (measured in `pause_check.py` 3a/3b). `record_agreement` is still inferred | R (`EndFrame` behaviour) + I (tool side effects) | Medium-high | `rg -n "HUMAN_ONLY\|call_mode" voice-agent` |
| B2d | Regression: the base code cancelled Room1 at transfer | S (base `orchestrator.py` `_leave_room1`) | High | `git show 5a98a66:voice-agent/orchestrator.py` |
| B3 | A runner that never matches its caller never exits, so later calls get 409 | **R on a local LiveKit server** (`livekit_local_e2e.py` E-C: still running after 20 s; second start returns 409) + S | High | Read `bot.py`; Pipecat default `IDLE_TIMEOUT_SECS=300` in `pipeline/task.py:57` |
| B4a | Caller restore runs after Room2 teardown | R + S (`orchestrator.py:481-495`) | High | `test_review_repro.py::test_failure_restores_caller_only_after_room2_teardown` |
| B4b | The recording upload is synchronous inside async code, with no timeout | S (`audio_storage.py:51-56`, supabase-py sync client) | High | Read the cited lines |
| B5a | LiveKit webhook delivery is best-effort: retried, then abandoned | D ([webhooks: Delivery and retries](https://docs.livekit.io/home/server/webhooks/#delivery-and-retries)) | High | Docs |
| B5b | Dedupe race: the event ID is recorded only after `start_agent` | S (`server.py:123-141`) | High | Read the code |
| B5c | A rejected or lost webhook leaves the caller with no agent | S + I (what the caller hears depends on trunk config) | Medium | Severity depends on author question Q1 |
| H1 | Voicemail counts as answered; the briefing shares internal rates; the handoff is decided by the LLM | D ([SIP API `wait_until_answered`](https://docs.livekit.io/reference/telephony/sip-api/), [outbound calls table](https://docs.livekit.io/telephony/making-calls/outbound-calls/)) + S (`context_builder.py`, `room2_pipeline_service.py:166`) | High (the prompt is carried over; the exposure path is new) | Docs and code |
| H3 | The broker's SID after `MoveParticipant` may differ | D (described as remove + add; SID not promised) + S (comparisons by SID) | **Low-medium (open question)** | Needs a live Cloud test, or ask the author |
| H4 | `ringing_timeout` and `max_call_duration` are unset | S (`livekit_telephony_service.py:107-119`) + D (fields exist; ring max 80 s) | High | Read the code |
| A1 | `MoveParticipant` is LiveKit Cloud only | D ([managing participants](https://docs.livekit.io/home/server/managing-participants/#move-participant)) + S (livekit-api docstring) | High | Docs |
| — | PR's test claims (127 voice + 17 subtests, 91 shared) | R | High | See the commands in §5 |
| — | 19 ruff findings on changed files with the repo config | R | High (mostly carried over) | `ruff check --no-fix` (in this repo, never run ruff without `--no-fix`; `ruff.toml` has `fix = true`) |

## 4. How I prioritised

**Severity is user impact × likelihood × how hard the failure is to detect.**

- **Blocker:** a realistic event sequence strands or misleads a live party, writes wrong business data, or takes the whole service offline. Fixing it needs a design change, not a one-line patch.
  - B1: carrier hangups during hold are common.
  - B2: changes what the bot does on every transfer.
  - B3: one bad call disables all later calls.
  - B4: callers stay on hold longer on every failure.
  - B5: missed calls with no signal.
- **High:** serious harm, but it depends on a specific condition (voicemail, a long hold, a SID change, a missing timeout), or a clear mitigation exists.
- **Medium/Low:** correctness, data quality or maintainability with limited live-call impact.

**What would change the recommendation:**
- If production autoscales one runner per call with routing (Q1), B5's dead-air part drops to High. B3 stays a blocker for any runner that is reused.
- If a live test shows the SID is kept on move, H3 can be dropped.
- Nothing I've learned changes B1, B2 or B4. They're verified and they're regressions or new failure paths.

## 5. Reproduce my checks

```bash
cd Frontline-Assignment/voice-agent    # checked out at f93f7a5
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r tests/requirements-test.txt -e ../shared
export SUPABASE_URL=http://test-suite.local SUPABASE_SERVICE_ROLE_KEY=test-key OPENAI_API_KEY=test-key \
  LIVEKIT_URL=wss://test-suite.livekit.invalid LIVEKIT_API_KEY=test-key LIVEKIT_API_SECRET=test-secret \
  LIVEKIT_SIP_INBOUND_TRUNK_ID=ST_test_inbound LIVEKIT_SIP_DISPATCH_RULE_ID=SDR_test_inbound \
  LIVEKIT_SIP_OUTBOUND_TRUNK_ID=ST_test_outbound LIVEKIT_SIP_OUTBOUND_NUMBER=+12025550199 \
  OUTBOUND_CALL_API_KEY=test-outbound-api-key
python -m pytest tests -q                   # 127 passed, 17 subtests
python -m pytest ../shared/tests -q         # 91 passed

cp ../evidence/test_review_repro.py tests/
python -m pytest tests/test_review_repro.py -q -s   # both fail on f93f7a5 (B1, B4)
rm tests/test_review_repro.py

python ../evidence/pause_check.py  # B2: 5/5 frames forwarded while paused
```

## 6. Before submitting: points to own personally

- B3 and B5c are established by reading code and reasoning, not by reproduction. They're stated that way in the review. Keep them as blockers only if you agree with the reasoning in §4.
- H3 is deliberately framed as a question.
- Final recommendation: **request changes**, as summarised in `../REVIEW.md`.
