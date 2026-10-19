# What the interface expects from the backend

`index.html` talks to these endpoints (JSON over HTTP). Serve the page from the
backend at `/`, or open it directly and set the base URL in the browser console:

```js
localStorage.setItem('apiBase', 'http://localhost:8000'); location.reload();
```

If the backend is unreachable the page falls back to **demo mode** with mock data.

| Method | Path | Request body | Response |
|--------|------|--------------|----------|
| GET | `/api/config` | – | `{ openrouter, kokoro, twilio, public_url, dry_run, supabase: bool, model, voice, company: string }` (drives the pipeline status pills in the header) |
| GET | `/api/employees` | – | `Employee[]` |
| POST | `/api/employees` | `Employee` without `id` | created `Employee` |
| DELETE | `/api/employees/{id}` | – | anything / 204 |
| POST | `/api/preview` | `OutreachRequest` | `{ script: string }` – the AI's opening line (call) or SMS body (message) |
| POST | `/api/call` | `OutreachRequest & { script?: string }` | `Session` |
| POST | `/api/message` | `OutreachRequest & { script?: string }` | `Session` |
| GET | `/api/sessions` | – | `Session[]`, newest first (polled every 3 s for the Activity panel) |

```ts
type Employee = { id: string; name: string; role: string; department: string;
                  phone: string /* E.164 */; language: string; notes: string };

type OutreachRequest = { employee_id: string; channel: 'call' | 'message';
                         intent: string; tone: string; supervisor_name: string };

type Session = { id: string; channel: 'call' | 'message'; employee: Employee; intent: string;
                 status: string;   // e.g. queued | initiated | ringing | in-progress | completed | failed | sent | delivered | dry-run
                 created: string;  // ISO timestamp
                 events: { t: string; kind: 'ai' | 'human' | 'system'; text: string }[] };  // live transcript
```

Errors: return a non-2xx status with `{ "detail": "message" }` and the UI shows it in a toast.
