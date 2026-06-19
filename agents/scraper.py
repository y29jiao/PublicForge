"""Agent 1 — Scraper (no LLM).

Black box: fetch peer/competitor data and hand back standardized JSON. The data
comes from the WeChat data query API via interfaces/data_source.py (configured in
.env); the API can be re-pointed there without touching this agent or downstream.
"""

from __future__ import annotations

from interfaces.data_source import fetch_samples
from state.article_state import ArticleState


def run(state: ArticleState) -> list:
    """Load the standardized sample data into the shared state."""
    state.samples = fetch_samples()
    return state.samples