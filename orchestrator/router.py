"""The deterministic orchestrator — a plain rule-based state machine (plan §4).

No LLM. Given the same state it always picks the same next step. It only reads
the structured numbers/flags the agents already produced (top_score,
blocking_issues, decision, human_decision) — it never re-judges quality itself.

This file has two parts:
  1. `decide_next(state)` — the pure routing table + the three safety guards.
     One input (the state), one output (where to go and why). Reusable by both
     the offline runner below and the Band orchestrator (orchestrator drives via
     @mentions using exactly this decision).
  2. `run_offline(...)` — the offline driver (build-order milestones 2 & 3): it
     runs the agent for the current state, then asks decide_next where to go, and
     repeats. Includes the human step and both send-back loops.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from agents import (
    analysis,
    brand_review,
    compliance_review,
    drafting,
    editorial,
    final_editor,
    scraper,
    topic_strategy,
)
from common.loaders import load_settings
from state.article_state import ArticleState


@dataclass
class Decision:
    """Where the router sends control next, and the one-line reason (for the log)."""
    next_state: str
    reason: str


# ----------------------------------------------------------------------------
# Part 1 — the pure routing decision (the table in plan §4 + the three guards).
# ----------------------------------------------------------------------------

def decide_next(state: ArticleState) -> Decision:
    """Return the next state given the current state and the fields agents emitted.

    Every branch is written out explicitly, including the else (guard #3). When a
    required field is missing (an agent errored), we DO NOT guess — we stay put and
    mark it waiting (guard #2).
    """
    settings = load_settings()
    threshold = float(settings["score_threshold"])
    cap = int(settings["rewrite_cap"])
    s = state.status

    if s == "idle":
        return Decision("scrape", "new task → scrape")

    if s == "scrape":
        if not state.samples:
            return Decision("scrape", "guard: samples missing → wait")
        return Decision("analysis", "samples ready → analysis")

    if s == "analysis":
        if state.analysis is None:
            return Decision("analysis", "guard: analysis missing → wait")
        return Decision("topic_strategy", "analysis ready → topic_strategy")

    if s == "topic_strategy":
        if not state.candidates:
            return Decision("topic_strategy", "guard: candidates missing → wait")
        return Decision("editorial", "candidates ready → editorial")

    if s == "editorial":
        if state.top_score is None:
            return Decision("editorial", "guard: top_score missing → wait")
        if state.top_score >= threshold:
            return Decision("human_review", f"top_score {state.top_score} >= {threshold} → human_review")
        # Loop A: below threshold. Loop guard first (guard #1).
        if state.topic_revision_count >= cap:
            return Decision("human_review", f"loop A cap {cap} reached → human_review")
        return Decision("topic_strategy", f"top_score {state.top_score} < {threshold} → topic_strategy (Loop A)")

    if s == "human_review":
        if state.human_decision is None:
            return Decision("human_review", "guard: awaiting human decision → wait")
        if state.human_decision == "approved":
            return Decision("drafting", "human approved → drafting")
        # rejected → Loop A. Loop guard first.
        if state.topic_revision_count >= cap:
            return Decision("human_review", f"loop A cap {cap} reached → human_review")
        return Decision("topic_strategy", "human rejected → topic_strategy (Loop A)")

    if s == "drafting":
        if state.draft is None:
            return Decision("drafting", "guard: draft missing → wait")
        return Decision("review", "draft ready → review")

    if s == "review":
        # Both reviewers must have posted before we move on (guard #2).
        if len(state.review_thread) < 2:
            return Decision("review", "guard: both reviews not in yet → wait")
        return Decision("final", "reviews in → final")

    if s == "final":
        if state.blocking_issues is None:
            return Decision("final", "guard: final decision missing → wait")
        if state.blocking_issues == 0:
            return Decision("done", "no blocking issues → done")
        # Loop B: rewrite needed. Loop guard first.
        if state.draft_revision_count >= cap:
            return Decision("human_review", f"loop B cap {cap} reached → human_review")
        return Decision("drafting", f"{state.blocking_issues} blocking → drafting (Loop B)")

    if s == "done":
        return Decision("done", "already done")

    # Unknown state — never guess.
    return Decision(s, f"guard: unknown state '{s}' → wait")


# ----------------------------------------------------------------------------
# Part 2 — the offline driver (milestones 2 & 3). Runs agents, applies decisions.
# ----------------------------------------------------------------------------

# A human decider takes the scored state and returns a structured reply string,
# e.g. "approve: topic_2" or "reject: 选题太宽泛". Injected so the CLI / Band / a
# test can each supply the human differently. The router parses it deterministically.
HumanDecider = Callable[[ArticleState], str]


def parse_human_reply(reply: str) -> tuple[str, str]:
    """Parse the fixed-format human reply into (decision, remainder).

    Format: 'approve: <topic_id>' or 'reject: <reason>'. Plain string logic only —
    never let an LLM interpret the human's text into a routing decision (plan §6).
    """
    head, _, tail = reply.partition(":")
    head = head.strip().lower()
    tail = tail.strip()
    if head == "approve":
        return "approved", tail
    if head == "reject":
        return "rejected", tail
    # Anything else is not a valid routing signal — treat as "stay and wait".
    return "invalid", tail


def _log(transition_log: list[str], frm: str, dec: Decision) -> None:
    """Record + print one line per transition (guard #3: every run is traceable)."""
    line = f"[router] {frm} -> {dec.next_state}  ({dec.reason})"
    transition_log.append(line)
    print(line)


def _run_agent_for(state: ArticleState) -> None:
    """Execute the agent(s) whose turn it is in the current state."""
    s = state.status
    if s == "scrape":
        scraper.run(state)
    elif s == "analysis":
        analysis.run(state)
    elif s == "topic_strategy":
        topic_strategy.run(state)
    elif s == "editorial":
        editorial.run(state)
    elif s == "drafting":
        drafting.run(state)
    elif s == "review":
        # Brand (spine) and Compliance (2nd framework) both post into the thread.
        brand_review.run(state)
        compliance_review.run(state)
    elif s == "final":
        final_editor.run(state)
    # idle / human_review / done do no agent work here.


def apply_human_reply(state: ArticleState, reply: str) -> None:
    """Record a human's structured reply ('approve: topic_x' / 'reject: ...') into state.

    Shared by the offline runner and the Band orchestrator so the human decision is
    parsed exactly one way (plain string logic, never an LLM — plan §6).
    """
    decision, remainder = parse_human_reply(reply)
    state.human_decision = decision if decision in ("approved", "rejected") else None
    if decision == "approved":
        # The human names the topic id; pin it as the chosen topic.
        chosen_id = remainder or (state.scores or {}).get("recommended_id", "")
        state.chosen_topic = find_candidate(state, chosen_id)
        state.human_reason = ""
    elif decision == "rejected":
        # Free-text reason is carried into Loop A as feedback context only.
        state.human_reason = remainder


def _apply_human(state: ArticleState, human: HumanDecider) -> None:
    """Offline helper: ask the injected decider, then record its reply."""
    apply_human_reply(state, human(state))


def find_candidate(state: ArticleState, topic_id: str) -> dict | None:
    """Look up a candidate by id; fall back to the recommended one."""
    for c in state.candidates or []:
        if c.get("id") == topic_id:
            return c
    rec = (state.scores or {}).get("recommended_id", "")
    for c in state.candidates or []:
        if c.get("id") == rec:
            return c
    return (state.candidates or [None])[0]


def enter_loop_a(state: ArticleState, from_human: bool) -> None:
    """Set up a Loop-A re-entry: feedback, counter, and clearing downstream fields."""
    state.topic_revision_count += 1
    if from_human and state.human_reason:
        state.rejection_feedback = state.human_reason
    else:
        # Below-threshold reject: feed back which dimensions were weak.
        state.rejection_feedback = _weak_dimensions_feedback(state)
    state.reset_for_new_topics()


def enter_loop_b(state: ArticleState) -> None:
    """Set up a Loop-B re-entry: keep feedback + chosen topic, clear draft/reviews."""
    state.draft_revision_count += 1
    # review_feedback was already set by the final editor.
    state.reset_for_new_draft()


def loop_kind(from_state: str, next_state: str) -> str:
    """Classify a transition so both drivers set up loops identically.

    Returns 'loop_a' (regenerate topics), 'loop_b' (rewrite draft), or 'normal'.
    """
    if next_state == "topic_strategy" and from_state in ("editorial", "human_review"):
        return "loop_a"
    if next_state == "drafting" and from_state == "final":
        return "loop_b"
    return "normal"


def _weak_dimensions_feedback(state: ArticleState) -> str:
    """Summarize the recommended candidate's weak (score==1) dimensions for Loop A."""
    scores = state.scores or {}
    rec_id = scores.get("recommended_id", "")
    for item in scores.get("scored", []):
        if item.get("id") == rec_id:
            weak = [d.get("name", "") for d in item.get("dimensions", []) if d.get("score") == 1]
            if weak:
                return "上一轮选题在以下维度偏弱，请针对性改进：" + "、".join(weak)
    return "上一轮选题整体得分不足，请产出更强的新选题。"


def run_offline(state: ArticleState, human: HumanDecider, *, max_steps: int = 40) -> ArticleState:
    """Drive the whole flow offline: run agent → decide_next → advance, until done.

    `human` supplies the structured approve/reject reply when we reach human_review.
    `max_steps` is a final backstop so a misconfiguration can never spin forever.
    """
    transition_log: list[str] = []
    state.status = "idle"

    for _ in range(max_steps):
        # 1. Do the work for the current state (agents post their structured results).
        _run_agent_for(state)

        # 2. The human step is special: collect the structured reply before deciding.
        if state.status == "human_review" and state.human_decision is None:
            _apply_human(state, human)

        # 3. Ask the deterministic router where to go next.
        decision = decide_next(state)

        # 4. A guard that returns the same state means "wait" — stop the offline run.
        if decision.next_state == state.status and state.status not in ("idle",):
            _log(transition_log, state.status, decision)
            print(f"[router] holding in '{state.status}' (waiting/guard). Stopping offline run.")
            break

        # 5. If we are taking a loop branch, set it up (counter + feedback + reset).
        kind = loop_kind(state.status, decision.next_state)
        if kind == "loop_a":
            enter_loop_a(state, from_human=(state.status == "human_review"))
        elif kind == "loop_b":
            enter_loop_b(state)

        _log(transition_log, state.status, decision)
        state.status = decision.next_state

        if state.status == "done":
            break

    return state
