# README.md: описание проекта.
Прочитай файл.

# Инструкции для AI Агента

> **Общее правило:** Все инструкции далее трактовать как "Если не указано явно иное, то ..."

## Подключение правил из субмодуля claude

**Если субмодуля нет:**
```powershell
git submodule add https://github.com/vadim-kosarev/claude.git claude
git submodule update --init --recursive
```

**Используемые правила:**
- `claude/CLAUDE.base.md` - базовые правила (язык, среда разработки, файлы)
- `claude/CLAUDE.python.md` - Python-специфичные правила
