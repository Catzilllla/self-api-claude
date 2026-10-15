# Claude CLI API — руководство

15 октября 2026

## Введение

Сервер `https://api.config-app.com` принимает HTTP-запросы и передаёт их агентному CLI, залогиненному на VPS через подписку. Ответ возвращается в JSON. API-ключи не нужны: CLI работают от пользователя `claude` под его учётными записями.

**Два движка.** Поле `engine` в запросе выбирает CLI: `claude` — Claude Code, `codex` — Codex от OpenAI. Без поля работает движок по умолчанию (`ENGINE`, сейчас `claude`). Ответы, задачи и события выглядят одинаково для обоих, различия — в разделе «Движки» справочника.

**Авторизация.** Каждый запрос, кроме `/health`, передаёт заголовок `Authorization: Bearer <WRAPPER_TOKEN>`. Без токена или с неверным токеном сервер отвечает `401`. Токен лежит в systemd-юните `claude-api` (или в `/etc/claude-api.env`, если вы вынесли его туда).

**Два режима работы:**

| Режим | Эндпоинты | Когда использовать |
| --- | --- | --- |
| Синхронный | `/v1/run`, `/v1/chat/completions` | Короткие вопросы до нескольких минут. Ответ приходит в том же соединении |
| Фоновые задачи | `/v1/tasks/…` | Долгая работа агента. Сервер сразу возвращает `id`, а вы возвращаетесь за ходом работы или результатом когда угодно |

В примерах ниже `$T` — ваш токен, `$U` — адрес сервера:

```bash
export T=ваш_токен
export U=https://api.config-app.com
```

Фоновые задачи работают после перезапуска сервиса на новой версии `server.py` (`sudo systemctl restart claude-api`).

## Часть 1. Руководство пользователя

Работа с сервером — это три действия: поставить задачу, посмотреть ход работы, забрать результат или дать уточнение. Все примеры ниже выполняются из любого терминала с `curl`.

### Проверить, что сервер работает

```bash
curl $U/health
# {"ok":true}
```

### Быстрый вопрос (синхронно)

Подходит, когда ответ нужен сразу и работа займёт не больше 10 минут. Если соединение оборвётся, результат потеряется.

```bash
curl -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
  -d '{"prompt":"Объясни разницу TCP и UDP в 3 предложениях","model":"sonnet"}' \
  $U/v1/run
```

В ответе приходят `result` (текст ответа), `session_id`, `cost_usd` и `usage`. Чтобы продолжить диалог, передайте этот `session_id` в следующем запросе.

### Поставить фоновую задачу

```bash
curl -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
  -d '{
    "title": "Парсер логов",
    "prompt": "Напиши на Python скрипт, который считает коды ответов в access.log, и проверь его",
    "model": "sonnet",
    "allowed_tools": "Bash,Read,Write,Edit",
    "subdir": "logparser"
  }' \
  $U/v1/tasks
```

Сервер сразу отвечает `202` и описанием задачи. Сохраните её `id`, он нужен для всех следующих действий. Если забыли его, смотрите список задач.

- `allowed_tools` решает, что агенту можно делать. Без этого поля агент только отвечает текстом. Распространённые наборы: `Read,Grep,Glob` (только читать), `Read,Write,Edit` (править файлы), `Bash,Read,Write,Edit` (ещё и запускать команды), `WebSearch,WebFetch` (интернет).
- `subdir` — рабочая папка задачи на сервере: `/home/claude/work/logparser`. Файлы агента остаются там и после завершения.
- `model` — `sonnet` (по умолчанию), `opus` для сложных задач или `haiku` для быстрых и дешёвых.

### Посмотреть список задач

```bash
curl -H "Authorization: Bearer $T" $U/v1/tasks
curl -H "Authorization: Bearer $T" "$U/v1/tasks?status=running"
```

У каждой задачи в списке есть статус, начало промпта и начало результата. Новые задачи идут первыми.

### Посмотреть, что делает агент

Одноразово, всё что уже сделано:

```bash
curl -H "Authorization: Bearer $T" $U/v1/tasks/ID/events
```

Вживую, пока задача не закончится (`-N` отключает буферизацию). Сначала придут уже накопленные события, потом новые. Ctrl+C закрывает только просмотр, агент продолжает работу.

```bash
curl -N -H "Authorization: Bearer $T" $U/v1/tasks/ID/stream
```

События выглядят так:

```json
{"seq": 1, "kind": "tool_use", "tool": "Bash", "input": "{\"command\": \"python3 parse.py access.log\"}"}
{"seq": 5, "kind": "tool_result", "is_error": false, "output": "200: 1532\n404: 17"}
{"seq": 6, "kind": "text", "text": "Скрипт работает, вот результаты…"}
```

### Забрать результат

```bash
curl -H "Authorization: Bearer $T" $U/v1/tasks/ID
```

Когда `status` станет `done`, ответ агента будет в `result`. При `failed` причина будет в `error`. Ответ для чтения удобно вытащить через `jq`:

```bash
curl -s -H "Authorization: Bearer $T" $U/v1/tasks/ID | jq -r .result
```

Файлы, созданные агентом, лежат на VPS в `/home/claude/work/<subdir>`. Скачайте их через `scp` или попросите агента вывести их содержимое в ответе.

### Дать уточнение по готовой задаче

```bash
curl -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
  -d '{"prompt":"Добавь фильтр по дате и тесты"}' \
  $U/v1/tasks/ID/continue
```

Создастся новая задача со своим `id`. Агент помнит всю предыдущую работу и работает в той же папке. Модель и инструменты наследуются, но их можно заменить полями `model` и `allowed_tools`. Так же продолжается задача, которая упала или была прервана перезапуском сервера.

### Остановить или удалить задачу

```bash
curl -X POST -H "Authorization: Bearer $T" $U/v1/tasks/ID/cancel   # остановить
curl -X DELETE -H "Authorization: Bearer $T" $U/v1/tasks/ID        # удалить запись и лог
```

Удалить можно только завершённую задачу. Файлы в `work/<subdir>` при этом остаются.

### Подключение из OpenAI-совместимых программ

Программа с поддержкой «своего OpenAI-сервера» подключается с такими настройками: Base URL `https://api.config-app.com/v1`, API key — ваш токен, модель из `GET /v1/models`: `sonnet`, `opus`, `haiku` для Claude или модель Codex, например `gpt-6-luna`. Движок выбирается по названию модели. Стриминг ответа не поддерживается, выключите его в настройках программы.

## Справочник API

Все эндпоинты, кроме `/health`, требуют `Authorization: Bearer <токен>`. Тела запросов и ответов — JSON, времена — ISO 8601 в UTC.

### Эндпоинты

| Метод | Путь | Что делает | Код успеха |
| --- | --- | --- | --- |
| GET | `/health` | Проверка живости, без токена | 200 |
| POST | `/v1/run` | Синхронный запуск, таймаут `REQUEST_TIMEOUT` (600 с) | 200 |
| POST | `/v1/chat/completions` | OpenAI-совместимый чат без стриминга | 200 |
| GET | `/v1/models` | Список моделей всех движков для OpenAI-клиентов, у каждой поле `engine` | 200 |
| GET | `/v1/engines` | Движки: установлен ли, модели, модель по умолчанию | 200 |
| POST | `/v1/tasks` | Создать фоновую задачу | 202 |
| GET | `/v1/tasks?status=&limit=` | Список задач, новые первыми; `limit` от 1 до 500, по умолчанию 50 | 200 |
| GET | `/v1/tasks/{id}` | Полное описание задачи | 200 |
| GET | `/v1/tasks/{id}/events?after=&raw=&limit=` | События задачи с позиции `after` | 200 |
| GET | `/v1/tasks/{id}/stream?after=&raw=` | События вживую через Server-Sent Events | 200 |
| POST | `/v1/tasks/{id}/cancel` | Остановить задачу в очереди или в работе | 200 |
| POST | `/v1/tasks/{id}/continue` | Новая задача в той же сессии CLI | 202 |
| DELETE | `/v1/tasks/{id}` | Удалить завершённую задачу и её логи | 200 |

### Поля запроса

Для `POST /v1/run` и `POST /v1/tasks`:

| Поле | Тип | Обязательное | Описание |
| --- | --- | --- | --- |
| `prompt` | string | да | Текст задачи |
| `engine` | string | нет | `claude` или `codex`; по умолчанию `ENGINE` |
| `title` | string | нет | Название для списка, только `/v1/tasks` |
| `model` | string | нет | Claude: `sonnet`, `opus`, `haiku` или полный id, по умолчанию `DEFAULT_MODEL`. Codex: модель из `/v1/engines`, по умолчанию `CODEX_DEFAULT_MODEL` |
| `allowed_tools` | string | нет | Инструменты через запятую, например `Bash,Read,Write`; `""` — только текст; по умолчанию `DEFAULT_TOOLS` |
| `subdir` | string | нет | Рабочая папка внутри `WORKDIR`, символы `A-Z a-z 0-9 _ -`, до 64 |
| `session_id` | string | нет | Продолжить существующую сессию CLI |
| `system_prompt` | string | нет | Дополнительные инструкции: у Claude добавляются к системному промпту, у Codex идут как `developer_instructions` |

Для `POST /v1/tasks/{id}/continue`: обязателен `prompt`, необязательны `title`, `model`, `allowed_tools` и `system_prompt`. Не указанные поля берутся из родительской задачи, `subdir` и `engine` всегда остаются её: сессию продолжает только тот CLI, который её создал. Другой `engine` в запросе даёт `409`.

Для `POST /v1/chat/completions`: стандартные `model` и `messages`, а также нестандартные `engine`, `allowed_tools`, `subdir` и `session_id`. Без `engine` движок выбирается по `model` (в OpenAI SDK передаются через `extra_body`). Сообщения `system` идут в системный промпт. Без `session_id` вся история склеивается в один промпт.

### Объект задачи

| Поле | Описание |
| --- | --- |
| `id` | 32 hex-символа |
| `status` | `queued`, `running`, `done`, `failed` или `cancelled` |
| `title`, `engine`, `model`, `subdir` | Как в запросе; `model` — фактическая модель с учётом умолчания |
| `parent_id` | `id` задачи, от которой сделан `/continue`, иначе `null` |
| `session_id` | Сессия CLI; появляется в первые секунды работы |
| `result` | Итоговый ответ агента (когда `done`) |
| `error` | Причина (когда `failed`) |
| `cost_usd`, `num_turns`, `duration_ms`, `usage` | Стоимость в долларах по оценке CLI (у Codex всегда `null`), число ходов (у Codex — вызовы инструментов + 1), длительность, токены |
| `created_at`, `started_at`, `finished_at` | Время создания, запуска и завершения |
| `prompt`, `allowed_tools` | Только в `GET /v1/tasks/{id}` |
| `prompt_preview`, `result_preview` | Только в списке: первые 200 символов |

### События

`GET /events` возвращает `{"status": "...", "events": [...], "next": N}`. У каждого события есть `seq` — номер строки в логе. Одна строка может дать несколько событий с одним `seq`, а служебные строки не дают ни одного, поэтому `seq` идут с пропусками.

| `kind` | Поля | Что означает |
| --- | --- | --- |
| `start` | `model`, `session_id` | CLI запустился |
| `text` | `text` | Агент написал текст |
| `thinking` | `text` | Рассуждение модели, обрезано до 1000 символов |
| `tool_use` | `tool`, `input` | Вызов инструмента; `input` — JSON-строка до 500 символов |
| `tool_result` | `is_error`, `output` | Результат инструмента, до 1000 символов |
| `result` | `is_error`, `result`, `cost_usd`, `num_turns`, `duration_ms` | Работа CLI закончена |

С `raw=1` приходят исходные события CLI без обрезки, с добавленным `seq`: у Claude — `stream-json`, у Codex — события `codex exec --json` (`thread.started`, `item.started`, `item.completed`, `turn.completed`, `turn.failed`).

У Codex событие `result` приходит без текста (`result: null`) и с полем `usage`: итоговый ответ — это последнее событие `text`, а также поле `result` задачи. Команды Codex показываются как `tool_use` с `tool: "Bash"`, правки файлов — `Edit`, поиск — `WebSearch`.

`/stream` отдаёт те же события в формате SSE: `id: <seq>` и `data: <json>`. В конце приходит `event: end` с кратким описанием задачи, после чего сервер закрывает поток. Пока новых событий нет, каждые 15 секунд приходит комментарий `: keepalive`.

### Движки

| | Claude (`claude`) | Codex (`codex`) |
| --- | --- | --- |
| Модели | `sonnet`, `opus`, `haiku` | список аккаунта из `GET /v1/engines` |
| Ограничение агента | точный список инструментов | песочница ОС, см. ниже |
| `cost_usd` | оценка CLI | `null`, только токены в `usage` |
| Продолжение сессии | да | да |

У Codex нет списка инструментов, поэтому `allowed_tools` в именах Claude переводится в режим песочницы:

| `allowed_tools` | Режим Codex | Что может агент |
| --- | --- | --- |
| `""` | `read-only`, shell выключен | Только отвечать текстом |
| `Read,Grep,Glob` | `read-only` | Читать файлы и запускать команды без записи |
| есть `Write`, `Edit` или `Bash(<правило>)` | `workspace-write` | Писать в рабочую папку и `/tmp`, без сети |
| есть `Bash` без правила | `danger-full-access` | Любые команды без песочницы, как `Bash` у Claude |
| есть `WebSearch` или `WebFetch` | + встроенный веб-поиск | Искать в интернете |

Режим Codex можно указать и напрямую: `"allowed_tools": "workspace-write"` или `"workspace-write,web"`. Коннекторы ChatGPT, плагины, браузер и управление компьютером в Codex выключены всегда, личные настройки `~/.codex/config.toml` не применяются.

### Ошибки

Тело ошибки всегда `{"detail": "..."}`.

| Код | Когда |
| --- | --- |
| 400 | Неизвестный или не установленный `engine`; неверный `model`, `allowed_tools`, `subdir` или `session_id`; значения, начинающиеся с `-`, отклоняются; `stream: true` в chat completions |
| 401 | Нет токена или он неверный |
| 404 | Задача не найдена |
| 409 | `cancel` уже завершённой задачи; `continue` или `DELETE` ещё активной; `continue` задачи без сессии или с другим `engine` |
| 422 | Неверная структура JSON, например нет `prompt` |
| 502 | CLI завершился с ошибкой (синхронные эндпоинты) |
| 504 | Превышен `REQUEST_TIMEOUT` (синхронные эндпоинты) |

У фоновой задачи ошибки CLI и таймаут не приходят HTTP-кодом: задача переходит в `failed`, а причина пишется в `error`.

## Часть 2. Руководство для разработчика клиента

Клиент делает четыре вещи: создаёт задачу, сохраняет её `id`, следит за событиями и читает итог. Всё состояние хранится на сервере, поэтому клиент можно закрывать и открывать снова в любой момент.

### Жизненный цикл задачи

Задача начинается в `queued` и заканчивается в одном из трёх конечных статусов: `done`, `failed` или `cancelled`. Из конечного статуса задача уже не меняется. `/continue` создаёт новую задачу с `parent_id`, а старая остаётся как есть.

```text
          /continue из любого конечного статуса создаёт новую задачу
     ┌──────────────────────────────────────────────────────────────┐
     ▼                                                              │
┌──────────┐ слот освободился ┌──────────┐      ┌───────────────────┴──┐
│  queued  │ ───────────────▶ │ running  │ ──┬─▶│ done                 │
└────┬─────┘                  └──────────┘   │  │ ответ в result       │
     │                                       │  └──────────────────────┘
     │                                       │  ┌──────────────────────┐
     │                                       ├─▶│ failed               │
     │                                       │  │ сбой/таймаут/рестарт │
     │                                       │  └──────────────────────┘
     │                                       │  ┌──────────────────────┐
     │                                       └─▶│ cancelled            │
     │      /cancel, пока задача в очереди      │ вызван /cancel       │
     └─────────────────────────────────────────▶└──────────────────────┘
```

При перезапуске сервера задачи в `running` становятся `failed`, а задачи в `queued` остаются в очереди и запускаются сами.

### Опрос или поток

| Способ | Как | Подходит для |
| --- | --- | --- |
| Опрос статуса | `GET /v1/tasks/{id}` раз в 5–10 с, пока статус не станет конечным | Скриптов, ботов и cron, которым нужен только итог |
| Опрос событий | `GET /events?after=<next>`, каждый раз с новым `next` из прошлого ответа | Простого UI без постоянного соединения |
| Поток SSE | `GET /stream?after=<N>`: сервер сам присылает события и закрывает поток событием `end` | Живого просмотра работы агента |

### Курсор событий

`after` — номер строки лога, с которой начинать. Лог только растёт, поэтому курсор не устаревает.

- В `/events` берите `next` из ответа и передавайте его как `after`. Пустой `events` с тем же `next` означает, что нового пока ничего нет.
- В `/stream` после обрыва переподключайтесь с `after = последний полученный seq + 1`. Сервер игнорирует заголовок `Last-Event-ID`, передавайте курсор параметром `after`.
- Одна строка лога может дать несколько событий с одним `seq`, например `text` и сразу `tool_use`. Если нельзя потерять ни одного, переподключайтесь с `after = seq` и отбрасывайте дубли.

### Как клиенту себя вести

1. **Сохраняйте `id` сразу** после `POST /v1/tasks`: в файл, базу или `localStorage`. По нему клиент находит свою задачу после перезапуска.
2. **Не повторяйте `POST /v1/tasks` вслепую.** Если сеть оборвалась до ответа, задача могла уже создаться. Сначала проверьте `GET /v1/tasks?limit=5` по `title` или `prompt_preview`, иначе агент выполнит работу дважды.
3. **Переподключайтесь только при сетевых ошибках и ответах 5xx.** Пока сервис перезапускается, Caddy отвечает `502`. Ответы 4xx повторять бессмысленно.
4. **Обрабатывайте `failed` после перезапуска.** При `error` вида `interrupted by server restart…` и непустом `session_id` предложите кнопку «Продолжить», которая вызывает `/continue` с промптом вроде «Продолжи с того места, где остановился».
5. **Показывайте `queued`.** Одновременно идут не больше `MAX_CONCURRENCY` задач (сейчас 3), остальные ждут. Ожидание в очереди — не зависание.
6. **Разным проектам — разные `subdir`.** Две задачи в одной папке могут править одни и те же файлы.
7. **Не держите токен в браузере.** Токен даёт полный доступ к агенту на VPS. Веб-клиенту нужен свой бэкенд, который хранит токен и проксирует запросы. К тому же браузерный `EventSource` не умеет отправлять `Authorization`, поэтому поток читают через `fetch`, как в примере ниже.
8. **Чистите старое** через `DELETE /v1/tasks/{id}`: логи задач накапливаются на диске VPS.

### Пример клиента на Python

Нужна библиотека `requests` (`pip install requests`).

```python
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
```

### Пример клиента на JavaScript

Код работает в Node.js 18+ (встроенный `fetch`) и на вашем бэкенде. В браузер его с токеном не кладите.

```javascript
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
```

## Администрирование и ограничения

Запрос проходит цепочку: клиент → Caddy (HTTPS, порт 443) → `uvicorn server:app` на `127.0.0.1:8787` → `claude -p` или `codex exec` от пользователя `claude`. Снаружи доступны только порты 80 и 443.

### Файлы на VPS

| Путь | Что там |
| --- | --- |
| `/home/claude/self-api-claude/` | Клон репозитория; сервис запускается отсюда, обновление — `git pull` и перезапуск |
| `/home/claude/venv/` | Python-окружение с FastAPI и uvicorn |
| `/home/claude/work/<subdir>/` | Рабочие папки задач, файлы агента |
| `/home/claude/tasks/tasks.db` | База задач (SQLite) |
| `/home/claude/tasks/<id>.jsonl`, `<id>.stderr` | Лог событий и stderr каждой задачи |
| `/home/claude/.claude/.credentials.json` | Авторизация Claude Code по подписке |
| `/home/claude/.codex/auth.json` | Авторизация Codex (ChatGPT); там же сессии Codex в `sessions/` |
| `/etc/systemd/system/claude-api.service` | Сервис и его переменные окружения |
| `/etc/caddy/Caddyfile` | HTTPS-прокси для `api.config-app.com` |

### Переменные окружения

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `WRAPPER_TOKEN` | — (обязательна) | Bearer-токен клиентов |
| `CLAUDE_BIN` | `claude` | Путь к Claude Code; для systemd нужен `/home/claude/.local/bin/claude` |
| `CODEX_BIN` | `codex` | Путь к Codex; для systemd нужен `/home/claude/.local/bin/codex` |
| `ENGINE` | `claude` | Движок, если клиент не указал |
| `MAX_CONCURRENCY` | 3 | Сколько CLI-процессов работает одновременно (общий лимит синхронных запросов и задач) |
| `WORKDIR` | `./work` | Корень рабочих папок |
| `TASKS_DIR` | `./tasks` | База и логи задач; должна быть вне `WORKDIR`; в юните задано `/home/claude/tasks` |
| `DEFAULT_MODEL` | `sonnet` | Модель Claude, если клиент не указал |
| `CODEX_DEFAULT_MODEL` | первая модель аккаунта | Модель Codex, если клиент не указал |
| `DEFAULT_TOOLS` | пусто | Инструменты, если клиент не указал; пусто = только текст |
| `REQUEST_TIMEOUT` | 600 | Секунд на синхронный запрос |
| `TASK_TIMEOUT` | 3600 | Секунд на фоновую задачу |

После любой правки юнита выполните `sudo systemctl daemon-reload && sudo systemctl restart claude-api`.

В `ExecStart` нужен флаг `--timeout-graceful-shutdown 5`. Без него uvicorn при остановке ждёт, пока клиенты сами закроют `/stream`, и `systemctl restart` висит до 90 секунд:

```ini
ExecStart=/home/claude/venv/bin/uvicorn server:app --host 127.0.0.1 --port 8787 --timeout-graceful-shutdown 5
```

### Повседневные команды

```bash
sudo systemctl status claude-api            # работает ли обёртка
sudo journalctl -u claude-api -f            # лог обёртки вживую
sudo journalctl -u caddy -n 50              # лог Caddy (сертификаты, ошибки прокси)
sudo systemctl restart claude-api           # перезапуск после git pull
sudo -u claude claude -p "ping" --model haiku   # жива ли авторизация Claude Code
sudo -u claude -i codex login status           # жива ли авторизация Codex
du -sh /home/claude/tasks /home/claude/work # сколько занимают логи и файлы
```

Если задачи падают с ошибкой авторизации, зайдите под пользователем `claude` и залогиньтесь заново: `claude` для Claude Code, `codex login` для Codex. Перезапускать обёртку после этого не нужно.

### Ограничения

- **Перезапуск обрывает работающие задачи.** Они получают `failed` и продолжаются через `/continue`. Задачи из очереди запускаются заново сами. Перед перезапуском проверьте `GET /v1/tasks?status=running`.
- **Один экземпляр.** Второй процесс с тем же `TASKS_DIR` не запустится. Запускайте uvicorn без `--workers`.
- **Лимиты подписки.** Задачи каждого движка тратят свой аккаунт (Claude или ChatGPT) с его окнами лимитов. Когда лимит исчерпан, задачи завершаются с `failed`. `cost_usd` — оценка CLI, а не сумма к оплате.
- **Размеры.** Тело запроса не больше 10 МБ (лимит в Caddyfile). Одно событие CLI не больше 64 МБ.
- **Нет стриминга в `/v1/chat/completions`.** Для живого вывода используйте задачи и `/stream`.

### Безопасность

- Токен равносилен доступу к агенту на сервере. С `allowed_tools: "Bash"` агент выполняет любые команды от пользователя `claude`, в том числе за пределами `subdir`; у Codex это режим `danger-full-access`.
- Песочница Codex `read-only` и `workspace-write` ограничивает запись, но не чтение: агент может прочитать любой файл, доступный пользователю `claude`, включая файлы авторизации CLI.
- Храните токен в `/etc/claude-api.env` с правами `600` (строка `EnvironmentFile=` в юните), а не в самом юните: его читают все пользователи.
- Чтобы сменить токен, сгенерируйте новый (`openssl rand -hex 32`), запишите его и перезапустите сервис. Старый токен перестанет работать сразу.
- Значения `model`, `allowed_tools`, `subdir` и `session_id` проверяются, так что клиент не может подсунуть CLI лишние флаги. Промпт передаётся через stdin и не виден в `ps`.

---

Онлайн-версия: https://claude.ai/code/artifact/c67ac9b3-6045-415f-bda5-c8b1e24ab999
