# MAX Ollama Bot

Бот для мессенджера [MAX](https://max.ru) с интеграцией [Ollama](https://ollama.com):
общение с локальными LLM, выбор моделей, работа с изображениями (vision-модели),
контроль доступа и статистика использования.

Работает поверх [MAX Bot API](https://dev.max.ru/docs-api/) через библиотеку
[maxapi](https://github.com/love-apples/maxapi).

## Возможности

- Чат с любой моделью Ollama, потоковый вывод ответа (сообщение дописывается по мере генерации)
- Выбор модели через inline-клавиатуру (`/models`) или командой (`/switch_model`)
- Обработка изображений vision-моделями (llava и аналогичные) с автопроверкой поддержки
- История диалога с ограничением по длине контекста
- Whitelist пользователей, режим тестирования (только админ), rate limiting
- Статистика по моделям и пользователям, рассылка
- HTTP health-check эндпоинт для мониторинга и Docker healthcheck

## Требования

- Python 3.12+
- Запущенный сервер Ollama с загруженными моделями
- Токен бота MAX (бот создаётся через MasterBot в мессенджере,
  см. [документацию MAX Bot API](https://dev.max.ru/docs-api/))

## Быстрый старт

```bash
git clone <repo> max-ollama && cd max-ollama
cp .env.example .env    # заполните MAX_BOT_TOKEN и ADMIN_ID
uv sync
uv run python -m bot.main
```

Через Docker:

```bash
cp .env.example .env
docker compose up -d
docker compose logs -f bot
```

Если Ollama работает на хосте, а бот в контейнере, укажите в `.env`:
`OLLAMA_HOST=http://host.docker.internal:11434` (Linux: адрес docker-моста, например `http://172.17.0.1:11434`).

## Конфигурация

Все параметры читаются из `.env` или переменных окружения.

| Переменная | По умолчанию | Описание |
|---|---|---|
| `MAX_BOT_TOKEN` | — | **Обязательно.** Токен бота MAX |
| `ADMIN_ID` | — | **Обязательно.** MAX user id администратора |
| `OLLAMA_HOST` | `http://localhost:11434` | Адрес Ollama API |
| `OLLAMA_TIMEOUT` | `60` | Таймаут запросов к Ollama, сек |
| `DATABASE_URL` | `sqlite:///data/bot.db` | SQLite или PostgreSQL |
| `DEFAULT_MODEL` | `llama2` | Модель для пользователей без выбора |
| `MAX_CONTEXT_LENGTH` | `4096` | Максимальная длина контекста диалога, символов |
| `TEST_MODE` | `false` | Отвечать только администратору |
| `LOG_LEVEL` | `INFO` | DEBUG / INFO / WARNING / ERROR / CRITICAL |
| `RATE_LIMIT_MESSAGES` | `10` | Сообщений на окно (админ не ограничен) |
| `RATE_LIMIT_WINDOW` | `60` | Длина окна, сек |
| `HEALTH_CHECK_ENABLED` | `true` | Поднимать HTTP health-сервер |
| `HEALTH_CHECK_PORT` | `8080` | Порт health-сервера |

Как узнать свой `ADMIN_ID`: запустите бота с любым значением, напишите ему `/start` —
идентификатор будет в тексте ответа и в логах, затем подставьте его в `.env` и перезапустите.

## Команды

**Общие**

| Команда | Описание |
|---|---|
| `/start` | Регистрация и приветствие |
| `/help` | Список доступных команд |
| `/status` | Состояние бота, Ollama и текущей модели |

**Модели**

| Команда | Описание |
|---|---|
| `/models` | Список моделей с выбором кнопками |
| `/switch_model <name>` | Переключить модель |
| `/current_model` | Показать выбранную модель |
| `/model_info [name]` | Параметры, квантизация, семейство, шаблон |

**Чат**

| Команда | Описание |
|---|---|
| любое сообщение | Отправить запрос модели |
| изображение | Отправить изображение vision-модели (подпись становится промптом) |
| `/clear` | Очистить контекст диалога |
| `/regenerate` | Перегенерировать последний ответ |
| `/history` | Последние 10 сообщений |

**Админ**

| Команда | Описание |
|---|---|
| `/add_user <id>` | Выдать доступ |
| `/remove_user <id>` | Отозвать доступ |
| `/list_users` | Список пользователей |
| `/test_mode [on\|off]` | Режим «только админ» |
| `/stats` | Статистика за 24 часа |
| `/clear_history <id\|all>` | Очистить историю |
| `/broadcast <текст>` | Рассылка активным пользователям |

## Архитектура

```
src/bot/
├── main.py            # запуск, инициализация, graceful shutdown
├── runtime.py         # общие Bot / Dispatcher / OllamaClient
├── config.py          # настройки (pydantic-settings)
├── decorators.py      # admin_only, authorized_only, rate_limited
├── database/          # модели и сессии SQLAlchemy (async)
├── handlers/          # common, admin, models, chat
└── utils/
    ├── events.py      # нормализация MessageCreated / MessageCallback
    ├── context.py     # контекст диалога
    ├── ollama.py      # HTTP-клиент Ollama (chat, stream, vision, /api/show)
    ├── health.py      # /health (и /metrics — заглушка под Prometheus)
    └── logging.py     # structlog
```

Порядок регистрации хендлеров = приоритет: MAX отдаёт событие **первому**
подходящему обработчику. Поэтому `handlers/__init__.py` импортирует модули в
порядке «команды → вложения → произвольный текст».

### Как устроена работа с MAX

- **Хендлеры** регистрируются декораторами диспетчера: `@dp.message_created(Command("stats"))`,
  `@dp.message_callback(...)`, `@dp.bot_started()`. Аргументы команды приходят
  в хендлер kwarg-ом `args` от фильтра `Command`.
- **Общие объекты** (`Bot`, `Dispatcher`, `OllamaClient`) живут в `bot/runtime.py`:
  в MAX нет объекта-контекста, который прокидывается в каждый хендлер.
- **Два типа событий** — `MessageCreated` (сообщение) и `MessageCallback` (нажатие
  кнопки) — по-разному хранят автора и цель ответа. `bot/utils/events.py` приводит
  их к общему виду, поэтому декораторы и хендлеры работают с обоими.
- **Inline-клавиатура** собирается через `InlineKeyboardBuilder` и отправляется
  как вложение сообщения. Выбор модели передаётся типизированным `CallbackPayload`
  (`select_model|<имя модели>`) вместо разбора строки вручную.
- **Стриминг ответа**: первое сообщение отправляется, когда накопилось ~50 символов,
  дальше дописывается через `bot.edit_message` — не чаще раза в секунду и не чаще
  чем раз в 100 символов, чтобы не упереться в лимиты MAX на редактирование.
- **Изображения** приходят вложением с прямым URL; бот качает их через
  `bot.download_bytes(url)` и отдаёт модели в base64.
- **HTML-разметка** (`format=ParseMode.HTML`): по хелперам форматирования maxapi это
  `<b>`, `<i>`, `<ins>`, `<s>`, `<code>`, `<h1>`, `<mark>`, `<blockquote>`, `<a>` —
  блочного `<pre>` нет, а подчёркивание это `<ins>`, а не `<u>`. Бот использует только
  `<b>`, `<i>` и `<code>`; пользовательский текст в `/history` и `/model_info`
  экранируется, чтобы угловые скобки в сообщении не ломали разметку.

### Эксплуатационные заметки

- Чтобы бот получал сообщения в **групповом чате, ему нужны права администратора**
  чата — иначе события до него не доходят.
- Бот работает на **long polling**. Это проще в развёртывании, но MAX ограничивает
  скорость и срок хранения событий; для нагруженного продакшена в maxapi есть
  webhook (`dp.handle_webhook`), потребуется HTTPS и сертификат доверенного CA.
- Polling не получает события, если у бота остались активные webhook-подписки.
  Снять их: `await bot.delete_webhook()`.

## Разработка

```bash
make install   # uv sync
make dev       # запуск бота
make test      # pytest с покрытием
make lint      # ruff + mypy
make format    # black + ruff --fix
```

## CI/CD

`.github/workflows/ci.yml` запускается на push и pull request в `Master`, а также на теги `v*`:

1. **test** — `uv sync --frozen`, `ruff check`, `pytest`.
2. **docker** — сборка образа и публикация в GitHub Container Registry
   (`ghcr.io/<owner>/<repo>`). На pull request образ только собирается, без публикации.

Теги образа: имя ветки, `sha-<short>`, `latest` для `Master`,
`X.Y.Z` и `X.Y` для тегов вида `v1.2.3`.

Дополнительная настройка не нужна — используется встроенный `GITHUB_TOKEN`.
Единственное, что стоит проверить в репозитории: **Settings → Actions → General →
Workflow permissions** должно разрешать запись пакетов (или права выданы на уровне job,
как в этом workflow). Свежесозданный пакет в GHCR по умолчанию приватный — при
необходимости сделайте его публичным в настройках пакета.

Запуск опубликованного образа:

```bash
docker run -d --name max-ollama-bot \
  --env-file .env \
  -v "$PWD/data:/app/data" \
  ghcr.io/<owner>/<repo>:latest
```

## Health check

```bash
curl http://localhost:8080/health
# {"status": "healthy", "bot": "online", "ollama": "online"}
```

Возвращает `503`, если Ollama недоступна.

## Лицензия

MIT
