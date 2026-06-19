"""Build each Band agent's system prompt from its prompts/*.md file.

Rule #2 (plan §0): the prompt text always comes from prompts/ — we only fill in
variables here. For a Band remote agent, the prompt splits into two kinds of
`{{placeholders}}`:

  * STATIC inputs — known when the agent starts (the scorecard, the tone spec, the
    direction, the competitor sample data from the WeChat API). We fill these into
    the system prompt now.
  * DYNAMIC inputs — produced by an earlier agent at run time (the analysis, the
    candidates, the draft, the review thread, loop feedback). These arrive inside
    the @mention message the orchestrator posts, so here we replace them with a
    short directive telling the agent to read them from that message.

This keeps the per-agent message small (only the genuinely inter-agent data) while
every agent still gets its full role + output contract from its prompt file.
"""

from __future__ import annotations

import json

from common.loaders import fill, load_prompt, load_scorecard, load_settings
from common.sample_digest import build_samples_digest
from interfaces.data_source import fetch_samples
from interfaces.hot_topic_provider import get_hot_topics
from interfaces.style_provider import get_style_spec

# Directive that replaces a dynamic placeholder in the system prompt.
_FROM_MESSAGE = "（该输入由编排器在 @你 的消息中以 JSON 形式提供，请从消息正文里读取）"


# Who each agent hands its result off to next, by agent_key (used by adapters.py).
# The creative chain hands off agent->agent (real Band collaboration); everyone
# else hands to the orchestrator, which coordinates the review fan-in (final needs
# the draft + BOTH reviews), the score threshold, the rewrite loop, and the human.
HANDOFF_NEXT_BY_AGENT = {
    "analysis": "topic_strategy",       # direct agent->agent handoff over Band
    "topic_strategy": "editorial",      # direct agent->agent handoff over Band
    "editorial": "orchestrator",        # decision: threshold + human approval
    "drafting": "orchestrator",         # orchestrator fans the draft out to reviewers
    "brand_review": "orchestrator",
    "compliance_review": "orchestrator",
    "final_editor": "orchestrator",     # decision: blocking issues -> rewrite or done
}

# The compliance agent runs on LangGraph (the cross-framework piece). LangGraph
# custom tools can't reach the room's send capability the way pydantic's can, so
# that one agent delivers via the standard band_send_message tool instead of the
# handoff_result tool — and it runs the stronger gpt-5.x judge, which follows the
# "mention the orchestrator" instruction reliably (unlike gpt-4.1 generators).
_LANGGRAPH_AGENTS = {"compliance_review"}


def _band_protocol(prompt_name: str) -> str:
    """How a Band remote agent delivers its result to the next teammate over Band.

    Two variants:
      * pydantic-ai agents get a dedicated `handoff_result` tool that needs NO
        target (code attaches the correct @mention) — the model can't mis-mention
        itself, the failure mode that broke band_send_message.
      * the LangGraph agent (compliance) uses the standard band_send_message tool,
        told to @mention the orchestrator (a different participant, so no
        cannot_mention_self), reliable on the gpt-5.x judge.
    """
    from band_app.config import get_handle  # local import: avoids a config cycle

    if prompt_name in {"compliance_review"}:
        orch = get_handle("orchestrator")
        return (
            "## 最高优先级：Band 协作交接协议（先读这一段，覆盖下文任何冲突）\n"
            "你是 Band 房间里的合规审查智能体。完成审查后，交付方式如下：\n"
            f"1. 调用 `band_send_message`：`content` = 你的**纯 JSON 审查结果**，"
            f"`mentions` 数组里**有且只有** `{orch}`（协调者）。\n"
            f"2. 绝不要把你自己放进 mentions（会报 cannot_mention_self）。只 @ `{orch}`。\n"
            "3. 不要调用 band_add_participant / band_lookup_peers / band_create_chatroom。\n"
            "4. 只发一条 band_send_message 然后停止。\n\n"
            "------ 以下是你的具体职责说明 ------\n\n"
        )

    return (
        "## 最高优先级：Band 协作交接协议（先读这一段，覆盖下文任何冲突）\n"
        "你是 Band 房间里的一个智能体。你被 @ 到，就意味着**轮到你亲自完成你的本职工作**。\n"
        "完成本职工作后，交付方式**只有一种**：\n"
        "1. 调用 `handoff_result` 工具，参数 `result_json` = 你产出的**纯 JSON 结果**"
        "（字符串；下一棒需要的上下文请一并放进这个 JSON 里）。\n"
        "2. 系统会自动把它转交给流程的下一棒，你**不需要、也不要**指定任何 @ 对象。\n"
        "3. **不要**调用 band_send_message / band_add_participant / band_lookup_peers / "
        "band_get_participants / band_create_chatroom；房间和成员都已就绪。\n"
        "4. 只调用一次 `handoff_result` 然后停止，不要反复尝试或刷屏。\n\n"
        "------ 以下是你的具体职责说明 ------\n\n"
    )


def _static_values() -> dict[str, str]:
    """The inputs that are fixed for a whole run, ready to fill into any prompt."""
    settings = load_settings()
    return {
        "direction": settings["direction"],
        "num_candidates": str(settings["num_candidates"]),
        "scorecard": json.dumps(load_scorecard(), ensure_ascii=False),
        "style_spec": get_style_spec(),
        "current_hot_topics": json.dumps(get_hot_topics(), ensure_ascii=False),
        # NOTE: `samples` is intentionally NOT here — it's a WeChat-API pull and only
        # a couple of prompts use it, so build_system_prompt fetches it lazily.
    }


# Which placeholders each agent receives dynamically (from the @mention message).
_DYNAMIC_PLACEHOLDERS = {
    "analysis": [],  # reads samples + direction, both static for the MVP
    "topic_strategy": ["analysis", "rejection_feedback"],
    "editorial_scoring": ["candidates"],
    "drafting": ["chosen_topic", "review_feedback"],
    "brand_review": ["draft"],
    "compliance_review": ["draft"],
    "final_editor": ["draft", "review_thread", "draft_revision_count"],
}


def build_system_prompt(prompt_name: str) -> str:
    """Return the fully-prepared system prompt for the agent whose file is prompt_name.

    prompt_name is the prompts/ file stem (e.g. 'editorial_scoring').
    """
    template = load_prompt(prompt_name)
    values = _static_values()
    # Samples are a WeChat-API pull; only fetch when this prompt actually uses them
    # (analysis & compliance_review), so the other agents don't each hit the API.
    if "{{samples}}" in template:
        values["samples"] = build_samples_digest(fetch_samples())
    # Dynamic placeholders get the "read it from the message" directive instead of a value.
    for key in _DYNAMIC_PLACEHOLDERS.get(prompt_name, []):
        values[key] = _FROM_MESSAGE
    # Lead with the Band handoff protocol so the agent passes its result correctly.
    return _band_protocol(prompt_name) + fill(template, **values)
