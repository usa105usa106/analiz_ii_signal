# Crypto Futures Signal Bot v0.05 для Railway

⚠️ Бот не гарантирует прибыль. Проценты успешности, score и “супер сделка” — это расчёт модели, не финансовая рекомендация.

## Railway deploy

1. Залей файлы в GitHub.
2. Railway → New Project → Deploy from GitHub.
3. Variables:

```text
TELEGRAM_BOT_TOKEN=токен_бота
ALLOW_LIVE_TRADING=false
ORDER_AMOUNT_USDT=10
SIGNAL_LOOP_SECONDS=300
MAX_SIGNAL_SCAN=500
SCAN_PROGRESS_EVERY=50
SUPER_ALERT_COOLDOWN_SECONDS=1800
```

Опционально для кнопки `Цена` по CoinMarketCap:

```text
CMC_API_KEY=твой_coinmarketcap_api_key
```

Если `CMC_API_KEY` не задан, кнопка `Цена` использует fallback по Binance public ticker.

## Главное в v0.05

- Исправлена ошибка Telegram `can't parse entities: Unsupported start tag`.
- `Signal` запускает анализ в фоне, чтобы бот не зависал при 100–500 монетах.
- `ALL SIGNAL` сортируется: сверху самые сильные setup по score/расчётной проходимости.
- Добавлена расчётная проходимость LONG/SHORT и Score 1–10.
- Зелёная стрелка вверх = LONG, красная стрелка вниз = SHORT, серый круг = NEUTRAL.
- График стал крупнее и чётче, линии Entry/SL/TP жирные и подписаны.
- Добавлена кнопка `Цена`.
- Команды цены:

```text
price top-10
price 3
price 50
```

- Добавлена кнопка `Супер сделка` ВКЛ/ВЫКЛ.
- Если setup имеет расчётную проходимость 95–97% и score от 7, бот присылает срочное уведомление.
- Добавлена защита от повторных супер-уведомлений по одной монете через cooldown.

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
price top-10
price 3
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
