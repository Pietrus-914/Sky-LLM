"""
Minimal shared LLM access for auxiliary features (F5 reflections, playbook
distillation). Separate from the entry/exit engines on purpose: those own
trading decisions and their prompts; this is a cheap utility channel using
the exit-tier model. Returns None when no key/package — callers must treat
LLM access as optional.
"""
from typing import Callable, Optional

from loguru import logger

from config import LLM_CONFIG, POSITION_MANAGEMENT_CONFIG, OPENROUTER_API_KEY


# Attribution for the OpenRouter dashboard. Its "App" rows group by the
# REFERER url, not by X-Title, so all three channels sharing one referer
# collapsed into a single row under whichever title was seen first — on
# 30.07.2026 the entry panel's votes were listed as "Exit Manager", which
# makes per-channel spend and latency impossible to read. One path per
# channel keeps entry / exit / aux separable in Generations.
OPENROUTER_SITE = "https://skytower-ai.local"


def openrouter_headers(channel: str, title: str) -> dict:
    """Attribution headers for one OpenRouter channel (entry/exit/aux)."""
    return {
        "HTTP-Referer": f"{OPENROUTER_SITE}/{channel}",
        "X-Title": title,
    }


# OpenRouter reasoning-effort levels (its unified reasoning API). Anything
# else is refused locally rather than sent, so a typo in .env cannot turn into
# a per-call API error at T-150s.
REASONING_EFFORTS = ("max", "xhigh", "high", "medium", "low", "minimal", "none")


def reasoning_body(effort) -> dict:
    """OpenRouter body fragment capping how long a model may think.

    The field is the nested object {"reasoning": {"effort": ...}} — the flat
    `reasoning_effort` is an OpenAI-only spelling that OpenRouter ignores.
    Models that cannot reason degrade gracefully on OpenRouter's side, so the
    same fragment is safe to send to every panel member.

    Why cap it at all: the entry panel runs against a WALL CLOCK (analysis
    starts at PRELOAD_SECONDS, the deadline is T-20s), so a model that thinks
    for two minutes does not cast a slow vote — it casts no vote. Providers
    that count reasoning against max_tokens make it worse, returning 200 with
    an empty body: the silent drop this panel had to learn to log.

    Returns {} when unset/invalid, restoring the provider default rather than
    failing the call.
    """
    effort = str(effort or "").strip().lower()
    if effort not in REASONING_EFFORTS:
        return {}
    return {"reasoning": {"effort": effort}}


def make_chat_fn(model: str = None, max_tokens: int = 400,
                 timeout: float = 45.0) -> Optional[Callable[[str, str], str]]:
    """chat_fn(system, user) -> reply text, or None when LLM is unavailable.
    Uses OpenRouter with the (cheaper) exit-tier model by default."""
    api_key = OPENROUTER_API_KEY
    if not api_key:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        logger.debug("openai package missing — auxiliary LLM disabled")
        return None
    client = OpenAI(api_key=api_key, base_url="https://openrouter.ai/api/v1")
    use_model = model or POSITION_MANAGEMENT_CONFIG.get(
        "exit_llm_model", LLM_CONFIG.get("model"))

    def chat(system: str, user: str) -> str:
        response = client.chat.completions.create(
            model=use_model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            max_tokens=max_tokens,
            temperature=0.3,
            timeout=timeout,
            extra_headers=openrouter_headers("aux", "SkyTower-AI Aux"),
        )
        return response.choices[0].message.content or ""

    return chat
