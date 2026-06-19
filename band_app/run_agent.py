"""Bring up ONE content agent as a Band remote agent (plan §12.4).

Usage:
  .venv\\Scripts\\python.exe -m band_app.run_agent analysis
  .venv\\Scripts\\python.exe -m band_app.run_agent compliance_review

The agent connects to Band, then idles until it is @mentioned in a room. When
mentioned, its adapter (pydantic-ai spine, or LangGraph for compliance) runs the
GPT model with the agent's prompt-file instructions and posts its JSON result
back into the room. The orchestrator reads that result and routes next.

Bring the 8 content agents up FIRST (they wait to be mentioned), THEN start the
orchestrator (band_app.orchestrator_app), which creates the room and drives it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv


def _enable_debug_logging() -> None:
    """When BAND_DEBUG=1, log band-SDK INFO (+ tool/mention errors, schema retries)
    to stderr so the launching supervisor/web UI can capture it for debugging."""
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr,
                        format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("band").setLevel(logging.INFO)
    for noisy in ("httpx", "httpcore", "openai", "websockets", "phoenix_channels_python_client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

from band import Agent, AgentConfig
from band.config import load_agent_config

from band_app.adapters import build_adapter
from band_app.config import LLM_AGENT_KEYS
from common.console import setup_utf8


async def run_one(agent_key: str) -> None:
    if os.getenv("BAND_DEBUG"):
        _enable_debug_logging()
    load_dotenv()  # OPENAI_API_KEY for the agent's reasoning
    agent_id, api_key = load_agent_config(agent_key)  # Band identity for this agent

    adapter = build_adapter(agent_key)
    # Do NOT auto-subscribe to every PRIOR room this agent was ever in: those stale
    # rooms carry a backlog of dead messages the runtime keeps re-syncing/retrying,
    # which has wedged an agent's single worker mid-run (e.g. final_editor going
    # silent on round 2). New rooms we are added to live (the orchestrator's room)
    # are still subscribed via the participant-added event, so the current run is
    # unaffected. This removes the stale-backlog churn at the source.
    config = AgentConfig(auto_subscribe_existing_rooms=False)
    agent = Agent.create(adapter=adapter, agent_id=agent_id, api_key=api_key,
                        config=config)

    print(f"[{agent_key}] connected to Band — waiting to be @mentioned.")
    await agent.run()  # blocks: respond whenever mentioned, until shut down


def main() -> None:
    setup_utf8()
    if len(sys.argv) != 2 or sys.argv[1] not in LLM_AGENT_KEYS:
        valid = ", ".join(LLM_AGENT_KEYS)
        print(f"usage: python -m band_app.run_agent <agent_key>\n  valid keys: {valid}")
        raise SystemExit(2)
    asyncio.run(run_one(sys.argv[1]))


if __name__ == "__main__":
    main()
