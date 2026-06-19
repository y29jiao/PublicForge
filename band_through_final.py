"""Web UI for the TRUE through-Band run - 完整产品原型版.

基于完整的产品原型，包含数据采集与对比、选题生成与评分、内容创作与编辑、内容管理。

Run (after filling agent_config.yaml):
  .venv\\Scripts\\python.exe band_through_final.py  # then open http://127.0.0.1:8003
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml
from dotenv import load_dotenv

from band_app.config import AGENT_KEYS, LLM_AGENT_KEYS
from band_app.orchestrator_through import BandThroughOrchestrator
from common.console import setup_utf8
from state.article_state import ArticleState

HOST, PORT = "127.0.0.1", 8003
PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "agent_config.yaml"
RUN_LOG = PROJECT_ROOT / "band_run.log"
STARTUP_GRACE_SECONDS = 8

_runlog_lock = threading.Lock()


def runlog(tag: str, msg: str) -> None:
    ts = _dt.datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {tag:14} {msg}\n"
    with _runlog_lock:
        with open(RUN_LOG, "a", encoding="utf-8") as f:
            f.write(line)


def _reset_runlog(direction: str) -> None:
    with _runlog_lock:
        with open(RUN_LOG, "w", encoding="utf-8") as f:
            f.write(f"=== through-Band run @ {_dt.datetime.now():%Y-%m-%d %H:%M:%S} ===\n")
            f.write(f"direction: {direction}\n\n")
    fh = logging.FileHandler(RUN_LOG, encoding="utf-8")
    fh.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)-5s %(name)s: %(message)s", datefmt="%H:%M:%S"))
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler):
            root.removeHandler(h)
    root.addHandler(fh)
    root.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)


_AGENT_ROLE = {
    "analysis": "default", "topic_strategy": "default", "editorial": "judge",
    "drafting": "drafting", "brand_review": "judge", "compliance_review": "judge",
    "final_editor": "judge",
}
_AGENT_FRAMEWORK = {"compliance_review": "LangGraph"}


def agent_models() -> dict:
    from common.loaders import load_settings
    from common.llm import is_reasoning_model
    s = load_settings()
    models = s["models"]
    out = {}
    for key, role in _AGENT_ROLE.items():
        model = models.get(role, models["default"])
        fw = _AGENT_FRAMEWORK.get(key, "pydantic-ai")
        label = f"{model} · {fw}"
        if role == "judge" and is_reasoning_model(model):
            label += " · reasoning(default medium)"
        out[key] = label
    return out


def check_prereqs() -> dict:
    problems: list[str] = []
    agents = {k: False for k in AGENT_KEYS}
    load_dotenv(PROJECT_ROOT / ".env")
    if not os.getenv("OPENAI_API_KEY"):
        problems.append("Missing OPENAI_API_KEY (should be set in .env).")
    if not CONFIG_PATH.exists():
        problems.append("Missing agent_config.yaml (copy agent_config.yaml.example and fill in Band credentials).")
        return {"ok": False, "problems": problems, "agents": agents}
    try:
        cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        problems.append(f"Failed to parse agent_config.yaml: {exc}")
        return {"ok": False, "problems": problems, "agents": agents}
    for key in AGENT_KEYS:
        block = cfg.get(key) or {}
        vals = [str(block.get(f, "")) for f in ("agent_id", "api_key", "handle")]
        ok = all(v and "REPLACE_WITH" not in v for v in vals)
        agents[key] = ok
        if not ok:
            problems.append(f"agent '{key}' is missing one of agent_id/api_key/handle.")
    return {"ok": not problems, "problems": problems, "agents": agents}


class Session:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.running = False
        self.done = False
        self.error: str | None = None
        self.room_id: str | None = None
        self.transitions: list[str] = []
        self.agent_log: list[str] = []
        self.calls: list[dict] = []
        self.state: ArticleState | None = None
        self.needs_human = False
        self.procs: list[subprocess.Popen] = []
        self.proc_by_key: dict[str, subprocess.Popen] = {}
        self._human_event = threading.Event()
        self._human_reply = ""
        self.current_page = "data"  # 当前页面: data/topic/create/manage

    def on_input(self, agent_key: str, content: str) -> None:
        with self.lock:
            self.calls.append({"agent": agent_key, "input": content, "output": None,
                               "status": "running", "error": None})
        runlog(f"INPUT->{agent_key}", content.replace("\n", "\\n"))

    def on_output(self, agent_key: str, parsed) -> None:
        with self.lock:
            for call in reversed(self.calls):
                if call["agent"] == agent_key and call["output"] is None and call["status"] == "running":
                    call["output"], call["status"] = parsed, "done"
                    break
            else:
                self.calls.append({"agent": agent_key, "input": "(triggered by previous agent's handoff via Band)",
                                   "output": parsed, "status": "done", "error": None})
        runlog(f"OUTPUT<-{agent_key}", json.dumps(parsed, ensure_ascii=False))

    def on_event(self, line: str) -> None:
        with self.lock:
            self.transitions.append(line)
        runlog("EVENT", line)

    def add_agent_log(self, line: str) -> None:
        with self.lock:
            self.agent_log.append(line)
        runlog("AGENT", line)

    def ask_human(self) -> str:
        with self.lock:
            self.needs_human = True
            self._human_event.clear()
            self._human_reply = ""
        self._human_event.wait()
        with self.lock:
            self.needs_human = False
            return self._human_reply

    def submit_human(self, reply: str) -> None:
        with self.lock:
            self._human_reply = reply
        self._human_event.set()


SESSION = Session()


def _pump(proc: subprocess.Popen, prefix: str) -> None:
    for raw in iter(proc.stdout.readline, ""):
        line = raw.rstrip("\n")
        if line and "PydanticAIDeprecation" not in line and "infer_model" not in line:
            SESSION.add_agent_log(f"[{prefix}] {line}")
    proc.stdout.close()
    SESSION.add_agent_log(f"[supervisor] agent '{prefix}' 进程已退出 (exit code {proc.poll()})")


def _agent_alive(agent_key: str) -> bool:
    proc = SESSION.proc_by_key.get(agent_key)
    return proc is None or proc.poll() is None


async def _worker_async(direction: str) -> None:
    loop = asyncio.get_event_loop()

    async def web_human(state):
        return await loop.run_in_executor(None, SESSION.ask_human)

    state = ArticleState(article_id="band-through-001", direction=direction)
    with SESSION.lock:
        SESSION.state = state
    orch = BandThroughOrchestrator(
        state, web_human,
        on_input=SESSION.on_input, on_output=SESSION.on_output, on_event=SESSION.on_event,
        agent_alive=_agent_alive,
    )
    await orch.run()
    with SESSION.lock:
        SESSION.room_id = orch.room.room_id if orch.room else None


def _worker(direction: str) -> None:
    python = sys.executable
    creflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    agent_env = {**os.environ, "BAND_DEBUG": "1"}
    try:
        for key in LLM_AGENT_KEYS:
            SESSION.add_agent_log(f"[supervisor] starting agent: {key}")
            proc = subprocess.Popen(
                [python, "-m", "band_app.run_agent", key],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                cwd=str(PROJECT_ROOT), creationflags=creflags, env=agent_env,
            )
            SESSION.procs.append(proc)
            SESSION.proc_by_key[key] = proc
            threading.Thread(target=_pump, args=(proc, key), daemon=True).start()
        SESSION.add_agent_log(f"[supervisor] waiting {STARTUP_GRACE_SECONDS}s for agents to connect...")
        time.sleep(STARTUP_GRACE_SECONDS)
        asyncio.run(_worker_async(direction))
    except Exception as exc:
        import traceback
        with SESSION.lock:
            SESSION.error = f"{type(exc).__name__}: {exc}"
        SESSION.on_event(f"[error] {type(exc).__name__}: {exc}")
        runlog("TRACEBACK", traceback.format_exc().replace("\n", "\\n"))
    finally:
        _stop_procs()
        with SESSION.lock:
            SESSION.running = False
            SESSION.done = True


def _stop_procs() -> None:
    for proc in SESSION.procs:
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass


def _snapshot() -> dict:
    with SESSION.lock:
        st = SESSION.state
        snap = {
            "running": SESSION.running, "done": SESSION.done, "error": SESSION.error,
            "room_id": SESSION.room_id, "needs_human": SESSION.needs_human,
            "transitions": list(SESSION.transitions),
            "agent_log": list(SESSION.agent_log[-200:]),
            "calls": [dict(c) for c in SESSION.calls],
            "status": st.status if st else "idle",
            "models": agent_models(),
            "current_page": SESSION.current_page,
        }
        if st:
            snap["recommended_id"] = (st.scores or {}).get("recommended_id")
            snap["top_score"] = st.top_score
            snap["candidates"] = st.candidates or []
            snap["output"] = st.output
            snap["draft"] = st.draft
            snap["draft_revision_count"] = st.draft_revision_count
        return snap


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a) -> None:
        pass

    def _send(self, code, body, ctype):
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path.startswith("/api/prereqs"):
            self._json(check_prereqs())
        elif self.path.startswith("/api/state"):
            self._json(_snapshot())
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            data = {}
        if self.path == "/api/start":
            pr = check_prereqs()
            if not pr["ok"]:
                self._json({"ok": False, "problems": pr["problems"]}, 400); return
            with SESSION.lock:
                if SESSION.running:
                    self._json({"ok": False, "problems": ["Already running"]}, 409); return
                SESSION.reset(); SESSION.running = True
            direction = (data.get("direction") or "").strip() or \
                __import__("common.loaders", fromlist=["load_settings"]).load_settings()["direction"]
            _reset_runlog(direction)
            threading.Thread(target=_worker, args=(direction,), daemon=True).start()
            self._json({"ok": True})
        elif self.path == "/api/decision":
            reply = (data.get("reply") or "").strip()
            if not reply:
                self._json({"ok": False, "msg": "empty reply"}, 400); return
            SESSION.submit_human(reply); self._json({"ok": True})
        elif self.path == "/api/stop":
            _stop_procs()
            with SESSION.lock:
                SESSION.running = False; SESSION.done = True
            self._json({"ok": True})
        elif self.path == "/api/set_page":
            page = data.get("page") or "data"
            with SESSION.lock:
                SESSION.current_page = page
            self._json({"ok": True})
        else:
            self._send(404, b"not found", "text/plain")


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>uni-content-engine - Intelligent Content Creation Platform</title>
    <link href="https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css" rel="stylesheet">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <style>
        :root {
            --accent: #4f8cff;
            --accent-light: #eff6ff;
            --bg: #f8f9fb;
            --card: #ffffff;
            --border: #e5e7eb;
            --success: #2ecc71;
            --warning: #f5a623;
            --danger: #ff5c5c;
        }
        body {
            background: var(--bg);
            font-family: "PingFang SC", "Microsoft YaHei", "Helvetica Neue", sans-serif;
        }
        .gradient-bg {
            background: linear-gradient(135deg, #1e3a5f 0%, #4f8cff 50%, #0f172a 100%);
        }
        .card {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 12px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.04);
        }
        .topic-card.selected {
            border-left: 4px solid var(--accent);
            background: var(--accent-light);
        }
        .score-bar {
            height: 8px;
            background: var(--border);
            border-radius: 4px;
            overflow: hidden;
        }
        .score-bar-fill {
            height: 100%;
            border-radius: 4px;
            transition: width 0.5s;
        }
        .score-high { background: var(--success); }
        .score-mid { background: var(--warning); }
        .score-low { background: var(--danger); }
        .nav-item {
            transition: all 0.2s;
        }
        .nav-item:hover, .nav-item.active {
            background: var(--accent-light);
            color: var(--accent);
        }
        .nav-item.active {
            border-left: 3px solid var(--accent);
        }
        .editor-toolbar {
            border-bottom: 1px solid var(--border);
        }
        .editor-toolbar button {
            transition: all 0.2s;
        }
        .editor-toolbar button:hover {
            background: var(--accent-light);
            color: var(--accent);
        }
        .page { display: none; }
        .page.active { display: block; }
        .dot { width:9px; height:9px; border-radius:50%; background:#3a3f4b; flex:none; }
        .dot.running { background:#7aa2ff; animation:p 1s infinite; } 
        .dot.done{ background:#2ecc71; } 
        .dot.error{ background:#ff5c5c; }
        @keyframes p { 50%{ opacity:.35; } }
        .spin {
            width:12px;
            height:12px;
            border:2px solid var(--border);
            border-top-color:var(--accent);
            border-radius:50%;
            display:inline-block;
            animation:s .8s linear infinite;
        }
        @keyframes s{ to{ transform:rotate(360deg); } }
    </style>
</head>
<body class="min-h-screen">

    <!-- 顶部导航栏 -->
    <header class="gradient-bg text-white shadow-md">
        <div class="container mx-auto px-4 py-4">
            <div class="flex justify-between items-center">
                <div class="flex items-center space-x-3">
                    <i class="fas fa-brain text-3xl"></i>
                    <h1 class="text-2xl font-bold">uni-content-engine</h1>
                    <span class="text-sm opacity-75 hidden md:inline">Intelligent Content Creation Platform</span>
                </div>
                <div class="flex items-center space-x-4">
                    <span class="px-4 py-2 bg-white/20 rounded-lg" id="statusBadge">Not started</span>
                    <div class="flex items-center space-x-2">
                        <div class="w-8 h-8 bg-white rounded-full flex items-center justify-center">
                            <i class="fas fa-user text-blue-600"></i>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </header>

    <div class="container mx-auto px-4 py-6">
        <div class="flex gap-6">

            <!-- 左侧导航 -->
            <aside class="w-64 hidden lg:block">
                <nav class="card p-4">
                    <div class="nav-item px-4 py-3 rounded-lg cursor-pointer mb-2 active" data-page="data" onclick="setPage('data')">
                        <i class="fas fa-database mr-3"></i>Data Collection & Comparison
                    </div>
                    <div class="nav-item px-4 py-3 rounded-lg cursor-pointer mb-2" data-page="topic" onclick="setPage('topic')">
                        <i class="fas fa-lightbulb mr-3"></i>Topic Generation & Scoring
                    </div>
                    <div class="nav-item px-4 py-3 rounded-lg cursor-pointer mb-2" data-page="create" onclick="setPage('create')">
                        <i class="fas fa-pen-fancy mr-3"></i>Content Creation & Editing
                    </div>
                    <div class="nav-item px-4 py-3 rounded-lg cursor-pointer" data-page="manage" onclick="setPage('manage')">
                        <i class="fas fa-folder-open mr-3"></i>Content Management
                    </div>
                </nav>

                <!-- Quick info -->
                <div class="card p-4 mt-6">
                    <h3 class="font-bold text-sm text-gray-500 mb-3">Current Configuration</h3>
                    <div class="text-sm space-y-2">
                        <p><span class="text-gray-500">Direction: </span><span id="configDirection">College admissions & major interpretation for students and parents</span></p>
                        <p><span class="text-gray-500">Models: </span>GPT-4.1 / GPT-5.2</p>
                        <p><span class="text-gray-500">Threshold: </span>3.5</p>
                    </div>
                </div>
            </aside>

            <!-- 主内容区 -->
            <main class="flex-1">

                <!-- 页面1：数据采集与对比 -->
                <section id="page-data" class="page active">
                    <div class="flex items-center justify-between mb-6">
                        <h2 class="text-2xl font-bold">Data Collection & Comparison</h2>
                        <div class="flex space-x-3">
                            <button id="startBtn-data" class="px-6 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition" onclick="start()">
                                <i class="fas fa-play mr-2"></i>Start Topic Analysis
                            </button>
                        </div>
                    </div>

                    <!-- Prerequisite check -->
                    <div class="card p-6 mb-6 hidden" id="setupPanel">
                        <h3 class="font-bold mb-3">⚠ Startup Prerequisites</h3>
                        <div id="setupBody"></div>
                    </div>

                    <!-- Filters -->
                    <div class="card p-6 mb-6">
                        <div class="grid md:grid-cols-3 gap-6">
                            <div>
                                <label class="block text-sm font-semibold mb-2">Time Range</label>
                                <select id="timeRange" class="w-full p-3 border border-gray-300 rounded-lg">
                                    <option>Last 1 month</option>
                                    <option>Last 3 months</option>
                                    <option>Last 6 months</option>
                                    <option>Custom</option>
                                </select>
                            </div>
                            <div>
                                <label class="block text-sm font-semibold mb-2">Competitor Universities</label>
                                <div class="space-y-2">
                                    <label class="flex items-center"><input type="checkbox" checked class="mr-2">Peking University</label>
                                    <label class="flex items-center"><input type="checkbox" checked class="mr-2">Tsinghua University</label>
                                    <label class="flex items-center"><input type="checkbox" class="mr-2">Shenzhen University</label>
                                </div>
                            </div>
                            <div>
                                <label class="block text-sm font-semibold mb-2">Platform</label>
                                <div class="space-y-2">
                                    <label class="flex items-center"><input type="checkbox" checked class="mr-2">Video Channel</label>
                                    <label class="flex items-center"><input type="checkbox" class="mr-2">Official Account</label>
                                </div>
                            </div>
                        </div>
                    </div>

                    <!-- Data overview -->
                    <div class="grid md:grid-cols-4 gap-4 mb-6">
                        <div class="card p-6 text-center">
                            <div class="text-3xl font-bold text-blue-600">156</div>
                            <div class="text-gray-500 text-sm">Accounts</div>
                        </div>
                        <div class="card p-6 text-center">
                            <div class="text-3xl font-bold text-green-600">3,847</div>
                            <div class="text-gray-500 text-sm">Posts</div>
                        </div>
                        <div class="card p-6 text-center">
                            <div class="text-3xl font-bold text-yellow-600">125K</div>
                            <div class="text-gray-500 text-sm">Total Likes</div>
                        </div>
                        <div class="card p-6 text-center">
                            <div class="text-3xl font-bold text-purple-600">42K</div>
                            <div class="text-gray-500 text-sm">Total Shares</div>
                        </div>
                    </div>

                    <!-- Competitor comparison table -->
                    <div class="card p-6">
                        <h3 class="font-bold mb-4">Competitor Comparison Overview</h3>
                        <div class="overflow-x-auto">
                            <table class="w-full text-sm">
                                <thead class="bg-gray-50">
                                    <tr>
                                        <th class="text-left p-3 font-semibold">Platform</th>
                                        <th class="text-left p-3 font-semibold">Metric</th>
                                        <th class="text-left p-3 font-semibold">Peking University</th>
                                        <th class="text-left p-3 font-semibold">Tsinghua University</th>
                                        <th class="text-left p-3 font-semibold">Shenzhen University</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    <tr class="border-b">
                                        <td rowspan="3" class="p-3 font-semibold">Video Channel</td>
                                        <td class="p-3">Posts</td>
                                        <td class="p-3">156</td>
                                        <td class="p-3">142</td>
                                        <td class="p-3">98</td>
                                    </tr>
                                    <tr class="border-b">
                                        <td class="p-3">Avg. Likes</td>
                                        <td class="p-3 text-green-600 font-semibold">324</td>
                                        <td class="p-3 text-green-600 font-semibold">298</td>
                                        <td class="p-3">187</td>
                                    </tr>
                                    <tr class="border-b">
                                        <td class="p-3">Avg. Shares</td>
                                        <td class="p-3 text-green-600 font-semibold">89</td>
                                        <td class="p-3 text-green-600 font-semibold">76</td>
                                        <td class="p-3">54</td>
                                    </tr>
                                    <tr>
                                        <td rowspan="2" class="p-3 font-semibold">Official Account</td>
                                        <td class="p-3">Posts</td>
                                        <td class="p-3">309</td>
                                        <td class="p-3">260</td>
                                        <td class="p-3">78</td>
                                    </tr>
                                    <tr>
                                        <td class="p-3">Avg. Reads</td>
                                        <td class="p-3 text-green-600 font-semibold">12.5K</td>
                                        <td class="p-3 text-green-600 font-semibold">11.8K</td>
                                        <td class="p-3">5.2K</td>
                                    </tr>
                                </tbody>
                            </table>
                        </div>
                    </div>

                    <!-- Orchestrator log -->
                    <div class="card p-6 mt-6">
                        <h3 class="font-bold mb-3">Orchestrator Routing Log</h3>
                        <div class="tlog" id="tlog-data" style="font:12px/1.5 ui-monospace,Consolas,monospace; background:#0c0e13; border:1px solid var(--border); border-radius:8px; padding:10px; overflow:auto; white-space:pre-wrap; max-height:200px; color:#e6e9ef;"></div>
                    </div>
                </section>

                <!-- 页面2：选题生成与评分 -->
                <section id="page-topic" class="page">
                    <div class="flex items-center justify-between mb-6">
                        <h2 class="text-2xl font-bold">Topic Generation & Scoring</h2>
                        <div class="flex space-x-3">
                            <button class="px-4 py-2 border border-gray-300 rounded-lg hover:bg-gray-50 transition">
                                <i class="fas fa-redo mr-2"></i>Regenerate
                            </button>
                            <button id="startBtn-topic" class="px-6 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition" onclick="start()">
                                <i class="fas fa-play mr-2"></i>Start Generating Topics
                            </button>
                        </div>
                    </div>

                    <div class="grid lg:grid-cols-3 gap-6">
                        <!-- Left: topic settings -->
                        <div class="lg:col-span-1">
                            <div class="card p-6 mb-6">
                                <h3 class="font-bold mb-4">Topic Direction</h3>
                                <textarea id="direction" class="w-full p-3 border border-gray-300 rounded-lg mb-4" rows="4" placeholder="Enter topic direction...">College admissions & major interpretation for students and parents, highlighting our university's strengths</textarea>
                                <button class="w-full py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition">
                                    Update Direction
                                </button>
                            </div>

                            <div class="card p-6">
                                <h3 class="font-bold mb-4">Trending Topics</h3>
                                <div class="flex flex-wrap gap-2">
                                    <span class="px-3 py-1 bg-blue-100 text-blue-600 rounded-full text-sm">College Application</span>
                                    <span class="px-3 py-1 bg-yellow-100 text-yellow-600 rounded-full text-sm">IC / Chip Majors</span>
                                    <span class="px-3 py-1 bg-green-100 text-green-600 rounded-full text-sm">Graduation Season</span>
                                    <span class="px-3 py-1 bg-purple-100 text-purple-600 rounded-full text-sm">Exam Results Date</span>
                                </div>
                            </div>
                        </div>

                        <!-- Center: candidate topic list -->
                        <div class="lg:col-span-2">
                            <div class="space-y-4" id="topicList">
                                <!-- Topic card 1 -->
                                <div class="topic-card card p-6 cursor-pointer selected">
                                    <div class="flex justify-between items-start mb-3">
                                        <h3 class="font-bold text-lg">Application Countdown — "The Truth About Majors" short-video series by current students</h3>
                                        <span class="px-3 py-1 bg-green-100 text-green-600 rounded-full font-bold">4.6</span>
                                    </div>
                                    <p class="text-gray-600 mb-4">Invite current students from different majors to share their real experiences in short videos, closing the information gap in college applications</p>
                                    <div class="grid grid-cols-5 gap-2 text-xs">
                                        <div class="text-center">
                                            <div class="text-gray-500 mb-1">Benchmark</div>
                                            <div class="score-bar"><div class="score-bar-fill score-high" style="width: 100%"></div></div>
                                        </div>
                                        <div class="text-center">
                                            <div class="text-gray-500 mb-1">Timing</div>
                                            <div class="score-bar"><div class="score-bar-fill score-high" style="width: 100%"></div></div>
                                        </div>
                                        <div class="text-center">
                                            <div class="text-gray-500 mb-1">Trend</div>
                                            <div class="score-bar"><div class="score-bar-fill score-high" style="width: 100%"></div></div>
                                        </div>
                                        <div class="text-center">
                                            <div class="text-gray-500 mb-1">Advantage</div>
                                            <div class="score-bar"><div class="score-bar-fill score-high" style="width: 100%"></div></div>
                                        </div>
                                        <div class="text-center">
                                            <div class="text-gray-500 mb-1">Feasibility</div>
                                            <div class="score-bar"><div class="score-bar-fill score-mid" style="width: 60%"></div></div>
                                        </div>
                                    </div>
                                </div>

                                <!-- Topic card 2 -->
                                <div class="topic-card card p-6 cursor-pointer">
                                    <div class="flex justify-between items-start mb-3">
                                        <h3 class="font-bold text-lg">IC School First Enrollment Feature — a new-engineering story amid the "chip boom"</h3>
                                        <span class="px-3 py-1 bg-green-100 text-green-600 rounded-full font-bold">4.35</span>
                                    </div>
                                    <p class="text-gray-600 mb-4">In-depth Official Account article + dean's interpretation on Video Channel, using a hot major + new school as a double hook</p>
                                    <div class="grid grid-cols-5 gap-2 text-xs">
                                        <div class="text-center">
                                            <div class="text-gray-500 mb-1">Benchmark</div>
                                            <div class="score-bar"><div class="score-bar-fill score-high" style="width: 100%"></div></div>
                                        </div>
                                        <div class="text-center">
                                            <div class="text-gray-500 mb-1">Timing</div>
                                            <div class="score-bar"><div class="score-bar-fill score-high" style="width: 100%"></div></div>
                                        </div>
                                        <div class="text-center">
                                            <div class="text-gray-500 mb-1">Trend</div>
                                            <div class="score-bar"><div class="score-bar-fill score-high" style="width: 100%"></div></div>
                                        </div>
                                        <div class="text-center">
                                            <div class="text-gray-500 mb-1">Advantage</div>
                                            <div class="score-bar"><div class="score-bar-fill score-high" style="width: 100%"></div></div>
                                        </div>
                                        <div class="text-center">
                                            <div class="text-gray-500 mb-1">Feasibility</div>
                                            <div class="score-bar"><div class="score-bar-fill score-mid" style="width: 60%"></div></div>
                                        </div>
                                    </div>
                                </div>

                                <!-- Topic card 3 -->
                                <div class="topic-card card p-6 cursor-pointer">
                                    <div class="flex justify-between items-start mb-3">
                                        <h3 class="font-bold text-lg">President's Open Letter to Applicants — Video Channel + Official Account combo</h3>
                                        <span class="px-3 py-1 bg-yellow-100 text-yellow-600 rounded-full font-bold">4.0</span>
                                    </div>
                                    <p class="text-gray-600 mb-4">A personal open letter, mirroring how the Tsinghua president speaks at the start-of-term / graduation moments</p>
                                    <div class="grid grid-cols-5 gap-2 text-xs">
                                        <div class="text-center">
                                            <div class="text-gray-500 mb-1">Benchmark</div>
                                            <div class="score-bar"><div class="score-bar-fill score-high" style="width: 100%"></div></div>
                                        </div>
                                        <div class="text-center">
                                            <div class="text-gray-500 mb-1">Timing</div>
                                            <div class="score-bar"><div class="score-bar-fill score-high" style="width: 100%"></div></div>
                                        </div>
                                        <div class="text-center">
                                            <div class="text-gray-500 mb-1">Trend</div>
                                            <div class="score-bar"><div class="score-bar-fill score-mid" style="width: 80%"></div></div>
                                        </div>
                                        <div class="text-center">
                                            <div class="text-gray-500 mb-1">Advantage</div>
                                            <div class="score-bar"><div class="score-bar-fill score-high" style="width: 100%"></div></div>
                                        </div>
                                        <div class="text-center">
                                            <div class="text-gray-500 mb-1">Feasibility</div>
                                            <div class="score-bar"><div class="score-bar-fill score-mid" style="width: 70%"></div></div>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>

                    <!-- Human confirmation area -->
                    <div class="card p-6 mt-6 border-2 border-yellow-300 hidden" id="humanPanel">
                        <div class="flex items-center justify-between">
                            <div>
                                <h3 class="font-bold text-lg mb-2"><i class="fas fa-user-check text-yellow-500 mr-2"></i>Human Confirmation</h3>
                                <p class="text-gray-600">Please confirm whether to approve this topic, or request a regeneration</p>
                                <p class="text-gray-600 mt-2">Recommended topic: <b id="recId-topic"></b> · top_score <b id="topScore-topic"></b></p>
                                <div id="candidates-topic" class="mt-2"></div>
                            </div>
                            <div class="space-x-3">
                                <input id="humanReply" class="border border-gray-300 rounded-lg px-3 py-2 mb-2 w-full" />
                                <button class="px-4 py-2 border border-gray-300 rounded-lg hover:bg-gray-50 transition" onclick="decide('reject')">
                                    <i class="fas fa-times mr-2"></i>Reject & Regenerate
                                </button>
                                <button class="px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 transition" onclick="decide('approve')">
                                    <i class="fas fa-check mr-2"></i>Approve Recommended Topic
                                </button>
                            </div>
                        </div>
                    </div>

                    <!-- Per-agent status -->
                    <div class="card p-6 mt-6">
                        <h3 class="font-bold mb-4">Each Agent's Input / Output</h3>
                        <div id="agents-topic"></div>
                    </div>
                </section>

                <!-- 页面3：内容创作与编辑 -->
                <section id="page-create" class="page">
                    <div class="flex items-center justify-between mb-6">
                        <h2 class="text-2xl font-bold">Content Creation & Editing</h2>
                        <div class="flex space-x-3">
                            <button class="px-4 py-2 border border-gray-300 rounded-lg hover:bg-gray-50 transition">
                                <i class="fas fa-save mr-2"></i>Save Draft
                            </button>
                            <button class="px-4 py-2 bg-yellow-600 text-white rounded-lg hover:bg-yellow-700 transition">
                                <i class="fas fa-eye mr-2"></i>Submit for Review
                            </button>
                            <button class="px-6 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 transition">
                                <i class="fas fa-paper-plane mr-2"></i>Publish
                            </button>
                        </div>
                    </div>

                    <div class="grid lg:grid-cols-3 gap-6">
                        <!-- Left: article navigation -->
                        <div class="lg:col-span-1">
                            <div class="card p-6 mb-6">
                                <h3 class="font-bold mb-4">Article Structure</h3>
                                <nav class="space-y-2">
                                    <a href="#" class="block px-4 py-2 bg-blue-100 text-blue-600 rounded-lg">Title</a>
                                    <a href="#" class="block px-4 py-2 hover:bg-gray-100 rounded-lg">Summary</a>
                                    <a href="#" class="block px-4 py-2 hover:bg-gray-100 rounded-lg">Body</a>
                                    <a href="#" class="block px-4 py-2 hover:bg-gray-100 rounded-lg">Cover Suggestions</a>
                                    <a href="#" class="block px-4 py-2 hover:bg-gray-100 rounded-lg">Push Time</a>
                                </nav>
                            </div>

                            <div class="card p-6">
                                <h3 class="font-bold mb-4">Review Status</h3>
                                <div class="space-y-3">
                                    <div class="flex items-center justify-between">
                                        <span>Brand Review</span>
                                        <span class="px-2 py-1 bg-yellow-100 text-yellow-600 rounded text-xs">Pending</span>
                                    </div>
                                    <div class="flex items-center justify-between">
                                        <span>Compliance Review</span>
                                        <span class="px-2 py-1 bg-yellow-100 text-yellow-600 rounded text-xs">Pending</span>
                                    </div>
                                    <div class="flex items-center justify-between">
                                        <span>Final Editing</span>
                                        <span class="px-2 py-1 bg-gray-100 text-gray-500 rounded text-xs">Not started</span>
                                    </div>
                                </div>
                            </div>
                        </div>

                        <!-- Center: editor -->
                        <div class="lg:col-span-2">
                            <div class="card overflow-hidden">
                                <!-- Editor toolbar -->
                                <div class="editor-toolbar bg-gray-50 p-3 flex flex-wrap gap-2">
                                    <button class="px-3 py-1 rounded"><i class="fas fa-bold"></i></button>
                                    <button class="px-3 py-1 rounded"><i class="fas fa-italic"></i></button>
                                    <button class="px-3 py-1 rounded"><i class="fas fa-underline"></i></button>
                                    <span class="border-l mx-2"></span>
                                    <button class="px-3 py-1 rounded"><i class="fas fa-list-ol"></i></button>
                                    <button class="px-3 py-1 rounded"><i class="fas fa-list-ul"></i></button>
                                    <span class="border-l mx-2"></span>
                                    <button class="px-3 py-1 rounded"><i class="fas fa-link"></i></button>
                                    <button class="px-3 py-1 rounded"><i class="fas fa-image"></i></button>
                                    <button class="px-3 py-1 rounded"><i class="fas fa-hashtag"></i></button>
                                </div>

                                <!-- Editing area -->
                                <div class="p-6">
                                    <input type="text" class="w-full text-2xl font-bold border-0 focus:ring-0 mb-6" placeholder="Enter title..." value="Application Countdown — &quot;The Truth About Majors&quot; short-video series by current students" />

                                    <textarea class="w-full border border-gray-300 rounded-lg p-4 mb-6 text-gray-600" rows="3" placeholder="Enter summary...">Invite current students from different majors to share their real experiences in short videos, closing the information gap in college applications</textarea>

                                    <textarea class="w-full border border-gray-300 rounded-lg p-4 min-h-96" placeholder="Enter body...">
# College Application Countdown

Dear applicants and parents,

The college entrance exam is over, and the crucial moment of filling in applications is approaching. At this important juncture, we have specially planned the "The Truth About Majors" short-video series, inviting current students from different majors at our university to share their real experiences in their own voices.

## Why this series?

Every application season, the questions applicants and parents care about most are usually:
- What does this major actually study?
- What are the job prospects?
- What is it really like to study in this major?

Official program descriptions tend to be abstract, so we hope that through current students' authentic accounts we can give everyone a more concrete reference.

## Series preview

We will invite students from the following majors:
- School of Integrated Circuits (first enrollment in 2026!)
- School of Computer Science
- School of Economics
- School of Foreign Languages
- School of Architecture

Each video is 60 seconds, telling the most authentic major experience!

## How to watch?

Please follow our Video Channel to get updates first.

We wish every applicant admission to their ideal university and major!
                                    </textarea>
                                </div>
                            </div>

                            <!-- Cover suggestions -->
                            <div class="card p-6 mt-6">
                                <h3 class="font-bold mb-4">Cover Suggestions</h3>
                                <div class="grid md:grid-cols-3 gap-4">
                                    <div class="border border-gray-300 rounded-lg p-4 bg-gray-50 text-center">
                                        <div class="h-32 bg-gradient-to-br from-blue-500 to-purple-600 rounded mb-3 flex items-center justify-center">
                                            <span class="text-white font-bold">Cover 1</span>
                                        </div>
                                        <p class="text-sm text-gray-500">Minimal big-type</p>
                                    </div>
                                    <div class="border border-gray-300 rounded-lg p-4 bg-gray-50 text-center">
                                        <div class="h-32 bg-gradient-to-br from-green-500 to-teal-600 rounded mb-3 flex items-center justify-center">
                                            <span class="text-white font-bold">Cover 2</span>
                                        </div>
                                        <p class="text-sm text-gray-500">Interview style</p>
                                    </div>
                                    <div class="border border-gray-300 rounded-lg p-4 bg-gray-50 text-center">
                                        <div class="h-32 bg-gradient-to-br from-yellow-500 to-orange-600 rounded mb-3 flex items-center justify-center">
                                            <span class="text-white font-bold">Cover 3</span>
                                        </div>
                                        <p class="text-sm text-gray-500">Data-viz style</p>
                                    </div>
                                </div>
                            </div>

                            <!-- Final draft preview -->
                            <div class="card p-6 mt-6 hidden" id="finalPanel-create">
                                <h3 class="font-bold mb-4">✅ Final Draft</h3>
                                <div id="finalBox-create" class="article" style="line-height:1.9;"></div>
                            </div>

                            <!-- Per-agent status -->
                            <div class="card p-6 mt-6">
                                <h3 class="font-bold mb-4">Each Agent's Input / Output</h3>
                                <div id="agents-create"></div>
                            </div>
                        </div>
                    </div>
                </section>

                <!-- 页面4：内容管理 -->
                <section id="page-manage" class="page">
                    <div class="flex items-center justify-between mb-6">
                        <h2 class="text-2xl font-bold">Content Management</h2>
                        <div class="flex space-x-3">
                            <button class="px-4 py-2 border border-gray-300 rounded-lg hover:bg-gray-50 transition">
                                <i class="fas fa-download mr-2"></i>Export Data
                            </button>
                        </div>
                    </div>

                    <!-- Tabs -->
                    <div class="mb-6">
                        <div class="flex space-x-1 border-b border-gray-300">
                            <button class="px-6 py-3 font-semibold text-blue-600 border-b-2 border-blue-600">Topic History</button>
                            <button class="px-6 py-3 text-gray-500 hover:text-gray-700">Publish Records</button>
                            <button class="px-6 py-3 text-gray-500 hover:text-gray-700">Template Management</button>
                        </div>
                    </div>

                    <!-- Topic history table -->
                    <div class="card overflow-hidden">
                        <table class="w-full text-sm">
                            <thead class="bg-gray-50">
                                <tr>
                                    <th class="text-left p-4 font-semibold">Date</th>
                                    <th class="text-left p-4 font-semibold">Topic Title</th>
                                    <th class="text-left p-4 font-semibold">Score</th>
                                    <th class="text-left p-4 font-semibold">Status</th>
                                    <th class="text-left p-4 font-semibold">Actions</th>
                                </tr>
                            </thead>
                            <tbody>
                                <tr class="border-b border-gray-300 hover:bg-gray-50">
                                    <td class="p-4">2026-06-16</td>
                                    <td class="p-4 font-medium">Application Countdown — "The Truth About Majors" short-video series by current students</td>
                                    <td class="p-4"><span class="text-green-600 font-bold">4.6</span></td>
                                    <td class="p-4"><span class="px-2 py-1 bg-green-100 text-green-600 rounded text-xs">Published</span></td>
                                    <td class="p-4">
                                        <button class="text-blue-600 hover:underline"><i class="fas fa-eye mr-1"></i>View</button>
                                    </td>
                                </tr>
                                <tr class="border-b border-gray-300 hover:bg-gray-50">
                                    <td class="p-4">2026-06-14</td>
                                    <td class="p-4 font-medium">IC School First Enrollment Feature — a new-engineering story amid the "chip boom"</td>
                                    <td class="p-4"><span class="text-green-600 font-bold">4.35</span></td>
                                    <td class="p-4"><span class="px-2 py-1 bg-yellow-100 text-yellow-600 rounded text-xs">In Review</span></td>
                                    <td class="p-4">
                                        <button class="text-blue-600 hover:underline"><i class="fas fa-eye mr-1"></i>View</button>
                                    </td>
                                </tr>
                                <tr class="border-b border-gray-300 hover:bg-gray-50">
                                    <td class="p-4">2026-06-10</td>
                                    <td class="p-4 font-medium">President's Open Letter to Applicants — Video Channel + Official Account combo</td>
                                    <td class="p-4"><span class="text-yellow-600 font-bold">4.0</span></td>
                                    <td class="p-4"><span class="px-2 py-1 bg-blue-100 text-blue-600 rounded text-xs">Draft</span></td>
                                    <td class="p-4">
                                        <button class="text-blue-600 hover:underline"><i class="fas fa-edit mr-1"></i>Edit</button>
                                    </td>
                                </tr>
                            </tbody>
                        </table>
                    </div>

                    <!-- Orchestrator log -->
                    <div class="card p-6 mt-6">
                        <h3 class="font-bold mb-3">Orchestrator Routing Log</h3>
                        <div class="tlog" id="tlog-manage" style="font:12px/1.5 ui-monospace,Consolas,monospace; background:#0c0e13; border:1px solid var(--border); border-radius:8px; padding:10px; overflow:auto; white-space:pre-wrap; max-height:200px; color:#e6e9ef;"></div>
                    </div>
                </section>
            </main>
        </div>
    </div>

    <script>
        const META = {
            analysis:["Analysis","gpt-4.1 · pydantic-ai"],
            topic_strategy:["Topic Strategy","gpt-4.1 · pydantic-ai"],
            editorial:["Editorial","gpt-5.x · pydantic-ai"],
            drafting:["Drafting","gpt-4.1 · pydantic-ai"],
            brand_review:["Brand Review","gpt-5.x · pydantic-ai"],
            compliance_review:["Compliance","gpt-5.x · LangGraph"],
            final_editor:["Final Editor","gpt-5.x · pydantic-ai"],
        };
        const ORDER = ["analysis","topic_strategy","editorial","drafting","brand_review","compliance_review","final_editor"];
        const open = {};
        let polling = null;
        let lastRec = "";
        let AGENT_MODELS = {};

        function esc(s) {
            return (s == null ? "" : String(s)).replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
        }

        // 页面切换函数
        async function setPage(page) {
            // 移除所有激活状态
            document.querySelectorAll(".nav-item").forEach(el => el.classList.remove("active"));
            document.querySelectorAll(".page").forEach(el => el.classList.remove("active"));

            // 添加当前激活状态
            const navItem = document.querySelector(`.nav-item[data-page="${page}"]`);
            if (navItem) navItem.classList.add("active");
            document.getElementById(`page-${page}`).classList.add("active");

            // 通知后端当前页面
            await fetch("/api/set_page", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ page: page })
            });
        }

        // 选题卡片点击事件
        document.querySelectorAll(".topic-card").forEach(card => {
            card.addEventListener("click", function() {
                document.querySelectorAll(".topic-card").forEach(el => el.classList.remove("selected"));
                this.classList.add("selected");
            });
        });

        async function loadPrereqs() {
            const p = await (await fetch("/api/prereqs")).json();
            const setupPanel = document.getElementById("setupPanel");
            if (p.ok) {
                setupPanel.classList.add("hidden");
            } else {
                setupPanel.classList.remove("hidden");
                document.getElementById("setupBody").innerHTML = "<ul>" + p.problems.map(x => "<li>" + esc(x) + "</li>").join("") + "</ul>";
            }
            // 禁用/启用启动按钮
            const startBtns = document.querySelectorAll("button[id^='startBtn']");
            startBtns.forEach(btn => btn.disabled = !p.ok);
        }

        async function start() {
            const startBtns = document.querySelectorAll("button[id^='startBtn']");
            startBtns.forEach(btn => btn.disabled = true);
            const hint = document.createElement("span");
            hint.innerHTML = '<span class="spin"></span> Starting (~8s for agents to connect)…';
            const firstBtn = startBtns[0];
            if (firstBtn) firstBtn.parentNode.appendChild(hint);

            const r = await fetch("/api/start", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ direction: document.getElementById("direction").value })
            });
            const j = await r.json();
            if (!j.ok) {
                alert((j.problems || ["Failed to start"]).join("\\n"));
                startBtns.forEach(btn => btn.disabled = false);
                return;
            }
            if (!polling) polling = setInterval(poll, 1500);
            poll();
        }

        async function stop() {
            await fetch("/api/stop", { method: "POST" });
        }

        async function decide(kind) {
            const box = document.getElementById("humanReply");
            let reply = box.value.trim();
            if (kind === "reject" && (!reply || reply.startsWith("approve")))
                reply = "reject: topic not suitable, please regenerate";
            if (kind === "approve")
                reply = reply || ("approve: " + (lastRec || ""));
            await fetch("/api/decision", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ reply: reply })
            });
            document.getElementById("humanPanel").classList.add("hidden");
        }

        function renderAgents(calls, targetId) {
            const byAgent = {};
            calls.forEach(c => { (byAgent[c.agent] = byAgent[c.agent] || []).push(c); });
            const container = document.getElementById(targetId);
            if (!container) return;
            container.innerHTML = ORDER.map(key => {
                const meta = META[key] || [key, ""];
                const cs = byAgent[key] || [];
                const last = cs[cs.length - 1];
                const status = last ? last.status : "pending";
                const runs = cs.length > 1 ? " ×" + cs.length : "";
                const role = AGENT_MODELS[key] || meta[1];
                const io = cs.map((c, i) => `<div><h4 style="margin:0 0 6px; font-size:11px; color:#9aa3b2; text-transform:uppercase;">Input${cs.length > 1 ? " #" + (i + 1) : ""}</h4><pre style="margin:0; background:#0c0e13; border:1px solid #e5e7eb; border-radius:7px; padding:9px; font:12px/1.5 ui-monospace,Consolas,monospace; white-space:pre-wrap; max-height:260px; overflow:auto;">${esc(c.input)}</pre></div><div><h4 style="margin:0 0 6px; font-size:11px; color:#9aa3b2; text-transform:uppercase;">Output${cs.length > 1 ? " #" + (i + 1) : ""}</h4><pre style="margin:0; background:#0c0e13; border:1px solid #e5e7eb; border-radius:7px; padding:9px; font:12px/1.5 ui-monospace,Consolas,monospace; white-space:pre-wrap; max-height:260px; overflow:auto;">${c.output == null ? (c.status === "running" ? "…running" : (c.error || "(none)")) : esc(JSON.stringify(c.output, null, 2))}</pre></div>`).join("");
                return `<div class="agent border border-gray-300 rounded-lg mb-4 overflow-hidden ${open[key] ? "open" : ""}" id="ag-${key}"><div class="hd flex items-center gap-2 p-3 cursor-pointer" onclick="toggle('${key}')"><span class="dot ${status}"></span><span class="name font-semibold">${esc(meta[0])}${runs}</span><span class="role text-gray-500 text-sm">${esc(role)}</span><span style="margin-left:auto" class="sub text-gray-500 text-sm">${status === "pending" ? "Pending" : status === "running" ? "Running" : status === "done" ? "Done" : "Error"} ▾</span></div><div class="io hidden border-t border-gray-300 p-3 gap-3" style="display: none;">${io || '<div class="sub text-gray-500 text-sm" style="grid-column:1/3">Not yet run</div>'}</div></div>`;
            }).join("");
        }

        function toggle(id) {
            open[id] = !open[id];
            const el = document.getElementById(`ag-${id}`);
            if (el) {
                el.classList.toggle("open");
                const ioDiv = el.querySelector(".io");
                if (ioDiv) {
                    ioDiv.style.display = open[id] ? "grid" : "none";
                }
            }
        }

        async function poll() {
            const s = await (await fetch("/api/state")).json();
            if (s.models) AGENT_MODELS = s.models;

            // 更新状态徽章
            document.getElementById("statusBadge").textContent = (s.running ? "Running · " : s.done ? "Finished · " : "") + s.status;

            // 渲染各页面的 Agents
            renderAgents(s.calls || [], "agents-topic");
            renderAgents(s.calls || [], "agents-create");

            // 更新所有日志
            ["tlog-data", "tlog-manage"].forEach(id => {
                const el = document.getElementById(id);
                if (el) {
                    el.innerHTML = (s.transitions || []).map(l => esc(l).replace(/-&gt;\\s*(\\w+)/g, "-> <b>$1</b>")).join("<br>");
                    el.scrollTop = el.scrollHeight;
                }
            });

            // 处理人工确认
            const hp = document.getElementById("humanPanel");
            if (s.needs_human) {
                lastRec = s.recommended_id || "";
                hp.classList.remove("hidden");
                document.getElementById("recId-topic").textContent = s.recommended_id || "—";
                document.getElementById("topScore-topic").textContent = s.top_score == null ? "—" : s.top_score;
                document.getElementById("humanReply").value = "approve: " + (s.recommended_id || "");
                document.getElementById("candidates-topic").innerHTML = (s.candidates || []).map(c => `<div class="cand border border-gray-300 rounded-lg p-2 mb-2 ${c.id === s.recommended_id ? "bg-green-50" : ""}"><b>${esc(c.id)}</b>: ${esc(c.title)}</div>`).join("");
            } else {
                hp.classList.add("hidden");
            }

            // 显示最终成稿
            if (s.output) {
                const o = s.output;
                const finalPanel = document.getElementById("finalPanel-create");
                finalPanel.classList.remove("hidden");
                document.getElementById("finalBox-create").innerHTML = `<h3 style="font-size:1.5em; font-weight:bold;">${esc(o.title)}</h3><div class="summary text-gray-500 italic">${esc(o.summary)}</div><div class="body whitespace-pre-wrap">${esc(o.body)}</div><div class="sub text-gray-500" style="margin-top:8px">Cover: ${esc(o.cover_image_suggestion)} · Push: ${esc(o.push_time_suggestion)}</div>`;
            }

            // 处理完成状态
            if (s.done) {
                clearInterval(polling);
                polling = null;
                const startBtns = document.querySelectorAll("button[id^='startBtn']");
                startBtns.forEach(btn => btn.disabled = false);
            }
        }

        // 初始化
        loadPrereqs();
    </script>
</body>
</html>
"""


def main() -> None:
    setup_utf8()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"True through-Band web UI (产品原型版) -> {url} (Ctrl+C to stop)")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
        _stop_procs()
        server.shutdown()


if __name__ == "__main__":
    main()
