"""Brand-tone source (swap point #2).

The brand voice is derived from our own account's real published articles: see
`common/brand_style.py`, which pulls 清华大学's most-read posts (live API + web
fetch), distils their writing style, and caches the profile to
data/brand_style.json. `get_style_spec()` returns that profile's `style_spec`.

If the profile has not been built yet (run `python -m common.brand_style`), we
fall back to the fixed MVP tone below — the Brand Review and Drafting agents read
the string returned here either way, so they do not change.
"""

from __future__ import annotations

from common.brand_style import load_brand_style

# Fallback tone, used only when the brand-style profile has not been built yet.
# Mirrors the default in prompts/brand_review.md.
MVP_STYLE_SPEC = (
    "语气官方、正式、克制；用词稳重专业；"
    "避免网络流行语、夸张表达和情绪化措辞。"
)


def get_style_spec() -> str:
    """Return the tone / brand requirement used by Drafting and Brand Review."""
    profile = load_brand_style()
    if profile and profile.get("style_spec"):
        return profile["style_spec"]
    return MVP_STYLE_SPEC
