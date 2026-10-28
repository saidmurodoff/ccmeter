# ccmeter

Учёт токенов и лимитов Claude Code по локальным логам (`~/.claude/projects/**/*.jsonl`).
Один файл на Python 3.9+, без зависимостей.

```
Opus 5.5 │ ctx 105.2k 11% │ сессия $0.94 │ окно 390.9k 42% · сброс 3ч 49м │ сегодня $5.26
```

## Установка

Через pipx:

```bash
pipx install git+https://github.com/saidmurodoff/ccmeter
ccmeter install          # подключит status line в ~/.claude/settings.json
```

Без pipx, одним файлом:

```bash
curl -fsSL https://raw.githubusercontent.com/saidmurodoff/ccmeter/main/ccmeter.py -o /tmp/ccmeter.py
python3 /tmp/ccmeter.py install   # скопирует в ~/.local/bin и подключит status line
```

`install` делает резервную копию `settings.json` в `settings.json.bak` и не трогает остальные настройки.
Если у вас уже настроена чужая status line, `install` её не заменит. Чтобы заменить, добавьте `--force`.

## Команды

```bash
ccmeter                              # то же, что daily
ccmeter daily | weekly | monthly     # сводка по периодам
ccmeter session -n 10                # последние 10 сессий
ccmeter blocks                       # 5-часовые окна лимита
ccmeter live                         # монитор текущего окна (обновление раз в 5 с)
ccmeter live --limit 2000000         # со своим лимитом токенов на окно
ccmeter live --limit-usd 50          # или лимитом в $-эквиваленте
```

Общие флаги: `--since 2026-10-01`, `--until 2026-10-31`, `--project имя`, `--json`, `--pricing prices.json`.

## Настройки

`~/.config/ccmeter/config.json` (все поля необязательные):

```json
{
  "limit": 2000000,
  "limit_usd": 50,
  "pricing": { "claude-new-model": [3.0, 15.0, 0.3] }
}
```

Лимит из этого файла используют и `live`, и status line. Цены указываются в долларах за 1M токенов: input, output и чтение из кэша.

## Как считается

- **Дедупликация.** Одно API-сообщение пишется в лог по разу на каждый блок ответа. Учитывается одна запись на пару `message.id` + `requestId`.
- **Окно лимита.** Окно начинается с часа первого сообщения и длится 5 часов. Если окно истекло или пауза длилась 5 часов и больше, начинается новое.
- **Токены лимита** = input + output + запись в кэш. Чтение из кэша показывается отдельно.
- **Лимит по умолчанию** — максимум, израсходованный в одном из прошлых окон. Точный лимит подписки в логах не хранится, поэтому официальное значение смотрите в `/usage`.
- **Стоимость** считается по ценам API с учётом записи в кэш (5m ×1.25, 1h ×2) и fast mode (×2). На подписке это ориентир, а не реальный счёт.
- **Кэш разбора** хранится в `~/.cache/ccmeter/`. Дописанные файлы дочитываются с последнего места, поэтому status line отрабатывает примерно за 80 мс.

## Разработка

```bash
python3 -m unittest discover -s tests -v
```
