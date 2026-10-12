# Evidence for `../REVIEW.md`

These scripts and notes support the review of PR #1. They test the PR head, so check out
`f93f7a5f79efdf08c18c583a6a6f7167084b93ae`, then run them from its `voice-agent/` directory using the pinned
dependencies and the env vars from `.github/workflows/voice-agent-tests.yml`.

| File | Supports | How to run |
|---|---|---|
| `ingress_real_webhook_check.py` | #1 | `python ../evidence/ingress_real_webhook_check.py` |
| `livekit_local_e2e.py` | #2, #4, #5 | Start `livekit-server --dev --bind 127.0.0.1 --node-ip 127.0.0.1`, then `python ../evidence/livekit_local_e2e.py` |
| `pause_check.py` | #4 | `python ../evidence/pause_check.py` |
| `test_room2_identity_keyed.py` | #2 | Copy into `tests/`, then run with pytest |
| `test_review_repro.py` | #3, #6 | Copy into `tests/`, then run with pytest |
| `test_review_repro_more.py` | #7, #9 | Copy into `tests/`, then run with pytest |
| `ADJUDICATION.md` | All | Every candidate finding considered, with its verdict, evidence and severity, including rejected claims |
| `LEDGER.md` | All | Review strategy, the AI-agent collaboration log, the claim ledger, and the commands |

No production credentials are needed and no calls are placed. The LiveKit checks use the open-source server in
dev mode only.
