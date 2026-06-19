"""Agent 4 — Editorial Decision (scoring).

Scores every candidate against config/scorecard.yaml (three-tier 5/3/1), picks
the recommended one, and emits `top_score`. The orchestrator reads `top_score`
to decide routing — this agent does NOT decide the next step itself.

Hot topics for the trend_fit dimension come from hot_topic_provider (algorithmic,
never hand-filled).
"""

from __future__ import annotations

import json

from common.llm import complete_json, model_for, temperature_for
from common.loaders import fill, load_prompt, load_scorecard
from interfaces.hot_topic_provider import get_hot_topics
from state.article_state import ArticleState


def run(state: ArticleState) -> dict:
    """Score the candidates; write the full scores + top_score into state."""
    prompt = fill(
        load_prompt("editorial_scoring"),
        candidates=json.dumps(state.candidates, ensure_ascii=False),
        scorecard=json.dumps(load_scorecard(), ensure_ascii=False),
        direction=state.direction,
        current_hot_topics=json.dumps(get_hot_topics(), ensure_ascii=False),
    )

    scores = complete_json(
        system_prompt=prompt,
        user_content="请对每个候选选题打分（仅返回 JSON）。",
        model=model_for("judge"),            # judge → stronger GPT, independent of the generator
        temperature=temperature_for("scoring"),
    )

    state.scores = scores
    # The router reads this number; keep it as a plain float.
    state.top_score = float(scores.get("top_score", 0))
    return scores
