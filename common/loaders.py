"""Helpers to load config and prompt files.

Rule #2 (plan §0): all prompt text lives in `prompts/`, one file per agent.
Code only *reads* a prompt and fills in `{{placeholders}}` — it never hard-codes
prompt text. These helpers are the only place that touches those files, so the
agents stay short and readable.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

# Project root = the folder that contains config/, prompts/, data/, ...
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
PROMPTS_DIR = PROJECT_ROOT / "prompts"


@lru_cache(maxsize=None)
def load_settings() -> dict:
    """config/settings.yaml — direction, thresholds, models, frameworks, etc."""
    with open(CONFIG_DIR / "settings.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=None)
def load_scorecard() -> dict:
    """config/scorecard.yaml — the five-dimension 5/3/1 scorecard."""
    with open(CONFIG_DIR / "scorecard.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    """Read prompts/<name>.md as raw text (placeholders are filled separately)."""
    with open(PROMPTS_DIR / f"{name}.md", encoding="utf-8") as f:
        return f.read()


def fill(template: str, **values: str) -> str:
    """Replace every `{{key}}` in the template with the matching value.

    Plain string replacement — explicit and easy to follow. Missing keys are
    left untouched on purpose (so a typo is visible in the rendered prompt).
    """
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", str(value))
    return rendered
