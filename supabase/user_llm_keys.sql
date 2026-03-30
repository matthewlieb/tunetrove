-- Optional: encrypted BYOK (bring-your-own) LLM API keys per Spotify user.
-- Run once in Supabase → SQL Editor after `spotify_users` exists (see taste_memory.sql).
-- Without these columns, the API cannot read/write BYOK flags (UI may show "degraded" or empty status).

alter table public.spotify_users
  add column if not exists llm_openai_key_encrypted text,
  add column if not exists llm_anthropic_key_encrypted text,
  add column if not exists llm_provider text;

comment on column public.spotify_users.llm_openai_key_encrypted is 'Fernet ciphertext; requires USER_LLM_KEYS_FERNET_KEY on API';
comment on column public.spotify_users.llm_anthropic_key_encrypted is 'Fernet ciphertext';
comment on column public.spotify_users.llm_provider is 'openai | anthropic — which key to use when both are set';
