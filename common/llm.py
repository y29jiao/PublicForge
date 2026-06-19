"""One small helper for calling the LLM and getting back parsed JSON.

For the offline main chain (build order milestone 2) the agents call the model
directly through this helper. When we later wrap agents as Band remote agents,
the same prompt files are reused as the adapter's instructions — so this helper
and the Band path stay consistent.

Every agent emits a JSON object (plan §6: "post structured JSON result"), so we
always ask for JSON and parse it here, in one place.
"""

from __future__ import annotations

import json
from functools import lru_cache

from dotenv import load_dotenv
from openai import OpenAI

from common.loaders import load_settings

# Load OPENAI_API_KEY from .env once, at import time.
load_dotenv()


@lru_cache(maxsize=1)
def _client() -> OpenAI:
    # Reads OPENAI_API_KEY from the environment (loaded from .env above).
    return OpenAI()


def model_for(role: str) -> str:
    """Look up which model a role uses, from settings.yaml `models`.

    role is one of the keys under `models` (default / drafting / judge).
    """
    models = load_settings()["models"]
    return models[role]


def temperature_for(kind: str) -> float:
    """Look up a temperature from settings.yaml `temperatures` (scoring/creative/analysis)."""
    return float(load_settings()["temperatures"][kind])


def is_reasoning_model(model: str) -> bool:
    """True for OpenAI reasoning models (gpt-5.x, o-series).

    Reasoning models reject `temperature` (only the default is allowed) and instead
    take a `reasoning_effort` knob — so callers must branch on this.
    """
    m = model.lower()
    return m.startswith("gpt-5") or m.startswith("o1") or m.startswith("o3") or m.startswith("o4")


def reasoning_effort_for() -> str:
    """The reasoning_effort to use for the judge model (settings.yaml `reasoning_effort`)."""
    return str(load_settings().get("reasoning_effort", "medium"))


def complete_json(
    system_prompt: str,
    user_content: str,
    *,
    model: str,
    temperature: float,
) -> dict:
    """Call the chat model and return its reply parsed as a JSON object.

    We force JSON output (response_format) so the structured-result contract the
    orchestrator relies on cannot drift into prose.
    """
    kwargs = dict(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    )
    # Reasoning models (gpt-5.x) reject temperature and take reasoning_effort instead.
    if is_reasoning_model(model):
        kwargs["reasoning_effort"] = reasoning_effort_for()
    else:
        kwargs["temperature"] = temperature

    response = _client().chat.completions.create(**kwargs)
    raw = response.choices[0].message.content
    return json.loads(raw)
