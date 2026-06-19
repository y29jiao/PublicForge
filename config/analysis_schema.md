# Analysis Output Schema (v0.2)

This is the structured result the **Analysis Agent** produces. It is the canonical
shape — kept identical to the JSON block in `prompts/analysis.md` (the prompt embeds
it so the model follows it exactly). Field names are in English; the text *values*
are written in Simplified Chinese (that is the article-facing content).

To add new analysis later, put new fields under `extensions` so the existing shape
does not break. Bump `schema_version` when the shape changes.

## Shape

```json
{
  "schema_version": "0.2",

  "meta": {
    "sample_count": 0,
    "sources": ["深圳大学", "北京大学"]
  },

  "recurring_topics": [
    {
      "topic": "招生宣传与报考指南",
      "signal": "high",
      "evidence": "多篇招生计划/简章为头条，阅读、点赞、分享、收藏均极高"
    }
  ],

  "audience_pain_points": [
    {
      "pain_point": "考生与家长对招生政策、专业选择信息的强烈需求",
      "evidence": "招生类文章阅读与互动远高于其他内容"
    }
  ],

  "content_patterns": {
    "title_patterns": [
      { "pattern": "“重磅/权威发布”等高关注词", "evidence": "如《重磅！…招生简章发布》" }
    ],
    "structure_habits": [
      { "habit": "文末附报名二维码/咨询群", "evidence": "招生、讲座、活动推文均如此" }
    ],
    "posting_times": ["工作日晚间 18:00-20:00"],
    "headline_topics": ["招生计划", "创新班/课程推介"]
  },

  "extensions": {}
}
```

(`signal` is one of: high | medium | low)

## Field notes

- `meta.sources` — which competitor schools/accounts the samples came from (coverage at a glance).
- `meta.sample_count` — how many articles were analysed.
- `recurring_topics` — candidate directions for Topic Strategy; `signal` is how strong the
  data is, weighted by the engagement metrics (read/like/share/collect/comment).
- `audience_pain_points` — what readers seem to want; drives angle selection.
- `content_patterns.title_patterns` — recurring title formulas, each with its evidence.
- `content_patterns.structure_habits` — how pieces are organised (hook / body / call-to-action).
- `content_patterns.posting_times` — observed posting-time habits (from `post_time_str`).
- `content_patterns.headline_topics` — what kind of content tends to run 头条 (`position` = 1).
- `extensions` — **reserved**. Future additions (e.g. per-school breakdowns, sentiment) go
  here without changing the rest.

## Rules for the agent

- Every item must be grounded in the sample data; put the support in `evidence`.
- If something is unknown, use an empty list/string — never fabricate.
- Keep it concise; this is an input to the next step, not a full report.
