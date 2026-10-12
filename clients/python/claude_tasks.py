import json
import time

import requests

TERMINAL = ("done", "failed", "cancelled")


class ClaudeTasks:
    def __init__(self, base_url: str, token: str, timeout: float = 30):
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"
        self.timeout = timeout

    def _req(self, method, path, **kw):
        r = self.s.request(method, self.base + path, timeout=self.timeout, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code}: {r.text}")
        return r.json()

    def create(self, prompt, **opts):
        """opts: title, model, allowed_tools, subdir, session_id, system_prompt"""
        return self._req("POST", "/v1/tasks", json={"prompt": prompt, **opts})

    def get(self, task_id):
        return self._req("GET", f"/v1/tasks/{task_id}")

    def list(self, status=None, limit=50):
        params = {"limit": limit, **({"status": status} if status else {})}
        return self._req("GET", "/v1/tasks", params=params)["tasks"]

    def events(self, task_id, after=0):
        return self._req("GET", f"/v1/tasks/{task_id}/events", params={"after": after})

    def cancel(self, task_id):
        return self._req("POST", f"/v1/tasks/{task_id}/cancel")

    def continue_(self, task_id, prompt, **opts):
        return self._req("POST", f"/v1/tasks/{task_id}/continue", json={"prompt": prompt, **opts})

    def delete(self, task_id):
        return self._req("DELETE", f"/v1/tasks/{task_id}")

    def wait(self, task_id, poll=5):
        """Block until the task ends; survives short server outages."""
        while True:
            try:
                t = self.get(task_id)
                if t["status"] in TERMINAL:
                    return t
            except (requests.ConnectionError, requests.Timeout):
                pass
            time.sleep(poll)

    def follow(self, task_id, after=0):
        """Yield progress events live; the last one has kind == "end"."""
        while True:
            try:
                with self.s.get(f"{self.base}/v1/tasks/{task_id}/stream",
                                params={"after": after}, stream=True, timeout=(10, 60)) as r:
                    if r.status_code >= 500:
                        raise requests.ConnectionError(r.status_code)
                    if r.status_code >= 400:
                        raise RuntimeError(f"{r.status_code}: {r.text}")
                    event = "message"
                    for raw in r.iter_lines():
                        line = raw.decode()
                        if line.startswith("event: "):
                            event = line[7:]
                        elif line.startswith("data: "):
                            data = json.loads(line[6:])
                            if event == "end":
                                yield {"kind": "end", **data}
                                return
                            after = data["seq"] + 1
                            yield data
                        elif line == "":
                            event = "message"
            except (requests.ConnectionError, requests.Timeout,
                    requests.exceptions.ChunkedEncodingError):
                time.sleep(3)  # network blip or service restart: resume from `after`


if __name__ == "__main__":
    import os

    api = ClaudeTasks("https://api.config-app.com", os.environ["CLAUDE_API_TOKEN"])
    task = api.create("Создай hello.py и запусти его",
                      title="demo", allowed_tools="Bash,Write", subdir="demo")
    print("task", task["id"])  # save it: this is how you come back later

    for ev in api.follow(task["id"]):
        if ev["kind"] == "tool_use":
            print(f"  -> {ev['tool']} {ev['input'][:80]}")
        elif ev["kind"] == "text":
            print(ev["text"])
        elif ev["kind"] == "end":
            print("status:", ev["status"])

    final = api.get(task["id"])
    print(final["result"] if final["status"] == "done" else final["error"])
