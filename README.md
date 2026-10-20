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
| Kokoro | Any OpenAI-compatible `/v1/audio/speech` server running Kokoro. Easiest: OpenRouter (`KOKORO_BASE_URL=https://openrouter.ai/api/v1`, `KOKORO_MODEL=hexgrad/kokoro-82m`, reuses `OPENROUTER_API_KEY`). Local: `docker run -p 8880:8880 ghcr.io/remsky/kokoro-fastapi-cpu:latest`. DeepInfra also hosts it (`hexgrad/Kokoro-82M`) | `KOKORO_BASE_URL`, `KOKORO_API_KEY`, `KOKORO_MODEL`, `KOKORO_VOICE` |
| Twilio | Account SID, auth token, a voice+SMS capable number | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` |
| Public URL | Twilio must reach your machine over HTTPS. Run `ngrok http 8000` and paste the https URL | `PUBLIC_BASE_URL` |
| Supabase (optional) | A Supabase project: its URL and an API key. Stores the team and every call/SMS transcript in Postgres (see below) | `SUPABASE_URL`, `SUPABASE_KEY` |

If Kokoro is down or unset, calls fall back to Twilio's built-in voice so the call
still goes through.

## Supabase (Postgres storage)

Without `SUPABASE_URL`/`SUPABASE_KEY` the team lives in `data/employees.json` and
sessions only in memory, which is fine for a single local server. With them set, the
backend keeps employees and sessions in two Postgres tables, so transcripts survive
restarts and the Twilio webhooks work with multiple workers.

1. Create the tables. Either paste the files in `supabase/migrations/` (in order) into
   the SQL editor of your project, or link and push with the CLI:
   ```powershell
   npx supabase link --project-ref <your-project-ref>
   npx supabase db push
   ```
   `supabase/seed.sql` adds three sample people to an empty table. (`npx supabase start`
   + `npx supabase db reset` gives you a local stack with both applied.)
2. Put the project URL and key in `.env`. The publishable key works; for a server a
   secret key (`sb_secret_…`) is the better choice.
3. Restart. The **Supabase** pill in the header turns green.

Table shape: `employees` has `id` (uuid), `name`, `phone_number`, `department`,
`is_active`, `role`, `language`, `notes`. The API still exposes `phone`; the backend
translates. Removing a person in the UI sets `is_active = false` rather than deleting
the row. `sessions` holds one row per call or SMS, including the transcript.

The migration enables row level security with a permissive policy for the API key,
because the app has no login of its own yet. Tighten the policies when you add auth.

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
- `data/employees.json` — team list when Supabase is not configured (auto-seeded with sample people)
- `supabase/migrations/` — Postgres schema for the `employees` and `sessions` tables; `supabase/seed.sql` sample data
- `audio_cache/` — generated Kokoro mp3s served to Twilio

## Production notes

- Run with Supabase configured when you use more than one worker; without it sessions
  live only in the memory of the process that created them.
- Validate Twilio webhook signatures (`twilio.request_validator.RequestValidator`)
  before trusting `/twilio/*` requests.
- Check local rules on AI-placed calls and recording consent. The system prompt
  already makes the agent identify itself as an AI.
