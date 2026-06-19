"""Build the @mention task message for each agent, and parse an agent's JSON reply.

The orchestrator carries the genuinely inter-agent data (the prior step's output)
inside the @mention message as a JSON block. The agent's system prompt already
told it to read those inputs from the message (see system_prompts.py). Static
inputs (scorecard, tone, samples, direction) are baked into the system prompt, so
they are NOT repeated here — that keeps each message small.
"""

from __future__ import annotations

import json
import re

from state.article_state import ArticleState


def _block(directive: str, inputs: dict) -> str:
    """A task directive followed by a fenced JSON block of the inputs."""
    payload = json.dumps(inputs, ensure_ascii=False, indent=2)
    return f"{directive}\n\n```json\n{payload}\n```"


def build_mention_for(agent_key: str, state: ArticleState) -> str:
    """Return the message content the orchestrator posts when @mentioning agent_key."""
    if agent_key == "analysis":
        # samples + direction are static in the system prompt; nothing dynamic to pass.
        return "请基于样本数据产出结构化分析结果（仅返回 JSON，遵循 analysis_schema）。"

    if agent_key == "topic_strategy":
        return _block(
            "请基于以下分析结果产出候选选题（仅返回 JSON）。"
            "若包含 rejection_feedback，请视上一轮候选为已否决并产出全新选题。",
            {"analysis": state.analysis, "rejection_feedback": state.rejection_feedback},
        )

    if agent_key == "editorial":
        return _block(
            "请对以下候选选题逐一打分并给出推荐（仅返回 JSON，top_score 必须等于推荐项的加权总分）。",
            {"candidates": state.candidates},
        )

    if agent_key == "drafting":
        return _block(
            "请基于以下已批准选题写出文章初稿（仅返回 JSON）。"
            "若包含 review_feedback，请按其修复所有 blocking 问题后重写。",
            {"chosen_topic": state.chosen_topic, "review_feedback": state.review_feedback},
        )

    if agent_key == "brand_review":
        return _block(
            "请审查以下初稿的语气/品牌契合度（仅返回 JSON）。",
            {"draft": state.draft},
        )

    if agent_key == "compliance_review":
        return _block(
            "请审查以下初稿的合规与版权风险（仅返回 JSON）。",
            {"draft": state.draft},
        )

    if agent_key == "final_editor":
        return _block(
            "请综合以下审查意见，决定通过或退回重写（仅返回 JSON，blocking_issues 必须准确）。"
            "请按 draft_revision_count 的版次政策处理：初稿(0)退回一次，重写后(≥1)主要问题已修即通过。",
            {"draft": state.draft, "review_thread": state.review_thread,
             "draft_revision_count": state.draft_revision_count},
        )

    raise ValueError(f"no mention builder for agent_key '{agent_key}'")


def parse_json_reply(content: str) -> dict:
    """Extract the JSON object an agent posted, tolerating ```json fences / prose.

    Agents are told to return JSON only, but we parse defensively so one chatty
    reply does not break routing.
    """
    # 1. The whole message is already JSON.
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        pass

    # 2. A fenced ```json ... ``` block.
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    if fence:
        return json.loads(fence.group(1))

    # 3. From the first '{' to the last '}'.
    start, end = content.find("{"), content.rfind("}")
    if start != -1 and end > start:
        return json.loads(content[start:end + 1])

    raise ValueError("no JSON object found in agent reply")
