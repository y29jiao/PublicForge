"""Agent 3 — Topic Strategy.

Generates candidate topics + titles from the analysis and our direction. On a
Loop-A re-entry it receives the rejection feedback and produces brand-new
candidates that address it (a real rewrite, not the same list).
"""

from __future__ import annotations

import json

from common.llm import complete_json, model_for, temperature_for
from common.loaders import fill, load_prompt, load_settings
from state.article_state import ArticleState


def run(state: ArticleState) -> list:
    """Read analysis + direction (+ optional feedback), return candidates."""
    num_candidates = load_settings()["num_candidates"]

    prompt = fill(
        load_prompt("topic_strategy"),
        analysis=json.dumps(state.analysis, ensure_ascii=False),
        direction=state.direction,
        num_candidates=str(num_candidates),
        # Empty on the first round; filled on a Loop-A re-entry.
        rejection_feedback=state.rejection_feedback,
    )

    result = complete_json(
        system_prompt=prompt,
        user_content="请产出候选选题（仅返回 JSON）。",
        model=model_for("default"),          # generator → base GPT
        temperature=temperature_for("creative"),
    )

    state.candidates = result.get("candidates", [])
    return state.candidates
