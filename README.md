# Crypto Futures Signal Bot v0.01 for Railway

Telegram-бот для сигналов по крипто-фьючерсам: MEXC/BingX через CCXT, младший/старший таймфреймы, график, entry, stop-loss, TP1/TP2/TP3, новости, top-N, настройки, paper-autotrade.

## Важно
Это не финансовый совет и не гарантия прибыли. Автоторговля выключена по умолчанию и работает в PAPER-режиме, пока `ALLOW_LIVE_TRADING=false`.

Создавай API-ключи биржи **без права вывода средств**. Не отправляй API-ключи в группы и общие чаты.

## Railway deploy
1. Создай Telegram bot через BotFather.
2. Залей репозиторий в GitHub.
3. Railway → New Project → Deploy from GitHub.
4. Variables: `TELEGRAM_BOT_TOKEN`.
5. Start command: `python bot.py`.
6. После запуска первым отправь `/start` в Telegram — этот чат станет админом.

## Админ
Первый пользователь/чат, который отправит `/start`, сохраняется как `admin_id` в `state.json`. Остальные чаты не смогут управлять ботом.

## API-ключи через чат
Команды:

- `api` — показать статус ключей.
- `api mexc` — добавить/заменить API Key и API Secret MEXC.
- `api bingx` — добавить/заменить API Key и API Secret BingX.
- `api status` — статус.
- `api delete mexc` — удалить ключи MEXC из chat-хранилища.
- `api delete bingx` — удалить ключи BingX из chat-хранилища.
- `cancel` — отменить ввод.

Ключи сохраняются в зашифрованный файл `api_keys.enc` в `DATA_DIR`/Railway Volume. Ключ шифрования хранится в `bot_secret.key` рядом с ним. Если удалить volume, ключи пропадут.

## Команды
`signal BTC/USDT`, `mexc top-100`, `top-200`, `new SOL/USDT`, `delete SOL/USDT`, `delete all`, `exchange mexc`, `exchange bingx`, `tf 15m 1h`, `auto on/off`, `trades 10`, `take 0.5 3`, `news on/off`, `ping`.

## Live trading
Для LIVE нужны:

1. `ALLOW_LIVE_TRADING=true` в Railway Variables.
2. API-ключи через чат: `api mexc` или `api bingx`.
3. Включить автоторговлю кнопкой `⚡ Автоторговля`.

Если `ALLOW_LIVE_TRADING=false`, бот создаёт только paper trades.
