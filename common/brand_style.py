"""Build our brand's writing-style profile from its own real published articles.

We treat 清华大学's official account as the brand. This module:
  1. queries the WeChat data API for that account's most-read articles (LIVE),
  2. fetches each article's full body text from mp.weixin.qq.com (LIVE),
  3. asks the LLM to distil the recurring writing style (prompts/brand_style.md),
  4. caches the resulting style profile to data/brand_style.json.

`interfaces/style_provider.get_style_spec()` reads the cached `style_spec`, so the
Brand Review and Drafting agents enforce our ACTUAL voice. Steps 1–2 are genuinely
real-time API / web fetches — run `python -m common.brand_style` to (re)build.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from common.llm import complete_json, model_for, temperature_for
from common.loaders import fill, load_prompt

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = PROJECT_ROOT / "data" / "brand_style.json"

API_BASE_URL = os.getenv("WECHAT_API_BASE_URL", "").rstrip("/")
# The brand = 清华大学 main official account. Override via .env if the brand changes.
BRAND_NAME = os.getenv("BRAND_ACCOUNT_NAME", "清华大学")
BRAND_USERNAME = os.getenv("BRAND_ACCOUNT_USERNAME", "gh_362a117272d3")
MAX_ARTICLES = 20            # only profile a handful of the most-read pieces
BODY_CHAR_LIMIT = 1500       # excerpt per article fed to the LLM (style, not full text)
REQUEST_TIMEOUT = 30


def _top_brand_articles() -> list[dict[str, Any]]:
    """Query the API for the brand account's articles, most-read first (LIVE)."""
    if not API_BASE_URL:
        raise RuntimeError("WECHAT_API_BASE_URL is not set in .env")
    resp = requests.get(
        f"{API_BASE_URL}/articles",
        params={"username": BRAND_USERNAME, "page_size": 100},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    articles = resp.json().get("data", [])
    articles.sort(key=lambda a: (a.get("read_num") or 0), reverse=True)
    return articles[:MAX_ARTICLES]


def build_brand_style() -> dict[str, Any]:
    """Pull the brand's top articles, fetch bodies, analyse the voice, cache it."""
    # Lazy import: the private (gitignored) fetcher is only needed for a live
    # rebuild — reading the cached profile must work without it.
    from common.wechat_fetch import fetch_article_body

    top = _top_brand_articles()

    excerpts = []
    for art in top:
        url = art.get("url")
        if not url:
            continue
        body = fetch_article_body(url)
        if not body:
            continue
        excerpts.append({"title": art.get("title", ""), "body": body[:BODY_CHAR_LIMIT]})

    if not excerpts:
        raise RuntimeError("could not fetch any brand article bodies")

    prompt = fill(load_prompt("brand_style"),
                  articles=json.dumps(excerpts, ensure_ascii=False))
    profile = complete_json(
        system_prompt=prompt,
        user_content="请根据上述本品牌真实文章总结文风画像（仅返回 JSON）。",
        model=model_for("default"),
        temperature=temperature_for("analysis"),
    )

    profile["brand_account"] = BRAND_NAME
    profile["username"] = BRAND_USERNAME
    profile["source_count"] = len(excerpts)
    profile["sources"] = [{"title": a.get("title", ""), "url": a.get("url", ""),
                           "read_num": a.get("read_num", 0)} for a in top]

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    return profile


def load_brand_style() -> dict[str, Any] | None:
    """Return the cached brand-style profile, or None if it has not been built."""
    if not CACHE_PATH.exists():
        return None
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


if __name__ == "__main__":
    from common.console import setup_utf8

    setup_utf8()
    print(f"Building brand style from '{BRAND_NAME}' ({BRAND_USERNAME}) — live API + web fetch...")
    result = build_brand_style()
    print(f"\nsources used: {result['source_count']}")
    print(f"\nstyle_spec:\n{result['style_spec']}")
    print(f"\ncached -> {CACHE_PATH}")
