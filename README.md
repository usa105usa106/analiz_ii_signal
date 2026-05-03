# Crypto Futures Signal Bot v0.15 для Railway

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
NEWS_REFRESH_SECONDS=900
NEWS_SIGNAL_REFRESH_SECONDS=300
NEWS_CACHE_LIMIT=60
NEWS_STRONG_SECONDS=10800
NEWS_EXPIRE_SECONDS=21600
```

Опционально для кнопки `Цена` по CoinMarketCap:

```text
CMC_API_KEY=твой_coinmarketcap_api_key
```

Если `CMC_API_KEY` не задан, кнопка `Цена` использует fallback по CoinGecko, затем по выбранной бирже MEXC/BingX.

## Главное в v0.15

- Исправлена ошибка Telegram `can't parse entities: Unsupported start tag`.
- `Signal` запускает анализ в фоне, чтобы бот не зависал при 100–500 монетах.
- `ALL SIGNAL` сортируется строго по расчётной проходимости: сверху максимальный % успешности, затем Score.
- `10 signal` оставляет только 10 лучших монет по расчётной успешности.
- Кнопка `Порог` фильтрует сигналы по успешности: 60/70/75/80/85/90/95%.
- Если отправить просто тикер, например `btc`, это равно `signal btc`.
- Кнопка `Цена` исключает стейблкойны из топа.
- Добавлена расчётная проходимость LONG/SHORT и Score 1–10.
- Зелёный круг = LONG, красный круг = SHORT, серый круг = NEUTRAL.
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


## Новости v0.15

- Кнопка `Новости` теперь обновляет RSS-новости и хранит их в `state.json`.
- Если есть новые новости, они добавляются сверху списка.
- Если новых нет, список не меняется.
- Когда `Новости ✅`, бот перед каждым сигналом обновляет новостной фон через кэш и учитывает его в процентах успешности.
- Позитивный фон повышает LONG и понижает SHORT, негативный фон делает наоборот.
- В подробном сигнале отображается `Новостной фон`, свежесть новостей и влияние на сделку в процентах.
- Срок действия новостей в сигналах: 0–3 часа = 100% влияния, 3–6 часов = 50%, старше 6 часов = 0%. Старые новости остаются в списке, но не искажают новые сигналы.

Команды:

```text
news on
news off
news refresh
```

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
10 signal
threshold 70
порог 80
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
