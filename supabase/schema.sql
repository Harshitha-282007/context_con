-- Clearance tables. Run once in the Supabase SQL editor.
-- These hold real email and verdicts. Row level security is on with no policies, so the
-- public anon key cannot read or write anything. Only the server-side service key can.

create table if not exists public.mail (
    id text primary key,
    origin text not null,
    sender text not null,
    subject text not null,
    text text not null,
    warnings jsonb not null default '[]',
    received text not null default '',
    added_at text not null
);

create table if not exists public.verdict (
    mail_id text primary key references public.mail (id) on delete cascade,
    result jsonb not null,
    credits numeric not null default 0,
    scanned_on text not null
);

create table if not exists public.meta (
    key text primary key,
    value text not null
);

alter table public.mail enable row level security;
alter table public.verdict enable row level security;
alter table public.meta enable row level security;
