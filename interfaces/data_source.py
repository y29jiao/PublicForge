"""Scraper interface (black-box point #1).

Queries the WeChat data query API for 公众号文章 (official-account articles) and
returns a list of standardized, university-grouped dicts. Videos / video accounts
are intentionally NOT fetched — analysis only consumes articles. The return shape
matches what `common/sample_digest.py` and the analysis agent read:

    {
      "university": <str>,
      "official_accounts": [
        {"account_info": {"user_name", "nick_name", "biz"},
         "articles": [{"article_info": {...all API article fields...}}]},
      ],
    }

The API has no comments endpoint; engagement lives directly on each article
(read_num / like_num / share_num / comment_num / collect_num).

Configuration comes from `.env` (no endpoints hard-coded). Docs: <base>/docs
"""

from __future__ import annotations

import os
import re
from typing import Any

import requests
from dotenv import load_dotenv

# Load WECHAT_API_* (and OPENAI_API_KEY) from .env once, at import time.
load_dotenv()

API_BASE_URL = os.getenv("WECHAT_API_BASE_URL", "").rstrip("/")
PAGE_SIZE = int(os.getenv("WECHAT_API_PAGE_SIZE", "100"))
MAX_PAGES = int(os.getenv("WECHAT_API_MAX_PAGES", "50"))
REQUEST_TIMEOUT = 30

# Our brand. Competitor analysis must EXCLUDE it — we analyse rivals, not ourselves.
# We match on the distinctive token (e.g. 清华大学 → 清华) so sibling accounts like
# "清华招生" are excluded too. The brand's own voice is profiled separately in
# common/brand_style.py.
BRAND_NAME = os.getenv("BRAND_ACCOUNT_NAME", "清华大学")
BRAND_TOKEN = re.sub(r"(大学|学院|学校)$", "", BRAND_NAME)

# Match the university an account name belongs to: the shortest prefix ending in
# 大学 / 学院 / 学校 (e.g. "深圳大学研究生招生" → "深圳大学"). Falls back to the full name.
_UNIVERSITY_RE = re.compile(r"^.+?(?:大学|学院|学校)")


def _time_window() -> dict[str, int]:
    """Optional {start_time, end_time} filter from .env (omitted if unset)."""
    window: dict[str, int] = {}
    start = os.getenv("WECHAT_API_START_TIME", "").strip()
    end = os.getenv("WECHAT_API_END_TIME", "").strip()
    if start:
        window["start_time"] = int(start)
    if end:
        window["end_time"] = int(end)
    return window


def _fetch_all(endpoint: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Fetch every page of a paginated list endpoint and return all rows.

    List endpoints return {total, page, page_size, data}. We page until we have
    `total` rows or hit the MAX_PAGES safety cap.
    """
    rows: list[dict[str, Any]] = []
    page = 1
    while page <= MAX_PAGES:
        response = requests.get(
            f"{API_BASE_URL}/{endpoint}",
            params={**params, "page": page, "page_size": PAGE_SIZE},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()

        batch = payload.get("data", [])
        rows.extend(batch)

        total = payload.get("total", len(rows))
        if not batch or len(rows) >= total:
            break
        page += 1

    return rows


def _university_of(account_name: str) -> str:
    """Derive the university label from a 公众号 name (regex, else the raw name)."""
    match = _UNIVERSITY_RE.match(account_name or "")
    return match.group(0) if match else (account_name or "未知")


def fetch_samples() -> list[dict[str, Any]]:
    """Query the articles API and return standardized, university-grouped samples."""
    if not API_BASE_URL:
        raise RuntimeError("WECHAT_API_BASE_URL is not set in .env")

    articles = _fetch_all("articles", _time_window())

    # Group articles into official accounts (keyed by 公众号 username).
    official_by_user: dict[str, dict[str, Any]] = {}
    for article in articles:
        username = article.get("username")
        if not username:
            continue
        account = official_by_user.setdefault(username, {
            "account_info": {
                "user_name": username,
                "nick_name": article.get("name", ""),
                "biz": article.get("biz", ""),
            },
            "articles": [],
        })
        account["articles"].append({"article_info": article})

    # Bucket accounts into universities (derived from the account name), skipping
    # our own brand — competitor analysis only looks at rival schools.
    universities: dict[str, dict[str, Any]] = {}
    for account in official_by_user.values():
        name = account["account_info"]["nick_name"]
        if BRAND_TOKEN and BRAND_TOKEN in name:
            continue
        label = _university_of(name)
        universities.setdefault(
            label, {"university": label, "official_accounts": []}
        )["official_accounts"].append(account)

    return list(universities.values())


if __name__ == "__main__":
    import json

    samples = fetch_samples()
    print(f"universities: {len(samples)}")
    for uni in samples:
        n = sum(len(a["articles"]) for a in uni["official_accounts"])
        print(f"  {uni['university']}: {len(uni['official_accounts'])} account(s), {n} article(s)")
    print(json.dumps(samples[:1], ensure_ascii=False, indent=2)[:2000])
