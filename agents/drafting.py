"""Agent 5 — Drafting.

Writes the first draft from the approved topic. Also handles Loop-B rewrites:
when review feedback is present, it fixes every blocking issue and keeps what was
already fine. The tone requirement comes from style_provider (swap point).
"""

from __future__ import annotations

import json

from common.llm import complete_json, model_for, temperature_for
from common.loaders import fill, load_prompt
from interfaces.style_provider import get_style_spec
from state.article_state import ArticleState


def run(state: ArticleState) -> dict:
    """Read the chosen topic (+ optional review feedback), return {title, body}."""
    prompt = fill(
        load_prompt("drafting"),
        chosen_topic=json.dumps(state.chosen_topic, ensure_ascii=False),
        direction=state.direction,
        style_spec=get_style_spec(),
        # Empty on the first draft; filled on a Loop-B rewrite.
        review_feedback=state.review_feedback,
    )

    draft = complete_json(
        system_prompt=prompt,
        user_content="请写出文章初稿（仅返回 JSON）。",
        model=model_for("drafting"),         # long-form Chinese writing
        temperature=temperature_for("creative"),
    )

    state.draft = draft
    return draft
