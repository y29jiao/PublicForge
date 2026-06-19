"""Hot-topic source for the `trend_fit` scoring dimension (swap point #3).

These hot topics are produced by ALGORITHM, never hand-filled per run. The MVP
provider returns the seeded list from settings.yaml (`hot_topic_seed`). Post-MVP
swaps the body for a live WebSearch / search-trend API behind this same function
— the Editorial Scoring agent only sees the returned list, so it does not change.
"""

from __future__ import annotations

from common.loaders import load_settings


def get_hot_topics() -> list[str]:
    """Return the current hot topics used by the trend_fit dimension.

    MVP: the seed list from config/settings.yaml. (Algorithmic source, not a
    per-run hand edit — see plan §7 / §9.)
    """
    return list(load_settings().get("hot_topic_seed", []))
