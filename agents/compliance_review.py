"""Agent 7 — Compliance & Copyright Review (SECOND framework).

This is the one agent that runs on a different framework (LangChain/LangGraph,
`ChatOpenAI`) instead of the pydantic-ai spine — that is how we satisfy the
cross-framework requirement (plan §7). It is single-shot and self-contained, so
the framework difference does not affect anything else. It posts its issues into
the same review thread agent 6 writes to.

Note: this uses LangChain's `ChatOpenAI` directly (not common/llm.py) precisely
so the second framework is genuinely exercised, not just declared.
"""

from __future__ import annotations

import json

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from common.llm import is_reasoning_model, model_for, reasoning_effort_for, temperature_for
from common.loaders import fill, load_prompt
from common.sample_digest import build_samples_digest
from state.article_state import ArticleState


def run(state: ArticleState) -> dict:
    """Review the draft for compliance; append the compliance review object."""
    prompt = fill(
        load_prompt("compliance_review"),
        draft=json.dumps(state.draft, ensure_ascii=False),
        samples=build_samples_digest(state.samples),
    )

    # The 2nd framework: a LangChain ChatOpenAI call, asked for a JSON object.
    judge = model_for("judge")
    mk = {"response_format": {"type": "json_object"}}
    if is_reasoning_model(judge):
        # Reasoning model: no temperature; pass reasoning_effort explicitly.
        llm = ChatOpenAI(model=judge, reasoning_effort=reasoning_effort_for(), model_kwargs=mk)
    else:
        llm = ChatOpenAI(model=judge, temperature=temperature_for("scoring"), model_kwargs=mk)
    response = llm.invoke([
        SystemMessage(content=prompt),
        HumanMessage(content="请审查初稿的合规与版权风险（仅返回 JSON）。"),
    ])

    review = json.loads(response.content)
    state.review_thread.append(review)
    return review
