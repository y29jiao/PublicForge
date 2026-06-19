"""Temp integration test: launch the 7 agents + run the through-Band orchestrator."""
import asyncio, subprocess, sys, time, os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
from band_app.config import LLM_AGENT_KEYS
from band_app.orchestrator_through import BandThroughOrchestrator
from state.article_state import ArticleState

async def auto_human(state):
    rec = (state.scores or {}).get("recommended_id", "")
    return f"approve: {rec}"

def on_input(key, content): print(f"  >>> INPUT to {key}: {content[:70]}...", flush=True)
def on_output(key, parsed):
    keys = list(parsed.keys()) if isinstance(parsed, dict) else type(parsed).__name__
    print(f"  <<< OUTPUT from {key}: {keys}", flush=True)
def on_event(line): print("  " + line, flush=True)

async def main():
    py = sys.executable
    procs = []
    for k in LLM_AGENT_KEYS:
        procs.append(subprocess.Popen([py, "-m", "band_app.run_agent", k]))
    print(f"launched {len(LLM_AGENT_KEYS)} agents; waiting 10s to connect...", flush=True)
    await asyncio.sleep(10)
    state = ArticleState(article_id="through-test", direction="面向考生与家长的高考志愿与专业解读，突出本校优势")
    orch = BandThroughOrchestrator(state, auto_human, on_input=on_input, on_output=on_output, on_event=on_event)
    try:
        await orch.run()
    finally:
        print("\n==== RESULT ====", flush=True)
        print("status:", state.status, "| has output:", bool(state.output), flush=True)
        print("topic_rev:", state.topic_revision_count, "draft_rev:", state.draft_revision_count, flush=True)
        if state.output:
            print("FINAL title:", state.output.get("title"), flush=True)
        for p in procs:
            if p.poll() is None: p.terminate()

asyncio.run(main())
