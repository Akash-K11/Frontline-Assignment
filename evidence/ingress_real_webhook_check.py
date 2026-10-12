"""X1 check: a real, correctly signed LiveKit participant_joined (SIP) webhook through the PR's ingress.

Run from Frontline-Assignment/voice-agent with the pinned venv.
"""

import asyncio
import base64
import hashlib
import os
import sys

sys.path.insert(0, os.getcwd())

from google.protobuf.json_format import MessageToJson
from livekit import api
from livekit.protocol import models, webhook

from livekit_ingress import LiveKitWebhookRejected, LiveKitWebhookService
from telephony_config import load_livekit_telephony_config

KEY, SECRET = "devkey", "secret-that-is-long-enough-for-hmac-signing"
config = load_livekit_telephony_config({
    "LIVEKIT_URL": "wss://example.livekit.cloud",
    "LIVEKIT_API_KEY": KEY,
    "LIVEKIT_API_SECRET": SECRET,
    "LIVEKIT_SIP_INBOUND_TRUNK_ID": "ST_inbound",
    "LIVEKIT_SIP_DISPATCH_RULE_ID": "SDR_rule",
    "LIVEKIT_SIP_OUTBOUND_TRUNK_ID": "ST_outbound",
    "LIVEKIT_SIP_OUTBOUND_NUMBER": "+12025550199",
})

event = webhook.WebhookEvent(
    event="participant_joined",
    id="EV_real_1",
    room=models.Room(name="call-room-1"),
    participant=models.ParticipantInfo(
        sid="PA_caller",
        identity="sip_+14155550123",
        kind=models.ParticipantInfo.Kind.SIP,
        attributes={
            "sip.callID": "SCL_1",
            "sip.callIDFull": "telnyx-call-1",
            "sip.trunkID": "ST_inbound",
            "sip.ruleID": "SDR_rule",
            "sip.trunkPhoneNumber": "+12025550100",
            "sip.phoneNumber": "+14155550123",
        },
    ),
)
body = MessageToJson(event)
token = (
    api.AccessToken(KEY, SECRET)
    .with_sha256(base64.b64encode(hashlib.sha256(body.encode()).digest()).decode())
    .to_jwt()
)

decoded = api.WebhookReceiver(api.TokenVerifier(KEY, SECRET)).receive(body, token)
print("SDK-decoded participant.kind:", repr(decoded.participant.kind))


async def main() -> None:
    service = LiveKitWebhookService(config)
    try:
        accepted = await service.accept(raw_body=body.encode(), authorization=token)
        print("ACCEPTED:", accepted)
    except LiveKitWebhookRejected as exc:
        print(f"REJECTED: code={exc.code} status={exc.status_code}")


asyncio.run(main())
