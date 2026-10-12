"""HTTP wrapper over `claude -p` (headless Claude Code CLI).

Endpoints:
  POST /v1/run                  — native, synchronous: prompt in, CLI JSON result out
  POST /v1/chat/completions     — OpenAI-compatible (non-streaming)
  GET  /v1/models               — model list for OpenAI clients
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

Config via env:
  WRAPPER_TOKEN    required, Bearer token clients must send
  MAX_CONCURRENCY  parallel CLI processes, shared by runs and tasks (default 3)
  WORKDIR          base dir; each request runs in WORKDIR/<subdir> (default ./work)
  TASKS_DIR        task database and event logs, outside WORKDIR (default ./tasks)
  DEFAULT_TOOLS    tools when the request doesn't specify any ("" = text only)
  DEFAULT_MODEL    default model alias (default "sonnet")
  CLAUDE_BIN       path to the CLI (default "claude")
  REQUEST_TIMEOUT  seconds per synchronous CLI call (default 600)
  TASK_TIMEOUT     seconds per background task (default 3600)
"""

import asyncio
import fcntl
import hmac
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

WRAPPER_TOKEN = os.environ.get("WRAPPER_TOKEN", "")
MAX_CONCURRENCY = int(os.environ.get("MAX_CONCURRENCY", "3"))
WORKDIR = Path(os.environ.get("WORKDIR", "./work")).resolve()
TASKS_DIR = Path(os.environ.get("TASKS_DIR", "./tasks")).resolve()
DEFAULT_TOOLS = os.environ.get("DEFAULT_TOOLS", "")
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "sonnet")
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "600"))
TASK_TIMEOUT = float(os.environ.get("TASK_TIMEOUT", "3600"))

if not WRAPPER_TOKEN:
    raise RuntimeError("WRAPPER_TOKEN is not set")

WORKDIR.mkdir(parents=True, exist_ok=True)
TASKS_DIR.mkdir(parents=True, exist_ok=True)

SUBDIR_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SESSION_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
TOOLS_RE = re.compile(r"^[A-Za-z0-9_,\s()*:./-]*$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\[\]-]{0,63}$")
# A tools entry starting with "-" would be parsed by the CLI as one of its own flags.
FLAG_IN_TOOLS_RE = re.compile(r"(^|[\s,])-")
TASK_ID_RE = re.compile(r"^[0-9a-f]{32}$")

semaphore = asyncio.Semaphore(MAX_CONCURRENCY)


def require_token(authorization: str = Header(default="")) -> None:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), WRAPPER_TOKEN.encode()):
        raise HTTPException(status_code=401, detail="invalid token")


def resolve_cwd(subdir: str | None) -> Path:
    if not subdir:
        return WORKDIR
    if not SUBDIR_RE.match(subdir):
        raise HTTPException(status_code=400, detail="subdir must match [A-Za-z0-9_-]{1,64}")
    path = WORKDIR / subdir
    path.mkdir(exist_ok=True)
    return path


def check_params(model: str | None, tools: str, session_id: str | None) -> None:
    if model and not MODEL_RE.match(model):
        raise HTTPException(status_code=400, detail="invalid model")
    if not TOOLS_RE.match(tools) or FLAG_IN_TOOLS_RE.search(tools):
        raise HTTPException(status_code=400, detail="invalid allowed_tools")
    if session_id and not SESSION_RE.match(session_id):
        raise HTTPException(status_code=400, detail="invalid session_id")


def build_args(
    output_format: str,
    model: str | None,
    tools: str,
    session_id: str | None,
    system_prompt: str | None,
) -> list[str]:
    # --tools limits what exists at all; --allowedTools pre-approves the same set,
    # since headless mode can't ask for permission interactively.
    # "--opt=value" keeps a value that starts with "-" from being read as a flag.
    args = [
        CLAUDE_BIN, "-p",
        "--output-format", output_format,
        f"--model={model or DEFAULT_MODEL}",
        "--tools", tools,
        "--strict-mcp-config",
    ]
    if output_format == "stream-json":
        args.append("--verbose")  # required by the CLI for stream-json in -p mode
    if tools:
        args += ["--allowedTools", tools]
    if session_id:
        args.append(f"--resume={session_id}")
    if system_prompt:
        args.append(f"--append-system-prompt={system_prompt}")
    return args


async def run_claude(
    prompt: str,
    model: str | None,
    allowed_tools: str | None,
    subdir: str | None,
    session_id: str | None,
    system_prompt: str | None,
) -> dict:
    tools = DEFAULT_TOOLS if allowed_tools is None else allowed_tools
    check_params(model, tools, session_id)
    args = build_args("json", model, tools, session_id, system_prompt)

    cwd = resolve_cwd(subdir)
    async with semaphore:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            # Prompt goes via stdin: no argv length limits, not visible in `ps`.
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode()), timeout=REQUEST_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise HTTPException(status_code=504, detail="claude timed out")

    try:
        result = json.loads(stdout)
    except json.JSONDecodeError:
        detail = (stderr or stdout).decode(errors="replace")[-2000:]
        raise HTTPException(status_code=502, detail=f"claude failed (exit {proc.returncode}): {detail}")
    if result.get("is_error"):
        raise HTTPException(status_code=502, detail=result.get("result") or result)
    return result


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


def task_out(t: dict, full: bool = True) -> dict:
    out = {
        "id": t["id"],
        "title": t["title"],
        "status": t["status"],
        "model": t["model"] or DEFAULT_MODEL,
        "subdir": t["subdir"],
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


def clip(value, limit: int = 2000) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + f"… [+{len(text) - limit} chars]"


def compact(ev: dict) -> list[dict]:
    """Turn one raw stream-json event into short, human-readable progress items."""
    kind = ev.get("type")
    if kind == "system" and ev.get("subtype") == "init":
        return [{"kind": "start", "model": ev.get("model"), "session_id": ev.get("session_id")}]
    if kind == "assistant":
        items = []
        for part in ev.get("message", {}).get("content") or []:
            if part.get("type") == "text":
                items.append({"kind": "text", "text": part.get("text", "")})
            elif part.get("type") == "tool_use":
                items.append({"kind": "tool_use", "tool": part.get("name"), "input": clip(part.get("input"), 500)})
            elif part.get("type") == "thinking" and part.get("thinking"):
                items.append({"kind": "thinking", "text": clip(part["thinking"], 1000)})
        return items
    if kind == "user":
        items = []
        content = ev.get("message", {}).get("content")
        for part in content if isinstance(content, list) else []:
            if isinstance(part, dict) and part.get("type") == "tool_result":
                output = part.get("content")
                if isinstance(output, list):
                    output = "\n".join(p.get("text", "") for p in output if isinstance(p, dict))
                items.append({"kind": "tool_result", "is_error": bool(part.get("is_error")), "output": clip(output or "", 1000)})
        return items
    if kind == "result":
        return [{
            "kind": "result",
            "is_error": ev.get("is_error"),
            "result": ev.get("result"),
            "cost_usd": ev.get("total_cost_usd"),
            "num_turns": ev.get("num_turns"),
            "duration_ms": ev.get("duration_ms"),
        }]
    return []


def stderr_tail(task_id: str) -> str:
    try:
        return stderr_path(task_id).read_text(errors="replace")[-2000:].strip()
    except OSError:
        return ""


async def run_task(task_id: str) -> None:
    try:
        t = load_task(task_id)
        args = build_args("stream-json", t["model"], t["allowed_tools"], t["resume_session_id"], t["system_prompt"])
        async with semaphore:
            if load_task(task_id)["status"] != "queued":  # cancelled while waiting
                return
            db_update(task_id, status="running", started_at=time.time())
            final, timed_out = None, False
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
                    nonlocal final
                    async for line in proc.stdout:
                        if not line.endswith(b"\n"):
                            line += b"\n"
                        events.write(line)
                        events.flush()
                        try:
                            ev = json.loads(line)
                        except ValueError:
                            continue
                        if ev.get("type") == "system" and ev.get("subtype") == "init":
                            # Saved early so a failed or interrupted task can still be continued.
                            db_update(task_id, session_id=ev.get("session_id"))
                        elif ev.get("type") == "result":
                            final = ev
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
        elif final and not final.get("is_error"):
            db_update(
                task_id,
                status="done",
                finished_at=now,
                result=final.get("result", ""),
                session_id=final.get("session_id"),
                cost_usd=final.get("total_cost_usd"),
                num_turns=final.get("num_turns"),
                duration_ms=final.get("duration_ms"),
                usage=json.dumps(final.get("usage")),
            )
        else:
            if timed_out:
                error = f"timed out after {TASK_TIMEOUT:.0f}s"
            elif final:
                error = final.get("result") or final.get("subtype") or "claude reported an error"
            else:
                error = f"claude exited with code {proc.returncode}: {stderr_tail(task_id)}"
            db_update(
                task_id,
                status="failed",
                finished_at=now,
                error=error,
                cost_usd=(final or {}).get("total_cost_usd"),
                num_turns=(final or {}).get("num_turns"),
            )
    except Exception as e:  # never leave a task stuck in "running"
        db_update(task_id, status="failed", finished_at=time.time(), error=f"wrapper error: {e!r}")
    finally:
        running_procs.pop(task_id, None)
        task_handles.pop(task_id, None)


def schedule(task_id: str) -> None:
    task_handles[task_id] = asyncio.create_task(run_task(task_id))


def create_task(
    prompt: str,
    title: str | None,
    model: str | None,
    allowed_tools: str | None,
    subdir: str | None,
    session_id: str | None,
    system_prompt: str | None,
    parent_id: str | None = None,
) -> dict:
    tools = DEFAULT_TOOLS if allowed_tools is None else allowed_tools
    check_params(model, tools, session_id)
    resolve_cwd(subdir)  # validate now, not in the background
    task_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO tasks (id, title, status, prompt, model, allowed_tools, subdir, system_prompt,"
        " parent_id, resume_session_id, created_at) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, title, prompt, model, tools, subdir, system_prompt, parent_id, session_id, time.time()),
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
    db.execute(
        "UPDATE tasks SET status = 'failed', finished_at = ?,"
        " error = 'interrupted by server restart; use /continue to resume the session'"
        " WHERE status = 'running'",
        (time.time(),),
    )
    for row in db.execute("SELECT id FROM tasks WHERE status = 'queued' ORDER BY created_at").fetchall():
        schedule(row["id"])
    yield
    for proc in list(running_procs.values()):
        proc.kill()
    lock.close()


app = FastAPI(title="Claude CLI API", lifespan=lifespan)


class RunRequest(BaseModel):
    prompt: str
    model: str | None = None
    allowed_tools: str | None = None
    subdir: str | None = None
    session_id: str | None = None
    system_prompt: str | None = None


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/v1/run", dependencies=[Depends(require_token)])
async def run(req: RunRequest):
    result = await run_claude(
        req.prompt, req.model, req.allowed_tools, req.subdir, req.session_id, req.system_prompt
    )
    return {
        "result": result.get("result", ""),
        "session_id": result.get("session_id"),
        "cost_usd": result.get("total_cost_usd"),
        "duration_ms": result.get("duration_ms"),
        "num_turns": result.get("num_turns"),
        "usage": result.get("usage"),
    }


class TaskRequest(RunRequest):
    title: str | None = None


class ContinueRequest(BaseModel):
    prompt: str
    title: str | None = None
    # Omitted fields are inherited from the parent task.
    model: str | None = None
    allowed_tools: str | None = None
    system_prompt: str | None = None


@app.post("/v1/tasks", dependencies=[Depends(require_token)], status_code=202)
async def tasks_create(req: TaskRequest):
    return create_task(
        req.prompt, req.title, req.model, req.allowed_tools, req.subdir, req.session_id, req.system_prompt
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


def read_events(task_id: str, after: int, raw: bool, limit: int) -> tuple[list[dict], int]:
    """Events from line `after` on; returns them and the line number to resume from."""
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
    status = load_task(task_id)["status"]
    events, next_seq = read_events(task_id, max(after, 0), raw, max(1, min(limit, 5000)))
    return {"status": status, "events": events, "next": next_seq}


@app.get("/v1/tasks/{task_id}/stream", dependencies=[Depends(require_token)])
async def tasks_stream(task_id: str, after: int = 0, raw: bool = False):
    load_task(task_id)

    async def gen():
        seq, idle = max(after, 0), 0.0
        while True:
            status = load_task(task_id)["status"]
            events, seq = read_events(task_id, seq, raw, 1000)
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
    return create_task(
        req.prompt,
        req.title or parent["title"],
        req.model or parent["model"],
        parent["allowed_tools"] if req.allowed_tools is None else req.allowed_tools,
        parent["subdir"],
        parent["session_id"],
        parent["system_prompt"] if req.system_prompt is None else req.system_prompt,
        parent_id=task_id,
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

    result = await run_claude(
        prompt, req.model, req.allowed_tools, req.subdir, req.session_id, system or None
    )
    usage = result.get("usage") or {}
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
        "model": req.model or DEFAULT_MODEL,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": result.get("result", "")},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
        "session_id": result.get("session_id"),
    }


@app.get("/v1/models", dependencies=[Depends(require_token)])
async def models():
    return {
        "object": "list",
        "data": [{"id": m, "object": "model", "owned_by": "anthropic"} for m in ("sonnet", "opus", "haiku")],
    }
