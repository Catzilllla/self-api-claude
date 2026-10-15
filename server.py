"""HTTP wrapper over agent CLIs: Claude Code (`claude -p`) and Codex (`codex exec`).

Every request picks an engine ("claude" or "codex", default ENGINE); a
continued task always stays on its parent's engine. See engines.py for how
each CLI is driven and how allowed_tools maps onto Codex sandboxes.

Endpoints:
  POST /v1/run                  — native, synchronous: prompt in, final result out
  POST /v1/chat/completions     — OpenAI-compatible (non-streaming)
  GET  /v1/models               — model list for OpenAI clients, all engines
  GET  /v1/engines              — engines, whether installed, their models and defaults
  GET  /health                  — liveness, no auth

Background tasks (survive client disconnects and service restarts):
  POST /v1/tasks                — start a task, returns its id immediately
  GET  /v1/tasks                — list tasks (?status=running&limit=50)
  GET  /v1/tasks/{id}           — status, result, cost, session_id
  GET  /v1/tasks/{id}/events    — progress so far (?after=<next>&raw=1)
  GET  /v1/tasks/{id}/stream    — live progress as Server-Sent Events
  POST /v1/tasks/{id}/cancel    — stop a queued or running task
  POST /v1/tasks/{id}/continue  — follow-up in the same CLI session (new task)
  DELETE /v1/tasks/{id}         — forget a finished task

A task may name an archive_dir inside WORKDIR: when it ends, its event log,
stderr, final state and the CLI's transcript of the whole session are copied
there (tasks/<id>.*, sessions/<engine>-<session_id>.jsonl).

Config via env:
  WRAPPER_TOKEN    required, Bearer token clients must send
  MAX_CONCURRENCY  parallel CLI processes, shared by runs and tasks (default 3)
  WORKDIR          base dir; each request runs in WORKDIR/<subdir> (default ./work);
                   subdir may be nested, e.g. "tg42/sessions/t7"
  TASKS_DIR        task database and event logs, outside WORKDIR (default ./tasks)
  DEFAULT_TOOLS    tools when the request doesn't specify any ("" = text only)
  ENGINE           engine when the request doesn't name one (default "claude")
  DEFAULT_MODEL    default Claude model alias (default "sonnet")
  CODEX_DEFAULT_MODEL  default Codex model (default: the account's top listed model)
  CLAUDE_BIN       path to the Claude CLI (default "claude")
  CODEX_BIN        path to the Codex CLI (default "codex")
  REQUEST_TIMEOUT  seconds per synchronous CLI call (default 600)
  TASK_TIMEOUT     seconds per background task (default 3600)
"""

import asyncio
import fcntl
import hmac
import json
import logging
import os
import re
import shutil
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from engines import Engine, load_engines

WRAPPER_TOKEN = os.environ.get("WRAPPER_TOKEN", "")
MAX_CONCURRENCY = int(os.environ.get("MAX_CONCURRENCY", "3"))
WORKDIR = Path(os.environ.get("WORKDIR", "./work")).resolve()
TASKS_DIR = Path(os.environ.get("TASKS_DIR", "./tasks")).resolve()
DEFAULT_TOOLS = os.environ.get("DEFAULT_TOOLS", "")
DEFAULT_ENGINE = os.environ.get("ENGINE", "claude")
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "600"))
TASK_TIMEOUT = float(os.environ.get("TASK_TIMEOUT", "3600"))

if not WRAPPER_TOKEN:
    raise RuntimeError("WRAPPER_TOKEN is not set")

ENGINES = load_engines()
if DEFAULT_ENGINE not in ENGINES:
    raise RuntimeError(f"ENGINE must be one of {', '.join(ENGINES)}")

WORKDIR.mkdir(parents=True, exist_ok=True)
TASKS_DIR.mkdir(parents=True, exist_ok=True)

# Up to 8 nested folders; without "." no segment can climb out of WORKDIR.
SUBDIR_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}(/[A-Za-z0-9_-]{1,64}){0,7}$")
SESSION_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
TOOLS_RE = re.compile(r"^[A-Za-z0-9_,\s()*:./-]*$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\[\]-]{0,63}$")
# A tools entry starting with "-" would be parsed by the CLI as one of its own flags.
FLAG_IN_TOOLS_RE = re.compile(r"(^|[\s,])-")
TASK_ID_RE = re.compile(r"^[0-9a-f]{32}$")

semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
log = logging.getLogger("uvicorn.error")


def require_token(authorization: str = Header(default="")) -> None:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), WRAPPER_TOKEN.encode()):
        raise HTTPException(status_code=401, detail="invalid token")


def resolve_cwd(subdir: str | None, field: str = "subdir") -> Path:
    if not subdir:
        return WORKDIR
    if not SUBDIR_RE.match(subdir):
        raise HTTPException(status_code=400, detail=f"{field} must be folders of [A-Za-z0-9_-]{{1,64}} joined by /")
    path = WORKDIR / subdir
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_engine(name: str | None) -> Engine:
    engine = ENGINES.get(name or DEFAULT_ENGINE)
    if engine is None:
        raise HTTPException(status_code=400, detail=f"engine must be one of {', '.join(ENGINES)}")
    if not engine.available():
        raise HTTPException(status_code=400, detail=f"engine {engine.name} is not installed on the server")
    return engine


def check_params(model: str | None, tools: str, session_id: str | None) -> None:
    if model and not MODEL_RE.match(model):
        raise HTTPException(status_code=400, detail="invalid model")
    if not TOOLS_RE.match(tools) or FLAG_IN_TOOLS_RE.search(tools):
        raise HTTPException(status_code=400, detail="invalid allowed_tools")
    if session_id and not SESSION_RE.match(session_id):
        raise HTTPException(status_code=400, detail="invalid session_id")


async def run_sync(
    engine_name: str | None,
    prompt: str,
    model: str | None,
    allowed_tools: str | None,
    subdir: str | None,
    session_id: str | None,
    system_prompt: str | None,
):
    engine = get_engine(engine_name)
    tools = DEFAULT_TOOLS if allowed_tools is None else allowed_tools
    check_params(model, tools, session_id)
    args = engine.build_args(model or engine.default_model, tools, session_id, system_prompt)
    result = engine.new_result()

    cwd = resolve_cwd(subdir)
    started = time.monotonic()
    async with semaphore:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=64 * 1024 * 1024,
        )

        async def consume() -> bytes:
            # Prompt goes via stdin: no argv length limits, not visible in `ps`.
            proc.stdin.write(prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()
            stderr_task = asyncio.create_task(proc.stderr.read())
            async for line in proc.stdout:
                try:
                    result.feed(json.loads(line))
                except ValueError:
                    continue
            await proc.wait()
            return await stderr_task

        try:
            stderr = await asyncio.wait_for(consume(), timeout=REQUEST_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise HTTPException(status_code=504, detail=f"{engine.name} timed out")

    if not result.finished:
        detail = stderr.decode(errors="replace")[-2000:]
        raise HTTPException(status_code=502, detail=f"{engine.name} failed (exit {proc.returncode}): {detail}")
    if result.is_error:
        raise HTTPException(status_code=502, detail=result.error)
    if result.duration_ms is None:
        result.duration_ms = int((time.monotonic() - started) * 1000)
    return engine, model or engine.default_model, result


# ---------------------------------------------------------------- background tasks

db = sqlite3.connect(TASKS_DIR / "tasks.db", check_same_thread=False, isolation_level=None)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")
db.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        title TEXT,
        status TEXT NOT NULL,
        prompt TEXT NOT NULL,
        model TEXT,
        allowed_tools TEXT NOT NULL,
        subdir TEXT,
        system_prompt TEXT,
        engine TEXT,
        parent_id TEXT,
        resume_session_id TEXT,
        session_id TEXT,
        result TEXT,
        error TEXT,
        cost_usd REAL,
        num_turns INTEGER,
        duration_ms INTEGER,
        usage TEXT,
        created_at REAL NOT NULL,
        started_at REAL,
        finished_at REAL
    )
""")
columns = {r["name"] for r in db.execute("PRAGMA table_info(tasks)")}
if "engine" not in columns:
    db.execute("ALTER TABLE tasks ADD COLUMN engine TEXT")  # older databases: those tasks ran on Claude
if "archive_dir" not in columns:
    db.execute("ALTER TABLE tasks ADD COLUMN archive_dir TEXT")

ACTIVE = ("queued", "running")
TERMINAL = ("done", "failed", "cancelled")

running_procs: dict[str, asyncio.subprocess.Process] = {}
task_handles: dict[str, asyncio.Task] = {}  # keeps background coroutines from being GC'd


def events_path(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.jsonl"


def stderr_path(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.stderr"


def db_update(task_id: str, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    db.execute(f"UPDATE tasks SET {cols} WHERE id = ?", (*fields.values(), task_id))


def load_task(task_id: str) -> dict:
    row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone() if TASK_ID_RE.match(task_id) else None
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    return dict(row)


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None


def task_engine(t: dict) -> Engine:
    return ENGINES[t["engine"] or "claude"]


def task_out(t: dict, full: bool = True) -> dict:
    out = {
        "id": t["id"],
        "title": t["title"],
        "status": t["status"],
        "engine": t["engine"] or "claude",
        "model": t["model"] or task_engine(t).default_model,
        "subdir": t["subdir"],
        "archive_dir": t["archive_dir"],
        "parent_id": t["parent_id"],
        "session_id": t["session_id"],
        "cost_usd": t["cost_usd"],
        "num_turns": t["num_turns"],
        "created_at": iso(t["created_at"]),
        "started_at": iso(t["started_at"]),
        "finished_at": iso(t["finished_at"]),
        "error": t["error"],
    }
    if full:
        out.update(
            prompt=t["prompt"],
            allowed_tools=t["allowed_tools"],
            result=t["result"],
            duration_ms=t["duration_ms"],
            usage=json.loads(t["usage"]) if t["usage"] else None,
        )
    else:
        out["prompt_preview"] = t["prompt"][:200]
        out["result_preview"] = (t["result"] or "")[:200] or None
    return out


def stderr_tail(task_id: str) -> str:
    try:
        return stderr_path(task_id).read_text(errors="replace")[-2000:].strip()
    except OSError:
        return ""


def archive(task_id: str) -> None:
    """Copy a finished task's logs and its CLI session transcript into WORKDIR/<archive_dir>."""
    t = load_task(task_id)
    if not t["archive_dir"]:
        return
    try:
        root = resolve_cwd(t["archive_dir"], "archive_dir")
        tasks = root / "tasks"
        tasks.mkdir(exist_ok=True)
        for src, name in ((events_path(task_id), f"{task_id}.events.jsonl"), (stderr_path(task_id), f"{task_id}.stderr.log")):
            if src.exists():
                shutil.copyfile(src, tasks / name)
        (tasks / f"{task_id}.json").write_text(json.dumps(task_out(t), ensure_ascii=False, indent=2))
        engine = task_engine(t)
        if t["session_id"] and (src := engine.session_file(t["session_id"])):
            (root / "sessions").mkdir(exist_ok=True)
            shutil.copyfile(src, root / "sessions" / f"{engine.name}-{t['session_id']}.jsonl")
    except Exception:  # an archive problem must never affect the task itself
        log.exception("archiving task %s failed", task_id)


async def run_task(task_id: str) -> None:
    try:
        t = load_task(task_id)
        engine = task_engine(t)
        args = engine.build_args(t["model"] or engine.default_model, t["allowed_tools"],
                                 t["resume_session_id"], t["system_prompt"])
        result = engine.new_result()
        async with semaphore:
            if load_task(task_id)["status"] != "queued":  # cancelled while waiting
                return
            started = time.time()
            db_update(task_id, status="running", started_at=started)
            timed_out = False
            with open(events_path(task_id), "ab") as events, open(stderr_path(task_id), "ab") as err:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    cwd=resolve_cwd(t["subdir"]),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=err,
                    limit=64 * 1024 * 1024,  # single events (big tool outputs) can be long lines
                )
                running_procs[task_id] = proc
                proc.stdin.write(t["prompt"].encode())
                await proc.stdin.drain()
                proc.stdin.close()

                async def consume():
                    async for line in proc.stdout:
                        if not line.endswith(b"\n"):
                            line += b"\n"
                        events.write(line)
                        events.flush()
                        try:
                            ev = json.loads(line)
                        except ValueError:
                            continue
                        result.feed(ev)
                        if session_id := engine.session_id(ev):
                            # Saved early so a failed or interrupted task can still be continued.
                            db_update(task_id, session_id=session_id)
                    await proc.wait()

                try:
                    await asyncio.wait_for(consume(), timeout=TASK_TIMEOUT)
                except asyncio.TimeoutError:
                    timed_out = True
                    proc.kill()
                    await proc.wait()

        now = time.time()
        if load_task(task_id)["status"] == "cancelled":
            pass
        elif result.finished and not result.is_error:
            db_update(
                task_id,
                status="done",
                finished_at=now,
                result=result.text,
                session_id=result.session_id,
                cost_usd=result.cost_usd,
                num_turns=result.num_turns,
                duration_ms=result.duration_ms or int((now - started) * 1000),
                usage=json.dumps(result.usage),
            )
        else:
            if timed_out:
                error = f"timed out after {TASK_TIMEOUT:.0f}s"
            elif result.finished:
                error = result.error
            else:
                error = f"{engine.name} exited with code {proc.returncode}: {stderr_tail(task_id)}"
            db_update(
                task_id,
                status="failed",
                finished_at=now,
                error=error,
                cost_usd=result.cost_usd,
                num_turns=result.num_turns,
            )
    except Exception as e:  # never leave a task stuck in "running"
        db_update(task_id, status="failed", finished_at=time.time(), error=f"wrapper error: {e!r}")
    finally:
        running_procs.pop(task_id, None)
        task_handles.pop(task_id, None)
        if load_task(task_id)["status"] in TERMINAL:
            archive(task_id)


def schedule(task_id: str) -> None:
    task_handles[task_id] = asyncio.create_task(run_task(task_id))


def create_task(
    engine_name: str | None,
    prompt: str,
    title: str | None,
    model: str | None,
    allowed_tools: str | None,
    subdir: str | None,
    session_id: str | None,
    system_prompt: str | None,
    parent_id: str | None = None,
    archive_dir: str | None = None,
) -> dict:
    engine = get_engine(engine_name)
    tools = DEFAULT_TOOLS if allowed_tools is None else allowed_tools
    check_params(model, tools, session_id)
    resolve_cwd(subdir)  # validate now, not in the background
    resolve_cwd(archive_dir, "archive_dir")
    task_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO tasks (id, title, status, prompt, model, allowed_tools, subdir, system_prompt, engine,"
        " parent_id, resume_session_id, archive_dir, created_at) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, title, prompt, model, tools, subdir, system_prompt, engine.name, parent_id, session_id,
         archive_dir, time.time()),
    )
    schedule(task_id)
    return task_out(load_task(task_id))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Startup runs before the port is bound, so a second instance would otherwise
    # mark the first one's live tasks as interrupted.
    lock = open(TASKS_DIR / "server.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError(f"another instance is already using {TASKS_DIR}")
    # Processes of a previous run died with it: mark them, and restart what never began.
    interrupted = [r["id"] for r in db.execute("SELECT id FROM tasks WHERE status = 'running'").fetchall()]
    db.execute(
        "UPDATE tasks SET status = 'failed', finished_at = ?,"
        " error = 'interrupted by server restart; use /continue to resume the session'"
        " WHERE status = 'running'",
        (time.time(),),
    )
    for task_id in interrupted:
        archive(task_id)
    for row in db.execute("SELECT id FROM tasks WHERE status = 'queued' ORDER BY created_at").fetchall():
        schedule(row["id"])
    yield
    for proc in list(running_procs.values()):
        proc.kill()
    lock.close()


app = FastAPI(title="Agent CLI API", lifespan=lifespan)


class RunRequest(BaseModel):
    prompt: str
    engine: str | None = None
    model: str | None = None
    allowed_tools: str | None = None
    subdir: str | None = None
    session_id: str | None = None
    system_prompt: str | None = None


@app.get("/health")
async def health():
    return {"ok": True, "engines": {name: e.available() for name, e in ENGINES.items()}}


@app.post("/v1/run", dependencies=[Depends(require_token)])
async def run(req: RunRequest):
    engine, model, result = await run_sync(
        req.engine, req.prompt, req.model, req.allowed_tools, req.subdir, req.session_id, req.system_prompt
    )
    return {
        "engine": engine.name,
        "model": model,
        "result": result.text,
        "session_id": result.session_id,
        "cost_usd": result.cost_usd,
        "duration_ms": result.duration_ms,
        "num_turns": result.num_turns,
        "usage": result.usage,
    }


class TaskRequest(RunRequest):
    title: str | None = None
    archive_dir: str | None = None  # where to copy logs when the task ends; inherited by /continue


class ContinueRequest(BaseModel):
    prompt: str
    title: str | None = None
    # Omitted fields are inherited from the parent task. The engine is always the
    # parent's: a CLI session can only be continued by the CLI that created it.
    engine: str | None = None
    model: str | None = None
    allowed_tools: str | None = None
    system_prompt: str | None = None
    archive_dir: str | None = None


@app.post("/v1/tasks", dependencies=[Depends(require_token)], status_code=202)
async def tasks_create(req: TaskRequest):
    return create_task(
        req.engine, req.prompt, req.title, req.model, req.allowed_tools, req.subdir, req.session_id,
        req.system_prompt, archive_dir=req.archive_dir,
    )


@app.get("/v1/tasks", dependencies=[Depends(require_token)])
async def tasks_list(status: str | None = None, limit: int = 50):
    limit = max(1, min(limit, 500))
    if status:
        rows = db.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ?", (status, limit)
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return {"tasks": [task_out(dict(r), full=False) for r in rows]}


@app.get("/v1/tasks/{task_id}", dependencies=[Depends(require_token)])
async def tasks_get(task_id: str):
    return task_out(load_task(task_id))


def read_events(task: dict, after: int, raw: bool, limit: int) -> tuple[list[dict], int]:
    """Events from line `after` on; returns them and the line number to resume from."""
    task_id, compact = task["id"], task_engine(task).compact
    items, seq = [], after
    try:
        with open(events_path(task_id), "rb") as f:
            for i, line in enumerate(f):
                if i < after:
                    continue
                if not line.endswith(b"\n") or len(items) >= limit:
                    break  # partially written line, or page is full
                seq = i + 1
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if raw:
                    items.append({"seq": i, **ev})
                else:
                    items.extend({"seq": i, **item} for item in compact(ev))
    except FileNotFoundError:
        pass
    return items, seq


@app.get("/v1/tasks/{task_id}/events", dependencies=[Depends(require_token)])
async def tasks_events(task_id: str, after: int = 0, raw: bool = False, limit: int = 1000):
    t = load_task(task_id)
    events, next_seq = read_events(t, max(after, 0), raw, max(1, min(limit, 5000)))
    return {"status": t["status"], "events": events, "next": next_seq}


@app.get("/v1/tasks/{task_id}/stream", dependencies=[Depends(require_token)])
async def tasks_stream(task_id: str, after: int = 0, raw: bool = False):
    load_task(task_id)

    async def gen():
        seq, idle = max(after, 0), 0.0
        while True:
            t = load_task(task_id)
            status = t["status"]
            events, seq = read_events(t, seq, raw, 1000)
            for ev in events:
                yield f"id: {ev['seq']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
            if events:
                idle = 0.0
            elif status in TERMINAL:
                # Status is read before the events, so nothing written before it finished is missed.
                yield f"event: end\ndata: {json.dumps(task_out(load_task(task_id), full=False), ensure_ascii=False)}\n\n"
                return
            else:
                idle += 1
                if idle >= 15:
                    yield ": keepalive\n\n"
                    idle = 0.0
            await asyncio.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.post("/v1/tasks/{task_id}/cancel", dependencies=[Depends(require_token)])
async def tasks_cancel(task_id: str):
    t = load_task(task_id)
    if t["status"] not in ACTIVE:
        raise HTTPException(status_code=409, detail=f"task is already {t['status']}")
    db_update(task_id, status="cancelled", finished_at=time.time())
    proc = running_procs.get(task_id)
    if proc and proc.returncode is None:
        proc.terminate()
    return task_out(load_task(task_id))


@app.post("/v1/tasks/{task_id}/continue", dependencies=[Depends(require_token)], status_code=202)
async def tasks_continue(task_id: str, req: ContinueRequest):
    parent = load_task(task_id)
    if parent["status"] in ACTIVE:
        raise HTTPException(status_code=409, detail="task is still active; wait or cancel it first")
    if not parent["session_id"]:
        raise HTTPException(status_code=409, detail="task has no CLI session to continue")
    engine = parent["engine"] or "claude"
    if req.engine and req.engine != engine:
        raise HTTPException(status_code=409, detail=f"this session belongs to {engine}; start a new task to switch engines")
    return create_task(
        engine,
        req.prompt,
        req.title or parent["title"],
        req.model or parent["model"],
        parent["allowed_tools"] if req.allowed_tools is None else req.allowed_tools,
        parent["subdir"],
        parent["session_id"],
        parent["system_prompt"] if req.system_prompt is None else req.system_prompt,
        parent_id=task_id,
        archive_dir=req.archive_dir or parent["archive_dir"],
    )


@app.delete("/v1/tasks/{task_id}", dependencies=[Depends(require_token)])
async def tasks_delete(task_id: str):
    t = load_task(task_id)
    if t["status"] in ACTIVE:
        raise HTTPException(status_code=409, detail="cancel the task first")
    db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    events_path(task_id).unlink(missing_ok=True)
    stderr_path(task_id).unlink(missing_ok=True)
    return {"deleted": task_id}


class ChatMessage(BaseModel):
    role: str
    content: str | list | None = None


class ChatRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage]
    stream: bool = False
    # Non-standard extras, passed through e.g. via `extra_body` in the OpenAI SDK.
    engine: str | None = None  # default: the engine that lists `model`, else ENGINE
    allowed_tools: str | None = None
    subdir: str | None = None
    session_id: str | None = None


def message_text(content: str | list | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")


@app.post("/v1/chat/completions", dependencies=[Depends(require_token)])
async def chat_completions(req: ChatRequest):
    if req.stream:
        raise HTTPException(status_code=400, detail="streaming is not supported")

    system = "\n\n".join(message_text(m.content) for m in req.messages if m.role in ("system", "developer"))
    dialog = [m for m in req.messages if m.role not in ("system", "developer")]
    if not dialog:
        raise HTTPException(status_code=400, detail="no user message")

    if req.session_id or len(dialog) == 1:
        # The CLI session already holds the history: send only the latest message.
        prompt = message_text(dialog[-1].content)
    else:
        # Stateless OpenAI clients resend the whole history each time: flatten it.
        lines = [f"{m.role.upper()}: {message_text(m.content)}" for m in dialog[:-1]]
        prompt = (
            "Conversation so far:\n\n" + "\n\n".join(lines)
            + "\n\nReply to the latest message:\n\n" + message_text(dialog[-1].content)
        )

    engine_name = req.engine or next(
        (name for name, e in ENGINES.items() if req.model and req.model in e.models()), None
    )
    engine, model, result = await run_sync(
        engine_name, prompt, req.model, req.allowed_tools, req.subdir, req.session_id, system or None
    )
    usage = result.usage or {}
    # Claude counts cached input separately; Codex includes it in input_tokens.
    prompt_tokens = (
        usage.get("input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
    )
    completion_tokens = usage.get("output_tokens", 0)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": result.text},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
        "session_id": result.session_id,
    }


@app.get("/v1/models", dependencies=[Depends(require_token)])
async def models():
    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "owned_by": e.owner, "engine": e.name}
            for e in ENGINES.values() if e.available() for m in e.models()
        ],
    }


@app.get("/v1/engines", dependencies=[Depends(require_token)])
async def engines():
    return {
        "default": DEFAULT_ENGINE,
        "engines": [
            {"name": e.name, "available": e.available(), "default_model": e.default_model, "models": e.models()}
            for e in ENGINES.values()
        ],
    }
