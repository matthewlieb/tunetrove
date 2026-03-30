# Bring-your-own API keys (BYOK)

## There is no keyless OpenAI API for `gpt-4o-mini`

This app calls OpenAI through the **HTTP API** (`/v1/chat/completions`, etc.). That always requires a **valid API key** and a **billing-enabled** OpenAI account. **ChatGPT in the browser** (consumer product) is separate and does **not** provide API access. **gpt-4o-mini** is *cheap per token*, not free at the API.

If you see **`401` / `invalid_api_key`**, the key in **`OPENAI_API_KEY`** (host) or the key you saved as **BYOK** is wrong, expired, or revoked — create a new secret at [API keys](https://platform.openai.com/api-keys), update env or **Save** in the UI, and restart the API.

---

Signed-in Spotify users can save **OpenAI** and/or **Anthropic** API keys on the server (encrypted with **`USER_LLM_KEYS_FERNET_KEY`** in Supabase). See **`supabase/user_llm_keys.sql`** and **`POST /auth/llm-keys`**.

## Models when users BYOK

- **OpenAI (BYOK):** the agent uses **`openai:gpt-4o-mini`** by default. If **`DEEPAGENTS_MODEL`** is set and starts with `openai:`, that model is used instead for OpenAI BYOK paths (see `src/agent/factory.py`).
- **Anthropic (BYOK):** defaults to a Haiku-style model unless **`DEEPAGENTS_MODEL`** starts with `anthropic:` or **`ANTHROPIC_MODEL`** is set.

Users **bring their own billing** via their provider keys; you are not charged for those turns.

## Host-paid users (no BYOK keys)

If the user has not saved keys, the API uses **`OPENAI_API_KEY`** / **`ANTHROPIC_API_KEY`** and **`DEEPAGENTS_MODEL`** (default in code and `.env.example`: **`openai:gpt-4o-mini`**).

## Security notes

- Never log or return raw keys in JSON.
- **`USER_LLM_KEYS_FERNET_KEY`** is server-only; never in Vercel `NEXT_PUBLIC_*` or client bundles.

## Related

- **`docs/COSTS_AND_BILLING.md`**
- **`docs/LAUNCH_CHECKLIST.md`**
