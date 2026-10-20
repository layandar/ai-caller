"""
AI Caller backend.

Flow:  supervisor UI  ->  /api/call or /api/message
       -> OpenRouter (LLM writes/continues the conversation)
       -> Kokoro (text -> speech, OpenAI-compatible /audio/speech endpoint)
       -> Twilio (places the real phone call / sends the SMS)
       -> Twilio calls back into /twilio/* with the callee's speech, loop continues.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from supabase import create_client
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import Gather, VoiceResponse

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# --------------------------------------------------------------------------- config
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")

KOKORO_BASE_URL = os.getenv("KOKORO_BASE_URL", "").rstrip("/")  # e.g. http://localhost:8880/v1
KOKORO_API_KEY = os.getenv("KOKORO_API_KEY", "")
KOKORO_MODEL = os.getenv("KOKORO_MODEL", "kokoro")
KOKORO_VOICE = os.getenv("KOKORO_VOICE", "af_heart")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "")

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")  # ngrok https URL
COMPANY_NAME = os.getenv("COMPANY_NAME", "the company")
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"  # no real Twilio traffic

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

DATA_FILE = ROOT / "data" / "employees.json"
AUDIO_DIR = ROOT / "audio_cache"
AUDIO_DIR.mkdir(exist_ok=True)

twilio_client = (
    TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and not DRY_RUN
    else None
)

# Postgres via Supabase for employees + sessions (tables in supabase/migrations/).
# Unset -> data/employees.json + in-memory sessions, as before.
supabase = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

app = FastAPI(title="AI Caller")
app.mount("/static", StaticFiles(directory=ROOT / "frontend"), name="static")


# --------------------------------------------------------------------------- models
class Employee(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str
    role: str = ""
    department: str = ""
    phone: str  # E.164, e.g. +14155551234
    language: str = "English"
    notes: str = ""


class PreviewRequest(BaseModel):
    employee_id: str
    channel: Literal["call", "message"]
    intent: str
    tone: str = "friendly and professional"
    supervisor_name: str = "your supervisor"


class SendRequest(PreviewRequest):
    script: str | None = None  # supervisor-edited opening line / SMS body


# --------------------------------------------------------------------------- storage
SEED_EMPLOYEES = [
    Employee(name="Amira Hassan", role="Field Technician", department="Operations", phone="+10000000001"),
    Employee(name="Daniel Park", role="Sales Associate", department="Sales", phone="+10000000002"),
    Employee(name="Priya Nair", role="Nurse", department="Clinic", phone="+10000000003"),
]


# Supabase `employees` table: uuid id, name, phone_number, department, is_active, plus
# role / language / notes (see supabase/migrations/). The API keeps the Employee shape
# (`phone`), so translate at this boundary. "Remove" in the UI is a soft delete.
def employee_from_row(row: dict) -> Employee:
    return Employee(id=row["id"], name=row.get("name") or "", role=row.get("role") or "",
                    department=row.get("department") or "", phone=row.get("phone_number") or "",
                    language=row.get("language") or "English", notes=row.get("notes") or "")


def employee_to_row(emp: Employee) -> dict:
    row = {"id": emp.id, "name": emp.name, "phone_number": emp.phone,
           "department": emp.department, "is_active": True}
    # Optional columns go only when set, so a table that lacks them still accepts the row.
    for col, default in (("role", ""), ("language", "English"), ("notes", "")):
        if getattr(emp, col) != default:
            row[col] = getattr(emp, col)
    return row


def load_employees() -> list[Employee]:
    if supabase:
        rows = (supabase.table("employees").select("*").eq("is_active", True)
                .order("created_at").execute().data)
        return [employee_from_row(r) for r in rows]
    if not DATA_FILE.exists():
        save_employees(SEED_EMPLOYEES)
    return [Employee(**e) for e in json.loads(DATA_FILE.read_text(encoding="utf-8"))]


def save_employees(items: list[Employee]) -> None:
    DATA_FILE.parent.mkdir(exist_ok=True)
    DATA_FILE.write_text(json.dumps([e.model_dump() for e in items], indent=2), encoding="utf-8")


def get_employee(employee_id: str) -> Employee:
    for e in load_employees():
        if e.id == employee_id:
            return e
    raise HTTPException(404, "employee not found")


# Call/message sessions. In-process cache; when Supabase is configured every change is
# upserted to the `sessions` table and unknown ids are fetched from it, so the Twilio
# webhooks keep working across restarts and multiple workers.
SESSIONS: dict[str, dict] = {}


def persist(session: dict) -> None:
    SESSIONS[session["id"]] = session
    if supabase:
        supabase.table("sessions").upsert(session).execute()


def get_session(sid: str) -> dict | None:
    session = SESSIONS.get(sid)
    if session is None and supabase:
        rows = supabase.table("sessions").select("*").eq("id", sid).limit(1).execute().data
        if rows:
            session = SESSIONS[sid] = rows[0]
    return session


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(session: dict, kind: str, text: str) -> None:
    session["events"].append({"t": now(), "kind": kind, "text": text})
    persist(session)  # every state change in this app ends with a log line


# --------------------------------------------------------------------------- OpenRouter
def system_prompt(emp: Employee, req: PreviewRequest) -> str:
    extra = f"- Extra notes about this person: {emp.notes}\n" if emp.notes else ""
    return (
        f"You are an AI assistant contacting someone by {req.channel} on behalf of {req.supervisor_name}, "
        f"a supervisor at {COMPANY_NAME}. You are speaking with {emp.name} ({emp.role}, {emp.department}). "
        f"Speak {emp.language}. Tone: {req.tone}.\n\n"
        f"Purpose of this contact, as given by the supervisor:\n{req.intent}\n\n"
        "Rules:\n"
        "- Introduce yourself honestly as an AI assistant calling on the supervisor's behalf.\n"
        "- Keep each turn short (1-3 sentences). This is spoken aloud, so no lists, markdown or emojis.\n"
        "- Stay on the purpose above. If asked something you don't know, say you'll pass it to the supervisor.\n"
        "- When the purpose is fulfilled or the person wants to stop, say goodbye and append the token [END] "
        "at the very end of your reply.\n" + extra
    )


async def llm(messages: list[dict], max_tokens: int = 220) -> str:
    if not OPENROUTER_API_KEY:
        # Offline fallback so the UI is testable without a key.
        return ("Hello, this is an AI assistant reaching out on behalf of your supervisor. "
                "Do you have a moment to talk? (OPENROUTER_API_KEY not set, canned reply)")
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "HTTP-Referer": PUBLIC_BASE_URL or "http://localhost:8000",
                "X-Title": "AI Caller",
            },
            json={"model": OPENROUTER_MODEL, "messages": messages, "max_tokens": max_tokens, "temperature": 0.6},
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()


# --------------------------------------------------------------------------- Kokoro TTS
async def tts(text: str) -> str | None:
    """Return a public URL to an mp3 of `text`, or None if Kokoro isn't configured/fails."""
    if not KOKORO_BASE_URL or not PUBLIC_BASE_URL:
        return None
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"{KOKORO_BASE_URL}/audio/speech",
                headers={"Authorization": f"Bearer {KOKORO_API_KEY}"} if KOKORO_API_KEY else {},
                json={"model": KOKORO_MODEL, "voice": KOKORO_VOICE, "input": text, "response_format": "mp3"},
            )
            r.raise_for_status()
        name = f"{uuid.uuid4().hex}.mp3"
        (AUDIO_DIR / name).write_bytes(r.content)
        return f"{PUBLIC_BASE_URL}/audio/{name}"
    except Exception as exc:  # fall back to Twilio <Say>
        print("Kokoro TTS failed:", exc)
        return None


def speak(target: VoiceResponse | Gather, text: str, audio_url: str | None) -> None:
    if audio_url:
        target.play(audio_url)
    else:
        target.say(text, voice="Polly.Joanna")


# --------------------------------------------------------------------------- API
@app.get("/", response_class=HTMLResponse)
def index():
    return (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")


@app.get("/api/config")
def config():
    return {
        "openrouter": bool(OPENROUTER_API_KEY),
        "model": OPENROUTER_MODEL,
        "kokoro": bool(KOKORO_BASE_URL),
        "voice": KOKORO_VOICE,
        "twilio": twilio_client is not None,
        "public_url": bool(PUBLIC_BASE_URL),
        "dry_run": DRY_RUN or twilio_client is None,
        "supabase": supabase is not None,
        "company": COMPANY_NAME,
    }


@app.get("/api/employees")
def list_employees():
    return load_employees()


@app.post("/api/employees")
def add_employee(emp: Employee):
    if supabase:
        emp.id = str(uuid.uuid4())  # the table's id column is a uuid
        supabase.table("employees").insert(employee_to_row(emp)).execute()
        return emp
    items = load_employees()
    items.append(emp)
    save_employees(items)
    return emp


@app.delete("/api/employees/{employee_id}")
def delete_employee(employee_id: str):
    if supabase:
        supabase.table("employees").update({"is_active": False}).eq("id", employee_id).execute()
        return {"ok": True}
    items = [e for e in load_employees() if e.id != employee_id]
    save_employees(items)
    return {"ok": True}


@app.post("/api/preview")
async def preview(req: PreviewRequest):
    emp = get_employee(req.employee_id)
    if req.channel == "message":
        instruction = ("Write the SMS text message to send. Plain text, under 300 characters, "
                       "identify yourself as an AI assistant for the supervisor. Output only the message.")
    else:
        instruction = "Write your opening line for when the person answers the phone. Output only what you'd say."
    text = await llm([{"role": "system", "content": system_prompt(emp, req)},
                      {"role": "user", "content": instruction}])
    return {"script": text.replace("[END]", "").strip()}


@app.post("/api/message")
async def send_message(req: SendRequest):
    emp = get_employee(req.employee_id)
    body = req.script or (await preview(req))["script"]
    sid = uuid.uuid4().hex[:10]
    session = {"id": sid, "channel": "message", "employee": emp.model_dump(), "intent": req.intent,
               "status": "queued", "created": now(), "events": []}
    persist(session)
    log(session, "ai", body)
    if twilio_client is None:
        session["status"] = "dry-run"
        log(session, "system", "DRY RUN: SMS not sent (Twilio not configured).")
    else:
        msg = twilio_client.messages.create(to=emp.phone, from_=TWILIO_FROM_NUMBER, body=body)
        session["status"] = msg.status
        session["twilio_sid"] = msg.sid
        log(session, "system", f"SMS {msg.sid} {msg.status}")
    return session


@app.post("/api/call")
async def start_call(req: SendRequest):
    emp = get_employee(req.employee_id)
    opening = req.script or (await preview(req))["script"]
    sid = uuid.uuid4().hex[:10]
    session = {
        "id": sid, "channel": "call", "employee": emp.model_dump(), "intent": req.intent,
        "status": "initiated", "created": now(), "events": [],
        "messages": [{"role": "system", "content": system_prompt(emp, req)},
                     {"role": "assistant", "content": opening}],
        "opening": opening, "opening_audio": None, "silences": 0,
    }
    persist(session)
    session["opening_audio"] = await tts(opening)  # pre-render so the callee hears no delay
    audio_state = "ready" if session["opening_audio"] else "unavailable, using Twilio Say"
    log(session, "system", f"Kokoro audio: {audio_state}")

    if twilio_client is None:
        session["status"] = "dry-run"
        log(session, "system", "DRY RUN: call not placed (Twilio not configured).")
        log(session, "ai", opening)
        return session
    if not PUBLIC_BASE_URL:
        raise HTTPException(400, "PUBLIC_BASE_URL must be set (e.g. your ngrok https URL) so Twilio can reach this server.")

    call = twilio_client.calls.create(
        to=emp.phone,
        from_=TWILIO_FROM_NUMBER,
        url=f"{PUBLIC_BASE_URL}/twilio/voice/{sid}",
        status_callback=f"{PUBLIC_BASE_URL}/twilio/status/{sid}",
        status_callback_event=["initiated", "ringing", "answered", "completed"],
        status_callback_method="POST",
        machine_detection="Enable",
    )
    session["twilio_sid"] = call.sid
    log(session, "system", f"Call {call.sid} created")
    return session


@app.get("/api/sessions")
def sessions():
    if supabase:
        return (supabase.table("sessions")
                .select("id,channel,employee,intent,status,created,twilio_sid,events")
                .order("created", desc=True).limit(200).execute().data)
    out = sorted(SESSIONS.values(), key=lambda s: s["created"], reverse=True)
    return [{k: v for k, v in s.items() if k != "messages"} for s in out]


@app.get("/audio/{name}")
def audio(name: str):
    path = AUDIO_DIR / Path(name).name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="audio/mpeg")


# --------------------------------------------------------------------------- Twilio webhooks
def twiml(vr: VoiceResponse) -> Response:
    return Response(content=str(vr), media_type="application/xml")


def gather_for(sid: str) -> Gather:
    return Gather(input="speech", action=f"/twilio/gather/{sid}", method="POST",
                  speech_timeout="auto", timeout=6, language="en-US")


@app.post("/twilio/voice/{sid}")
async def voice(sid: str, AnsweredBy: str = Form(default="")):
    session = get_session(sid)
    vr = VoiceResponse()
    if not session:
        vr.say("Sorry, this call session was not found. Goodbye.")
        vr.hangup()
        return twiml(vr)
    if AnsweredBy.startswith("machine"):
        # Voicemail: leave the opening line and hang up.
        log(session, "system", "Voicemail detected, leaving message")
        vr.pause(length=1)
        speak(vr, session["opening"], session["opening_audio"])
        vr.hangup()
        return twiml(vr)
    log(session, "ai", session["opening"])
    g = gather_for(sid)
    speak(g, session["opening"], session["opening_audio"])
    vr.append(g)
    vr.redirect(f"/twilio/gather/{sid}?silence=1", method="POST")
    return twiml(vr)


@app.post("/twilio/gather/{sid}")
async def gather(sid: str, request: Request, SpeechResult: str = Form(default="")):
    session = get_session(sid)
    vr = VoiceResponse()
    if not session:
        vr.hangup()
        return twiml(vr)

    silence = request.query_params.get("silence") == "1"
    if silence or not SpeechResult.strip():
        session["silences"] += 1
        if session["silences"] >= 2:
            log(session, "system", "No response twice, ending call")
            speak(vr, "I didn't catch anything. I'll let your supervisor know to follow up. Goodbye.", None)
            vr.hangup()
            return twiml(vr)
        user_text = "(no response, prompt them again briefly)"
    else:
        session["silences"] = 0
        user_text = SpeechResult.strip()
        log(session, "human", user_text)

    session["messages"].append({"role": "user", "content": user_text})
    try:
        reply = await llm(session["messages"])
    except Exception as exc:
        log(session, "system", f"LLM error: {exc}")
        reply = "I'm sorry, I'm having trouble right now. Your supervisor will follow up with you. Goodbye. [END]"
    session["messages"].append({"role": "assistant", "content": reply})
    ending = "[END]" in reply
    reply = reply.replace("[END]", "").strip()
    log(session, "ai", reply)

    audio_url = await tts(reply)
    if ending:
        speak(vr, reply, audio_url)
        vr.hangup()
    else:
        g = gather_for(sid)
        speak(g, reply, audio_url)
        vr.append(g)
        vr.redirect(f"/twilio/gather/{sid}?silence=1", method="POST")
    return twiml(vr)


@app.post("/twilio/status/{sid}")
async def status(sid: str, CallStatus: str = Form(default=""), CallDuration: str = Form(default="")):
    session = get_session(sid)
    if session:
        session["status"] = CallStatus
        suffix = f" ({CallDuration}s)" if CallDuration else ""
        log(session, "system", f"Call {CallStatus}{suffix}")
    return Response(status_code=204)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=True)
