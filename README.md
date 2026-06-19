# PublicForge

PublicForge is a multi-agent content engine for public-account publishing.
A team of **8 AI agents** plus **1 deterministic orchestrator** collaborate inside a
**Band** room to turn raw sample data into a publish-ready article: it scrapes
peer/competitor samples → analyzes them → generates candidate topics → scores the
topics and asks a human to confirm → drafts the article → runs brand & compliance
reviews → does a final edit → and outputs a complete, publish-ready package. Two
**send-back loops** let the agents iterate (regenerate topics, or rewrite the draft)
until the work clears review, with a guard so it never loops forever.

> The full design, decisions, and the verbatim file bodies are in **`plan.md`** (the
> source of truth). This README is the operational quick-start.

## How PublicForge meets the challenge

> **Challenge:** *Build a cross-framework multi-agent system with Band — at least 3 agents
> collaborating through Band across planning, execution, review, decision-making, or task
> handoff, where Band is part of the actual collaboration layer, not a thin wrapper or a
> final notification channel.*

PublicForge is a content-publishing workflow that maps directly onto that brief. It fits
**Track 1 (Internal Enterprise Workflows)** as a cross-team editorial operation: work moves
from analysis → planning → review → human approval → publishing, exactly the kind of
hand-off, approval, and decision pipeline the track describes.

| Requirement | How PublicForge satisfies it |
|---|---|
| **≥ 3 agents collaborating through Band** | 8 LLM agents + 1 deterministic orchestrator. The orchestrator creates a real Band room and @mentions the next agent at every step. |
| **Real agent-to-agent collaboration** | Each agent reads the previous agents' structured output from the shared `ArticleState` and posts its own result back. Topic Strategy hands candidates to Editorial; Editorial's score drives a human decision; Drafting hands to Brand + Compliance reviewers; Final Editor integrates both reviews. |
| **Band is the collaboration layer, not a wrapper** | The hand-offs *happen in the room*: every agent input/output is posted into the Band room, the next agent is @mentioned there, and the **human approves inside the room** with a structured command (`approve: topic_2` / `reject: <reason>`). Band carries the live coordination, not just a final notification. |
| **Cross-framework** | The spine runs on **pydantic-ai**; the isolated **Compliance** agent runs on **LangGraph** — two frameworks coordinating through the same Band workflow. |
| **Decision-making + task hand-off** | Two **send-back loops** (Editorial → Topic Strategy, Final Editor → Drafting) let agents delegate rework based on review outcomes, with a `rewrite_cap` guard that escalates to the human instead of looping forever. |
| **Planning / execution / review / decision** | Analysis & Topic Strategy = planning, Drafting = execution, Brand & Compliance = review, Editorial + human + Final Editor = decision-making — all stages of the brief are present. |

## The 8 agents + orchestrator

| # | Agent | Job | Framework |
|---|---|---|---|
| 1 | Scraper | sample data → standardized JSON (no LLM) | — |
| 2 | Analysis | analyze the samples | pydantic-ai (spine) |
| 3 | Topic Strategy | generate 5 candidate topics | pydantic-ai |
| 4 | Editorial | score topics on the 5/3/1 scorecard, then hand to a human | pydantic-ai |
| 5 | Drafting | write / rewrite the draft | pydantic-ai |
| 6 | Brand Review | tone / brand fit | pydantic-ai |
| 7 | Compliance Review | imitation / copyright / citations | **LangGraph (2nd framework)** |
| 8 | Final Editor | integrate reviews → final package or rewrite | pydantic-ai |
| — | Orchestrator | deterministic router (rule code, **no LLM**) | — |

**Cross-framework** is satisfied at the framework layer: the spine runs on pydantic-ai,
the single isolated Compliance agent runs on LangGraph. Every agent runs a **GPT** model.

**The two loops** (the real collaboration points):
- **Loop A — Editorial → Topic Strategy:** top score below threshold, or human rejects.
- **Loop B — Final Editor → Drafting:** blocking review issues → rewrite.
Each loop has a `rewrite_cap` guard (default 3) → hands off to the human instead of looping forever.

## Project layout

```
config/        settings.yaml, scorecard.yaml, analysis_schema.md   (team-tunable)
prompts/       one prompt file per agent                            (business owners edit here)
interfaces/    data_source / style_provider / hot_topic_provider    (the 3 swap points)
agents/        one file per agent, plain run(state) logic
orchestrator/  router.py — deterministic state machine + 3 guards + offline runner
state/         article_state.py — the shared per-article state object
common/        loaders, llm helper, sample digest, console utf-8
band_app/      Band integration (see note below)
data/samples/  the sample JSON (Peking University / Tsinghua University)
main.py        offline runner (the whole flow, no Band)
```

> **Folder-name note:** the plan calls the Band folder `band/`, but the installed SDK's
> import package is also `band`. A local `band/` package would shadow the SDK, so it is
> named **`band_app/`** here. That is the one intentional deviation from the plan layout.

## Setup

The virtualenv and deps are already installed. Secrets live in (gitignored):
- `.env` — `OPENAI_API_KEY` (the agents' reasoning)
- `agent_config.yaml` — per-agent Band `agent_id` / `api_key` / `handle`

Run Python via the venv interpreter: `.venv\Scripts\python.exe`.

## Run it — offline (recommended first)

Runs the full 8-agent flow + both loops + human approval on the sample data, **without Band**.

```powershell
# auto-approve the recommended topic (non-interactive, good for a quick demo):
.venv\Scripts\python.exe main.py --auto

# interactive human approval (you type `approve: topic_2` or `reject: <reason>`):
.venv\Scripts\python.exe main.py
```

You will see one `[router] from -> to (reason)` line per transition (a full audit trail),
then the final Chinese package (title / summary / body / cover suggestion / push time /
open questions). A verified run triggers **Loop B** and the loop **guard** before finishing.

## Run it — Web UI (the through-Band product prototype)

`band_through_final.py` is the main Web UI: the full product prototype that runs the
**true through-Band** flow. The agents are real Band remote agents that do their own LLM
work and hand off **through Band**; the server launches the LLM content agents, drives the
orchestrator, and mirrors the whole run in the browser (data scraping & comparison, topic
generation & scoring, content creation & editing, and content management) with live
progress, candidate cards, in-page human approval, draft preview, and final-package
rendering.

```powershell
.venv\Scripts\python.exe band_through_final.py
```

Then open:

```text
http://127.0.0.1:8003
```

Notes:
- Requires both `.env` (`OPENAI_API_KEY`) and a filled `agent_config.yaml` with the Band
  agent credentials — the hand-offs happen in a real Band room.
- When the flow reaches human review, approve or reject the recommended topic in the page
  (or in the Band room) instead of typing in the terminal.
- Keep the terminal open while using the page; press `Ctrl+C` there to stop the local server.

## Run it — on Band

Brings each agent up as a Band remote agent; the orchestrator creates a room, @mentions the
next agent each step, and the human approves **inside the room** with the structured command.

```powershell
# launches the 7 LLM agents, then the orchestrator (which runs the scraper in code):
.venv\Scripts\python.exe -m band_app.supervisor
```

Optional env (so the orchestrator can @mention the human; otherwise it posts a plain prompt
and waits for any room message in the fixed format):
```
HUMAN_PARTICIPANT_ID=<the human's Band participant id>
HUMAN_HANDLE=@yusen8/...
```

The human replies in the room with the **fixed format** `approve: topic_2` or `reject: <reason>`;
the orchestrator parses it deterministically (never via an LLM).

## Model configuration

All models are GPT, set in `config/settings.yaml` → `models`:
- generators/extractors (analysis, topic_strategy, drafting): `gpt-4.1`
- judges (editorial, brand, compliance, final): `gpt-5.2` (a full GPT-5.x reasoning tier;
  the plan named `gpt-5.3`, which this account does not expose as a full reasoning model —
  swap the one `judge:` line if a different id is preferred).
