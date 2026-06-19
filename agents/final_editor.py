"""Agent 8 — Final Editor.

Reads the whole review thread (brand + compliance), counts the blocking issues,
and either approves (producing the final package) or sends the draft back for a
Loop-B rewrite. The orchestrator reads `blocking_issues` and `decision` to route.
"""

from __future__ import annotations

import json

from common.llm import complete_json, model_for, temperature_for
from common.loaders import fill, load_prompt
from state.article_state import ArticleState


def run(state: ArticleState) -> dict:
    """Integrate the reviews; write blocking_issues + either output or feedback."""
    prompt = fill(
        load_prompt("final_editor"),
        draft=json.dumps(state.draft, ensure_ascii=False),
        review_thread=json.dumps(state.review_thread, ensure_ascii=False),
        draft_revision_count=str(state.draft_revision_count),
    )

    result = complete_json(
        system_prompt=prompt,
        user_content="请综合两份审查意见，决定通过或退回重写（仅返回 JSON）。",
        model=model_for("judge"),            # judge → stronger GPT
        temperature=temperature_for("scoring"),
    )

    # The router reads this count and the decision; keep them plain.
    state.blocking_issues = int(result.get("blocking_issues", 0))

    if result.get("decision") == "approve":
        state.output = result.get("final")
        state.review_feedback = ""
    else:
        # Loop B: carry the consolidated feedback back to drafting.
        state.review_feedback = result.get("rewrite_feedback", "")

    return result
