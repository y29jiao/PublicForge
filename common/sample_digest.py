"""Build a compact, readable digest of the standardized sample data.

Analysis only consumes 公众号文章 (articles). The raw sample list is large, so we
extract just the signals the analysis agent needs: title, summary, posting time,
头条/原创 flags, and the real engagement metrics the API provides (reads / likes /
shares / favourites / comment count). The digest keeps every item grounded in
real data (plan §0 rule: never fabricate).

Free-text fields (title / digest) are cleaned of newlines and emoji, and the JSON
is emitted without indentation — both shrink the token count with no information
loss. Returned as a JSON string so it drops straight into a `{{samples}}` placeholder.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Emoji and pictographic symbols (covers the common Unicode emoji blocks). These
# carry no analytical signal and waste tokens, so we strip them from free text.
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # emoji, symbols & pictographs, supplemental
    "\U00002600-\U000027BF"   # misc symbols + dingbats
    "\U00002B00-\U00002BFF"   # misc symbols and arrows
    "\U0001F1E6-\U0001F1FF"   # regional indicator flags
    "\U0000FE00-\U0000FE0F"   # variation selectors
    "\U0000200D"              # zero-width joiner
    "]+",
    flags=re.UNICODE,
)
_WHITESPACE_RE = re.compile(r"\s+")


def _clean(text: str) -> str:
    """Strip emoji and collapse all whitespace/newlines to single spaces."""
    return _WHITESPACE_RE.sub(" ", _EMOJI_RE.sub("", text or "")).strip()


def _digest_official_account(account: dict[str, Any]) -> dict[str, Any]:
    info = account.get("account_info", {})
    articles = []
    for item in account.get("articles", []):
        ai = item.get("article_info", {})
        articles.append({
            "title": _clean(ai.get("title", "")),
            "digest": _clean(ai.get("digest", "")),
            "post_time_str": ai.get("post_time_str", ""),
            "position": ai.get("position", 0),       # 1 = 头条, 2+ = 次条
            "original": ai.get("original", 0),       # 1 = 原创
            # Real engagement signals from the API.
            "read_num": ai.get("read_num", 0),
            "like_num": ai.get("like_num", 0),
            "old_like_num": ai.get("old_like_num", 0),   # 在看
            "collect_num": ai.get("collect_num", 0),     # 收藏
            "share_num": ai.get("share_num", 0),         # 分享
            "comment_num": ai.get("comment_num", 0),
        })
    return {"account": info.get("nick_name", ""), "articles": articles}


def build_samples_digest(samples: list[dict[str, Any]]) -> str:
    """Compress the raw sample list into a small JSON string for prompts."""
    digest = []
    for uni in samples:
        digest.append({
            "university": uni.get("university", ""),
            "official_accounts": [
                _digest_official_account(a) for a in uni.get("official_accounts", [])
            ],
        })
    return json.dumps(digest, ensure_ascii=False, separators=(",", ":"))
