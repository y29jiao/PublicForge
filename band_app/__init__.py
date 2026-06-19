"""Band integration layer (plan §6, §12).

NOTE ON THE FOLDER NAME: the plan's structure calls this folder `band/`, but the
installed SDK's import package is also named `band`. A local top-level package
named `band/` would SHADOW the SDK (`import band` would resolve here), breaking
every `from band import ...`. So this package is named `band_app/` instead. That
is the one intentional deviation from the plan's folder layout — everything else
matches.

What's here:
- config.py            — read each agent's agent_id / handle from agent_config.yaml
- system_prompts.py    — build each agent's system prompt from its prompts/*.md file
- adapters.py          — build the right framework adapter per agent (spine vs 2nd)
- client.py            — thin REST wrapper: create room, add participants, send/poll
- messages.py          — build the @mention task message + parse an agent's JSON reply
- run_agent.py         — entry point: bring up ONE content agent as a Band remote agent
- orchestrator_app.py  — the deterministic orchestrator as a Band participant
- supervisor.py        — launch all 8 agents + the orchestrator
"""
