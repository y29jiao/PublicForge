"""Launch the whole crew for a Band demo (plan §12.7).

Brings up the 7 LLM content agents FIRST (they connect and idle, waiting to be
@mentioned), gives them a moment to register, THEN starts the orchestrator, which
creates the room, adds participants, and drives the state machine to the final
package. The scraper has no process — the orchestrator runs it in code.

Each child is a separate Python process (one agent per process), so a crash in
one agent does not take the others down. Ctrl+C stops everyone.

Usage:
  .venv\\Scripts\\python.exe -m band_app.supervisor
"""

from __future__ import annotations

import subprocess
import sys
import time

from band_app.config import LLM_AGENT_KEYS
from common.console import setup_utf8

STARTUP_GRACE_SECONDS = 6  # let the agents connect before the orchestrator mentions them


def main() -> None:
    setup_utf8()
    python = sys.executable  # the venv interpreter running this supervisor
    children: list[subprocess.Popen] = []

    try:
        # 1. Start the LLM content agents (they idle until @mentioned).
        for key in LLM_AGENT_KEYS:
            print(f"[supervisor] starting agent: {key}")
            children.append(subprocess.Popen([python, "-m", "band_app.run_agent", key]))

        # 2. Give them time to connect to Band.
        print(f"[supervisor] waiting {STARTUP_GRACE_SECONDS}s for agents to connect...")
        time.sleep(STARTUP_GRACE_SECONDS)

        # 3. Start the orchestrator (creates the room and drives the flow).
        print("[supervisor] starting orchestrator")
        orchestrator = subprocess.Popen([python, "-m", "band_app.orchestrator_app"])
        children.append(orchestrator)

        # 4. Wait for the orchestrator to finish, then stop the agents.
        orchestrator.wait()
        print("[supervisor] orchestrator finished — shutting down agents.")
    except KeyboardInterrupt:
        print("\n[supervisor] interrupted — shutting down.")
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()


if __name__ == "__main__":
    main()
