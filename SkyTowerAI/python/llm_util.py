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
            extra_headers={"HTTP-Referer": "https://skytower-ai.local",
                           "X-Title": "SkyTower-AI Aux"},
        )
        return response.choices[0].message.content or ""

    return chat
