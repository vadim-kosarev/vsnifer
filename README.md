# vsnifer — сборщик видеоконтента из Telegram-каналов

## Содержание

- [Назначение](#назначение)
- [Состав](#состав)
- [Возможности](#возможности)
  - [Загрузка постов (`vk_vsf_bot.py`)](#загрузка-постов-vk_vsf_botpy)
  - [Объединение видео (`join_video.py`)](#объединение-видео-join_videopy)
  - [LLM-классификация рекламы (`check_ad.py`)](#llm-классификация-рекламы-check_adpy)
  - [Детектор обнажённого контента (`check_ad.py update-nudes`)](#детектор-обнажённого-контента-check_adpy-update-nudes)
- [Установка](#установка)
  - [Требования](#требования)
  - [Шаги](#шаги)
- [Конфигурация](#конфигурация)
  - [`.env` — основные параметры](#env--основные-параметры)
  - [`.env.json` — расширенная конфигурация](#envjson--расширенная-конфигурация)
- [Использование](#использование)
  - [`vk_vsf_bot.py` — просмотр и загрузка](#vk_vsf_botpy--просмотр-и-загрузка)
  - [`check_ad.py` — LLM-классификация рекламы и детектор контента](#check_adpy--llm-классификация-рекламы-и-детектор-контента)
  - [`join_video.py` — объединение видео](#join_videopy--объединение-видео)
- [Структура каталогов](#структура-каталогов)
  - [Содержимое `meta.json`](#содержимое-metajson)
- [Выходной видеофайл](#выходной-видеофайл)
- [Логи](#логи)
- [MCP-сервер (`mcp_server.py`)](#mcp-сервер-mcp_serverpy)

---

## Назначение

Инструмент для автоматического сбора видео из Telegram-каналов и объединения их в один Full HD файл для просмотра или публикации на YouTube.

Раньше я смотрел подборки смешных видео на YouTube, но все каналы закрылись. Теперь контент ищу в Telegram-каналах. Хочу смотреть подборки в одном месте, не переходя между каналами.

## Состав

| Файл | Назначение |
|---|---|
| `vk_vsf_bot.py` | Подключается к Telegram-каналам, просматривает и скачивает посты с медиа |
| `join_video.py` | Объединяет скачанные видеофайлы в один Full HD файл через FFmpeg |
| `check_ad.py` | LLM-классификатор рекламы (`update`) и детектор обнажённого контента (`update-nudes`): дописывает `ad_check` и `nude_check` в `meta.json` |
| `config.py` | Конфигурация, управление прокси, ротация MTProxy, фильтр рекламы |
| `.env` | Учётные данные и параметры (не коммитится) |
| `.env.json` | Расширенная конфигурация: список прокси, правила фильтрации рекламы |

## Возможности

### Загрузка постов (`vk_vsf_bot.py`)

- Просмотр последних постов из Telegram-каналов с метриками (просмотры, реакции, форварды)
- Загрузка постов с медиафайлами (видео, фото, документы)
- Пакетная загрузка сразу со всех каналов (`--all-channels`)
- Фильтрация по дате начала (`--since`) — не скачивает посты старше указанной даты
- Идемпотентная загрузка — уже скачанные посты пропускаются автоматически (проверка по `meta.json`)
- Кэширование числового ID канала — не зависит от смены username
- Сохранение метаданных: просмотры, форварды, ответы, реакции с разбивкой по эмодзи
- Поддержка прокси: SOCKS4, SOCKS5, MTProto (в т.ч. FakeTLS с `ee`-секретом)
- Автоматическая ротация MTProxy при обрывах соединения (список прокси в `.env.json`)

### Объединение видео (`join_video.py`)

- Объединение видео из нескольких каналов в один файл Full HD горизонтальный (1920×1080) или вертикальный (1080×1920, Reels/Shorts/TikTok)
- Надёжная обработка аудио через concat filter: разные sample rate, отсутствие аудиодорожки (заполняется тишиной)
- Для вертикального и квадратного видео — размытый фон вместо чёрных полос
- Главы (chapters) в MP4 с метками по каналу/посту
- Файл тайм-кодов для YouTube-описания (`<output>.timestamps.txt`)
- Несколько режимов сортировки
- Фильтрация по диапазону дат (`--start-date`, `--end-date`) или за последние N дней (`--last-days`)
- Коррекция рассинхрона аудио/видео (`--audio-delay-ms`)
- Фильтрация рекламных роликов по правилам из `.env.json`

### LLM-классификация рекламы (`check_ad.py`)

- Обходит `WORK_DIR`, находит посты без отметки `ad_check` в `meta.json`
- Обрабатывает посты начиная с самых новых — при частичном запуске (`--limit`) сначала закрываются свежие посты
- Отправляет в локальный Ollama по 5 постов за запрос — быстро и без лишней нагрузки
- Дописывает результат прямо в `meta.json`: `ad_rate` (0.0–1.0) и `proof_of_ad` (объяснение)
- Идемпотентно — уже проверенные посты пропускаются (переопределяется `--force`)
- `--dry-run` — покажет что будет обработано без единого запроса к LLM
- Работает с любой моделью Ollama через `--model` или `OLLAMA_MODEL` в `.env`
- `--think` — включает chain-of-thought (медленнее, полезно для отладки промптов)
- Промпты настраиваемые: `--prompt-before` (system) и `--prompt-after` (user) — по умолчанию `llm/check_ad_prompt.p1.md` и `llm/check_ad_prompt.p2.md`

### Детектор обнажённого контента (`check_ad.py update-nudes`)

- Сканирует `WORK_DIR` в поиске постов с видеофайлами (`.mp4`, `.mov`, `.avi`, `.mkv`, `.webm`, `.flv`)
- Извлекает до N равномерно распределённых кадров из каждого видео (`--frames`, по умолчанию 10)
- Прогоняет кадры через `NudeDetector` — детектирует обнажённые классы (`FEMALE_BREAST_EXPOSED`, `FEMALE_GENITALIA_EXPOSED`, `MALE_GENITALIA_EXPOSED`, `ANUS_EXPOSED`, `BUTTOCKS_EXPOSED`)
- Итоговый `nude_rate` = максимальный score детекции среди всех кадров (0.0 = чисто, 1.0 = явный контент)
- Дописывает результат в `meta.json`: `nude_check` с `nude_rate`, именем файла и числом проанализированных кадров
- Идемпотентно — уже проверенные посты пропускаются (переопределяется `--force`)
- `--dry-run` — покажет что будет обработано без запуска детектора

## Установка

### Требования
- Python 3.10+
- FFmpeg 6+ (для объединения видео)
- Ollama (для `check_ad.py update`) — https://ollama.com
- `nudenet` + `opencv-python` (для `check_ad.py update-nudes`) — устанавливаются через `requirements.txt`

### Шаги

1. Клонируйте репозиторий с субмодулями:
```powershell
git clone --recurse-submodules <repo-url>
cd vsnifer
```

2. Создайте виртуальное окружение:
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

3. Установите зависимости:
```powershell
pip install -r requirements.txt
```

4. Получите Telegram API credentials на https://my.telegram.org/apps

5. Создайте `.env` на основе `.env.example` и заполните реальными значениями.

6. *(Опционально)* Создайте `.env.json` на основе `.env.json.example` для прокси и правил фильтрации.

## Конфигурация

### `.env` — основные параметры

#### Обязательные
| Переменная | Описание |
|---|---|
| `API_ID` | ID приложения из https://my.telegram.org/apps |
| `API_HASH` | Hash приложения |

#### Авторизация (одно из двух)
| Переменная | Описание |
|---|---|
| `PHONE` | Номер телефона пользователя (предпочтительно — даёт доступ к любым каналам) |
| `BOT_TOKEN` | Token бота от BotFather (ограниченный доступ) |

#### Каналы
| Переменная | Описание |
|---|---|
| `TARGET_CHANNEL` | Канал по умолчанию для `view-recent` и `download` без флагов |
| `CHANNELS` | Список каналов через запятую для `download --all-channels` и белый список для `check_ad.py` |
| `RECENT_POSTS_COUNT` | Кол-во постов по умолчанию (default: `10`) |

Форматы: `@username`, `+<invite_hash>`, `https://t.me/+xxx`, числовой ID.

```dotenv
CHANNELS=@babazoyka, https://t.me/+otRtx2aMM0ZlMTVi, +HRom-yzU75JhYzIy
```

#### Инфраструктура
| Переменная | Описание | По умолчанию |
|---|---|---|
| `WORK_DIR` | Рабочий каталог для загрузки | `work` |
| `FFMPEG_HOME` | Путь к каталогу FFmpeg | берётся из PATH |
| `LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` | `INFO` |

#### Прокси (резервный, если нет `.env.json`)
| Переменная | Описание |
|---|---|
| `PROXY_TYPE` | `socks4`, `socks5`, `mtproto` |
| `PROXY_HOST` | Хост |
| `PROXY_PORT` | Порт |
| `PROXY_SECRET` | Секрет MTProto (`ee`-префикс = FakeTLS) |

#### Параметры join_video.py
| Переменная | Описание | По умолчанию |
|---|---|---|
| `OUTPUT_DIR` | Каталог для выходного файла. Имя файла генерируется автоматически: `vk_vsf_output-YYYYMMDD.mp4` | `\\luigi\temp` |
| `AUDIO_DELAY_MS` | Коррекция рассинхрона аудио в мс (>0 — аудио запаздывает; <0 — опережает; 0 — выкл.) | `0` |
| `INTEREST_W_REACTIONS` | Вес реакций для interest-score | `10` |
| `INTEREST_W_FORWARDS` | Вес форвардов | `5` |
| `INTEREST_W_REPLIES` | Вес ответов | `2` |

#### Параметры check_ad.py (Ollama)
| Переменная | Описание | По умолчанию |
|---|---|---|
| `OLLAMA_BASE_URL` | URL сервера Ollama | `http://localhost:11434` |
| `OLLAMA_MODEL` | Модель для классификации | `qwen3.5:9b` |
| `OLLAMA_TIMEOUT_SEC` | Таймаут запроса в секундах | `300` |

---

### `.env.json` — расширенная конфигурация

Файл JSON рядом со скриптами. Не содержит учётных данных.

```json
{
  "proxy_switch_max_drops": 3,
  "proxy_switch_window_secs": 300,
  "proxies": [
    { "type": "mtproto", "host": "proxy1.example.com", "port": 2443, "secret": "abc..." },
    { "type": "mtproto", "host": "proxy2.example.com", "port": 2443, "secret": "def..." }
  ],
  "ad_filter": {
    "ban_text_contains": ["wildberries", "промокод", "скидка"],
    "ban_text_regex": ["https?://(?!t\\.me)\\S+"],
    "ban_channel_mentions": ["@some_ads_channel"],
    "ban_min_views": 0
  }
}
```

#### Ротация прокси
- `proxies` — список MTProxy/SOCKS серверов. При обрыве соединения автоматически переключается на следующий.
- `proxy_switch_max_drops` — сколько обрывов в окне до переключения (default: `3`).
- `proxy_switch_window_secs` — ширина окна в секундах (default: `300`).

#### Фильтр рекламы (`ad_filter`)
| Поле | Тип | Описание |
|---|---|---|
| `ban_text_contains` | список строк | Бан если текст поста содержит любую из строк (без учёта регистра) |
| `ban_text_regex` | список regex | Бан если текст совпадает с любым регулярным выражением |
| `ban_channel_mentions` | список `@channel` | Бан если текст упоминает любой из каналов через `@` или `t.me/` |
| `ban_min_views` | число | Бан если просмотров меньше указанного (`0` = выкл.) |
| `ban_min_duration_sec` | число | Бан если ролик короче N секунд (`0` = выкл.) |
| `ban_max_duration_sec` | число | Бан если ролик длиннее N секунд (`0` = выкл.) |
| `ban_max_file_size_mb` | число | Бан если файл тяжелее N МБ (`0` = выкл.) |
| `ban_llm_ad_rate_threshold` | число 0.0–1.0 | Бан если `meta.json["ad_check"]["ad_rate"]` >= значения. `0.0` = выкл. По умолчанию `0.85` |
| `ban_no_ad_check` | bool | Бан постов без `ad_check` в `meta.json` (ещё не проверены `check_ad.py`). По умолчанию `true` |

Дополнительные правила на Python добавляются непосредственно в функцию `build_ban_rules()` в `join_video.py` — там есть секция с примерами.

#### Текущие активные правила (`.env.json`)

**Текстовые подстроки** — ролик банится, если текст поста содержит любое из слов (регистр не важен):

| Категория | Ключевые слова |
|---|---|
| Маркетплейсы | `wildberries`, `wb.ru`, `вайлдберриз`, `ozon.ru`, `ozon.com`, `aliexpress`, `lamoda` |
| Промо-язык | `промокод`, `promo`, `скидка`, `скидку`, `скидкой`, `по промо`, `выгодн` |
| Реклама / партнёрство | `реклама`, `рекламн`, `партнер`, `партнёр`, `**Канал` |
| Призывы к действию | `по ссылке`, `купить здесь`, `купить тут`, `перейти по ссылке`, `жми сюда`, `жми на ссылку`, `переходите в канал`, `переходите по ссылке`, `подписывайтесь`, `подписывайтесь на` |
| Кликбейт | `лайфхаки`, `реально работает`, `проверено`, `гарантия`, `успей`, `аналог` |

**Длительность:**
- короче **3 секунд** — пропуск (скорее всего стикер или нарезка-артефакт)
- длиннее **35 секунд** — пропуск (длинные ролики не нужны в подборке)

## Использование

### `vk_vsf_bot.py` — просмотр и загрузка

#### Параметры и их умолчания

| Параметр | Возможные значения | Умолчание |
|---|---|---|
| `--channel` | `@username`, `+invite_hash`, `https://t.me/+xxx`, числовой ID | `TARGET_CHANNEL` из `.env` |
| `--count` | целое число ≥ 1 | `RECENT_POSTS_COUNT` из `.env` → `10` |
| `--work-dir` | путь к каталогу | `WORK_DIR` из `.env` → `work` |
| `--since` | `YYYY-MM-DD` или `YYYY-MM-DDTHH:MM:SS` | не задано — без ограничения по дате |
| `--refresh-meta` | флаг (без значения) | выкл. — уже скачанные посты пропускаются |

```powershell
# Справка
python vk_vsf_bot.py
python vk_vsf_bot.py --help

# Просмотр последних 10 постов из TARGET_CHANNEL
python vk_vsf_bot.py view-recent

# Просмотр постов из конкретного канала
python vk_vsf_bot.py view-recent --channel @babazoyka --count 20

# Загрузка последних 50 постов из одного канала
python vk_vsf_bot.py download --channel @babazoyka --count 50

# Пакетная загрузка со всех каналов из CHANNELS (по 200 постов)
python vk_vsf_bot.py download --all-channels --count 200

# Загрузка только постов начиная с даты (останавливается, как только встречает более старый)
python vk_vsf_bot.py download --all-channels --since 2026-04-01

# Загрузка с указанием рабочего каталога
python vk_vsf_bot.py download --all-channels --count 100 --work-dir H:\TEMP\vk_vsf
```

**Приоритет выбора канала:**
1. `--channel @foo` — один явный канал
2. `--all-channels` — все каналы из `CHANNELS` в `.env`
3. без флагов — `TARGET_CHANNEL` из `.env`

При ошибке на одном канале загрузка остальных продолжается.

---

### `check_ad.py` — LLM-классификация рекламы и детектор контента

#### Параметры команды `update`

| Параметр | Описание | Умолчание |
|---|---|---|
| `--work-dir` | Рабочий каталог с постами | `WORK_DIR` из `.env` |
| `--channel NAME` | Обработать только этот канал (имя папки) | все каналы |
| `--batch-size N` | Постов за один запрос к LLM | `5` |
| `--model MODEL` | Модель Ollama | `OLLAMA_MODEL` из `.env` |
| `--force` | Перепроверить уже проверенные посты | выкл. |
| `--dry-run` | Показать что будет обработано, без запросов к LLM | выкл. |
| `--limit N` | Обработать не более N постов (для теста) | без ограничений |
| `--think` | Включить chain-of-thought в модели (медленней, полезно для отладки) | выкл. |
| `--start-date YYYY-MM-DD` | Обработать только посты, опубликованные начиная с этой даты | без ограничения |
| `--end-date YYYY-MM-DD` | Обработать только посты, опубликованные не позже этой даты | без ограничения |
| `--last-days N` | Обработать только посты за последние N дней (включая сегодня). Принимает `7` или `7d`. Перекрывает `--start-date`. | без ограничения |
| `--prompt-before FILE` | Файл system-промпта | `llm/check_ad_prompt.p1.md` |
| `--prompt-after FILE` | Файл user-промпта (должен содержать `{{ content }}`) | `llm/check_ad_prompt.p2.md` |

```powershell
# Справка
python check_ad.py
python check_ad.py update --help

# Посмотреть что будет обработано (без запросов к LLM)
python check_ad.py update --dry-run

# Проверить самые новые 5 постов (один батч)
python check_ad.py update --limit 5

# Проверить один канал
python check_ad.py update --channel babazoyka

# Перепроверить уже проверенные посты
python check_ad.py update --force --limit 10

# Запустить на всё что есть
python check_ad.py update

# С отладочным выводом промптов и ответа LLM
python check_ad.py --log-level DEBUG update --limit 5

# С chain-of-thought (медленнее, но полезно при отладке промпта)
python check_ad.py update --think --limit 3

# Фильтрация по датам
python check_ad.py update --last-days 7
python check_ad.py update --start-date 2026-05-01
python check_ad.py update --start-date 2026-05-01 --end-date 2026-05-31

# Перепроверить уже проверенные посты, но только за последнюю неделю
python check_ad.py update --force --last-days 7

# Использовать свои промпты
python check_ad.py update --prompt-before llm/my_system.md --prompt-after llm/my_user.md
```

Результат дописывается в каждый `meta.json`:
```json
"ad_check": {
  "ad_rate": 0.9,
  "proof_of_ad": "Пост продвигает канал микроМИР через invite-ссылку, которой нет в белом списке."
}
```

`ad_rate`: `0.0` = точно не реклама, `1.0` = точно реклама.

---

#### Параметры команды `update-nudes`

| Параметр | Описание | Умолчание |
|---|---|---|
| `--work-dir` | Рабочий каталог с постами | `WORK_DIR` из `.env` |
| `--channel NAME` | Обработать только этот канал (имя папки) | все каналы |
| `--frames N` | Количество кадров для выборки из каждого видео | `10` |
| `--force` | Перепроверить уже проверенные посты | выкл. |
| `--dry-run` | Показать что будет обработано, без запуска детектора | выкл. |
| `--limit N` | Обработать не более N постов (для теста) | без ограничений |
| `--start-date YYYY-MM-DD` | Обработать только посты, опубликованные начиная с этой даты | без ограничения |
| `--end-date YYYY-MM-DD` | Обработать только посты, опубликованные не позже этой даты | без ограничения |
| `--last-days N` | Обработать только посты за последние N дней (включая сегодня). Принимает `7` или `7d`. Перекрывает `--start-date`. | без ограничения |

```powershell
# Справка
python check_ad.py update-nudes --help

# Посмотреть что будет обработано (без запуска детектора)
python check_ad.py update-nudes --dry-run

# Запустить на все посты с видео
python check_ad.py update-nudes

# Только один канал, больше кадров для точности
python check_ad.py update-nudes --channel babazoyka --frames 20

# Только посты за последние 7 дней
python check_ad.py update-nudes --last-days 7

# Перепроверить за конкретный период
python check_ad.py update-nudes --force --start-date 2026-05-01 --end-date 2026-05-31

# Перепроверить уже проверенные
python check_ad.py update-nudes --force --limit 10
```

Результат дописывается в каждый `meta.json`:
```json
"nude_check": {
  "nude_rate": 0.0,
  "video_file": "video.mp4",
  "frames_sampled": 10
}
```

`nude_rate`: `0.0` = чисто, `1.0` = явный контент. Используется в `join_video.py` через `--nude-rate-threshold` (порог по умолчанию: `0.65`, задаётся через `NUDE_RATE_THRESHOLD` в `.env`).

---

### `join_video.py` — объединение видео

#### Параметры и их умолчания

| Параметр | Возможные значения | Умолчание |
|---|---|---|
| `--work-dir` | путь к каталогу | `WORK_DIR` из `.env` → `H:\TEMP\vk_vsf` |
| `--output` | путь к файлу `.mp4` | `OUTPUT_DIR\vk_vsf_output-YYYYMMDD-<ориентация>[-<период>].mp4` |
| `--sort` | `asc`, `desc`, `interest-asc`, `interest-desc` | `asc` |
| `--start-date` | `YYYY-MM-DD` | не задано — без нижней границы |
| `--end-date` | `YYYY-MM-DD` | не задано — без верхней границы |
| `--last-days` | целое число ≥ 1, допускается суффикс `d` (например `7` или `7d`) | не задано — фильтр выкл. |
| `--audio-delay-ms` | целое число в мс, положительное или отрицательное | `AUDIO_DELAY_MS` из `.env` → `0` |
| `--orientation` | `horizontal`, `vertical` | `horizontal` (1920×1080) |
| `--no-ad-filter` | флаг (без значения) | выкл. — фильтр рекламы активен |
| `--ad-rate-threshold` | число 0.0–1.0 | `0.85` (из `.env.json` `ban_llm_ad_rate_threshold`) |
| `--nude-rate-threshold` | число 0.0–1.0 | `0.65` (из `.env` `NUDE_RATE_THRESHOLD`) |
| `--limit` | целое число ≥ 1 | не задано — берутся все ролики. Случайно выбирает N роликов из отфильтрованного набора, сохраняя порядок сортировки. |

```powershell
# Справка (также выводится при запуске без аргументов: python join_video.py)
python join_video.py --help

# Объединить все видео из WORK_DIR (сортировка по дате, по возрастанию)
python join_video.py --output result.mp4

# Из подкаталога конкретного канала
python join_video.py --work-dir H:\TEMP\vk_vsf\babazoyka --output babazoyka_full.mp4

# Сортировка: от новых к старым
python join_video.py --output result.mp4 --sort desc

# Сортировка по интересности: от скучных к интересным (финал зрелищный)
python join_video.py --output result.mp4 --sort interest-asc

# Сортировка по интересности: от интересных к скучным
python join_video.py --output result.mp4 --sort interest-desc

# Только ролики за определённый период
python join_video.py --output result.mp4 --start-date 2026-01-01 --end-date 2026-04-30

# Только ролики за последние 7 дней (включая сегодня)
python join_video.py --output result.mp4 --last-days 7

# То же самое с суффиксом 'd'
python join_video.py --output result.mp4 --last-days 7d

# Коррекция рассинхрона аудио (+200 мс — аудио запаздывало)
python join_video.py --output result.mp4 --audio-delay-ms 200

# Отключить фильтр рекламы (показать все ролики без исключений)
python join_video.py --output result.mp4 --no-ad-filter

# Использовать строгий порог для LLM-классификации (исключать при ad_rate >= 0.7)
python join_video.py --output result.mp4 --ad-rate-threshold 0.7

# Вертикальное видео (Reels / Shorts / TikTok) — 1080×1920
python join_video.py --output result_vertical.mp4 --orientation vertical

# Вертикальное + за последние 7 дней + по интересности
python join_video.py --output result_vertical.mp4 --orientation vertical --last-days 7 --sort interest-asc

# Несколько опций вместе
python join_video.py --output result.mp4 --sort interest-asc --last-days 7 --audio-delay-ms 250
python join_video.py --output result.mp4 --sort interest-asc --start-date 2026-01-01 --audio-delay-ms 250

# Случайная выборка: взять 20 случайных роликов из всего набора
python join_video.py --output result.mp4 --limit 20

# Случайная выборка из отфильтрованного набора (за 7 дней, без рекламы, по интересности)
python join_video.py --output result.mp4 --limit 15 --last-days 7 --sort interest-asc
```

#### Фильтрация по дате

| Параметр | Описание |
|---|---|
| `--start-date YYYY-MM-DD` | Включить только ролики, опубликованные начиная с этой даты (включительно) |
| `--end-date YYYY-MM-DD` | Включить только ролики, опубликованные не позже этой даты (включительно) |
| `--last-days N` | Включить только ролики за последние N дней, включая сегодня. Принимает `7` или `7d`. Перекрывает `--start-date`. |

`--last-days 7` эквивалентно `--start-date <6 дней назад>` (сегодня + 6 предыдущих = 7 дней).

#### Параметры сортировки

| `--sort` | Порядок |
|---|---|
| `asc` (default) | По дате, старые первые |
| `desc` | По дате, новые первые |
| `interest-asc` | По интересности, скучные первые — финал зрелищный |
| `interest-desc` | По интересности, интересные первые |

#### Как считается interest-score

**Шаг 1 — сырой engagement rate:**
```
raw = (reactions × W_REACTIONS + forwards × W_FORWARDS + replies × W_REPLIES) / max(views, 1)
```

Деление на просмотры убирает зависимость от размера аудитории канала: видео с 200 реакциями из 3 000 просмотров (6.7%) опережает видео с 500 реакциями из 100 000 просмотров (0.5%).

**Шаг 2 — min-max нормализация внутри каждого канала отдельно:**
```
score = (raw − raw_min_channel) / (raw_max_channel − raw_min_channel)   →  [0.0 … 1.0]
```

Нормализация считается независимо для каждого канала. Таким образом в каждом канале ровно один ролик получает 0.0 и ровно один — 1.0. Это гарантирует, что ни один канал не доминирует в начале или конце подборки — «лучшие» видео равномерно перемежаются из всех каналов.

Если в канале все ролики имеют одинаковый raw (или только один ролик) — присваивается `0.5`.

Веса задаются в `.env`: `INTEREST_W_REACTIONS`, `INTEREST_W_FORWARDS`, `INTEREST_W_REPLIES`.

#### Коррекция аудио (`--audio-delay-ms`)

| Значение | Ситуация | Действие |
|---|---|---|
| `+200` | Аудио запаздывает на 200 мс | Обрезает первые 200 мс аудио (сдвигает влево) |
| `-200` | Аудио опережает на 200 мс | Добавляет 200 мс тишины в начало (сдвигает вправо) |
| `0` | Синхронизация нормальная | Ничего не делает |

Значение по умолчанию читается из `AUDIO_DELAY_MS` в `.env`.

## Структура каталогов

```
WORK_DIR/
  <channel_name>/
    channel_id.txt        — кэш числового ID канала
    <post_id>/
      meta.json           — метаданные поста (id, date, media_type,
                            views, forwards, replies, reactions, ad_check)
      text.txt            — текст поста (если есть)
      <video_file>.mp4    — медиафайл
```

### Содержимое `meta.json`

```json
{
  "post_id": 23605,
  "date": "2026-05-04T13:05:05+00:00",
  "has_media": true,
  "media_type": "MessageMediaDocument",
  "views": 2270,
  "forwards": 15,
  "replies": null,
  "reactions_total": 20,
  "reactions": { "🌚": 11, "😁": 9 },
  "ad_check": {
    "ad_rate": 0.05,
    "proof_of_ad": "Мем без ссылок на сторонние каналы, короткая подпись с эмодзи."
  },
  "nude_check": {
    "nude_rate": 0.0,
    "video_file": "video.mp4",
    "frames_sampled": 10
  }
}
```

`ad_check` добавляется скриптом `check_ad.py update`. До запуска этого скрипта поле отсутствует.

`nude_check` добавляется скриптом `check_ad.py update-nudes`. До запуска этого скрипта поле отсутствует.

## Выходной видеофайл

| Параметр | Горизонтальный (default) | Вертикальный (`--orientation vertical`) |
|---|---|---|
| Разрешение | 1920×1080 (Full HD) | 1080×1920 (Full HD portrait) |
| Видеокодек | H.264 High Profile, Level 4.1, CRF 23 | ← то же |
| Аудиокодек | AAC-LC, 192 kbps, 48 kHz, стерео | ← то же |
| Контейнер | MP4, оптимизирован для стриминга (`+faststart`) | ← то же |
| Нестандартное соотношение сторон | Размытый фон (boxblur) | ← то же |
| Клипы без аудио | Заменяются тишиной автоматически | ← то же |
| Главы | Встраиваются в MP4 по каналу/посту/дате | ← то же |

Рядом с выходным файлом создаётся `<output>.timestamps.txt` — список тайм-кодов для YouTube-описания.

## Логи

| Файл | Скрипт |
|---|---|
| `logs/vk_vsf_bot.log` | `vk_vsf_bot.py` |
| `logs/join_video.log` | `join_video.py` |
| `logs/check_ad.log` | `check_ad.py` |
| `logs/mcp_server.log` | `mcp_server.py` |

Уровень задаётся через `LOG_LEVEL` в `.env`. Логи пишутся одновременно в файл и в консоль.

---

## MCP-сервер (`mcp_server.py`)

FastMCP HTTP-сервер, предоставляющий AI-агентам инструменты для чтения и классификации постов.

```powershell
# Запуск
python mcp_server.py run

# Список инструментов
python mcp_server.py list-tools
```

Endpoint: `http://localhost:3100/mcp`  
Переменные окружения: `MCP_HOST`, `MCP_PORT` (см. `.env`).

### Тестирование через PowerShell (curl)

Сервер работает в режиме **stateless + json_response** — session ID не нужен, каждый запрос независим, ответ возвращается как обычный JSON (не SSE-стрим).

#### Функция-обёртка для вызова инструментов

```powershell
function Invoke-McpTool {
    param(
        [string]$Tool,
        [hashtable]$Params = @{}   # NB: не $Args — это зарезервированная переменная PS
    )
    $body = @{
        jsonrpc = "2.0"
        id      = 1
        method  = "tools/call"
        params  = @{
            name      = $Tool
            arguments = $Params
        }
    } | ConvertTo-Json -Depth 10

    curl.exe -s -X POST http://localhost:3100/mcp `
      -H "Content-Type: application/json" `
      -H "Accept: application/json" `
      -d $body
}
```

#### Примеры вызовов

```powershell
# Список каналов
Invoke-McpTool -Tool "list_channels"

# Статистика по каналу
Invoke-McpTool -Tool "get_stats" -Params @{ channel = "babazoyka" }

# Следующий батч непроверенных постов (все каналы, 3 поста)
Invoke-McpTool -Tool "get_unchecked_batch" -Params @{ batch_size = 3 }

# Непроверенные посты конкретного канала
Invoke-McpTool -Tool "get_unchecked_batch" -Params @{ channel = "babazoyka"; batch_size = 5 }

# Посты по списку ID
Invoke-McpTool -Tool "get_posts" -Params @{ channel = "babazoyka"; ids = @("23609", "23610") }

# Список инструментов через JSON-RPC
curl.exe -s -X POST http://localhost:3100/mcp `
  -H "Content-Type: application/json" `
  -H "Accept: application/json" `
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

> ⚠️ При переключении сервера обратно в stateful-режим (без `stateless_http=True`) потребуется снова добавить шаг инициализации сессии и заголовок `Mcp-Session-Id`.

