"""The shared state object — one per article.

Every agent reads from this object and writes its result back into it. The
orchestrator (orchestrator/router.py) only reads the structured numbers/flags
stored here (top_score, blocking_issues, decision, human_decision) to decide
routing. See plan.md §5.

Readability rule (plan §0): this is a plain dataclass with explicit fields, no
magic. A person who did not write it should understand it at a glance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ArticleState:
    # --- identity / current position in the state machine ---
    article_id: str
    status: str = "idle"               # current router state (see plan §4 routing table)

    # --- our one-line direction / theme (MVP stand-in for "our own posting style") ---
    direction: str = ""

    # --- the standardized sample data the scraper produced (agent 1 output) ---
    samples: list[dict[str, Any]] = field(default_factory=list)

    # --- per-agent outputs, filled as the flow progresses ---
    analysis: Optional[dict[str, Any]] = None        # agent 2
    candidates: Optional[list[dict[str, Any]]] = None  # agent 3
    scores: Optional[dict[str, Any]] = None          # agent 4 (full scoring object)
    top_score: Optional[float] = None                # agent 4 — the router reads this
    chosen_topic: Optional[dict[str, Any]] = None    # filled after human approval
    draft: Optional[dict[str, Any]] = None           # agent 5
    review_thread: list[dict[str, Any]] = field(default_factory=list)  # agents 6 & 7
    blocking_issues: Optional[int] = None            # agent 8 — the router reads this
    output: Optional[dict[str, Any]] = None          # agent 8 final package

    # --- loop bookkeeping (the three router guards, plan §4) ---
    topic_revision_count: int = 0    # Loop A: how many times topics were regenerated
    draft_revision_count: int = 0    # Loop B: how many times the draft was rewritten

    # --- feedback carried back into a loop ---
    rejection_feedback: str = ""     # Loop A: why the topics were sent back
    review_feedback: str = ""        # Loop B: why the draft was sent back

    # --- human-in-the-loop ---
    human_decision: Optional[str] = None   # "approved" | "rejected" (parsed from structured reply)
    human_reason: str = ""                 # free-text reason (passed along as Loop-A feedback only)

    def reset_for_new_topics(self) -> None:
        """Loop A re-entry: clear everything downstream of topic generation."""
        self.candidates = None
        self.scores = None
        self.top_score = None
        self.chosen_topic = None
        self.human_decision = None
        self.human_reason = ""

    def reset_for_new_draft(self) -> None:
        """Loop B re-entry: clear the draft and its reviews, keep the chosen topic."""
        self.draft = None
        self.review_thread = []
        self.blocking_issues = None
        self.output = None
