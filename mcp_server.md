# MCP Server для vsnifer — план реализации

---

## Правила разработки MCP-инструментов (tools)

### 1. Строгая типизация в сигнатуре

Каждый tool обязан объявлять точные типы параметров и возвращаемого значения:

```python
@mcp.tool()
def get_posts(channel: Optional[str] = None, ids: Optional[list[str]] = None) -> list[PostFull]:
    ...
```

Это необходимо, чтобы FastMCP генерировал корректную JSON-schema, которую видит агент.

---

### 2. Агент всё равно пришлёт что-то не то

Агент (LLM) **не гарантирует** правильный формат аргументов, даже если schema описана точно.
Наблюдаемые нарушения:

| Ожидается | Агент прислал | Решение |
|---|---|---|
| `list[str]` | JSON-строка `'["a","b"]'` | `BeforeValidator(_coerce_str_list)` |
| `list[str]` | `list[dict]` вида `[{"id":"..."}]` | вытаскиваем значение из dict |
| `list[Model]` | JSON-строка всего списка | `BeforeValidator(_coerce_obj_list)` |
| `list[Model]` | `list[str]` (каждый элемент — JSON-объект) | `_coerce_obj_list` парсит каждый элемент |
| `{channel, post_id, ...}` | `{id: "channel/post_id", ...}` | `model_validator(mode="before")` в модели |
| `str` (proof_of_ad) | `False`, `None`, `42` | приводим через `str(v)` |
| разделитель `/` | `:` в составном id | `replace(":", "/", 1)` при парсинге |
| обязательный `ids` | только `channel` (без `ids`) | делаем параметр опциональным |

---

### 3. Паттерн защитного парсинга

**Для `list[str]` параметров** (ids, списки имён):

```python
def _coerce_str_list(v: object) -> object:
    # 1. Outer JSON string → list
    v = _coerce_json_list(v, parse_items=False)
    # 2. Each dict item → extract string value by key priority
    if isinstance(v, list):
        result = []
        for item in v:
            if isinstance(item, dict):
                for key in ("id", "post_id", "name", "value"):
                    if key in item:
                        result.append(str(item[key])); break
                else:
                    result.append(str(next(iter(item.values()))))
            else:
                result.append(item)
        return result
    return v

JsonStrList = Annotated[list[str], BeforeValidator(_coerce_str_list)]
```

**Для `list[Model]` параметров** (results, batch):

```python
def _coerce_obj_list(v: object) -> object:
    # Outer JSON string → list, inner JSON strings → dict
    return _coerce_json_list(v, parse_items=True)

JsonAdCheckInputList = Annotated[list[AdCheckInput], BeforeValidator(_coerce_obj_list)]
```

**Для моделей с составным id** (`"channel/post_id"`):

```python
class AdCheckInput(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data):
        d = dict(data)
        if "id" in d and "channel" not in d:
            ch, pid = d.pop("id").split("/", 1)
            d.setdefault("channel", ch)
            d.setdefault("post_id", int(pid))
        if "proof_of_ad" in d and not isinstance(d["proof_of_ad"], str):
            v = d["proof_of_ad"]
            d["proof_of_ad"] = "" if v in (None, False) else str(v)
        return d
```

---

### 4. Трейсинг входящих параметров

В начале каждого tool добавлять `logger.debug` с сырыми аргументами:

```python
@mcp.tool()
def get_posts(channel, ids):
    logger.debug("get_posts called: channel=%r, ids=%r", channel, ids)
    ...
```

Включить в `.env`:
```dotenv
LOG_LEVEL=DEBUG
```

---

### 5. Нормализация составных id

Составной id поста — `"channel/post_id"`. Агент может использовать другие разделители:

```python
sid = str(raw_id).replace(":", "/", 1)   # нормализация разделителя
if "/" not in sid and channel:
    sid = f"{channel}/{sid}"              # bare numeric id + known channel
```

---

## Назначение

MCP-сервер предоставляет внешнему AI-агенту (Claude Desktop, Claude Code, любой MCP-клиент)
набор **tools** для работы с загруженными постами из Telegram:

- читать списки постов и их содержимое (meta.json + text.txt)
- получать батчи непроверенных постов для анализа
- записывать результаты классификации (`ad_rate`, `proof_of_ad`) обратно в `meta.json`

Таким образом агент сам выступает LLM-судьёй и заменяет (или дополняет) локальный `check_ad.py update`.

---

## Архитектура

```
Claude Desktop / Claude Code / любой MCP-клиент
        │  HTTP  POST /mcp  (MCP Streamable HTTP)
        ▼
  mcp_server.py  :3100  ──► WORK_DIR (H:\TEMP\vk_vsf)
        │                       channel_a/post_id/meta.json
        │                       channel_a/post_id/text.txt
        └──► config.py (.env, .env.json)
```

**Транспорт:** `streamable-http` (по умолчанию) — сервер слушает на порту `3100`,
клиент подключается по URL `http://localhost:3100/mcp`.
Это позволяет запускать сервер один раз (в том числе в Docker) и подключаться к нему из любого клиента.

**Язык:** Python 3.10+, библиотека `fastmcp>=2.0`.

---

## Файлы

| Файл | Назначение |
|---|---|
| `mcp_server.py` | Точка входа, регистрация tools |
| `mcp_tools/models.py` | Pydantic-модели ответов MCP |
| `mcp_tools/post_store.py` | Чтение постов из WORK_DIR |
| `mcp_tools/ad_writer.py` | Запись `ad_check` в meta.json |

Маленький проект — допустимо уложить всё в один `mcp_server.py` (~300 строк).

---

## Зависимости

```
fastmcp>=2.0
```

Добавить в `requirements.txt`. Установка:

```powershell
pip install fastmcp
```

`fastmcp` тянет `mcp` (официальный Anthropic SDK) как зависимость автоматически.

---

## Tools (инструменты агента)

### 1. `list_channels`

Возвращает список каналов (имена папок в WORK_DIR).

```python
@mcp.tool()
def list_channels() -> list[str]:
    """List all downloaded Telegram channels available in WORK_DIR."""
```

**Ответ:**
```json
["babazoyka", "LIKEHUMOR", "mens_mem"]
```

---

### 2. `get_stats`

Статистика по каждому каналу: сколько постов всего, сколько проверено, сколько ждут проверки.

```python
@mcp.tool()
def get_stats(channel: str | None = None) -> list[ChannelStats]:
    """
    Return per-channel statistics.
    channel: if given, return stats only for this channel.
    """
```

**Ответ (модель `ChannelStats`):**
```json
[
  {
    "channel": "babazoyka",
    "total_posts": 312,
    "checked": 280,
    "unchecked": 32,
    "ad_count": 45,
    "clean_count": 235
  }
]
```

---

### 3. `list_posts`

Возвращает список постов с фильтрацией. Не включает содержимое text.txt (только мета).

```python
@mcp.tool()
def list_posts(
    channel: str | None = None,
    has_ad_check: bool | None = None,   # None=all, True=checked, False=unchecked
    min_ad_rate: float | None = None,
    max_ad_rate: float | None = None,
    date_from: str | None = None,       # YYYY-MM-DD
    date_to: str | None = None,         # YYYY-MM-DD
    sort: str = "date_desc",            # date_asc | date_desc
    limit: int = 50,
    offset: int = 0,
) -> list[PostInfo]:
    """List posts with optional filters. Does not include post text."""
```

**Ответ (модель `PostInfo`):**
```json
[
  {
    "id": "babazoyka/23609",
    "channel": "babazoyka",
    "post_id": 23609,
    "date": "2026-05-04T13:05:05+00:00",
    "has_media": true,
    "views": 2270,
    "reactions_total": 20,
    "ad_check": {
      "ad_rate": 0.05,
      "proof_of_ad": "Мем без ссылок."
    }
  }
]
```

---

### 4. `get_post`

Возвращает полные данные одного поста: meta.json + содержимое text.txt.
Принимает составной `id` в формате `"channel/post_id"` — именно так он приходит в ответах `list_posts` и `get_unchecked_batch`.

```python
@mcp.tool()
def get_post(id: str) -> PostFull:
    """
    Get full post data including text content.
    id: composite "channel/post_id" as returned by list_posts / get_unchecked_batch.
    """
```

---

### 4a. `get_posts`

Батчевая версия `get_post` — принимает список `id` и возвращает список `PostFull`.
Ненайденные посты пропускаются без ошибки.

```python
@mcp.tool()
def get_posts(ids: list[str]) -> list[PostFull]:
    """
    Get full data for multiple posts at once, including text content.
    ids: list of "channel/post_id" identifiers.
    """
```

**Пример:**
```json
["babazoyka/23609", "babazoyka/23610", "LIKEHUMOR/4501"]
```

**Ответ (модель `PostFull`):**
```json
{
  "id": "babazoyka/23609",
  "channel": "babazoyka",
  "post_id": 23609,
  "date": "2026-05-04T13:05:05+00:00",
  "views": 2270,
  "reactions_total": 20,
  "reactions": { "🌚": 11, "😁": 9 },
  "ad_check": null,
  "text": "Когда программист идёт в отпуск 😂\n#юмор"
}
```

---

### 5. `get_unchecked_batch`

Возвращает следующие N непроверенных постов (с текстом), отсортированных по дате убывания (новые первые — как в `check_ad.py`). Основной инструмент для агента-классификатора.

```python
@mcp.tool()
def get_unchecked_batch(
    channel: str | None = None,
    batch_size: int = 5,
) -> list[PostFull]:
    """
    Return the next batch of posts without ad_check, newest first.
    Use set_ad_check_batch to write results back.
    """
```

---

### 6. `set_ad_check`

Записывает результат классификации для одного поста в `meta.json`.

```python
@mcp.tool()
def set_ad_check(
    channel: str,
    post_id: int,
    ad_rate: float,        # 0.0 = точно не реклама, 1.0 = точно реклама
    proof_of_ad: str,      # одно предложение объяснения
) -> SetAdCheckResult:
    """Write ad classification result to meta.json for a single post."""
```

**Ответ:**
```json
{ "ok": true, "id": "babazoyka/23609" }
```

---

### 7. `set_ad_check_batch`

Записывает результаты классификации для нескольких постов за раз — идеально после `get_unchecked_batch`.

```python
@mcp.tool()
def set_ad_check_batch(results: list[AdCheckInput]) -> BatchResult:
    """
    Write ad classification results for multiple posts at once.
    results: list of {channel, post_id, ad_rate, proof_of_ad}
    """
```

**Ответ:**
```json
{
  "written": 5,
  "errors": [],
  "ids": ["babazoyka/23609", "babazoyka/23610", "LIKEHUMOR/4501"]
}
```

---

## Pydantic-модели

```python
class AdCheckInfo(BaseModel):
    ad_rate: float
    proof_of_ad: str

class PostInfo(BaseModel):
    id: str                        # "channel/post_id"
    channel: str
    post_id: int
    date: str | None
    has_media: bool
    views: int
    reactions_total: int
    ad_check: AdCheckInfo | None

class PostFull(PostInfo):
    reactions: dict[str, int]
    text: str                      # "" если text.txt отсутствует

class ChannelStats(BaseModel):
    channel: str
    total_posts: int
    checked: int
    unchecked: int
    ad_count: int                  # checked с ad_rate >= 0.85
    clean_count: int               # checked с ad_rate < 0.85

class AdCheckInput(BaseModel):
    channel: str
    post_id: int
    ad_rate: float
    proof_of_ad: str

class SetAdCheckResult(BaseModel):
    ok: bool
    id: str
    error: str | None = None

class BatchResult(BaseModel):
    written: int
    errors: list[str]
    ids: list[str]
```

---

## Скелет `mcp_server.py`

```python
# mcp_server.py
import json
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastmcp import FastMCP
from pydantic import BaseModel

load_dotenv()

WORK_DIR = Path(os.getenv("WORK_DIR", "work"))
MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("MCP_PORT", "3100"))
AD_THRESHOLD = 0.85

mcp = FastMCP("vsnifer", instructions="""
Ты анализируешь посты из Telegram-каналов на наличие рекламы.
Используй get_unchecked_batch чтобы получить посты, проанализируй каждый и вызови set_ad_check_batch.
ad_rate: 0.0 = точно не реклама, 1.0 = точно реклама.
""")


@mcp.tool()
def list_channels() -> list[str]: ...

@mcp.tool()
def get_stats(channel: Optional[str] = None) -> list[ChannelStats]: ...

@mcp.tool()
def list_posts(...) -> list[PostInfo]: ...

@mcp.tool()
def get_post(channel: str, post_id: int) -> PostFull: ...

@mcp.tool()
def get_unchecked_batch(channel: Optional[str] = None, batch_size: int = 5) -> list[PostFull]: ...

@mcp.tool()
def set_ad_check(channel: str, post_id: int, ad_rate: float, proof_of_ad: str) -> SetAdCheckResult: ...

@mcp.tool()
def set_ad_check_batch(results: list[AdCheckInput]) -> BatchResult: ...


if __name__ == "__main__":
    _parser = _build_parser()
    _args = _parser.parse_args()

    if not _args.command:
        _parser.print_help()
        raise SystemExit(0)

    if _args.command == "run":
        _host = _args.host or MCP_HOST
        _port = _args.port or MCP_PORT
        mcp.run(transport="streamable-http", host=_host, port=_port)
```

---

## Команды запуска

```powershell
# Показать справку (без аргументов или с --help)
python mcp_server.py
python mcp_server.py --help

# Показать все зарегистрированные tools
python mcp_server.py list-tools

# Запустить сервер локально (HTTP, порт 3100)
python mcp_server.py run

# Другой порт (через аргумент)
python mcp_server.py run --port 8080

# Другой хост и порт
python mcp_server.py run --host 127.0.0.1 --port 8080

# Через переменные окружения (аргументы имеют приоритет)
$env:MCP_PORT = "8080"; python mcp_server.py run

# Проверить список tools (fastmcp inspector)
fastmcp dev mcp_server.py
```

Эндпоинт: `http://localhost:3100/mcp`

---

## Конфигурация Claude Desktop

Файл: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "vsnifer": {
      "type": "streamable-http",
      "url": "http://localhost:3100/mcp"
    }
  }
}
```

После изменения — перезапустить Claude Desktop.  
Сервер должен быть запущен заранее (`python mcp_server.py`).

---

## Конфигурация Claude Code (VS Code / JetBrains)

Файл `.mcp.json` в корне проекта:

```json
{
  "mcpServers": {
    "vsnifer": {
      "type": "streamable-http",
      "url": "http://localhost:3100/mcp"
    }
  }
}
```

---

## Docker

### `Dockerfile`

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=3100

EXPOSE 3100

CMD ["python", "mcp_server.py"]
```

### `docker-compose.yml`

```yaml
services:
  vsnifer-mcp:
    build: .
    container_name: vsnifer-mcp
    restart: unless-stopped
    ports:
      - "3100:3100"
    volumes:
      # WORK_DIR с постами — монтируем с хоста (read-write, сервер пишет ad_check)
      - H:/TEMP/vk_vsf:/data/vk_vsf
      # .env с конфигом (read-only)
      - ./\.env:/app/.env:ro
      - ./\.env.json:/app/.env.json:ro
    environment:
      WORK_DIR: /data/vk_vsf
      MCP_HOST: 0.0.0.0
      MCP_PORT: 3100
```

```powershell
# Сборка и запуск
docker compose up -d --build

# Логи
docker compose logs -f vsnifer-mcp
```

### Конфигурация клиента при Docker

Если MCP-клиент (Claude Desktop, другой агент) работает **на том же хосте**, что и Docker:

```json
{
  "mcpServers": {
    "vsnifer": {
      "type": "streamable-http",
      "url": "http://localhost:3100/mcp"
    }
  }
}
```

Если MCP-клиент работает **внутри другого Docker-контейнера** на том же хосте:

```json
{
  "mcpServers": {
    "vsnifer": {
      "type": "streamable-http",
      "url": "http://host.docker.internal:3100/mcp"
    }
  }
}
```

Конфигурация MCP-tools для Flowise:

```json
{
      "type": "streamable-http",
      "url": "http://host.docker.internal:3100/mcp"
}
```

`host.docker.internal` — магическое DNS-имя, которое Docker (Desktop на Windows/Mac,
Engine с `--add-host` на Linux) разрешает в IP хост-машины.
Контейнер-клиент обращается по нему к сервису, запущенному на хосте или в соседнем контейнере.

> **Linux:** `host.docker.internal` не резолвится автоматически.
> Добавить в `docker-compose.yml` клиентского контейнера:
> ```yaml
> extra_hosts:
>   - "host.docker.internal:host-gateway"
> ```

---

## Сценарий работы агента

```
Агент:  list_channels()
          → ["babazoyka", "LIKEHUMOR", "mens_mem"]

Агент:  get_stats()
          → [{channel: "babazoyka", unchecked: 47}, ...]

Агент:  get_unchecked_batch(batch_size=5)
          → [PostFull(id="babazoyka/23700", text="..."), ...]

Агент:  [анализирует каждый пост самостоятельно]

Агент:  set_ad_check_batch([
          {channel:"babazoyka", post_id:23700, ad_rate:0.05, proof_of_ad:"Мем без ссылок"},
          {channel:"babazoyka", post_id:23698, ad_rate:0.95, proof_of_ad:"Invite-ссылка на чужой канал"},
          ...
        ])
          → {written: 5, errors: []}

[повторяет get_unchecked_batch → анализ → set_ad_check_batch пока unchecked > 0]
```

---

## Что НЕ входит в MCP-сервер

| Исключено | Причина |
|---|---|
| Скачивание из Telegram | Задача `vk_vsf_bot.py`, требует сессию Telethon |
| Вызов Ollama | Задача агента — он сам модель; дублирование не нужно |
| Сборка видео | Задача `join_video.py`; нет смысла в MCP-обёртке |
| Аутентификация MCP | Сервер слушает только локально или в изолированной сети Docker |

---

## Приоритет реализации

1. `mcp_server.py` — базовая структура FastMCP + HTTP транспорт + `list_channels` + `get_stats`
2. `get_unchecked_batch` + `set_ad_check_batch` — ключевой flow
3. `list_posts` + `get_post` — для исследования данных агентом
4. `set_ad_check` — одиночная запись (удобно для отладки)
5. `Dockerfile` + `docker-compose.yml` — упаковка для постоянной работы
