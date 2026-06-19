"""The deterministic orchestrator as a Band participant (plan §6, §12.6).

This is the rule-code controller — NO LLM. It:
  1. creates one room for the article and adds the 8 content agents + the human,
  2. runs the scraper directly and posts its output (plan §12.5),
  3. drives the state machine: @mention the next agent, wait for its JSON reply,
     write the structured fields into shared state, then call `decide_next`,
  4. handles the human approval step and both send-back loops with the 3 guards.

All routing decisions come from orchestrator/router.py — the exact same logic the
offline runner uses, so Band changes only the transport, never the control flow.
"""

from __future__ import annotations

import asyncio
import json
import os

from dotenv import load_dotenv

from band.config import load_agent_config

from band_app.client import BandRoom
from band_app.config import LLM_AGENT_KEYS, get_agent_id, get_handle
from band_app.messages import build_mention_for, parse_json_reply
from common.console import setup_utf8
from orchestrator.router import (
    apply_human_reply,
    decide_next,
    enter_loop_a,
    enter_loop_b,
    loop_kind,
)
from agents import scraper
from state.article_state import ArticleState

POLL_SECONDS = 3.0          # how often to poll the room for new messages
REPLY_TIMEOUT_SECONDS = 600  # how long to wait for one agent's reply before giving up

# Map each agent's platform id back to its key, so we can match replies by sender.
AGENT_ID_TO_KEY = {get_agent_id(k): k for k in LLM_AGENT_KEYS}
ALL_AGENT_IDS = set(AGENT_ID_TO_KEY) | {get_agent_id("scraper"), get_agent_id("orchestrator")}


class BandOrchestrator:
    """Drives one article through the room until 'done' (or a guard hands off)."""

    def __init__(self, state: ArticleState) -> None:
        self.state = state
        self.room: BandRoom | None = None
        self._seen = 0  # how many room messages we have already consumed

    # --- room setup ---------------------------------------------------------

    async def setup_room(self) -> None:
        _, api_key = load_agent_config("orchestrator")
        self.room = BandRoom(api_key=api_key)
        await self.room.create_room(title=f"内容引擎 · {self.state.article_id}")
        # Bring the content agents into the room (they are already connected & idle).
        for key in LLM_AGENT_KEYS:
            await self.room.add_participant(get_agent_id(key))
        # Add the human approver if a participant id is configured.
        human_id = os.getenv("HUMAN_PARTICIPANT_ID")
        if human_id:
            await self.room.add_participant(human_id)
        print(f"[orchestrator] room {self.room.room_id} ready with {len(LLM_AGENT_KEYS)} agents.")

    # --- low-level: wait for the next room message from a given sender -------

    async def _wait_for(self, predicate) -> str:
        """Poll the room until a NEW message satisfies predicate(msg); return its content."""
        waited = 0.0
        while waited < REPLY_TIMEOUT_SECONDS:
            messages = await self.room.list_messages()
            # Only look at messages we have not consumed yet.
            for msg in messages[self._seen:]:
                self._seen += 1
                if predicate(msg):
                    return msg.content
            await asyncio.sleep(POLL_SECONDS)
            waited += POLL_SECONDS
        raise TimeoutError("timed out waiting for a room reply")

    async def _ask_agent(self, agent_key: str) -> dict:
        """@mention one agent, wait for its reply, and return the parsed JSON."""
        content = build_mention_for(agent_key, self.state)
        await self.room.mention(content, get_agent_id(agent_key), get_handle(agent_key))
        target_id = get_agent_id(agent_key)
        reply = await self._wait_for(lambda m: m.sender_id == target_id)
        return parse_json_reply(reply)

    # --- per-state work: post the task, ingest the structured reply ---------

    async def _do_state(self, status: str) -> None:
        if status == "scrape":
            # No LLM: run the scraper in code and post a short summary for the room.
            scraper.run(self.state)
            await self.room.post(f"[scraper] 已载入样本数据：{len(self.state.samples)} 所高校。")

        elif status == "analysis":
            self.state.analysis = await self._ask_agent("analysis")

        elif status == "topic_strategy":
            result = await self._ask_agent("topic_strategy")
            self.state.candidates = result.get("candidates", [])

        elif status == "editorial":
            scores = await self._ask_agent("editorial")
            self.state.scores = scores
            self.state.top_score = float(scores.get("top_score", 0))

        elif status == "drafting":
            self.state.draft = await self._ask_agent("drafting")

        elif status == "review":
            # Brand (spine) + Compliance (2nd framework) both post into the thread.
            self.state.review_thread.append(await self._ask_agent("brand_review"))
            self.state.review_thread.append(await self._ask_agent("compliance_review"))

        elif status == "final":
            result = await self._ask_agent("final_editor")
            self.state.blocking_issues = int(result.get("blocking_issues", 0))
            if result.get("decision") == "approve":
                self.state.output = result.get("final")
                self.state.review_feedback = ""
            else:
                self.state.review_feedback = result.get("rewrite_feedback", "")

        elif status == "human_review":
            await self._do_human_review()

    async def _do_human_review(self) -> None:
        """@mention the human, then wait for their fixed-format reply in the room."""
        rec = (self.state.scores or {}).get("recommended_id", "")
        prompt = (
            f"@人工 请确认选题。推荐：{rec}，top_score={self.state.top_score}。\n"
            "请在房间内用固定格式回复：`approve: topic_2` 或 `reject: <理由>`。"
        )
        human_handle = os.getenv("HUMAN_HANDLE")
        human_id = os.getenv("HUMAN_PARTICIPANT_ID")
        if human_id and human_handle:
            await self.room.mention(prompt, human_id, human_handle)
        else:
            await self.room.post(prompt)

        # The human reply is a message from a non-agent sender in the fixed format.
        def is_human_decision(msg) -> bool:
            text = (msg.content or "").strip().lower()
            return msg.sender_id not in ALL_AGENT_IDS and (
                text.startswith("approve:") or text.startswith("reject:")
            )

        reply = await self._wait_for(is_human_decision)
        apply_human_reply(self.state, reply)

    # --- the main loop ------------------------------------------------------

    async def run(self, *, max_steps: int = 40) -> ArticleState:
        await self.setup_room()
        self.state.status = "idle"

        for _ in range(max_steps):
            # 1. Do the work for the current state (agents post results into the room).
            await self._do_state(self.state.status)

            # 2. Ask the deterministic router where to go next.
            decision = decide_next(self.state)
            line = f"[router] {self.state.status} -> {decision.next_state}  ({decision.reason})"
            print(line)
            await self.room.post(line)  # routing is visible in the room (audit trail)

            # 3. A guard that returns the same state means "wait" — stop here.
            if decision.next_state == self.state.status and self.state.status != "idle":
                print(f"[orchestrator] holding in '{self.state.status}' (guard). Stopping.")
                break

            # 4. Set up a loop branch if we are taking one.
            kind = loop_kind(self.state.status, decision.next_state)
            if kind == "loop_a":
                enter_loop_a(self.state, from_human=(self.state.status == "human_review"))
            elif kind == "loop_b":
                enter_loop_b(self.state)

            self.state.status = decision.next_state
            if self.state.status == "done":
                await self.room.post("[orchestrator] 完成。最终稿已产出。")
                break

        return self.state


async def main_async() -> None:
    load_dotenv()
    from common.loaders import load_settings

    state = ArticleState(article_id="band-demo-001", direction=load_settings()["direction"])
    orchestrator = BandOrchestrator(state)
    await orchestrator.run()

    print("\n================ 最终结果 (Final) ================")
    if state.output:
        print(json.dumps(state.output, ensure_ascii=False, indent=2))
    else:
        print(f"status={state.status} (no final package; flow stopped before 'done')")


def main() -> None:
    setup_utf8()
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
