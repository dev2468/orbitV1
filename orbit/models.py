"""The model catalog — which brains this build can be pointed at.

Its own module, and dependency-light on purpose, for the same reason
`orbit/policy.py` was split from `orbit/safety_plugin.py` on 2026-09-08: the
GUI needs this list to populate its model selector, and `orbit/agent.py`
cannot be imported to get it because `import litellm` costs 8.7s that a
desktop app must not pay at startup.

So the catalog lives here, `agent.py` imports it, and the GUI imports it too.
**Nothing in this file may import litellm, google-adk, or PySide6.**
`tests/test_policy.py` has a guard test for the equivalent rule on
`orbit/policy.py`; the same discipline applies here.
"""

from __future__ import annotations

import os

# All LLM calls go through OpenRouter (https://openrouter.ai).
# LiteLLM routes on the "openrouter/" prefix; OpenRouter needs OPENROUTER_API_KEY.
#
# Measured on 2026-09-07, same prompt, streaming, via OpenRouter:
#   gemini-2.5-flash   first content 0.90s   full answer 1.12s
#   claude-haiku-4-5   first content 1.08s   full answer 1.09s
#   gemini-3.7-flash   first content 3.27s   full answer 3.70s
#
# 3.7-flash is a reasoning model and OpenRouter refuses to turn that off for
# it — `reasoning={"max_tokens": 0}` comes back "Reasoning is mandatory for
# this endpoint", and `effort: low` only trims 225 reasoning tokens to 181.
# On the prompt "count from 1 to 20" it spent 225 tokens thinking before
# emitting a character. That is a ~3s floor on EVERY turn of the agent loop,
# paid even by "hi", and it is why a warm task took ~8-10s end to end.
#
# So the default is the fast model, not the smart one. 3.7-flash stays in
# KNOWN_MODELS and is still the right choice for hard vision/planning work —
# pick it from the GUI's model selector, or set ORBIT_MODEL.
DEFAULT_MODEL = "openrouter/google/gemini-2.5-flash"

_REQUIRED_KEY_BY_PREFIX = {
    "openrouter/": "OPENROUTER_API_KEY",
}

# Every entry here was checked to support tool calling. That is not a
# nice-to-have: an agent whose model cannot call tools cannot do anything at
# all, so this list doubles as the allowlist the GUI's selector is built from.
# Do not add a model without verifying it, and do not let the GUI offer a
# free-text box instead — that would be a way to break the app, not a way to
# choose.
KNOWN_MODELS = {
    "openrouter/google/gemini-2.5-flash": (
        "Gemini 2.5 Flash via OpenRouter. 1M context, strong tool calling, "
        "no forced reasoning — ~0.9s to first token. Default."
    ),
    "openrouter/google/gemini-3.7-flash": (
        "Gemini 3.7 Flash via OpenRouter. Stronger reasoning and vision, "
        "1M context — but reasoning CANNOT be disabled on this endpoint, "
        "which costs ~3s before the first token on every turn. Pick it for "
        "hard planning/vision work, not for conversation."
    ),
    "openrouter/anthropic/claude-sonnet-4-5": (
        "Claude Sonnet 4.5 via OpenRouter. Strong reasoning and instruction "
        "following. 200K context."
    ),
    "openrouter/anthropic/claude-haiku-4-5": (
        "Claude Haiku 4.5 via OpenRouter. Fastest Claude model. "
        "Good for simple/fast tasks."
    ),
    "openrouter/deepseek/deepseek-r1": (
        "DeepSeek R1 via OpenRouter. Strong reasoning model."
    ),
    "openrouter/meta-llama/llama-4-maverick": (
        "Llama 4 Maverick via OpenRouter. Open-weight, strong tool use."
    ),
}


def short_model_name(full_name: str) -> str:
    """`openrouter/google/gemini-2.5-flash` -> `gemini-2.5-flash`.

    For UI labels only. The full string is what any caller must pass on, so
    never round-trip a display name back into a model id — the GUI keeps the
    full name in the combo box's item data for exactly that reason.
    """
    return full_name.rsplit("/", 1)[-1] if "/" in full_name else full_name


def resolve_model_name(model_name: str | None = None) -> str:
    """The model a task will actually run on: an explicit choice, else
    ORBIT_MODEL, else DEFAULT_MODEL.

    One definition, used by `agent.select_model` to build the model and by
    `run_task` to record it on the task row, so the recorded model cannot
    drift from the one that ran.
    """
    return model_name or os.environ.get("ORBIT_MODEL") or DEFAULT_MODEL


def validate_model_key(model_name: str | None = None) -> None:
    """Raise RuntimeError if the chosen model's API key is missing.

    Separated from model construction so callers that only need the catalog
    (the GUI's selector, tests inspecting agent structure) don't crash before
    an API call is even attempted. run_task calls this explicitly before
    submitting work.
    """
    model_name = resolve_model_name(model_name)
    for prefix, required_key in _REQUIRED_KEY_BY_PREFIX.items():
        if model_name.startswith(prefix) and not os.environ.get(required_key):
            raise RuntimeError(
                f"{required_key} is not set, but model {model_name!r} needs it.\n"
                f"Add this line to the .env file in the project root:\n"
                f"    {required_key}=your-key-here"
            )
