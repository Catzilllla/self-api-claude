const BASE = "https://api.config-app.com";
const HEADERS = {
  Authorization: `Bearer ${process.env.CLAUDE_API_TOKEN}`,
  "Content-Type": "application/json",
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function api(method, path, body) {
  const r = await fetch(BASE + path, {
    method,
    headers: HEADERS,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
  return r.json();
}

export const createTask = (prompt, opts = {}) => api("POST", "/v1/tasks", { prompt, ...opts });
export const getTask = (id) => api("GET", `/v1/tasks/${id}`);
export const listTasks = (status) => api("GET", `/v1/tasks${status ? `?status=${status}` : ""}`);
export const getEvents = (id, after = 0) => api("GET", `/v1/tasks/${id}/events?after=${after}`);
export const cancelTask = (id) => api("POST", `/v1/tasks/${id}/cancel`);
export const continueTask = (id, prompt, opts = {}) =>
  api("POST", `/v1/tasks/${id}/continue`, { prompt, ...opts });
export const deleteTask = (id) => api("DELETE", `/v1/tasks/${id}`);

// Live progress; the last yielded event has kind === "end".
export async function* follow(id, after = 0) {
  for (;;) {
    try {
      const r = await fetch(`${BASE}/v1/tasks/${id}/stream?after=${after}`, { headers: HEADERS });
      if (r.status >= 400 && r.status < 500) {
        const err = new Error(`${r.status}: ${await r.text()}`);
        err.fatal = true;
        throw err;
      }
      if (!r.ok) throw new Error(`${r.status}`);
      const reader = r.body.pipeThrough(new TextDecoderStream()).getReader();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += value;
        let i;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const block = buf.slice(0, i);
          buf = buf.slice(i + 2);
          let event = "message";
          let data = null;
          for (const line of block.split("\n")) {
            if (line.startsWith("event: ")) event = line.slice(7);
            else if (line.startsWith("data: ")) data = JSON.parse(line.slice(6));
          }
          if (!data) continue; // keepalive comment
          if (event === "end") {
            yield { kind: "end", ...data };
            return;
          }
          after = data.seq + 1;
          yield data;
        }
      }
    } catch (e) {
      if (e.fatal) throw e;
    }
    await sleep(3000); // dropped connection or service restart: resume from `after`
  }
}

// Usage
const task = await createTask("Найди TODO в проекте", {
  title: "todo",
  allowed_tools: "Read,Grep,Glob",
  subdir: "myproject",
});
for await (const ev of follow(task.id)) {
  if (ev.kind === "tool_use") console.log("->", ev.tool, ev.input.slice(0, 80));
  if (ev.kind === "text") console.log(ev.text);
  if (ev.kind === "end") console.log("status:", ev.status);
}
console.log((await getTask(task.id)).result);
