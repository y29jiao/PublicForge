"""Agent 2 — Analysis.

Analyzes the sample data and produces a structured analysis following
config/analysis_schema.md. It only analyzes; it never invents topics.
"""

from __future__ import annotations

from common.llm import complete_json, model_for, temperature_for
from common.loaders import fill, load_prompt
from common.sample_digest import build_samples_digest
from state.article_state import ArticleState


def run(state: ArticleState) -> dict:
    """Read samples + direction, return the analysis object; write it to state."""
    # Fill the prompt with a compact digest of the samples (grounded, not fabricated).
    prompt = fill(
        load_prompt("analysis"),
        samples=build_samples_digest(state.samples),
        direction=state.direction,
    )

    analysis = complete_json(
        system_prompt=prompt,
        user_content="请根据上述样本数据产出分析结果（仅返回 JSON）。",
        model=model_for("default"),          # generator/extractor → base GPT
        temperature=temperature_for("analysis"),
    )

    state.analysis = analysis
    return analysis
