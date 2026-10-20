-- AI Caller: team members the supervisor can contact, and every call / SMS
-- session with its live transcript. Mirrors Employee / Session in backend/main.py.

create table if not exists public.employees (
  id            uuid primary key default gen_random_uuid(),
  created_at    timestamptz not null default now(),
  name          text not null,
  phone_number  text not null,                   -- E.164, e.g. +14155551234
  department    text not null default '',
  is_active     boolean not null default true,   -- "Remove" in the UI sets this false
  role          text not null default '',
  language      text not null default 'English',
  notes         text not null default ''         -- hints for the AI about this person
);

create table if not exists public.sessions (
  id             text primary key,
  channel        text not null check (channel in ('call', 'message')),
  employee       jsonb not null,             -- snapshot of the Employee at send time
  intent         text not null default '',
  status         text not null default '',   -- queued | initiated | ringing | in-progress | completed | sent | dry-run | ...
  created        text not null,              -- ISO-8601 UTC, as the app writes it
  twilio_sid     text,
  events         jsonb not null default '[]'::jsonb,   -- transcript shown in the UI
  messages       jsonb,                      -- LLM chat history (calls only)
  opening        text,
  opening_audio  text,
  silences       integer not null default 0
);

create index if not exists sessions_created_idx on public.sessions (created desc);

-- The backend reaches Supabase with the key in .env. Until the app has its own
-- auth, that key may read and write both tables. Tighten these policies (or
-- switch the backend to a secret key and drop them) when you add Supabase Auth.
alter table public.employees enable row level security;
alter table public.sessions  enable row level security;

drop policy if exists "backend full access" on public.employees;
drop policy if exists "backend full access" on public.sessions;
create policy "backend full access" on public.employees
  for all to anon, authenticated using (true) with check (true);
create policy "backend full access" on public.sessions
  for all to anon, authenticated using (true) with check (true);
