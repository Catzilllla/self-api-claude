"""Agent CLIs the API can drive: Claude Code (`claude -p`) and Codex (`codex exec`).

Each engine knows how to build its command line, turn its JSONL event
stream into the API's compact progress items, and fold the stream into a
final result. Both are always run in streaming mode; the synchronous
endpoints just wait for the stream to end.

`allowed_tools` uses Claude's tool names for both engines. Codex has no
per-tool allowlist, only OS sandbox modes, so the list is mapped:
  ""                          -> read-only, shell disabled (text only)
  Read, Grep, Glob, …         -> read-only
  Write / Edit / Bash(<rule>) -> workspace-write (writes only in the working dir, no network)
  Bash                        -> danger-full-access (no sandbox, like Claude's Bash)
  WebSearch / WebFetch        -> built-in web search on
A Codex sandbox name (read-only, workspace-write, danger-full-access) may
also be given directly, optionally followed by ",web".
"""

import json
import os
import re
import shutil
from pathlib import Path

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))

# Codex features that reach beyond the working directory: ChatGPT connectors
# (they include their own web search), plugins, browser and desktop control.
CODEX_DISABLED_FEATURES = (
    "apps", "plugins", "remote_plugin", "browser_use", "browser_use_external",
    "computer_use", "in_app_browser", "multi_agent",
)
CODEX_SANDBOXES = ("read-only", "workspace-write", "danger-full-access")
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
WEB_TOOLS = {"WebSearch", "WebFetch"}


def clip(value, limit: int = 2000) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + f"… [+{len(text) - limit} chars]"


def toml_string(text: str) -> str:
    # JSON string escapes are valid TOML; TOML additionally forbids a raw DEL.
    return json.dumps(text, ensure_ascii=False).replace("\x7f", "\\u007f")


class Engine:
    name: str
    bin: str
    owner: str

    def __init__(self, default_model: str):
        self.default_model = default_model
        if not self.default_model:
            self.default_model = self.fallback_model()

    def available(self) -> bool:
        return shutil.which(self.bin) is not None

    def models(self) -> list[str]:
        raise NotImplementedError

    def fallback_model(self) -> str:
        models = self.models()
        return models[0] if models else ""

    def build_args(self, model: str, tools: str, session_id: str | None, system_prompt: str | None) -> list[str]:
        raise NotImplementedError

    def session_id(self, ev: dict) -> str | None:
        """Session id, if this event announces it (saved early, so failed tasks can be continued)."""
        raise NotImplementedError

    def compact(self, ev: dict) -> list[dict]:
        """One raw event -> short, human-readable progress items."""
        raise NotImplementedError

    def new_result(self) -> "Result":
        raise NotImplementedError


class Result:
    """Folds an engine's event stream into the final outcome."""

    def __init__(self):
        self.finished = False  # the CLI reported an outcome, success or error
        self.is_error = False
        self.text = ""
        self.error: str | None = None
        self.session_id: str | None = None
        self.cost_usd: float | None = None
        self.num_turns: int | None = None
        self.duration_ms: int | None = None  # None: the wrapper measures it
        self.usage: dict | None = None

    def feed(self, ev: dict) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------- Claude Code


class Claude(Engine):
    name, owner = "claude", "anthropic"

    def __init__(self, default_model: str):
        self.bin = CLAUDE_BIN
        super().__init__(default_model)

    def models(self) -> list[str]:
        return ["sonnet", "opus", "haiku"]

    def build_args(self, model, tools, session_id, system_prompt):
        # --tools limits what exists at all; --allowedTools pre-approves the same set,
        # since headless mode can't ask for permission interactively.
        # "--opt=value" keeps a value that starts with "-" from being read as a flag.
        args = [
            self.bin, "-p",
            "--output-format", "stream-json", "--verbose",
            f"--model={model}",
            "--tools", tools,
            "--strict-mcp-config",
        ]
        if tools:
            args += ["--allowedTools", tools]
        if session_id:
            args.append(f"--resume={session_id}")
        if system_prompt:
            args.append(f"--append-system-prompt={system_prompt}")
        return args

    def session_id(self, ev):
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            return ev.get("session_id")
        return None

    def compact(self, ev):
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

    def new_result(self):
        return ClaudeResult()


class ClaudeResult(Result):
    def feed(self, ev):
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            self.session_id = ev.get("session_id")
        elif ev.get("type") == "result":
            self.finished = True
            self.is_error = bool(ev.get("is_error"))
            self.text = ev.get("result") or ""
            if self.is_error:
                self.error = ev.get("result") or ev.get("subtype") or "claude reported an error"
            self.session_id = ev.get("session_id") or self.session_id
            self.cost_usd = ev.get("total_cost_usd")
            self.num_turns = ev.get("num_turns")
            self.duration_ms = ev.get("duration_ms")
            self.usage = ev.get("usage")


# ---------------------------------------------------------------- Codex


def codex_policy(tools: str) -> tuple[str, bool, bool]:
    """Claude-style tool list -> (sandbox mode, web search, shell)."""
    entries = [t.strip() for t in tools.split(",") if t.strip()]
    if entries and entries[0] in CODEX_SANDBOXES:
        return entries[0], "web" in entries[1:], True
    names = {re.split(r"[(\s]", t, 1)[0] for t in entries}
    web = bool(names & WEB_TOOLS)
    if "Bash" in entries:  # unrestricted Bash runs anything as the service user
        sandbox = "danger-full-access"
    elif names & WRITE_TOOLS or "Bash" in names:  # Bash(<rule>) can't be mirrored: sandbox it
        sandbox = "workspace-write"
    else:
        sandbox = "read-only"
    return sandbox, web, bool(names)


def codex_error_message(raw) -> str:
    # Errors often carry the API's JSON body as a string: dig out the readable message.
    text = raw.get("message", "") if isinstance(raw, dict) else str(raw or "")
    try:
        body = json.loads(text)
        return body.get("error", {}).get("message") or body.get("message") or text
    except (ValueError, AttributeError):
        return text


def shell_command(command: str) -> str:
    """Codex runs everything as `/bin/bash -lc '<cmd>'`: show just <cmd>."""
    m = re.fullmatch(r"(?:/usr)?/bin/(?:ba|z)?sh -lc (['\"])(.*)\1", command, re.S)
    if not m:
        return command
    if m.group(1) == "'":
        return m.group(2).replace("'\\''", "'")
    return re.sub(r'\\([\\"$`])', r"\1", m.group(2))


class Codex(Engine):
    name, owner = "codex", "openai"

    def __init__(self, default_model: str):
        self.bin = CODEX_BIN
        super().__init__(default_model)

    def models(self) -> list[str]:
        # The CLI caches the models this account can use; listed ones, best first.
        try:
            cache = json.loads((CODEX_HOME / "models_cache.json").read_text())
            listed = [m for m in cache.get("models", []) if m.get("visibility") == "list" and m.get("slug")]
            slugs = [m["slug"] for m in sorted(listed, key=lambda m: m.get("priority", 1000))]
        except (OSError, ValueError, AttributeError):
            slugs = []
        return slugs or ([self.default_model] if self.default_model else [])

    def build_args(self, model, tools, session_id, system_prompt):
        sandbox, web, shell = codex_policy(tools)
        args = [self.bin, "exec"]
        if session_id:
            args.append("resume")  # takes no --sandbox/--cd: everything goes through -c
        args += ["--json", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules"]
        if model:
            args.append(f"--model={model}")
        config = {
            "sandbox_mode": toml_string(sandbox),
            "approval_policy": '"never"',  # nobody is there to approve: denied actions just fail
            "web_search": '"live"' if web else '"disabled"',
        }
        for feature in CODEX_DISABLED_FEATURES:
            config[f"features.{feature}"] = "false"
        if not shell:
            config["features.shell_tool"] = config["features.unified_exec"] = "false"
        if system_prompt:
            config["developer_instructions"] = toml_string(system_prompt)
        for key, value in config.items():
            args += ["-c", f"{key}={value}"]
        if session_id:
            args.append(session_id)
        args.append("-")  # prompt from stdin
        return args

    def session_id(self, ev):
        return ev.get("thread_id") if ev.get("type") == "thread.started" else None

    def compact(self, ev):
        kind, item = ev.get("type"), ev.get("item") or {}
        itype = item.get("type")
        if kind == "thread.started":
            return [{"kind": "start", "model": None, "session_id": ev.get("thread_id")}]
        if kind == "turn.completed":
            return [{"kind": "result", "is_error": False, "result": None, "cost_usd": None,
                     "num_turns": None, "duration_ms": None, "usage": ev.get("usage")}]
        if kind == "turn.failed":
            return [{"kind": "result", "is_error": True, "result": codex_error_message(ev.get("error")),
                     "cost_usd": None, "num_turns": None, "duration_ms": None}]
        if kind not in ("item.started", "item.completed"):
            return []
        started = kind == "item.started"
        if itype == "agent_message" and not started:
            return [{"kind": "text", "text": item.get("text", "")}]
        if itype == "reasoning" and not started and item.get("text"):
            return [{"kind": "thinking", "text": clip(item["text"], 1000)}]
        if itype == "command_execution":
            if started:
                return [{"kind": "tool_use", "tool": "Bash", "input": clip({"command": shell_command(item.get("command", ""))}, 500)}]
            failed = item.get("status") != "completed" or (item.get("exit_code") or 0) != 0
            return [{"kind": "tool_result", "is_error": failed, "output": clip(item.get("aggregated_output") or "", 1000)}]
        if itype == "file_change":
            changes = item.get("changes") or []
            summary = ", ".join(f"{c.get('kind')} {Path(c.get('path', '')).name}" for c in changes)
            if started:
                path = changes[0].get("path", "") if len(changes) == 1 else summary
                return [{"kind": "tool_use", "tool": "Edit", "input": clip({"file_path": path}, 500)}]
            return [{"kind": "tool_result", "is_error": item.get("status") != "completed", "output": clip(summary, 1000)}]
        if itype == "web_search" and not started:  # the query is only known once it ran
            return [{"kind": "tool_use", "tool": "WebSearch", "input": clip({"query": item.get("query", "")}, 500)}]
        if itype == "mcp_tool_call":
            tool = f"{item.get('server')}.{item.get('tool')}"
            if started:
                return [{"kind": "tool_use", "tool": tool, "input": clip(item.get("arguments"), 500)}]
            return [{"kind": "tool_result", "is_error": bool(item.get("error")),
                     "output": clip(item.get("error") or item.get("result") or "", 1000)}]
        if itype == "todo_list":
            todos = [f"{'✓' if t.get('completed') else '·'} {t.get('text', '')}" for t in item.get("items") or []]
            return [{"kind": "tool_use", "tool": "TodoWrite", "input": clip({"description": "; ".join(todos)}, 500)}]
        return []

    def new_result(self):
        return CodexResult()


class CodexResult(Result):
    def __init__(self):
        super().__init__()
        self.tool_calls = 0
        self.last_stream_error: str | None = None

    def feed(self, ev):
        kind, item = ev.get("type"), ev.get("item") or {}
        if kind == "thread.started":
            self.session_id = ev.get("thread_id")
        elif kind == "item.completed" and item.get("type") == "agent_message":
            self.text = item.get("text", "")  # the last message is the answer
        elif kind == "item.completed" and item.get("type") in ("command_execution", "file_change", "web_search", "mcp_tool_call"):
            self.tool_calls += 1
        elif kind == "error":
            self.last_stream_error = codex_error_message(ev)
        elif kind == "turn.completed":
            self.finished = True
            self.num_turns = self.tool_calls + 1
            usage = ev.get("usage") or {}
            if self.usage:  # several turns in one run: add them up
                usage = {k: self.usage.get(k, 0) + v for k, v in usage.items() if isinstance(v, int)}
            self.usage = usage
        elif kind == "turn.failed":
            self.finished = self.is_error = True
            self.error = codex_error_message(ev.get("error")) or self.last_stream_error or "codex reported an error"


def load_engines() -> dict[str, Engine]:
    return {
        "claude": Claude(os.environ.get("DEFAULT_MODEL", "sonnet")),
        "codex": Codex(os.environ.get("CODEX_DEFAULT_MODEL", "")),
    }
