"""Agent 6 — Brand & Style Review.

Checks the draft against our brand voice / tone (from style_provider) and posts
its issues into the shared review thread. It reviews tone only — compliance and
copyright are agent 7's job. Both write into the same review_thread so agent 8
can read both.
"""

from __future__ import annotations

import json

from common.llm import complete_json, model_for, temperature_for
from common.loaders import fill, load_prompt
from interfaces.style_provider import get_style_spec
from state.article_state import ArticleState


def run(state: ArticleState) -> dict:
    """Review the draft's tone; append the {reviewer:'brand', issues:[...]} object."""
    prompt = fill(
        load_prompt("brand_review"),
        draft=json.dumps(state.draft, ensure_ascii=False),
        style_spec=get_style_spec(),
    )

    review = complete_json(
        system_prompt=prompt,
        user_content="请审查初稿的语气与品牌契合度（仅返回 JSON）。",
        model=model_for("judge"),            # judge → stronger GPT
        temperature=temperature_for("scoring"),
    )

    state.review_thread.append(review)
    return review
