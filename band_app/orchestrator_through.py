"""True through-Band orchestrator: agents collaborate by handing off over Band.

Unlike the in-process hybrid (which computed everything locally and mirrored it to
Band), here each content agent is a real Band remote agent that does its own LLM
work and delivers its result THROUGH Band:

  * Creative chain is direct agent->agent handoff over Band:
        analysis ──▶ topic_strategy ──▶ editorial
    (each calls its handoff_result tool, which @mentions the next agent).
  * The orchestrator is a Band participant that COORDINATES the parts that need
    cross-agent context or a decision — it never generates content:
        - editorial ▶ orchestrator: score threshold + human approval
        - drafting/brand/compliance ▶ orchestrator: fan the draft out to each
          reviewer and then hand the draft + BOTH reviews to the final editor
        - final ▶ orchestrator: 0 blocking -> done, else rewrite loop (cap) / human

Everything moves as Band messages; the orchestrator drives by @mentioning the next
participant, exactly the collaboration layer the challenge asks for.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

log = logging.getLogger("through")  # flows into band_run.log when the web UI is driving

from band.config import load_agent_config

from band_app.client import BandRoom
from band_app.config import CONTENT_AGENT_KEYS, get_agent_id, get_handle
from band_app.messages import build_mention_for, parse_json_reply
from common.loaders import load_settings
from orchestrator.router import (
    apply_human_reply, enter_loop_a, enter_loop_b, find_candidate,
)
from state.article_state import ArticleState

POLL_SECONDS = 2.0
OVERALL_TIMEOUT_SECONDS = 1200   # whole-run backstop
STEP_TIMEOUT_SECONDS = 180       # per-step: if an agent doesn't reply in time, retry once


def _as_count(value) -> int:
    """Coerce a 'blocking_issues' field that agents emit in varied shapes.

    Conversational agents return it as an int, a list of issues, or even a dict —
    we need a count. Be tolerant rather than crash the whole run.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        return len(value)
    if isinstance(value, str):
        s = value.strip()
        return int(s) if s.isdigit() else (0 if s in ("", "0", "none", "无") else 1)
    return 0


def _derive_top_score(scores: dict) -> float:
    """Get top_score from the editorial result, deriving it if the field is absent.

    Agents sometimes omit the top-level top_score; fall back to the best per-item
    weighted_total / total in `scored`, so a valid scoring isn't lost to a missing key.
    """
    if not isinstance(scores, dict):
        return 0.0
    raw = scores.get("top_score")
    if isinstance(raw, (int, float)):
        return float(raw)
    best = 0.0
    for item in scores.get("scored", []) or []:
        if not isinstance(item, dict):
            continue
        for k in ("weighted_total", "total", "score", "weighted_score"):
            v = item.get(k)
            if isinstance(v, (int, float)):
                best = max(best, float(v))
    return best

# A human decider: given the scored state, return "approve: topic_x" / "reject: ...".
HumanDecider = Callable[[ArticleState], Awaitable[str]]


class BandThroughOrchestrator:
    """Drives one article to a final package via real Band handoffs."""

    def __init__(self, state: ArticleState, human: HumanDecider,
                 *, on_input=None, on_output=None, on_event=None,
                 agent_alive=None) -> None:
        self.state = state
        self.human = human
        self.settings = load_settings()
        self.threshold = float(self.settings["score_threshold"])
        self.cap = int(self.settings["rewrite_cap"])
        self.room: BandRoom | None = None
        self._seen_ids: set = set()   # message ids already handled (order-independent)
        self._id_to_key = {get_agent_id(k): k for k in CONTENT_AGENT_KEYS}
        # per-step stall detection (an agent can hang with no error → flow waits forever)
        self._waited = 0.0
        self._progress_at = 0.0       # self._waited value at the last sign of progress
        self._retried = False         # whether we already re-mentioned the current step
        self._last_mentioned: str | None = None
        self._orch_id = get_agent_id("orchestrator")
        self.finished = False
        # UI hooks (all optional): report inputs we post, outputs we receive, events.
        self.on_input = on_input or (lambda key, content: None)
        self.on_output = on_output or (lambda key, parsed: None)
        self.on_event = on_event or (lambda line: None)
        # Liveness probe (optional): given an agent key, return False if its Band
        # remote process has died. Lets us fall back immediately instead of waiting
        # out the full step timeout for an agent that will never reply.
        self.agent_alive = agent_alive or (lambda key: True)

    # --- low-level helpers --------------------------------------------------

    async def _mention(self, agent_key: str) -> None:
        """@mention an agent with the task/context it needs (the orchestrator's job)."""
        content = build_mention_for(agent_key, self.state)
        self.on_input(agent_key, content)
        await self.room.mention(content, get_agent_id(agent_key), get_handle(agent_key))
        self._last_mentioned = agent_key   # the agent we now expect a reply from
        self._event(f"[router] orchestrator @mentions {agent_key}")

    async def _post(self, content: str) -> None:
        await self.room.post(content)

    def _event(self, line: str) -> None:
        self.on_event(line)
        print(line)

    # --- room setup + kickoff ----------------------------------------------

    async def setup_room(self) -> None:
        _, api_key = load_agent_config("orchestrator")
        self.room = BandRoom(api_key=api_key)
        await self.room.create_room(title=f"内容引擎 · 通过Band协作 · {self.state.article_id}")
        for key in CONTENT_AGENT_KEYS:
            try:
                await self.room.add_participant(get_agent_id(key))
            except Exception as exc:
                self._event(f"[warn] add {key} failed: {type(exc).__name__}")
        self._event(f"[orchestrator] room {self.room.room_id} ready; {len(CONTENT_AGENT_KEYS)} agents.")

    # --- react to each agent's handoff -------------------------------------

    async def _handle_agent_message(self, agent_key: str, parsed: dict) -> None:
        """One agent just delivered its result over Band — record it and coordinate."""
        # A message arrived = progress; reset the per-step stall timer.
        self._progress_at = self._waited
        self._retried = False
        self.on_output(agent_key, parsed)

        if agent_key == "analysis":
            self.state.analysis = parsed
            # analysis hands directly to topic_strategy over Band; we don't act, but
            # track that topic_strategy is now the agent we're waiting on (so the
            # stall/liveness guard watches the real baton, not the previous step).
            self._last_mentioned = "topic_strategy"

        elif agent_key == "topic_strategy":
            self.state.candidates = parsed.get("candidates", parsed if isinstance(parsed, list) else [])
            # topic_strategy hands directly to editorial; track editorial as next.
            self._last_mentioned = "editorial"

        elif agent_key == "editorial":
            self.state.scores = parsed
            self.state.top_score = _derive_top_score(parsed)
            await self._decide_after_editorial()

        elif agent_key == "drafting":
            self.state.draft = parsed
            self._event("[router] draft ready -> fan out to brand_review")
            await self._mention("brand_review")

        elif agent_key == "brand_review":
            self.state.review_thread.append(parsed)
            self._event("[router] brand review in -> compliance_review")
            await self._mention("compliance_review")

        elif agent_key == "compliance_review":
            self.state.review_thread.append(parsed)
            self._event("[router] compliance review in -> final_editor")
            await self._mention("final_editor")

        elif agent_key == "final_editor":
            self.state.blocking_issues = _as_count(parsed.get("blocking_issues", 0))
            if parsed.get("decision") == "approve":
                self.state.output = parsed.get("final")
                self.state.review_feedback = ""
            else:
                self.state.review_feedback = parsed.get("rewrite_feedback", "")
            await self._decide_after_final()

    async def _decide_after_editorial(self) -> None:
        if self.state.top_score >= self.threshold:
            self._event(f"[router] top_score {self.state.top_score} >= {self.threshold} -> human_review")
            await self._human_then_draft()
            return
        if self.state.topic_revision_count >= self.cap:
            self._event(f"[router] loop A cap {self.cap} reached -> human_review")
            await self._human_then_draft()
            return
        self._event(f"[router] top_score {self.state.top_score} < {self.threshold} -> topic_strategy (Loop A)")
        enter_loop_a(self.state, from_human=False)
        await self._mention("topic_strategy")

    async def _human_then_draft(self) -> None:
        await self._post(
            f"[orchestrator] @人工 请确认选题：推荐 "
            f"{(self.state.scores or {}).get('recommended_id')}，top_score={self.state.top_score}"
        )
        reply = await self.human(self.state)
        self._event(f"[human] {reply}")
        apply_human_reply(self.state, reply)
        if self.state.human_decision == "approved":
            if not self.state.chosen_topic:
                rec = (self.state.scores or {}).get("recommended_id", "")
                self.state.chosen_topic = find_candidate(self.state, rec)
            self._event("[router] human approved -> drafting")
            await self._mention("drafting")
        else:
            if self.state.topic_revision_count >= self.cap:
                self._event(f"[router] loop A cap {self.cap} reached -> stop")
                self.finished = True
                return
            self._event("[router] human rejected -> topic_strategy (Loop A)")
            enter_loop_a(self.state, from_human=True)
            await self._mention("topic_strategy")

    async def _decide_after_final(self) -> None:
        if self.state.blocking_issues == 0:
            self._event("[router] no blocking issues -> done")
            await self._post("[orchestrator] 完成。最终稿已产出。")
            self.finished = True
            return
        if self.state.draft_revision_count >= self.cap:
            self._event(f"[router] loop B cap {self.cap} reached -> stop")
            self.finished = True
            return
        self._event(f"[router] {self.state.blocking_issues} blocking -> drafting (Loop B)")
        enter_loop_b(self.state)
        await self._mention("drafting")

    # --- local fallback (reliability net) -----------------------------------

    def _local_run(self, agent_key: str):
        """Run this step's PROVEN in-process logic (same functions main.py uses).

        Imported lazily so a plain through-Band run never pays for them. Each
        agent's run(state) reads what it needs from state (already populated by
        the orchestrator) and writes its result back into state.
        """
        from agents import (analysis, brand_review, compliance_review, drafting,
                            editorial, final_editor, topic_strategy)
        runners = {
            "analysis": analysis.run, "topic_strategy": topic_strategy.run,
            "editorial": editorial.run, "drafting": drafting.run,
            "brand_review": brand_review.run,
            "compliance_review": compliance_review.run,
            "final_editor": final_editor.run,
        }
        runner = runners.get(agent_key)
        if runner is None:
            raise ValueError(f"no local fallback for agent '{agent_key}'")
        return runner(self.state)

    async def _run_local_fallback(self, agent_key: str) -> None:
        """A Band agent didn't deliver (stalled or died) — compute the step in
        process so the run still produces an article. Band stays the collaboration
        layer for every step that DOES respond; this is only the safety net."""
        self._event(f"[fallback] {agent_key} 未经 Band 响应，改用进程内逻辑计算该步骤")
        try:
            # Off-thread: the in-process logic makes a blocking LLM call.
            result = await asyncio.to_thread(self._local_run, agent_key)
        except Exception as exc:
            self._event(f"[error] 本地兜底 {agent_key} 失败: {type(exc).__name__}: {exc}")
            self.finished = True
            return
        # Mirror the result back into the room so Band still shows the full thread.
        await self._post(f"[fallback:{agent_key}] {json.dumps(result, ensure_ascii=False)}")
        # Complete the pending call record for this step in the UI.
        self.on_output(agent_key, result if isinstance(result, dict)
                                  else {"candidates": result})
        # Clear stall tracking: this step is done, the next @mention starts fresh.
        self._progress_at = self._waited
        self._retried = False
        self._last_mentioned = None
        await self._route_after_local(agent_key)

    async def _route_after_local(self, agent_key: str) -> None:
        """Drive the next step after a local fallback. The creative-chain agents
        normally hand off directly over Band; since we computed locally instead,
        the orchestrator @mentions the next agent itself."""
        if agent_key == "analysis":
            await self._mention("topic_strategy")
        elif agent_key == "topic_strategy":
            await self._mention("editorial")
        elif agent_key == "editorial":
            self.state.top_score = _derive_top_score(self.state.scores)
            await self._decide_after_editorial()
        elif agent_key == "drafting":
            self._event("[router] draft ready (local) -> fan out to brand_review")
            await self._mention("brand_review")
        elif agent_key == "brand_review":
            self._event("[router] brand review in (local) -> compliance_review")
            await self._mention("compliance_review")
        elif agent_key == "compliance_review":
            self._event("[router] compliance review in (local) -> final_editor")
            await self._mention("final_editor")
        elif agent_key == "final_editor":
            # final_editor.run already set blocking_issues/output/review_feedback.
            self.state.blocking_issues = _as_count(self.state.blocking_issues)
            await self._decide_after_final()

    # --- main loop ----------------------------------------------------------

    async def run(self) -> ArticleState:
        await self.setup_room()
        # Kick off the creative chain. analysis -> topic_strategy -> editorial run
        # as direct agent-to-agent handoffs over Band.
        self.state.status = "running"
        await self._mention("analysis")
        self._progress_at = self._waited = 0.0

        while not self.finished and self._waited < OVERALL_TIMEOUT_SECONDS:
            # Per-step stall guard. If the agent we're waiting on goes silent we
            # don't poll forever (an agent can hang with no error): if its process
            # has died, fall back at once; otherwise re-@mention once, and if it's
            # still silent after that, compute the step in-process so the run
            # always completes instead of aborting with no article.
            if self._last_mentioned:
                stalled = self._last_mentioned
                dead = not self.agent_alive(stalled)
                timed_out = (self._waited - self._progress_at) > STEP_TIMEOUT_SECONDS
                if dead or timed_out:
                    if dead:
                        self._event(f"[warn] {stalled} 进程已退出，立即启用本地兜底")
                        await self._run_local_fallback(stalled)
                    elif not self._retried:
                        self._retried = True
                        self._progress_at = self._waited
                        self._event(f"[warn] {stalled} {STEP_TIMEOUT_SECONDS}s 未回复，重新 @ 一次")
                        await self._mention(stalled)
                    else:
                        self._event(f"[warn] {stalled} 仍未响应（已重试），启用本地兜底")
                        await self._run_local_fallback(stalled)
                    if self.finished:
                        break
                    continue   # re-evaluate; fallback already advanced the flow
            messages = await self.room.list_messages()
            # Process in CHRONOLOGICAL order (list_messages may not be sorted), tracked
            # by message id — otherwise a later agent (editorial) can be handled before
            # an earlier one (topic_strategy) and we block at human before recording
            # candidates.
            ordered = sorted(messages, key=lambda m: getattr(m, "inserted_at", "") or "")
            log.info("poll: %d messages in room, %d already seen", len(ordered), len(self._seen_ids))
            for msg in ordered:
                mid = getattr(msg, "id", None) or id(msg)
                if mid in self._seen_ids:
                    continue
                self._seen_ids.add(mid)
                sender = getattr(msg, "sender_id", None)
                key = self._id_to_key.get(sender)
                if not key:
                    log.info("skip msg from non-agent sender %s", sender)
                    continue  # orchestrator's own posts / human / unknown
                try:
                    parsed = parse_json_reply(msg.content)
                except Exception as exc:
                    log.warning("UNPARSEABLE message from %s: %s | raw=%.200s", key, exc, msg.content)
                    self._event(f"[warn] {key} message wasn't parseable JSON; skipping")
                    continue
                keys = list(parsed.keys()) if isinstance(parsed, dict) else type(parsed).__name__
                log.info("handle message from %s; output keys=%s", key, keys)
                await self._handle_agent_message(key, parsed)
                if self.finished:
                    break
            if not self.finished:
                await asyncio.sleep(POLL_SECONDS)
                self._waited += POLL_SECONDS

        self.state.status = "done" if self.state.output else self.state.status
        return self.state
