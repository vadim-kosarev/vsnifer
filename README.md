# vsnifer — сборщик видеоконтента из Telegram-каналов

## Назначение

Инструмент для автоматического сбора видео из Telegram-каналов и объединения их в один Full HD файл для просмотра или публикации на YouTube.

Раньше я смотрел подборки смешных видео на YouTube, но все каналы закрылись. Теперь контент ищу в Telegram-каналах. Хочу смотреть подборки в одном месте, не переходя между каналами.

## Состав

| Скрипт | Назначение |
|---|---|
| `vk_vsf_bot.py` | Подключается к Telegram-каналам, просматривает и скачивает посты с медиа |
| `join_video.py` | Объединяет скачанные видеофайлы в один Full HD файл через FFmpeg |

## Возможности

- Просмотр последних постов из Telegram-каналов
- Загрузка постов с медиафайлами (видео, фото, документы)
- Пакетная загрузка сразу со всех каналов (`--all-channels`)
- Идемпотентная загрузка — уже скачанные посты пропускаются
- Кэширование числового ID канала (не зависит от смены username)
- Поддержка прокси: SOCKS4, SOCKS5, MTProto (в т.ч. FakeTLS)
- Объединение видео в один Full HD файл (1920×1080)
- Надёжная обработка аудио через concat filter: разные sample rate, отсутствие аудиодорожки
- Для вертикального и квадратного видео — размытый фон вместо чёрных полос
- Сортировка видео по дате поста (из `meta.json`)

## Установка

### Требования
- Python 3.8+
- FFmpeg (для объединения видео)

### Шаги установки

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

4. Получите Telegram API credentials:

   **Шаг 1:** Перейдите на https://my.telegram.org/apps

   **Шаг 2:** Войдите в ваш Telegram аккаунт

   **Шаг 3:** Создайте новое приложение (если его ещё нет):
   - Нажмите "Create new application"
   - Заполните форму (App title, Shortname, Url — опционально)
   - Получите **API_ID** и **API_HASH**

5. Создайте `.env` файл на основе `.env.example` и заполните реальными значениями:
```
API_ID=123456789
API_HASH=abcdef1234567890abcdef1234567890
PHONE=+79001234567
TARGET_CHANNEL=@babazoyka
CHANNELS=@babazoyka, https://t.me/+otRtx2aMM0ZlMTVi, https://t.me/+5wnJFWU8yLZjNTdi
WORK_DIR=H:\TEMP\vk_vsf
```

## Конфигурация

Все параметры хранятся в файле `.env`:

### Обязательные
| Переменная | Описание |
|---|---|
| `API_ID` | ID приложения из https://my.telegram.org/apps |
| `API_HASH` | Hash приложения из https://my.telegram.org/apps |

### Авторизация (одно из двух)
| Переменная | Описание |
|---|---|
| `PHONE` | Номер телефона пользователя (предпочтительно — позволяет читать любые каналы) |
| `BOT_TOKEN` | Token бота от BotFather (ограниченный доступ к каналам) |

### Каналы
| Переменная | Описание | По умолчанию |
|---|---|---|
| `TARGET_CHANNEL` | Канал для `view-recent` и `download` без флагов | — |
| `CHANNELS` | Список каналов для `download --all-channels`, через запятую | — |

Форматы channel ID в `CHANNELS`: `@username`, `+<invite_hash>`, `https://t.me/+xxx`, числовой ID. Пробелы вокруг запятых допустимы.

```
CHANNELS=@babazoyka, https://t.me/+otRtx2aMM0ZlMTVi, +HRom-yzU75JhYzIy
```

### Прочие параметры
| Переменная | Описание | По умолчанию |
|---|---|---|
| `RECENT_POSTS_COUNT` | Количество постов по умолчанию | `10` |
| `WORK_DIR` | Рабочий каталог для загрузки | `work` |
| `FFMPEG_HOME` | Путь к каталогу FFmpeg | берётся из PATH |
| `LOG_LEVEL` | Уровень логирования: `DEBUG`, `INFO`, `WARNING`, `ERROR` | `INFO` |

### Прокси (необязательно)
| Переменная | Описание |
|---|---|
| `PROXY_TYPE` | Тип прокси: `socks4`, `socks5`, `mtproto` |
| `PROXY_HOST` | Хост прокси |
| `PROXY_PORT` | Порт прокси |
| `PROXY_SECRET` | Секрет MTProto (с `ee`-префиксом для FakeTLS) |

## Использование

### vk_vsf_bot.py — просмотр и загрузка постов

```powershell
# Справка
python vk_vsf_bot.py
python vk_vsf_bot.py --help

# Просмотр последних 10 постов из TARGET_CHANNEL
python vk_vsf_bot.py view-recent

# Просмотр постов из конкретного канала
python vk_vsf_bot.py view-recent --channel @babazoyka --count 20

# Загрузка из TARGET_CHANNEL (10 последних)
python vk_vsf_bot.py download

# Загрузка из одного канала явно
python vk_vsf_bot.py download --channel @babazoyka --count 50

# Пакетная загрузка со всех каналов из CHANNELS (по 20 постов с каждого)
python vk_vsf_bot.py download --all-channels --count 20

# Загрузка в указанный каталог
python vk_vsf_bot.py download --all-channels --count 30 --work-dir H:\TEMP\vk_vsf
```

**Приоритет выбора канала для `download`:**
1. `--channel @foo` — один явный канал
2. `--all-channels` — все каналы из `CHANNELS` в `.env`
3. без флагов — `TARGET_CHANNEL` из `.env`

При ошибке на одном канале загрузка остальных продолжается.

### join_video.py — объединение видео в один файл

```powershell
# Справка
python join_video.py --help

# Объединить видео из WORK_DIR в result.mp4
python join_video.py --output result.mp4

# Объединить видео из конкретного подкаталога канала
python join_video.py --work-dir H:\TEMP\vk_vsf\babazoyka --output babazoyka_full.mp4

# Сортировка от новых к старым
python join_video.py --work-dir H:\TEMP\vk_vsf\babazoyka --output babazoyka_full.mp4 --sort desc
```

## Структура каталогов загрузки

```
WORK_DIR/
  <channel_name>/
    channel_id.txt       — кэш числового ID канала
    <post_id>/
      meta.json          — метаданные поста (id, date, media_type, views, forwards)
      text.txt           — текст поста (если есть)
      <video_file>.mp4   — медиафайл (если есть)
```

## Выходной видеофайл

- Разрешение: 1920×1080 (Full HD)
- Видеокодек: H.264 High Profile, Level 4.1, CRF 23
- Аудиокодек: AAC-LC, 192 kbps, 48 kHz, стерео
- Обработка аудио: concat filter с `asetpts=N/SR/TB` — надёжная синхронизация при разных sample rate и channel layout между клипами; клипы без аудиодорожки заполняются тишиной автоматически
- Для нестандартного соотношения сторон (вертикальное, квадратное): размытый фон вместо чёрных полос
- Оптимизирован для стриминга (`+faststart`)

## Логи

Каждый скрипт пишет в свой файл и в консоль:

| Файл | Скрипт |
|---|---|
| `logs/vk_vsf_bot.log` | `vk_vsf_bot.py` |
| `logs/join_video.log` | `join_video.py` |

Уровень логирования задаётся через `LOG_LEVEL` в `.env`.

## О боте

Зарегистрирован в BotFather: t.me/vk_vsf_bot

Подробнее об API: https://core.telegram.org/bots/api
