-- Run in Supabase SQL editor
-- 1) Enable pgvector
create extension if not exists vector;

-- 2) Taste memory table
create table if not exists public.taste_memory (
  id bigserial primary key,
  user_id text not null,
  source text not null,
  text text not null,
  metadata jsonb not null default '{}'::jsonb,
  embedding vector(1536) not null,
  created_at timestamptz not null default now()
);

create index if not exists taste_memory_user_id_idx on public.taste_memory(user_id);
create index if not exists taste_memory_embedding_idx
  on public.taste_memory using ivfflat (embedding vector_cosine_ops) with (lists = 100);

-- 3) Similarity search RPC used by backend
create or replace function public.match_taste_memory(
  query_embedding vector(1536),
  match_user_id text,
  match_count int default 5
)
returns table (
  id bigint,
  user_id text,
  source text,
  text text,
  metadata jsonb,
  created_at timestamptz,
  score float
)
language sql
stable
as $$
  select
    tm.id,
    tm.user_id,
    tm.source,
    tm.text,
    tm.metadata,
    tm.created_at,
    1 - (tm.embedding <=> query_embedding) as score
  from public.taste_memory tm
  where tm.user_id = match_user_id
  order by tm.embedding <=> query_embedding
  limit greatest(match_count, 1);
$$;

-- 4) OAuth token storage (per Spotify user)
create table if not exists public.spotify_users (
  user_id text primary key,
  display_name text,
  email text,
  token_json jsonb not null,
  updated_at timestamptz not null default now()
);

