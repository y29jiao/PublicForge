"""Build the right framework adapter for each content agent (plan §7, §12.3).

Cross-framework split:
  * SPINE = pydantic-ai (`PydanticAIAdapter`) for analysis, topic_strategy,
    editorial, drafting, brand_review, final_editor.
  * 2nd FRAMEWORK = LangGraph (`LangGraphAdapter`, with `ChatOpenAI`) for the one
    isolated, single-shot agent: compliance_review.

Both run a GPT model — the cross-framework requirement is met at the framework
layer, not the model layer. Each agent's instructions come from its prompts/*.md
file, supplied as the adapter's `custom_section` (built in system_prompts.py).
"""

from __future__ import annotations

import json

from band.adapters import LangGraphAdapter, PydanticAIAdapter
from band.core.protocols import AgentToolsProtocol
from pydantic_ai import RunContext

from band_app.config import get_agent_id
from band_app.system_prompts import HANDOFF_NEXT_BY_AGENT, build_system_prompt
from common.llm import is_reasoning_model, model_for, reasoning_effort_for, temperature_for


def _pydantic_model(model_name: str):
    """Build the pydantic-ai model string.

    Note on reasoning models (gpt-5.x): we do NOT set an explicit reasoning_effort here.
    Our agents use a function tool (handoff_result); OpenAI rejects "function tools +
    reasoning_effort" on Chat Completions, and the Responses-API alternative conflicts
    with this framework's tool-only output handling (the model loops calling the tool
    and never returns the final text pydantic-ai expects). gpt-5.x defaults to MEDIUM
    reasoning, so the net behavior matches what we want. No temperature is sent (the
    adapter doesn't set one), which reasoning models require.
    """
    return "openai:" + model_name

# RunContext / AgentToolsProtocol must be importable at MODULE scope: with
# `from __future__ import annotations`, pydantic-ai resolves the handoff tool's
# annotations from this module's globals (not the closure), so a local import
# would raise NameError when the tool is registered.


def _schema_error(agent_key: str, obj) -> str | None:
    """Validate an agent's result against the shape the orchestrator needs.

    Returns a precise error message (so the LLM can self-correct and re-call
    handoff_result) or None if the payload is acceptable. Only the critical fields
    the deterministic router reads are enforced; everything else stays flexible.
    """
    if not isinstance(obj, dict):
        return "result_json 必须是一个 JSON 对象（{...}）。"

    if agent_key == "topic_strategy":
        cands = obj.get("candidates")
        if not isinstance(cands, list) or not cands:
            return "必须包含非空数组 candidates；每个候选至少有 id 和 title 字段。"
        if not all(isinstance(c, dict) and c.get("id") and c.get("title") for c in cands):
            return "candidates 里每一项都必须含有非空的 id 和 title。"

    elif agent_key == "editorial":
        if not isinstance(obj.get("scored"), list) or not obj["scored"]:
            return "必须包含非空数组 scored（每个候选的逐项打分）。"
        if not isinstance(obj.get("top_score"), (int, float)):
            return "必须包含数值字段 top_score（推荐候选的加权总分，数字类型）。"
        if not obj.get("recommended_id"):
            return "必须包含 recommended_id（推荐候选的 id）。"

    elif agent_key == "drafting":
        title, body = obj.get("title"), obj.get("body")
        if not isinstance(title, str) or not title.strip():
            return "必须包含非空字符串 title。你收到的 @ 消息里有 chosen_topic，请据此写作。"
        if not isinstance(body, str) or len(body.strip()) < 80:
            return "必须包含 body（完整中文正文，至少 80 字）。你收到的消息里有 chosen_topic，请据此成文。"

    elif agent_key == "final_editor":
        decision = obj.get("decision")
        if decision not in ("approve", "rewrite"):
            return "decision 必须是 'approve' 或 'rewrite'。"
        bi = obj.get("blocking_issues")
        if not isinstance(bi, (int, list)):
            return "blocking_issues 必须是整数（blocking 问题的数量）。"
        if decision == "approve":
            final = obj.get("final")
            if not isinstance(final, dict) or not final.get("title") or not final.get("body"):
                return "decision=approve 时必须给出 final 对象，至少含非空 title 和 body。"

    return None


def _make_handoff_tool(agent_key: str):
    """A deterministic 'handoff_result' tool: the LLM supplies only its JSON; the
    code attaches the correct next-teammate @mention AND validates the schema.

    Two reliability fixes in one tool:
      * the LLM never chooses a mention (code attaches the next teammate) — no more
        cannot_mention_self;
      * the result is schema-checked before handoff; on mismatch we return a precise
        error so the model self-corrects, instead of passing junk downstream.
    """
    next_id = get_agent_id(HANDOFF_NEXT_BY_AGENT[agent_key])
    orch_id = get_agent_id("orchestrator")
    # Always cc the orchestrator so it can OBSERVE the handoff. Band's list_messages
    # only returns messages that @mention the querying agent, so without this cc the
    # orchestrator never sees direct agent->agent handoffs (analysis->topic_strategy,
    # topic_strategy->editorial) and loses their output (e.g. the candidate topics).
    mentions = [next_id] if next_id == orch_id else [next_id, orch_id]

    async def handoff_result(ctx: RunContext[AgentToolsProtocol], result_json: str) -> str:
        """交付你的最终 JSON 结果给流程的下一棒（系统会自动 @ 正确对象，你无需指定）。

        result_json: 你产出的纯 JSON 字符串（你的本职结果），下一棒需要的上下文请一并放进去。
        """
        try:
            obj = json.loads(result_json)
        except (json.JSONDecodeError, TypeError):
            return "❌ result_json 不是合法 JSON 字符串，请修正后重新调用 handoff_result。"
        err = _schema_error(agent_key, obj)
        if err:
            return f"❌ 你的结果不符合要求，请修正后重新调用 handoff_result。问题：{err}"
        await ctx.deps.send_message(result_json, mentions)
        return "已交付给下一棒。任务完成，停止。"

    return handoff_result


class _HandoffPydanticAIAdapter(PydanticAIAdapter):
    """PydanticAIAdapter that exposes ONLY the handoff_result tool.

    gpt-4.1, given the full band_* toolset, flails: it calls band_send_message
    (mentioning itself), band_create_chatroom, band_add_participant… in a loop.
    Stripping every platform tool but our deterministic handoff_result leaves the
    model exactly one way to act, which makes the through-Band handoff reliable.
    """

    _KEEP_TOOLS = {"handoff_result"}

    def _create_agent(self):
        agent = super()._create_agent()
        toolset = agent._function_toolset
        for name in list(toolset.tools.keys()):
            if name not in self._KEEP_TOOLS:
                del toolset.tools[name]
        # Reasoning models (gpt-5.x) sometimes call the handoff tool without emitting a
        # final text turn; pydantic-ai's default of 1 output/tool retry then aborts the
        # run. Raise the limits so the model gets enough turns to complete the handoff.
        for attr in ("_max_output_retries", "_max_tool_retries"):
            if hasattr(agent, attr):
                setattr(agent, attr, 5)
        return agent

# agent key -> its prompts/ file stem (scraper/orchestrator have no LLM prompt).
PROMPT_FILE = {
    "analysis": "analysis",
    "topic_strategy": "topic_strategy",
    "editorial": "editorial_scoring",
    "drafting": "drafting",
    "brand_review": "brand_review",
    "compliance_review": "compliance_review",
    "final_editor": "final_editor",
}

# agent key -> which model role it uses (see settings.yaml `models`).
MODEL_ROLE = {
    "analysis": "default",         # generator/extractor
    "topic_strategy": "default",   # generator
    "drafting": "drafting",        # long-form writer
    "editorial": "judge",          # judge
    "brand_review": "judge",       # judge
    "final_editor": "judge",       # judge
    # compliance_review is a judge too, but built below via the 2nd framework.
}


def build_adapter(agent_key: str):
    """Return a ready framework adapter for the given content-agent key."""
    section = build_system_prompt(PROMPT_FILE[agent_key])

    # --- the SECOND framework: compliance_review on LangGraph + ChatOpenAI ---
    if agent_key == "compliance_review":
        from langchain_openai import ChatOpenAI
        from langgraph.checkpoint.memory import InMemorySaver

        judge = model_for("judge")
        if is_reasoning_model(judge):
            # Reasoning model: only the default temperature (1) is allowed, and we don't
            # set reasoning_effort (tools + reasoning_effort is rejected on Chat
            # Completions). gpt-5.x defaults to medium reasoning.
            llm = ChatOpenAI(model=judge, temperature=1)
        else:
            llm = ChatOpenAI(model=judge, temperature=temperature_for("scoring"))
        return LangGraphAdapter(
            llm=llm,
            checkpointer=InMemorySaver(),
            custom_section=section,
            inject_system_prompt=True,
        )

    # --- the spine: every other content agent on pydantic-ai ---
    return _HandoffPydanticAIAdapter(
        model=_pydantic_model(model_for(MODEL_ROLE[agent_key])),
        custom_section=section,
        additional_tools=[_make_handoff_tool(agent_key)],
    )
