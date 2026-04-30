# vsnifer - Telegram Bot для сбора видеоконтента

## Назначение

Бот автоматически собирает видео из указанных Telegram-каналов, объединяет их в единый видео-файл для просмотра и последующей публикации на YouTube.

Раньше я смотрел подборки смешных видео на YouTube, но все каналы закрылись. Теперь контент ищу в Telegram-каналах и Instagram. Хочу смотреть подборки в одном месте, не переходя между каналами.

## Возможности

- Просмотр последних постов из Telegram-каналов
- Загрузка видео из каналов
- Объединение видео в один файл через FFmpeg
- Управление через команды

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
   - Заполните форму (App title, Shortname, Url - опционально)
   - Получите **API_ID** и **API_HASH**
   
   **Шаг 4:** Скопируйте эти значения для следующего шага

5. Создайте `.env` файл на основе `.env.example`:
```powershell
copy .env.example .env
```

6. Отредактируйте `.env` и замените значения на реальные:
```
API_ID=123456789
API_HASH=abcdef1234567890abcdef1234567890
BOT_TOKEN=8770438622:AAGG6eUujQuuSKsfs3cgevSNxN1afu_EOxQ
TARGET_CHANNEL=+otRtx2aMM0ZlMTVi
```

   - `API_ID` - ваше значение из https://my.telegram.org/apps
   - `API_HASH` - ваше значение из https://my.telegram.org/apps
   - `BOT_TOKEN` - уже указан (от BotFather)
   - `TARGET_CHANNEL` - канал по умолчанию (или оставьте как есть)

## Использование

### Просмотр последних постов
```powershell
python vk_vsf_bot.py view-recent
```

### Просмотр последних 20 постов
```powershell
python vk_vsf_bot.py view-recent --count 20
```

### Просмотр постов из конкретного канала
```powershell
python vk_vsf_bot.py view-recent --channel "+otRtx2aMM0ZlMTVi" --count 10
```

### Справка
```powershell
python vk_vsf_bot.py
python vk_vsf_bot.py --help
python vk_vsf_bot.py view-recent --help
```

## Конфигурация

Все параметры хранятся в файле `.env`:

- `API_ID` - ID приложения из https://my.telegram.org/apps
- `API_HASH` - Hash приложения из https://my.telegram.org/apps
- `BOT_TOKEN` - Token от BotFather
- `TARGET_CHANNEL` - Канал по умолчанию для просмотра
- `RECENT_POSTS_COUNT` - Количество постов для загрузки по умолчанию
- `DEBUG` - Режим отладки

## Логи

Логи сохраняются в папке `logs/` и также выводятся в консоль.

## От BotFather

Бот зарегистрирован: t.me/vk_vsf_bot

Token (сохранён в .env):
```
87......:AAGG6................
```

Подробнее об API: https://core.telegram.org/bots/api

