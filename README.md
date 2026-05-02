# Crypto Futures Signal Bot v0.04 для Railway

## Railway deploy

1. Залей файлы в GitHub.
2. Railway → New Project → Deploy from GitHub.
3. Variables:

```text
TELEGRAM_BOT_TOKEN=токен_бота
ALLOW_LIVE_TRADING=false
ORDER_AMOUNT_USDT=10
SIGNAL_LOOP_SECONDS=300
```

4. После запуска первым отправь боту `/start`. Первый чат становится админом.

## Главное в v0.04

- Кнопка `Signal` анализирует все загруженные монеты.
- `all signal` — краткий сигнал по всем монетам одним/несколькими сообщениями.
- `one signal` — подробный сигнал по каждой монете отдельным сообщением, со свечным графиком.
- `signal btc` — подробный сигнал по одной монете.
- `MEXC top+` / `BINGX top+` → отправь `top-100`, `top-250`, `100` и т.д.
- При загрузке MEXC top+ список BINGX заменяется, и наоборот.
- Таймфреймы: `15m`, `1h`, `4h`, `1d`, `1w`.
- График теперь свечной, крупный.
- Entry — лимитный выгодный вход, не просто текущая цена.
- TP сортируются корректно: LONG вверх, SHORT вниз.
- Тейки расширяются по старшему TF: чем выше TF, тем шире диапазон целей.
- Биржа mexc/bingx меняется кнопкой `Биржа` или командой `exchange mexc` / `exchange bingx`.
- Включённые режимы видны прямо на кнопках: ✅/❌.
- `Закрыть всё` закрывает PAPER-сделки в журнале.
- `Профит` показывает открытые/закрытые PAPER-сделки и PNL.
- `Ping` показывает latency, uptime, version и memory.
- Новости берутся из RSS, выводятся с русскоязычных источников, если доступны.

## Команды

```text
signal btc
mexc top-100
bingx top-200
top-50
new sol
delete sol
delete all
tf 15m 4h
take 0.5 4
trades 10
all signal
one signal
auto on
auto off
api mexc
api bingx
api status
api delete mexc
close all
profit
```

## Автоторговля

По умолчанию LIVE выключен:

```text
ALLOW_LIVE_TRADING=false
```

Так бот создаёт только PAPER-сделки. Для реальных сделок нужно отдельно поставить `ALLOW_LIVE_TRADING=true`, добавить API-ключи и понимать риск. API-ключи создавай без права вывода средств.

⚠️ Бот не гарантирует прибыль. Это инструмент анализа/автоматизации, не финансовая рекомендация.
