# AI Caller

A supervisor picks a team member, describes what needs to happen, and an AI agent
phones or texts that person for real.

```
Supervisor UI (frontend/index.html)
        │  POST /api/call | /api/message
        ▼
FastAPI backend (backend/main.py)
        ├─► OpenRouter   — LLM drafts the opening line and continues the conversation
        ├─► Kokoro       — turns each AI line into speech (mp3 served from /audio/…)
        └─► Twilio       — places the phone call / sends the SMS
                 ▲  │
                 │  └─ Twilio POSTs the callee's speech to /twilio/gather/{id}
                 └──── backend answers with TwiML (<Play> Kokoro audio + <Gather speech>)
```

## Run it

1. Copy `.env.example` to `.env` and fill in the keys (see below).
2. Start the server:
   ```powershell
   .\run.ps1
   ```
3. Open http://localhost:8000

Without any keys the app still works in **dry-run** mode: the UI, people list and
canned previews all function, nothing is dialed.

## Services

| Step | What you need | Env vars |
|------|---------------|----------|
| OpenRouter | API key from openrouter.ai, any chat model id | `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` |
| Kokoro | Any OpenAI-compatible `/v1/audio/speech` server running Kokoro. Local: `docker run -p 8880:8880 ghcr.io/remsky/kokoro-fastapi-cpu:latest`. Hosted: DeepInfra (`KOKORO_MODEL=hexgrad/Kokoro-82M`) | `KOKORO_BASE_URL`, `KOKORO_API_KEY`, `KOKORO_MODEL`, `KOKORO_VOICE` |
| Twilio | Account SID, auth token, a voice+SMS capable number | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` |
| Public URL | Twilio must reach your machine over HTTPS. Run `ngrok http 8000` and paste the https URL | `PUBLIC_BASE_URL` |

If Kokoro is down or unset, calls fall back to Twilio's built-in voice so the call
still goes through.

## How a call works

1. Supervisor selects a person, writes the intent, optionally clicks **Generate preview**
   to see and edit the AI's opening line, then **Place call**.
2. Backend asks OpenRouter for the opening line, renders it with Kokoro, and tells
   Twilio to dial the number with `url=/twilio/voice/{session}`.
3. When the person answers, Twilio fetches TwiML: play the Kokoro mp3, then
   `<Gather input="speech">`.
4. Twilio transcribes the person's reply and POSTs it to `/twilio/gather/{session}`.
   The backend appends it to the conversation, asks OpenRouter for the next line,
   renders it with Kokoro, and returns new TwiML. Repeat.
5. When the LLM ends its reply with `[END]` the call hangs up. Two silent turns also
   end the call. Voicemail is detected and the opening line is left as a message.
6. The Activity panel in the UI polls `/api/sessions` and shows the live transcript
   and Twilio status (ringing, in-progress, completed, ...).

## Files

- `backend/main.py` — API, OpenRouter + Kokoro + Twilio glue, Twilio webhooks
- `frontend/index.html` — supervisor interface (vanilla JS, no build step)
- `data/employees.json` — team list (auto-seeded with sample people)
- `audio_cache/` — generated Kokoro mp3s served to Twilio

## Production notes

- Sessions live in memory; move `SESSIONS` to Redis/Postgres for multiple workers.
- Validate Twilio webhook signatures (`twilio.request_validator.RequestValidator`)
  before trusting `/twilio/*` requests.
- Check local rules on AI-placed calls and recording consent. The system prompt
  already makes the agent identify itself as an AI.
