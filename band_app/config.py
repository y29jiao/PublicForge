"""Read per-agent Band identity (agent_id + handle) from agent_config.yaml.

The SDK's `load_agent_config(key)` returns (agent_id, api_key) but not the
`handle`, and the orchestrator needs handles to @mention the next agent. This
helper reads the same root agent_config.yaml for the handle. Secrets (api_key)
are never logged or returned by name here — use the SDK loader for the key.
"""

from __future__ import annotations

from pathlib import Path

import yaml

# The nine Band agents defined in agent_config.yaml (8 content + orchestrator).
AGENT_KEYS = [
    "scraper",
    "analysis",
    "topic_strategy",
    "editorial",
    "drafting",
    "brand_review",
    "compliance_review",
    "final_editor",
    "orchestrator",
]

# The eight content agents the orchestrator brings into the room and @mentions.
CONTENT_AGENT_KEYS = [k for k in AGENT_KEYS if k != "orchestrator"]

# The agents that run as Band remote agents with an LLM adapter. The scraper has
# no LLM (plan §12.5): the orchestrator calls it directly and posts its output,
# so it is not launched via run_agent.
LLM_AGENT_KEYS = [k for k in CONTENT_AGENT_KEYS if k != "scraper"]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "agent_config.yaml"  # band.config default location


def _raw_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_handle(agent_key: str) -> str:
    """Return the @handle for an agent key, e.g. 'topic_strategy' -> '@yusen8/topic-strategy'."""
    return _raw_config()[agent_key]["handle"]


def get_agent_id(agent_key: str) -> str:
    """Return the platform agent_id for an agent key (used as a mention/participant id)."""
    return _raw_config()[agent_key]["agent_id"]
