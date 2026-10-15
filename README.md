# self-api-claude

HTTP API поверх агентных CLI на своём VPS: Claude Code (`claude -p`) и Codex (`codex exec`). Вы отправляете запрос на сервер, сервер вызывает уже залогиненный CLI и возвращает ответ. API-ключи не нужны: работают подписки, под которыми залогинены CLI.

## Возможности

- **Два движка:** поле `engine` (`claude` или `codex`) в любом запросе; по умолчанию `ENGINE`. Продолжение задачи всегда идёт на движке родителя.
- **Синхронные запросы:** `POST /v1/run` и OpenAI-совместимый `POST /v1/chat/completions`.
- **Фоновые задачи:** поставили задачу, отключились и вернулись позже за ходом работы или результатом (`/v1/tasks`).
- **Живой прогресс:** вызовы инструментов, вывод, текст агента — через `/events` (опрос) или `/stream` (SSE).
- **Продолжение:** `/continue` даёт уточнение в той же сессии CLI, в том числе после сбоя или перезапуска сервера.
- **Состояние на диске:** задачи хранятся в SQLite и переживают перезапуск сервиса.
- **Архив логов:** с `archive_dir` по завершении задачи её события, stderr и полный транскрипт сессии CLI копируются в указанную папку внутри `WORKDIR`.
- **Защита:** Bearer-токен, лимит одновременных процессов, проверка параметров против подстановки флагов CLI.

## Состав

| Путь | Что это |
| --- | --- |
| `server.py` | Сервер на FastAPI |
| `engines.py` | Запуск Claude Code и Codex, разбор их событий |
| `pyproject.toml`, `uv.lock` | Зависимости Python для [uv](https://docs.astral.sh/uv/) |
| `deploy/claude-api.service` | systemd-юнит |
| `deploy/claude-api.env.example` | Шаблон файла с токеном |
| `deploy/Caddyfile` | HTTPS-прокси через Caddy |
| `clients/python/claude_tasks.py` | Клиент на Python (`requests`) |
| `clients/js/claude-tasks.mjs` | Клиент на JavaScript (Node.js 18+) |
| `docs/MANUAL.md` | Полное руководство: пользователь, API, разработка клиента, администрирование |

## Установка (Ubuntu/Debian)

```bash
# под пользователем claude: CLI и вход в аккаунт
curl -fsSL https://claude.ai/install.sh | bash
claude            # залогиниться один раз
# Codex (необязательно): установить в ~/.local/bin и войти
codex login

# окружение и код (сервис запускается прямо из клона)
git clone https://github.com/Catzilllla/self-api-claude.git ~/self-api-claude
curl -LsSf https://astral.sh/uv/install.sh | sh      # uv в ~/.local/bin
cd ~/self-api-claude && uv sync --frozen              # создаёт .venv по uv.lock

# токен и сервис (root)
sudo sh -c 'echo "WRAPPER_TOKEN=$(openssl rand -hex 32)" > /etc/claude-api.env && chmod 600 /etc/claude-api.env'
sudo cp deploy/claude-api.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now claude-api

# HTTPS: установить Caddy, поправить домен в deploy/Caddyfile
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile && sudo systemctl reload caddy
```

Сервис слушает только `127.0.0.1:8787`, наружу его отдаёт Caddy на 443. Для домена нужна A/AAAA-запись на VPS и открытые порты 80 и 443.

Обновление: `git pull && uv sync --frozen` в `~/self-api-claude`, затем `sudo systemctl restart claude-api`.

## Быстрый пример

```bash
export T=ваш_токен U=https://api.example.com

# поставить задачу
curl -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
  -d '{"title":"demo","prompt":"Создай hello.py и запусти","allowed_tools":"Bash,Write","subdir":"demo"}' \
  $U/v1/tasks

curl -N -H "Authorization: Bearer $T" $U/v1/tasks/ID/stream   # смотреть вживую
curl    -H "Authorization: Bearer $T" $U/v1/tasks/ID          # результат
```

Та же задача на Codex: добавьте `"engine":"codex"`. Список движков и моделей: `GET /v1/engines`.

Подробности — в [docs/MANUAL.md](docs/MANUAL.md).

## Безопасность

Токен даёт доступ к агенту на сервере. С `allowed_tools: "Bash"` агент выполняет любые команды от пользователя `claude`, у Codex это режим без песочницы. Не публикуйте токен и не встраивайте его во фронтенд.
