"""CLI for the music discovery agent."""

import os
import sys
from dotenv import load_dotenv

load_dotenv()

# Re-raise with a clearer message when OpenAI quota is exceeded
try:
    import openai
except ImportError:
    openai = None


def _last_ai_content(messages):
    from langchain_core.messages import AIMessage
    for m in reversed(messages):
        if isinstance(m, AIMessage) and m.content:
            return m.content
    return None


def _use_persistent_checkpointer() -> bool:
    from src.agent import thread_uses_langgraph_checkpoint

    return thread_uses_langgraph_checkpoint()


def main():
    from src.agent import create_agent, run_chat
    from src.tools.spotify_context import set_spotify_anonymous_allowed

    # Allow Spotify OAuth fallback (spotipy auth_manager) for CLI only — never for FastAPI /chat.
    set_spotify_anonymous_allowed(True)
    agent = create_agent()
    thread_id = "cli"

    def _run(msg, prev_messages=None):
        try:
            turn = run_chat(agent, msg, thread_id, previous_messages=prev_messages)
            messages = list(turn.get("messages") or [])
            hitl = turn.get("hitl")
            if hitl:
                import json

                print(
                    "\n[Human approval required — use the web UI or POST /chat/resume, "
                    "or set DEEPAGENTS_HITL=0 to disable HITL]\n",
                    json.dumps(hitl, default=str, indent=2)[:4000],
                )
            return _last_ai_content(messages), messages
        except Exception as e:
            if openai and isinstance(e, openai.RateLimitError):
                raise SystemExit(
                    "OpenAI rate limit / quota exceeded. Use Claude instead: set ANTHROPIC_API_KEY in .env and LLM_PROVIDER=anthropic."
                ) from e
            raise

    if len(sys.argv) > 1:
        msg = " ".join(sys.argv[1:])
        content, _ = _run(msg)
        print(content if content else "No reply from agent.")
        return

    print("Music discovery agent. Say something about music or your mood (or 'quit'). You can continue the conversation, e.g. 'add those to my playlist'.")
    use_checkpoint = _use_persistent_checkpointer()
    messages = []
    while True:
        try:
            line = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line or line.lower() in ("q", "quit", "exit"):
            break
        prev = None if use_checkpoint else messages
        content, messages = _run(line, prev_messages=prev)
        print("Agent:", content if content else "(no reply)")


if __name__ == "__main__":
    main()
