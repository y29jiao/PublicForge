"""Offline runner for the content engine (build-order milestones 2 & 3).

Runs the full chain 1→8 on the sample JSON, with both send-back loops and the
human approval step, WITHOUT Band. Band wiring is milestone 4 (band/, agent entry
scripts). This is the script to use to confirm each agent's input/output lines up.

Usage:
  .venv\\Scripts\\python.exe main.py                     # interactive human approval
  .venv\\Scripts\\python.exe main.py --auto              # auto-approve recommended topic
  .venv\\Scripts\\python.exe main.py --direction "面向高考考生的专业解读"
"""

from __future__ import annotations

import argparse
import json

from common.console import setup_utf8
from common.loaders import load_settings
from orchestrator.router import run_offline
from state.article_state import ArticleState


def cli_human_decider(state: ArticleState) -> str:
    """Show the scores, then read the human's structured reply from the terminal."""
    scores = state.scores or {}
    print("\n================ 人工确认 (Human Review) ================")
    print(f"推荐选题 recommended_id: {scores.get('recommended_id')}  top_score: {state.top_score}")
    for c in state.candidates or []:
        print(f"  - {c.get('id')}: {c.get('title')}")
    print("回复格式 (structured): 'approve: topic_2'  或  'reject: <理由>'")
    reply = input("你的决定> ").strip()
    return reply or f"approve: {scores.get('recommended_id', '')}"


def auto_human_decider(state: ArticleState) -> str:
    """Non-interactive: approve the recommended topic. For demos / CI."""
    rec = (state.scores or {}).get("recommended_id", "")
    print(f"\n[auto] 人工自动通过推荐选题: {rec}")
    return f"approve: {rec}"


def main() -> None:
    setup_utf8()  # so Chinese article output prints on Windows terminals
    parser = argparse.ArgumentParser(description="Public-account content engine (offline).")
    parser.add_argument("--auto", action="store_true", help="auto-approve the recommended topic")
    parser.add_argument("--direction", default=None,
                        help="our one-line direction / theme (defaults to settings.yaml `direction`)")
    args = parser.parse_args()

    settings = load_settings()
    direction = args.direction or settings["direction"]
    print("配置: threshold=%s  rewrite_cap=%s  num_candidates=%s"
          % (settings["score_threshold"], settings["rewrite_cap"], settings["num_candidates"]))

    state = ArticleState(article_id="demo-001", direction=direction)
    human = auto_human_decider if args.auto else cli_human_decider

    run_offline(state, human)

    print("\n================ 最终结果 (Final) ================")
    print(f"status: {state.status}")
    print(f"topic_revision_count: {state.topic_revision_count}  draft_revision_count: {state.draft_revision_count}")
    if state.output:
        print(json.dumps(state.output, ensure_ascii=False, indent=2))
    else:
        print("(no final package produced — flow stopped before 'done')")


if __name__ == "__main__":
    main()
