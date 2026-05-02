from __future__ import annotations

import base64
import hashlib
import html
import json
import math
import os
import re
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import ccxt
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import numpy as np
import requests
import telebot
from telebot import types
from cryptography.fernet import Fernet, InvalidToken

try:
    import psutil
except Exception:  # Railway всё равно поставит psutil из requirements.txt
    psutil = None

VERSION = "0.04"
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN не установлен")

DATA_DIR = Path(os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or os.getenv("DATA_DIR") or ".").resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"
TRADES_FILE = DATA_DIR / "trades.json"
API_KEYS_FILE = DATA_DIR / "api_keys.enc"
SECRET_KEY_FILE = DATA_DIR / "bot_secret.key"
CHART_DIR = DATA_DIR / "charts"
CHART_DIR.mkdir(parents=True, exist_ok=True)

SIGNAL_LOOP_SECONDS = int(os.getenv("SIGNAL_LOOP_SECONDS", "300"))
LIVE_TRADING_ENABLED = os.getenv("ALLOW_LIVE_TRADING", "false").lower() == "true"
DEFAULT_NOTIONAL_USDT = float(os.getenv("ORDER_AMOUNT_USDT", "10"))
MAX_SIGNAL_SCAN = int(os.getenv("MAX_SIGNAL_SCAN", "500"))
MAX_ONE_SIGNAL_CHARTS = int(os.getenv("MAX_ONE_SIGNAL_CHARTS", "40"))
MSK_OFFSET = 3

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")
start_time = time.time()
state_lock = threading.RLock()

TF_LIST = ["15m", "1h", "4h", "1d", "1w"]
TF_PAIRS = [("15m", "1h"), ("15m", "4h"), ("1h", "4h"), ("4h", "1d"), ("1d", "1w")]
ANALYSIS_MODES = ["multi", "best", "max_profit", "auto_ai"]
INTERNAL_PROFILES = ["trend", "momentum", "breakout", "mean_reversion"]

DEFAULT_STATE: Dict[str, Any] = {
    "exchange": "mexc",
    "symbols": ["BTC/USDT:USDT", "ETH/USDT:USDT"],
    "lower_tf": "15m",
    "higher_tf": "1h",
    "auto_signals": False,
    "autotrade": False,
    "adaptive_improvement": False,
    "news_enabled": False,
    "take_enabled": True,
    "take_min_profit_pct": 0.3,
    "take_max_profit_pct": 3.0,
    "take_auto_by_tf": True,
    "analysis_mode": "multi",
    "strategy_profile": "trend",
    "signal_output_mode": "all",  # all = одно короткое сообщение, one = подробно отдельными сообщениями
    "daily_trades_limit": 5,
    "daily_trades_count": 0,
    "daily_trades_date": "",
    "session_asia": False,
    "session_america": False,
    "last_top_exchange": "mexc",
    "admin_id": None,
}

api_input_sessions: Dict[int, Dict[str, str]] = {}
top_input_sessions: Dict[int, str] = {}


def esc(x: Any) -> str:
    return html.escape(str(x), quote=False)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_state() -> Dict[str, Any]:
    with state_lock:
        data = load_json(STATE_FILE, {})
        merged = DEFAULT_STATE.copy()
        if isinstance(data, dict):
            merged.update(data)
        # миграция старых версий
        for k, v in DEFAULT_STATE.items():
            merged.setdefault(k, v)
        return merged


def save_state(state: Dict[str, Any]) -> None:
    with state_lock:
        save_json(STATE_FILE, state)


def reset_state(preserve_admin: bool = True) -> Dict[str, Any]:
    old = load_state()
    state = DEFAULT_STATE.copy()
    if preserve_admin:
        state["admin_id"] = old.get("admin_id")
    save_state(state)
    return state


def load_trades() -> List[Dict[str, Any]]:
    data = load_json(TRADES_FILE, [])
    return data if isinstance(data, list) else []


def save_trades(trades: List[Dict[str, Any]]) -> None:
    save_json(TRADES_FILE, trades[-10000:])


def get_cipher() -> Fernet:
    if SECRET_KEY_FILE.exists():
        key = SECRET_KEY_FILE.read_bytes().strip()
    else:
        key = Fernet.generate_key()
        SECRET_KEY_FILE.write_bytes(key)
    return Fernet(key)


def load_api_keys() -> Dict[str, Dict[str, str]]:
    if not API_KEYS_FILE.exists():
        return {}
    try:
        raw = API_KEYS_FILE.read_bytes()
        if not raw:
            return {}
        data = json.loads(get_cipher().decrypt(raw).decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (InvalidToken, Exception) as e:
        print(f"api keys decrypt/load error: {e}", flush=True)
        return {}


def save_api_keys(data: Dict[str, Dict[str, str]]) -> None:
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    tmp = API_KEYS_FILE.with_suffix(API_KEYS_FILE.suffix + ".tmp")
    tmp.write_bytes(get_cipher().encrypt(payload))
    tmp.replace(API_KEYS_FILE)


def set_api_credentials(exchange: str, api_key: str, api_secret: str) -> None:
    exchange = exchange.lower().strip()
    if exchange not in {"mexc", "bingx"}:
        raise ValueError("Поддерживаются только mexc или bingx")
    data = load_api_keys()
    data[exchange] = {
        "api_key": api_key.strip(),
        "api_secret": api_secret.strip(),
        "updated_at": str(int(time.time())),
    }
    save_api_keys(data)


def delete_api_credentials(exchange: str) -> bool:
    exchange = exchange.lower().strip()
    data = load_api_keys()
    existed = exchange in data
    data.pop(exchange, None)
    save_api_keys(data)
    return existed


def get_api_credentials(exchange: str) -> Tuple[str, str]:
    exchange = exchange.lower().strip()
    data = load_api_keys().get(exchange, {})
    key = data.get("api_key") or os.getenv(f"{exchange.upper()}_API_KEY", "")
    secret = data.get("api_secret") or os.getenv(f"{exchange.upper()}_API_SECRET", "")
    return key.strip(), secret.strip()


def mask_secret(value: str) -> str:
    if not value:
        return "нет"
    if len(value) <= 8:
        return value[:2] + "***"
    return value[:4] + "…" + value[-4:]


def api_status_short() -> str:
    parts = []
    data = load_api_keys()
    for ex in ["mexc", "bingx"]:
        key, secret = get_api_credentials(ex)
        source = "chat" if ex in data else "env" if key and secret else "нет"
        parts.append(f"{ex.upper()}={source}")
    return ", ".join(parts)


def api_status_text() -> str:
    lines = ["🔑 <b>API ключи</b>"]
    data = load_api_keys()
    for ex in ["mexc", "bingx"]:
        key, secret = get_api_credentials(ex)
        source = "загружены через чат" if ex in data else "из Railway Variables" if key and secret else "не заданы"
        lines.append(f"{ex.upper()}: <b>{source}</b> | key: <code>{esc(mask_secret(key))}</code> | secret: <code>{esc(mask_secret(secret))}</code>")
    lines += [
        "",
        "Команды:",
        "<code>api mexc</code> — добавить/заменить ключи MEXC",
        "<code>api bingx</code> — добавить/заменить ключи BingX",
        "<code>api status</code> — статус",
        "<code>api delete mexc</code> — удалить ключи",
        "<code>cancel</code> — отменить ввод",
        "",
        "⚠️ Создавай API-ключи без права вывода средств. Бот попытается удалить сообщения с ключами.",
    ]
    return "\n".join(lines)


def is_admin(chat_id: int) -> bool:
    s = load_state()
    return str(s.get("admin_id") or "") == str(chat_id)


def ensure_admin_claim(chat_id: int) -> Tuple[bool, str]:
    s = load_state()
    if not s.get("admin_id"):
        s["admin_id"] = int(chat_id)
        save_state(s)
        return True, "✅ Ты назначен админом, потому что первым отправил /start."
    if str(s.get("admin_id")) == str(chat_id):
        return True, "✅ Ты админ."
    return False, "⛔ Админ уже назначен. Управление ботом закрыто для этого чата."


def admin_only(message) -> bool:
    if is_admin(message.chat.id):
        return True
    bot.send_message(message.chat.id, "⛔ Доступ запрещён. Админом становится первый чат, который отправил /start.", reply_markup=main_keyboard())
    return False


def safe_delete_user_message(message) -> None:
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass


def onoff(v: bool) -> str:
    return "✅" if v else "❌"


def main_keyboard() -> types.ReplyKeyboardMarkup:
    s = load_state()
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("📡 Signal", "⚙️ Настройки")
    kb.add("📈 MEXC top+", "📈 BINGX top+")
    kb.add(f"🏦 Биржа: {s['exchange'].upper()}", f"⏱ TF {s['lower_tf']}/{s['higher_tf']}")
    kb.add(f"📰 Новости {onoff(s['news_enabled'])}", f"🤖 Генератор: {s['analysis_mode']}")
    kb.add(f"🧠 Улучшения {onoff(s['adaptive_improvement'])}", f"💼 Сделки: {s['daily_trades_limit']}/сут")
    kb.add(f"🌏 Азия {onoff(s['session_asia'])}", f"🇺🇸 Америка {onoff(s['session_america'])}")
    kb.add(f"🎯 Тейк {onoff(s['take_enabled'])}", f"⚡ Автоторговля {onoff(s['autotrade'])}")
    kb.add(f"📨 {'all signal' if s['signal_output_mode']=='all' else 'one signal'}", "📊 Профит")
    kb.add("🔑 API ключи", "⛔ Закрыть всё")
    kb.add("🏓 Ping", "♻️ Сброс")
    kb.add("🗑 delete all")
    return kb


def settings_text() -> str:
    s = load_state()
    return (
        f"⚙️ <b>Настройки v{VERSION}</b>\n"
        f"Биржа: <b>{esc(s['exchange'])}</b>\n"
        f"Монет: <b>{len(s['symbols'])}</b>\n"
        f"TF: <b>{esc(s['lower_tf'])}</b> / <b>{esc(s['higher_tf'])}</b>\n"
        f"Режим сигналов: <b>{'all signal — кратко одним сообщением' if s['signal_output_mode']=='all' else 'one signal — подробно отдельными сообщениями'}</b>\n"
        f"Автосигналы: <b>{'ВКЛ' if s['auto_signals'] else 'ВЫКЛ'}</b>\n"
        f"Автоторговля: <b>{'ВКЛ' if s['autotrade'] else 'ВЫКЛ'}</b> ({'LIVE' if LIVE_TRADING_ENABLED else 'PAPER'})\n"
        f"Улучшения: <b>{'ВКЛ' if s['adaptive_improvement'] else 'ВЫКЛ'}</b>\n"
        f"Новости: <b>{'ВКЛ' if s['news_enabled'] else 'ВЫКЛ'}</b>\n"
        f"Тейк: <b>{'ВКЛ' if s['take_enabled'] else 'ВЫКЛ'}</b>, user range {s['take_min_profit_pct']}% / {s['take_max_profit_pct']}%, TF auto: {'ВКЛ' if s.get('take_auto_by_tf') else 'ВЫКЛ'}\n"
        f"Анализ: <b>{esc(s['analysis_mode'])}</b>, профиль: <b>{esc(s['strategy_profile'])}</b>\n"
        f"Сделок/сутки: <b>{s['daily_trades_limit']}</b>, сегодня: <b>{s['daily_trades_count']}</b>\n"
        f"Азия 03:00 МСК: <b>{'ВКЛ' if s['session_asia'] else 'ВЫКЛ'}</b>\n"
        f"Америка 16:30 МСК: <b>{'ВКЛ' if s['session_america'] else 'ВЫКЛ'}</b>\n"
        f"Админ: <b>{esc(s.get('admin_id') or 'не назначен')}</b>\n"
        f"API: <b>{esc(api_status_short())}</b>\n\n"
        "Команды:\n"
        "<code>signal btc</code> / <code>signal btc/usdt</code>\n"
        "<code>mexc top-100</code> / <code>bingx top-200</code> / <code>top-50</code>\n"
        "<code>new sol</code> / <code>delete sol</code> / <code>delete all</code>\n"
        "<code>tf 15m 4h</code> / <code>take 0.5 4</code> / <code>trades 10</code>\n"
        "<code>all signal</code> / <code>one signal</code>\n"
        "<code>auto on</code> / <code>auto off</code>\n"
        "<code>api mexc</code> / <code>api bingx</code> / <code>api status</code>"
    )


def make_exchange(name: Optional[str] = None, private: bool = False):
    s = load_state()
    exchange_name = (name or s["exchange"]).lower()
    if exchange_name not in {"mexc", "bingx"}:
        raise ValueError("Поддерживаются mexc или bingx")
    cls = getattr(ccxt, exchange_name)
    cfg: Dict[str, Any] = {
        "enableRateLimit": True,
        "timeout": 20000,
        "options": {"defaultType": "swap"},
    }
    if private:
        key, secret = get_api_credentials(exchange_name)
        if not key or not secret:
            raise RuntimeError(f"{exchange_name.upper()} API ключи не заданы. Используй: api {exchange_name}")
        cfg.update({"apiKey": key, "secret": secret})
    return cls(cfg)


def normalize_symbol(raw: str) -> str:
    raw = raw.strip().upper().replace("-", "/").replace("_", "/")
    raw = raw.replace(" ", "")
    if raw.endswith(":USDT") and "/" in raw:
        return raw
    if "/" not in raw:
        raw = raw + "/USDT"
    if raw.endswith("/USDT") and ":USDT" not in raw:
        raw += ":USDT"
    return raw


def base_from_symbol(symbol: str) -> str:
    return normalize_symbol(symbol).split("/")[0]


def resolve_symbol(exchange, symbol: str) -> str:
    target = normalize_symbol(symbol)
    markets = exchange.load_markets()
    if target in markets:
        return target
    base = target.split("/")[0]
    for sym, m in markets.items():
        if m.get("base", "").upper() == base and m.get("quote", "").upper() == "USDT" and (m.get("swap") or m.get("future") or m.get("contract")):
            return sym
    spot = f"{base}/USDT"
    if spot in markets:
        return spot
    raise ValueError(f"Символ не найден: {symbol}")


def fetch_top_symbols(exchange_name: str, limit: int) -> List[str]:
    ex = make_exchange(exchange_name)
    markets = ex.load_markets()
    tickers = ex.fetch_tickers()
    rows: List[Tuple[float, str]] = []
    for sym, m in markets.items():
        if not (m.get("swap") or m.get("future") or m.get("contract")):
            continue
        if m.get("quote", "").upper() != "USDT" or not m.get("active", True):
            continue
        t = tickers.get(sym) or {}
        vol = t.get("quoteVolume")
        if vol is None:
            vol = float(t.get("baseVolume") or 0) * float(t.get("last") or t.get("close") or 0)
        try:
            vol = float(vol or 0)
        except Exception:
            vol = 0.0
        rows.append((vol, sym))
    rows.sort(reverse=True, key=lambda x: x[0])
    return [sym for _, sym in rows[:limit]]


def ema(v: np.ndarray, period: int) -> np.ndarray:
    if len(v) == 0:
        return v
    alpha = 2 / (period + 1)
    out = np.empty_like(v, dtype=float)
    out[0] = v[0]
    for i in range(1, len(v)):
        out[i] = alpha * v[i] + (1 - alpha) * out[i - 1]
    return out


def rsi(v: np.ndarray, period: int = 14) -> np.ndarray:
    if len(v) < period + 1:
        return np.full_like(v, 50.0, dtype=float)
    d = np.diff(v)
    out = np.full_like(v, 50.0, dtype=float)
    up = max(d[:period].mean(), 0)
    down = max(-d[:period].mean(), 0)
    for i in range(period + 1, len(v)):
        delta = d[i - 1]
        up = (up * (period - 1) + max(delta, 0)) / period
        down = (down * (period - 1) + max(-delta, 0)) / period
        out[i] = 100 if down == 0 else 100 - 100 / (1 + up / down)
    return out


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    tr = [high[0] - low[0]]
    for i in range(1, len(close)):
        tr.append(max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))
    return ema(np.array(tr, dtype=float), period)


def macd(v: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    line = ema(v, 12) - ema(v, 26)
    sig = ema(line, 9)
    return line, sig, line - sig


def linreg_line(v: np.ndarray, n: int = 60) -> Tuple[float, float, float]:
    sample = v[-min(n, len(v)):]
    x = np.arange(len(sample))
    if len(sample) < 2:
        return 0.0, float(v[-1]), float(v[-1])
    slope, intercept = np.polyfit(x, sample, 1)
    return float(slope), float(intercept), float(slope * (len(sample) - 1) + intercept)


def fetch_ohlcv(exchange, symbol: str, timeframe: str, limit: int = 260) -> Dict[str, np.ndarray]:
    data = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    if len(data) < 60:
        raise RuntimeError(f"Недостаточно свечей: {symbol} {timeframe}")
    arr = np.array(data, dtype=float)
    return {"ts": arr[:, 0], "open": arr[:, 1], "high": arr[:, 2], "low": arr[:, 3], "close": arr[:, 4], "volume": arr[:, 5]}


def features(o: Dict[str, np.ndarray]) -> Dict[str, float]:
    close = o["close"]
    high = o["high"]
    low = o["low"]
    vol = o["volume"]
    macd_line, macd_sig, macd_hist = macd(close)
    slope, start, end = linreg_line(close, 60)
    look = min(100, len(close))
    atr14 = atr(high, low, close, 14)
    ema20 = ema(close, 20)
    ema50 = ema(close, 50)
    ema200 = ema(close, min(200, len(close) - 1)) if len(close) > 20 else ema(close, 20)
    return {
        "price": float(close[-1]),
        "ema20": float(ema20[-1]),
        "ema50": float(ema50[-1]),
        "ema200": float(ema200[-1]),
        "rsi": float(rsi(close, 14)[-1]),
        "atr": float(atr14[-1]),
        "atr_pct": float(atr14[-1] / close[-1] * 100) if close[-1] else 0.0,
        "macd_hist": float(macd_hist[-1]),
        "support": float(np.min(low[-look:])),
        "resistance": float(np.max(high[-look:])),
        "slope": float(slope),
        "trend_start": start,
        "trend_end": end,
        "vol": float(vol[-1]),
        "vol_sma": float(np.mean(vol[-30:])),
    }


def fetch_crypto_news(limit: int = 8) -> List[str]:
    """Русскоязычные источники + fallback. Без API-ключей."""
    urls = [
        "https://forklog.com/feed",
        "https://bits.media/rss/",
        "https://cointelegraph.com/rss/tag/bitcoin",
    ]
    out: List[str] = []
    for url in urls:
        try:
            r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                continue
            titles = re.findall(r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>", r.text, re.I | re.S)
            for a, b in titles:
                t = re.sub(r"\s+", " ", (a or b).strip())
                t = re.sub(r"<.*?>", "", t)
                if t and not any(x.lower() == t.lower() for x in out) and "RSS" not in t:
                    out.append(t)
        except Exception:
            pass
    return out[:limit]


def news_bias(headlines: List[str]) -> int:
    text = " ".join(headlines).lower()
    bull = ["рост", "одобр", "etf", "bull", "rally", "surge", "adoption", "снижен", "trump", "musk", "маск", "трамп", "институцион"]
    bear = ["взлом", "иск", "ban", "crackdown", "selloff", "ликвидац", "exploit", "rate hike", "штраф", "паден"]
    return max(-6, min(6, sum(text.count(w) for w in bull) - sum(text.count(w) for w in bear)))


def profile_weights(profile: str) -> Dict[str, float]:
    table = {
        "trend": {"trend": 1.55, "momentum": 1.05, "mean": 0.60, "breakout": 0.90, "volume": 1.00},
        "momentum": {"trend": 1.10, "momentum": 1.60, "mean": 0.55, "breakout": 1.20, "volume": 1.20},
        "breakout": {"trend": 1.10, "momentum": 1.25, "mean": 0.40, "breakout": 1.80, "volume": 1.45},
        "mean_reversion": {"trend": 0.70, "momentum": 0.70, "mean": 1.85, "breakout": 0.45, "volume": 0.80},
        "max_profit": {"trend": 1.30, "momentum": 1.40, "mean": 0.40, "breakout": 1.70, "volume": 1.30},
    }
    return table.get(profile, table["trend"])


def score_profile(lf: Dict[str, float], hf: Dict[str, float], profile: str, nb: int = 0) -> Tuple[float, float, List[str]]:
    w = profile_weights(profile)
    long = short = 0.0
    reasons: List[str] = []

    def L(points: float, reason: str) -> None:
        nonlocal long
        long += points
        reasons.append("🟢 " + reason)

    def S(points: float, reason: str) -> None:
        nonlocal short
        short += points
        reasons.append("🔴 " + reason)

    L(18 * w["trend"], "HTF EMA20>EMA50") if hf["ema20"] > hf["ema50"] else S(18 * w["trend"], "HTF EMA20<EMA50")
    L(12 * w["trend"], "Цена выше EMA200 старший TF") if hf["price"] > hf["ema200"] else S(12 * w["trend"], "Цена ниже EMA200 старший TF")
    L(12 * w["trend"], "LTF EMA20>EMA50") if lf["ema20"] > lf["ema50"] else S(12 * w["trend"], "LTF EMA20<EMA50")
    L(11 * w["momentum"], "MACD положительный") if lf["macd_hist"] > 0 else S(11 * w["momentum"], "MACD отрицательный")

    if lf["rsi"] < 32:
        L(11 * w["mean"], f"RSI перепроданность {lf['rsi']:.1f}")
    elif lf["rsi"] > 68:
        S(11 * w["mean"], f"RSI перекупленность {lf['rsi']:.1f}")
    elif lf["rsi"] > 55:
        L(6 * w["momentum"], f"RSI импульс {lf['rsi']:.1f}")
    elif lf["rsi"] < 45:
        S(6 * w["momentum"], f"RSI слабость {lf['rsi']:.1f}")

    if lf["price"] > lf["resistance"] - 0.35 * lf["atr"]:
        L(9 * w["breakout"], "Цена у сопротивления/пробой вверх")
    if lf["price"] < lf["support"] + 0.35 * lf["atr"]:
        S(9 * w["breakout"], "Цена у поддержки/пробой вниз")
    L(7 * w["trend"], "Наклонный уровень вверх") if lf["slope"] > 0 else S(7 * w["trend"], "Наклонный уровень вниз")

    if lf["vol"] > lf["vol_sma"] * 1.25:
        L(5 * w["volume"], "Объём выше среднего вверх") if lf["price"] > lf["ema20"] else S(5 * w["volume"], "Объём выше среднего вниз")
    if nb > 0:
        L(abs(nb), "Новостной фон позитивный")
    elif nb < 0:
        S(abs(nb), "Новостной фон негативный")
    return long, short, reasons


def simple_past_score(o: Dict[str, np.ndarray], profile: str) -> float:
    """Мини-бэктест по последним свечам: профиль получает балл за правильное направление будущего движения."""
    close = o["close"]
    high = o["high"]
    low = o["low"]
    if len(close) < 90:
        return 0.0
    wins = total = 0
    step = max(3, len(close) // 80)
    horizon = 8
    for i in range(60, len(close) - horizon, step):
        sub = {"open": o["open"][:i], "high": high[:i], "low": low[:i], "close": close[:i], "volume": o["volume"][:i]}
        lf = features(sub)
        # для мини-теста HTF заменяем тем же срезом, это быстрый выбор профиля, не полноценный бэктест
        long, short, _ = score_profile(lf, lf, profile, 0)
        direction = 1 if long >= short else -1
        future = (close[i + horizon] - close[i]) / close[i]
        if (future > 0 and direction > 0) or (future < 0 and direction < 0):
            wins += 1
        total += 1
    return wins / max(total, 1)


def choose_profile(mode: str, lo: Dict[str, np.ndarray], hi: Dict[str, np.ndarray]) -> str:
    s = load_state()
    if mode == "max_profit":
        return "max_profit"
    scores = {p: simple_past_score(lo, p) * 0.65 + simple_past_score(hi, p) * 0.35 for p in INTERNAL_PROFILES}
    if mode == "best":
        return max(scores, key=scores.get)
    if mode == "auto_ai":
        # если последние paper-сделки в минусе — усиливаем профиль, который лучше прошёл мини-бэктест
        closed = [t for t in load_trades() if t.get("status") == "closed" and "pnl_pct" in t][-30:]
        avg = sum(float(t.get("pnl_pct", 0)) for t in closed) / len(closed) if closed else 0
        if avg < 0 or s.get("adaptive_improvement"):
            best = max(scores, key=scores.get)
            s["strategy_profile"] = best
            save_state(s)
            return best
        return s.get("strategy_profile", "trend") if s.get("strategy_profile") in INTERNAL_PROFILES else max(scores, key=scores.get)
    return "multi"


def compute_take_range(s: Dict[str, Any], higher_tf: str, atr_pct: float, mode: str) -> Tuple[float, float]:
    # проценты, чем выше TF — тем шире диапазон тейков
    base = {
        "15m": (0.25, 1.2),
        "1h": (0.40, 2.0),
        "4h": (0.80, 4.0),
        "1d": (1.50, 7.0),
        "1w": (3.00, 14.0),
    }.get(higher_tf, (0.40, 2.0))
    user_min = float(s.get("take_min_profit_pct", 0.3))
    user_max = float(s.get("take_max_profit_pct", 3.0))
    if not s.get("take_auto_by_tf", True):
        mn, mx = user_min, user_max
    else:
        mn = max(base[0], user_min, atr_pct * 0.8)
        mx = max(base[1], user_max, atr_pct * 3.0)
    if mode == "max_profit":
        mx *= 1.35
    if mx <= mn:
        mx = mn * 2
    return mn, mx


def build_entry_plan(direction: str, lf: Dict[str, float], s: Dict[str, Any], mode: str) -> Tuple[float, float, List[float]]:
    price = lf["price"]
    av = max(lf["atr"], price * 0.002)
    mn_pct, mx_pct = compute_take_range(s, s.get("higher_tf", "1h"), lf.get("atr_pct", 0.3), mode)
    mid_pct = (mn_pct + mx_pct) / 2
    if direction == "SHORT":
        # выгодный вход выше текущей цены, а не market-price
        entry = max(price * 1.001, min(lf["resistance"] - av * 0.15, price + av * 0.35))
        if entry <= price:
            entry = price + av * 0.25
        stop = max(entry * (1 + max(0.25, mn_pct * 0.45) / 100), lf["resistance"] + av * 0.25)
        tp = [entry * (1 - mn_pct / 100), entry * (1 - mid_pct / 100), entry * (1 - mx_pct / 100)]
        tp = sorted(tp, reverse=True)  # TP1 ближе, TP3 дальше для SHORT
    elif direction == "LONG":
        # выгодный вход ниже текущей цены, а не market-price
        entry = min(price * 0.999, max(lf["support"] + av * 0.15, price - av * 0.35, lf["ema20"] - av * 0.05))
        if entry >= price:
            entry = price - av * 0.25
        stop = min(entry * (1 - max(0.25, mn_pct * 0.45) / 100), lf["support"] - av * 0.25)
        tp = [entry * (1 + mn_pct / 100), entry * (1 + mid_pct / 100), entry * (1 + mx_pct / 100)]
        tp = sorted(tp)  # TP1 ближе, TP3 дальше для LONG
    else:
        entry = price
        stop = price - av
        tp = [price + av, price + 2 * av, price + 3 * av]
    return float(entry), float(stop), [float(x) for x in tp]


def build_signal(symbol: str, with_chart: bool = True) -> Dict[str, Any]:
    s = load_state()
    ex = make_exchange(s["exchange"])
    symbol = resolve_symbol(ex, symbol)
    lo = fetch_ohlcv(ex, symbol, s["lower_tf"])
    hi = fetch_ohlcv(ex, symbol, s["higher_tf"])
    lf = features(lo)
    hf = features(hi)
    headlines: List[str] = []
    nb = 0
    if s.get("news_enabled"):
        headlines = fetch_crypto_news(6)
        nb = news_bias(headlines)

    mode = s.get("analysis_mode", "multi")
    chosen_profile = choose_profile(mode, lo, hi)
    if chosen_profile == "multi":
        scores = [score_profile(lf, hf, p, nb) for p in INTERNAL_PROFILES]
        long = sum(x[0] for x in scores) / len(scores)
        short = sum(x[1] for x in scores) / len(scores)
        reasons = []
        for _, _, rs in scores:
            for r in rs:
                if r not in reasons:
                    reasons.append(r)
        profile = "multi(avg)"
    else:
        long, short, reasons = score_profile(lf, hf, chosen_profile, nb)
        profile = chosen_profile

    total = max(long + short, 1)
    long_pct = round(long / total * 100, 1)
    short_pct = round(short / total * 100, 1)
    direction = "LONG" if long_pct >= 55 else "SHORT" if short_pct >= 55 else "NEUTRAL"
    entry, stop, tp = build_entry_plan(direction, lf, s, mode)
    chart = draw_chart(symbol, lo, direction, long_pct, short_pct, entry, stop, tp) if with_chart else None
    return {
        "symbol": symbol,
        "exchange": s["exchange"],
        "direction": direction,
        "long_pct": long_pct,
        "short_pct": short_pct,
        "entry": entry,
        "stop": stop,
        "tp": tp,
        "price": lf["price"],
        "rsi": lf["rsi"],
        "atr": lf["atr"],
        "atr_pct": lf["atr_pct"],
        "support": lf["support"],
        "resistance": lf["resistance"],
        "profile": profile,
        "mode": mode,
        "lower_tf": s["lower_tf"],
        "higher_tf": s["higher_tf"],
        "reasons": reasons[:10],
        "headlines": headlines[:5],
        "chart_path": chart,
    }


def draw_candles(ax, o: Dict[str, np.ndarray]) -> None:
    open_ = o["open"][-140:]
    high = o["high"][-140:]
    low = o["low"][-140:]
    close = o["close"][-140:]
    x = np.arange(len(close))
    width = 0.58
    for i in range(len(close)):
        up = close[i] >= open_[i]
        color = "#18a058" if up else "#d03050"
        ax.vlines(x[i], low[i], high[i], color=color, linewidth=1.0, alpha=0.9)
        bottom = min(open_[i], close[i])
        height = abs(close[i] - open_[i])
        if height == 0:
            height = max((high[i] - low[i]) * 0.02, close[i] * 0.00005)
        ax.add_patch(Rectangle((x[i] - width / 2, bottom), width, height, facecolor=color, edgecolor=color, alpha=0.85))


def draw_chart(symbol: str, o: Dict[str, np.ndarray], direction: str, long_pct: float, short_pct: float, entry: float, stop: float, tp: List[float]) -> str:
    close = o["close"][-140:]
    x = np.arange(len(close))
    e20 = ema(close, 20)
    e50 = ema(close, 50)
    slope, st, en = linreg_line(close, 60)
    n = min(60, len(close))
    tx = np.arange(len(close) - n, len(close))
    trend = np.linspace(st, en, n)

    fig, ax = plt.subplots(figsize=(16, 9), dpi=160)
    draw_candles(ax, {k: v[-140:] for k, v in o.items() if k in {"open", "high", "low", "close", "volume"}})
    ax.plot(x, e20, label="EMA20", linewidth=1.5)
    ax.plot(x, e50, label="EMA50", linewidth=1.5)
    ax.plot(tx, trend, linestyle="--", label="Наклонный уровень", linewidth=1.6)
    ax.axhline(entry, label=f"Entry {entry:.6g}", linewidth=1.4)
    ax.axhline(stop, linestyle="--", label=f"SL {stop:.6g}", linewidth=1.3)
    for i, t in enumerate(tp, 1):
        ax.axhline(t, linestyle=":", label=f"TP{i} {t:.6g}", linewidth=1.3)
    ax.set_title(f"{symbol} | {direction} | LONG {long_pct}% / SHORT {short_pct}%", fontsize=14)
    ax.grid(True, alpha=0.22)
    ax.legend(fontsize=9, loc="best")
    ax.set_xlim(-2, len(close) + 2)
    fig.tight_layout()
    p = CHART_DIR / (re.sub(r"[^A-Za-z0-9_.-]+", "_", symbol) + f"_{int(time.time())}.png")
    fig.savefig(p)
    plt.close(fig)
    return str(p)


def format_signal(sig: Dict[str, Any]) -> str:
    tp = sig["tp"]
    reasons = "\n".join("• " + esc(r) for r in sig["reasons"])
    news = ""
    if sig.get("headlines"):
        news = "\n\n📰 <b>Новости:</b>\n" + "\n".join("• " + esc(h) for h in sig["headlines"][:4])
    return (
        f"📡 <b>Signal {esc(sig['symbol'])}</b>\n"
        f"Биржа: <b>{esc(sig['exchange'])}</b> | TF: <b>{esc(sig['lower_tf'])}/{esc(sig['higher_tf'])}</b>\n"
        f"Направление: <b>{sig['direction']}</b>\n"
        f"Long/Short: <b>{sig['long_pct']}%</b> / <b>{sig['short_pct']}%</b>\n\n"
        f"💵 Цена: <code>{sig['price']:.8g}</code>\n"
        f"🎯 Выгодный Entry: <code>{sig['entry']:.8g}</code>\n"
        f"🛑 Stop-loss: <code>{sig['stop']:.8g}</code>\n"
        f"✅ TP1: <code>{tp[0]:.8g}</code>\n"
        f"✅ TP2: <code>{tp[1]:.8g}</code>\n"
        f"✅ TP3: <code>{tp[2]:.8g}</code>\n\n"
        f"RSI: <b>{sig['rsi']:.1f}</b> | ATR: <b>{sig['atr']:.8g}</b> ({sig['atr_pct']:.2f}%)\n"
        f"Support/Resistance: <code>{sig['support']:.8g}</code> / <code>{sig['resistance']:.8g}</code>\n"
        f"Режим: <b>{esc(sig['mode'])}</b>, профиль: <b>{esc(sig['profile'])}</b>\n\n"
        f"🧩 <b>Факторы:</b>\n{reasons}{news}\n\n"
        "⚠️ Не финсовет. Риск обязателен."
    )


def format_signal_brief(sig: Dict[str, Any]) -> str:
    tp = sig["tp"]
    base = base_from_symbol(sig["symbol"])
    emoji = "🟢" if sig["direction"] == "LONG" else "🔴" if sig["direction"] == "SHORT" else "⚪"
    return (
        f"{emoji} <b>{esc(base)}</b> {sig['direction']} "
        f"L/S {sig['long_pct']}/{sig['short_pct']} | "
        f"Entry <code>{sig['entry']:.6g}</code> | SL <code>{sig['stop']:.6g}</code> | "
        f"TP <code>{tp[0]:.6g}</code>/<code>{tp[1]:.6g}</code>/<code>{tp[2]:.6g}</code>"
    )


def today_msk() -> str:
    return datetime.utcfromtimestamp(time.time() + MSK_OFFSET * 3600).strftime("%Y-%m-%d")


def can_open_trade() -> bool:
    s = load_state()
    day = today_msk()
    if s.get("daily_trades_date") != day:
        s["daily_trades_date"] = day
        s["daily_trades_count"] = 0
        save_state(s)
    return int(s.get("daily_trades_count", 0)) < int(s.get("daily_trades_limit", 5))


def mark_trade_used() -> None:
    s = load_state()
    day = today_msk()
    if s.get("daily_trades_date") != day:
        s["daily_trades_date"] = day
        s["daily_trades_count"] = 0
    s["daily_trades_count"] = int(s.get("daily_trades_count", 0)) + 1
    save_state(s)


def execute_trade_if_allowed(chat_id: int, sig: Dict[str, Any]) -> None:
    s = load_state()
    if not s.get("autotrade") or sig["direction"] == "NEUTRAL" or max(sig["long_pct"], sig["short_pct"]) < 62:
        return
    if not can_open_trade():
        bot.send_message(chat_id, "💼 Лимит сделок на сутки исчерпан.", reply_markup=main_keyboard())
        return
    side = "buy" if sig["direction"] == "LONG" else "sell"
    amount = DEFAULT_NOTIONAL_USDT / sig["entry"]
    if not LIVE_TRADING_ENABLED:
        tr = load_trades()
        tr.append({
            "ts": time.time(),
            "status": "open",
            "mode": "paper",
            "exchange": sig["exchange"],
            "symbol": sig["symbol"],
            "direction": sig["direction"],
            "entry": sig["entry"],
            "stop": sig["stop"],
            "tp": sig["tp"],
            "notional_usdt": DEFAULT_NOTIONAL_USDT,
        })
        save_trades(tr)
        mark_trade_used()
        bot.send_message(chat_id, f"🧾 PAPER trade: {esc(sig['symbol'])} {sig['direction']} ~{DEFAULT_NOTIONAL_USDT} USDT.", reply_markup=main_keyboard())
        return
    ex = make_exchange(sig["exchange"], private=True)
    order = ex.create_order(sig["symbol"], "market", side, amount)
    mark_trade_used()
    bot.send_message(chat_id, f"⚡ LIVE order:\n<code>{esc(order)}</code>", reply_markup=main_keyboard())


def within_enabled_sessions() -> bool:
    s = load_state()
    if not s.get("session_asia") and not s.get("session_america"):
        return True
    now = datetime.utcfromtimestamp(time.time() + MSK_OFFSET * 3600)
    minutes = now.hour * 60 + now.minute
    window = 4 * 60
    asia_start = 3 * 60
    america_start = 16 * 60 + 30
    return bool((s.get("session_asia") and asia_start <= minutes <= asia_start + window) or (s.get("session_america") and america_start <= minutes <= america_start + window))


def open_trade_pnl(trade: Dict[str, Any], price: float) -> float:
    entry = float(trade.get("entry") or 0)
    if not entry:
        return 0.0
    if trade.get("direction") == "SHORT":
        return (entry - price) / entry * 100
    return (price - entry) / entry * 100


def close_all_trades(chat_id: int) -> None:
    trades = load_trades()
    open_trades = [t for t in trades if t.get("status", "open") == "open"]
    if not open_trades:
        bot.send_message(chat_id, "⛔ Открытых сделок нет.", reply_markup=main_keyboard())
        return
    s = load_state()
    ex = make_exchange(s["exchange"])
    closed = 0
    for t in trades:
        if t.get("status", "open") != "open":
            continue
        try:
            ticker = ex.fetch_ticker(t["symbol"])
            price = float(ticker.get("last") or ticker.get("close") or t.get("entry"))
        except Exception:
            price = float(t.get("entry", 0) or 0)
        t["status"] = "closed"
        t["closed_ts"] = time.time()
        t["exit"] = price
        t["pnl_pct"] = open_trade_pnl(t, price)
        closed += 1
    save_trades(trades)
    bot.send_message(chat_id, f"⛔ Закрыто PAPER-сделок: <b>{closed}</b>.\nLIVE-закрытие намеренно не делается без отдельного подтверждения через биржу.", reply_markup=main_keyboard())


def profit_text() -> str:
    trades = load_trades()
    if not trades:
        return "📊 Сделок пока нет."
    open_trades = [t for t in trades if t.get("status", "open") == "open"]
    closed = [t for t in trades if t.get("status") == "closed"]
    total = sum(float(t.get("pnl_pct", 0)) for t in closed)
    wins = sum(1 for t in closed if float(t.get("pnl_pct", 0)) > 0)
    lines = [
        "📊 <b>Профит / сделки</b>",
        f"Открытые: <b>{len(open_trades)}</b>",
        f"Закрытые: <b>{len(closed)}</b>",
        f"PNL закрытых: <b>{total:.2f}%</b>",
        f"Winrate: <b>{(wins/max(len(closed),1)*100):.1f}%</b>",
        "",
        "<b>Последние сделки:</b>",
    ]
    for t in trades[-15:]:
        status = t.get("status", "open")
        pnl = t.get("pnl_pct")
        pnl_s = "" if pnl is None else f" | PNL {float(pnl):.2f}%"
        lines.append(f"• {esc(t.get('symbol'))} {esc(t.get('direction'))} {status} | entry {float(t.get('entry',0)):.6g}{pnl_s}")
    return "\n".join(lines)


def memory_text() -> str:
    if psutil is None:
        return "Memory: psutil не установлен"
    proc = psutil.Process(os.getpid())
    rss = proc.memory_info().rss / 1024 / 1024
    try:
        vm = psutil.virtual_memory()
        return f"Memory: <b>{rss:.1f} MB</b> RSS | system used {vm.percent}%"
    except Exception:
        return f"Memory: <b>{rss:.1f} MB</b> RSS"


def send_text_chunks(chat_id: int, header: str, lines: List[str]) -> None:
    buf = header
    for line in lines:
        if len(buf) + len(line) + 1 > 3500:
            bot.send_message(chat_id, buf, reply_markup=main_keyboard())
            buf = ""
        buf += line + "\n"
    if buf.strip():
        bot.send_message(chat_id, buf, reply_markup=main_keyboard())


def send_signal(chat_id: int, symbol: str) -> None:
    bot.send_message(chat_id, f"⏳ Анализирую {esc(symbol)}...", reply_markup=main_keyboard())
    sig = build_signal(symbol, True)
    caption = format_signal(sig)
    p = sig.get("chart_path")
    if p and Path(p).exists():
        with open(p, "rb") as f:
            bot.send_photo(chat_id, f, caption=caption, reply_markup=main_keyboard())
    else:
        bot.send_message(chat_id, caption, reply_markup=main_keyboard())
    execute_trade_if_allowed(chat_id, sig)


def send_all_signals(chat_id: int) -> None:
    s = load_state()
    symbols = list(s.get("symbols", []))[:MAX_SIGNAL_SCAN]
    if not symbols:
        bot.send_message(chat_id, "Список монет пуст. Нажми MEXC top+ / BINGX top+ или new BTC.", reply_markup=main_keyboard())
        return
    if s.get("signal_output_mode") == "one":
        bot.send_message(chat_id, f"⏳ Подробный анализ {len(symbols)} монет. Каждая монета отдельным сообщением.", reply_markup=main_keyboard())
        for idx, sym in enumerate(symbols, 1):
            try:
                with_chart = idx <= MAX_ONE_SIGNAL_CHARTS
                sig = build_signal(sym, with_chart)
                if with_chart and sig.get("chart_path") and Path(sig["chart_path"]).exists():
                    with open(sig["chart_path"], "rb") as f:
                        bot.send_photo(chat_id, f, caption=format_signal(sig), reply_markup=main_keyboard())
                else:
                    bot.send_message(chat_id, format_signal(sig), reply_markup=main_keyboard())
                execute_trade_if_allowed(chat_id, sig)
                time.sleep(0.7)
            except Exception as e:
                bot.send_message(chat_id, f"❌ {esc(sym)}: {esc(str(e)[:250])}", reply_markup=main_keyboard())
        return

    bot.send_message(chat_id, f"⏳ Краткий анализ {len(symbols)} монет одним/несколькими сообщениями...", reply_markup=main_keyboard())
    lines: List[str] = []
    for sym in symbols:
        try:
            sig = build_signal(sym, with_chart=False)
            lines.append(format_signal_brief(sig))
            execute_trade_if_allowed(chat_id, sig)
        except Exception as e:
            lines.append(f"❌ <b>{esc(base_from_symbol(sym))}</b>: {esc(str(e)[:160])}")
    header = f"📡 <b>ALL SIGNAL</b> | {esc(s['exchange']).upper()} | TF {esc(s['lower_tf'])}/{esc(s['higher_tf'])} | монет: {len(symbols)}\n\n"
    send_text_chunks(chat_id, header, lines)


def load_top(chat_id: int, exchange_name: str, n: int) -> None:
    n = max(1, min(1000, int(n)))
    bot.send_message(chat_id, f"⏳ Загружаю top-{n} {exchange_name.upper()} futures...", reply_markup=main_keyboard())
    symbols = fetch_top_symbols(exchange_name, n)
    s = load_state()
    s["exchange"] = exchange_name
    s["last_top_exchange"] = exchange_name
    s["symbols"] = symbols  # если MEXC загружен — BINGX список удаляется, и наоборот
    save_state(s)
    bot.send_message(chat_id, f"✅ {exchange_name.upper()} top-{n}: загружено {len(symbols)} монет.\nПервые 10:\n" + "\n".join(symbols[:10]), reply_markup=main_keyboard())


def signal_loop() -> None:
    while True:
        try:
            s = load_state()
            auto_chats = [x.strip() for x in os.getenv("AUTO_SIGNAL_CHAT_IDS", "").split(",") if x.strip()]
            if not auto_chats and s.get("admin_id"):
                auto_chats = [str(s["admin_id"])]
            if s.get("auto_signals") and within_enabled_sessions() and auto_chats:
                symbols = list(s.get("symbols", []))[:MAX_SIGNAL_SCAN]
                for chat_id in auto_chats:
                    if s.get("signal_output_mode") == "all":
                        lines: List[str] = []
                        for symbol in symbols:
                            try:
                                sig = build_signal(symbol, with_chart=False)
                                if sig["direction"] != "NEUTRAL" and max(sig["long_pct"], sig["short_pct"]) >= 60:
                                    lines.append(format_signal_brief(sig))
                                    execute_trade_if_allowed(int(chat_id), sig)
                            except Exception as e:
                                print("auto signal error", symbol, e, flush=True)
                        if lines:
                            send_text_chunks(int(chat_id), "📡 <b>AUTO ALL SIGNAL</b>\n\n", lines)
                    else:
                        for symbol in symbols:
                            try:
                                sig = build_signal(symbol, with_chart=False)
                                if sig["direction"] != "NEUTRAL" and max(sig["long_pct"], sig["short_pct"]) >= 65:
                                    bot.send_message(int(chat_id), format_signal(sig), reply_markup=main_keyboard())
                                    execute_trade_if_allowed(int(chat_id), sig)
                            except Exception as e:
                                print("auto signal error", symbol, e, flush=True)
            time.sleep(SIGNAL_LOOP_SECONDS)
        except Exception as e:
            print("signal_loop error", e, flush=True)
            time.sleep(30)


@bot.message_handler(commands=["start", "help"])
def start(message):
    ok, admin_msg = ensure_admin_claim(message.chat.id)
    text = (
        f"{admin_msg}\n\n"
        f"👋 <b>Crypto Futures Signal Bot v{VERSION}</b>\n\n"
        "Кнопка 📡 Signal анализирует все загруженные монеты.\n"
        "Команда: <code>signal btc</code> — подробный анализ одной монеты."
    )
    bot.send_message(message.chat.id, text, reply_markup=main_keyboard())


@bot.message_handler(func=lambda m: True)
def handle(message):
    text = (message.text or "").strip()
    low = text.lower().strip()
    s = load_state()
    try:
        pending = api_input_sessions.get(message.chat.id)
        if pending:
            if low in {"cancel", "отмена", "/cancel"}:
                api_input_sessions.pop(message.chat.id, None)
                safe_delete_user_message(message)
                bot.send_message(message.chat.id, "❌ Ввод API ключей отменён.", reply_markup=main_keyboard())
                return
            if not admin_only(message):
                api_input_sessions.pop(message.chat.id, None)
                return
            step = pending.get("step")
            exchange = pending.get("exchange", "mexc")
            if step == "api_key":
                pending["api_key"] = text.strip()
                pending["step"] = "api_secret"
                safe_delete_user_message(message)
                bot.send_message(message.chat.id, f"🔐 {exchange.upper()}: теперь отправь API Secret.", reply_markup=main_keyboard())
                return
            if step == "api_secret":
                api_key = pending.get("api_key", "").strip()
                api_secret = text.strip()
                safe_delete_user_message(message)
                if not api_key or not api_secret:
                    api_input_sessions.pop(message.chat.id, None)
                    bot.send_message(message.chat.id, "❌ Пустой API Key или Secret. Повтори: api mexc / api bingx", reply_markup=main_keyboard())
                    return
                set_api_credentials(exchange, api_key, api_secret)
                api_input_sessions.pop(message.chat.id, None)
                bot.send_message(message.chat.id, f"✅ API ключи для {exchange.upper()} сохранены. Key: <code>{esc(mask_secret(api_key))}</code>", reply_markup=main_keyboard())
                return

        if low not in {"/start", "/help"} and not is_admin(message.chat.id):
            bot.send_message(message.chat.id, "⛔ Сначала админ должен отправить /start. Админом становится первый чат.", reply_markup=main_keyboard())
            return

        # ожидание top-количества после кнопки MEXC/BINGX top+
        if message.chat.id in top_input_sessions:
            mt = re.search(r"top[-\s]?(\d+)|(\d+)", low)
            if mt:
                n = int(mt.group(1) or mt.group(2))
                exname = top_input_sessions.pop(message.chat.id)
                load_top(message.chat.id, exname, n)
                return
            if low in {"cancel", "отмена"}:
                top_input_sessions.pop(message.chat.id, None)
                bot.send_message(message.chat.id, "Отменено.", reply_markup=main_keyboard())
                return

        if low in {"🏓 ping", "ping", "/ping"}:
            st = time.perf_counter()
            bot.get_me()
            ms = int((time.perf_counter() - st) * 1000)
            up = int(time.time() - start_time)
            bot.send_message(message.chat.id, f"🏓 Ping: <b>{ms} ms</b>\n⏱ Uptime: <b>{up//3600}h {(up%3600)//60}m</b>\n{memory_text()}\n🔢 Version: <b>{VERSION}</b>", reply_markup=main_keyboard())
            return

        if low in {"⚙️ настройки", "settings", "/settings"}:
            bot.send_message(message.chat.id, settings_text(), reply_markup=main_keyboard())
            return

        if low in {"🔑 api ключи", "api", "/api"}:
            bot.send_message(message.chat.id, api_status_text(), reply_markup=main_keyboard())
            return
        if low in {"api status", "/api_status"}:
            bot.send_message(message.chat.id, api_status_text(), reply_markup=main_keyboard())
            return
        if low in {"api mexc", "api bingx"}:
            exchange = low.split()[-1]
            api_input_sessions[message.chat.id] = {"exchange": exchange, "step": "api_key"}
            bot.send_message(message.chat.id, f"🔑 {exchange.upper()}: отправь API Key следующим сообщением. Для отмены: <code>cancel</code>\n\n⚠️ Ключ без прав вывода средств.", reply_markup=main_keyboard())
            return
        if low.startswith("api delete "):
            exchange = low.split(maxsplit=2)[2].strip().lower()
            if exchange not in {"mexc", "bingx"}:
                bot.send_message(message.chat.id, "Формат: api delete mexc или api delete bingx", reply_markup=main_keyboard())
                return
            existed = delete_api_credentials(exchange)
            bot.send_message(message.chat.id, f"{'✅ Удалены' if existed else 'ℹ️ Не были сохранены'} ключи {exchange.upper()} из chat-хранилища.", reply_markup=main_keyboard())
            return

        if low in {"♻️ сброс", "reset", "/reset"}:
            reset_state()
            bot.send_message(message.chat.id, "♻️ Настройки сброшены.", reply_markup=main_keyboard())
            return

        if low in {"📡 signal", "signal", "/signal"}:
            send_all_signals(message.chat.id)
            return
        if low.startswith("signal "):
            send_signal(message.chat.id, text.split(maxsplit=1)[1])
            return

        if low in {"📈 mexc top+", "mexc top+"}:
            top_input_sessions[message.chat.id] = "mexc"
            bot.send_message(message.chat.id, "📈 MEXC top+: отправь количество, например <code>top-100</code>, <code>top-250</code> или просто <code>100</code>.", reply_markup=main_keyboard())
            return
        if low in {"📈 bingx top+", "bingx top+"}:
            top_input_sessions[message.chat.id] = "bingx"
            bot.send_message(message.chat.id, "📈 BINGX top+: отправь количество, например <code>top-100</code>, <code>top-250</code> или просто <code>100</code>.", reply_markup=main_keyboard())
            return
        mt = re.match(r"^(mexc|bingx)\s+top[-\s]?(\d+)$", low)
        if mt:
            load_top(message.chat.id, mt.group(1), int(mt.group(2)))
            return
        mt = re.match(r"^top[-\s]?(\d+)$", low)
        if mt:
            load_top(message.chat.id, s.get("last_top_exchange") or s.get("exchange"), int(mt.group(1)))
            return

        if low.startswith("new "):
            sym = normalize_symbol(text.split(maxsplit=1)[1])
            if sym not in s["symbols"]:
                s["symbols"].append(sym)
            save_state(s)
            bot.send_message(message.chat.id, f"Добавлено: {esc(sym)}", reply_markup=main_keyboard())
            return
        if low in {"🗑 delete all", "delete all"}:
            s["symbols"] = []
            save_state(s)
            bot.send_message(message.chat.id, "Список монет очищен.", reply_markup=main_keyboard())
            return
        if low.startswith("delete "):
            sym = normalize_symbol(text.split(maxsplit=1)[1])
            before = len(s["symbols"])
            s["symbols"] = [x for x in s["symbols"] if x.upper() != sym.upper() and base_from_symbol(x) != base_from_symbol(sym)]
            save_state(s)
            bot.send_message(message.chat.id, f"Удалено: {before-len(s['symbols'])}", reply_markup=main_keyboard())
            return

        if low.startswith("exchange ") or low.startswith("🏦 биржа"):
            if low.startswith("exchange "):
                exname = low.split(maxsplit=1)[1].strip()
                if exname not in {"mexc", "bingx"}:
                    bot.send_message(message.chat.id, "Биржа: mexc или bingx", reply_markup=main_keyboard())
                    return
            else:
                exname = "bingx" if s.get("exchange") == "mexc" else "mexc"
            s["exchange"] = exname
            save_state(s)
            bot.send_message(message.chat.id, f"🏦 Биржа изменена: <b>{exname.upper()}</b>", reply_markup=main_keyboard())
            return

        if low.startswith("tf "):
            p = low.split()
            if len(p) != 3 or p[1] not in TF_LIST or p[2] not in TF_LIST:
                bot.send_message(message.chat.id, "Формат: tf 15m 1h. Доступно: 15m, 1h, 4h, 1d, 1w", reply_markup=main_keyboard())
                return
            s["lower_tf"] = p[1]
            s["higher_tf"] = p[2]
            save_state(s)
            bot.send_message(message.chat.id, f"⏱ TF: {p[1]} / {p[2]}", reply_markup=main_keyboard())
            return
        if low.startswith("⏱ tf"):
            cur = (s.get("lower_tf"), s.get("higher_tf"))
            idx = TF_PAIRS.index(cur) if cur in TF_PAIRS else 0
            nxt = TF_PAIRS[(idx + 1) % len(TF_PAIRS)]
            s["lower_tf"], s["higher_tf"] = nxt
            save_state(s)
            bot.send_message(message.chat.id, f"⏱ TF: {nxt[0]} / {nxt[1]}", reply_markup=main_keyboard())
            return

        if low in {"📰 новости", "news", "/news"} or low.startswith("📰 новости"):
            s["news_enabled"] = not s.get("news_enabled", False)
            save_state(s)
            h = fetch_crypto_news(10)
            msg = f"📰 Новости: <b>{'ВКЛ' if s['news_enabled'] else 'ВЫКЛ'}</b>\n"
            msg += "Теперь новостной фон " + ("учитывается" if s["news_enabled"] else "не учитывается") + " в сигналах.\n\n"
            msg += "<b>Последние новости:</b>\n" + ("\n".join("• " + esc(x) for x in h) if h else "Новости не получены.")
            bot.send_message(message.chat.id, msg, reply_markup=main_keyboard())
            return
        if low in {"news on", "news off"}:
            s["news_enabled"] = low.endswith("on")
            save_state(s)
            bot.send_message(message.chat.id, f"Новости: {'ВКЛ' if s['news_enabled'] else 'ВЫКЛ'}", reply_markup=main_keyboard())
            return

        if low in {"auto on", "auto off"}:
            s["auto_signals"] = low.endswith("on")
            save_state(s)
            bot.send_message(message.chat.id, f"Автосигналы: {'ВКЛ' if s['auto_signals'] else 'ВЫКЛ'}", reply_markup=main_keyboard())
            return

        if low.startswith("⚡ автоторговля") or low in {"autotrade"}:
            s["autotrade"] = not s.get("autotrade", False)
            save_state(s)
            key, secret = get_api_credentials(s.get("exchange", "mexc"))
            note = "" if key and secret else "\n⚠️ API ключи текущей биржи не заданы. Для LIVE используй: api mexc / api bingx."
            bot.send_message(message.chat.id, f"Автоторговля: {'ВКЛ' if s['autotrade'] else 'ВЫКЛ'} ({'LIVE' if LIVE_TRADING_ENABLED else 'PAPER'}){note}", reply_markup=main_keyboard())
            return

        if low.startswith("🧠 улучшения") or low in {"improve"}:
            s["adaptive_improvement"] = not s.get("adaptive_improvement", False)
            save_state(s)
            bot.send_message(message.chat.id, f"Улучшения: {'ВКЛ' if s['adaptive_improvement'] else 'ВЫКЛ'}\nЕсли последние paper-сделки в минусе, auto_ai/best будет менять профиль по мини-бэктесту.", reply_markup=main_keyboard())
            return

        if low.startswith("🤖 генератор") or low in {"generator"}:
            cur = s.get("analysis_mode", "multi")
            nxt = ANALYSIS_MODES[(ANALYSIS_MODES.index(cur) + 1) % len(ANALYSIS_MODES)] if cur in ANALYSIS_MODES else "multi"
            s["analysis_mode"] = nxt
            if nxt == "multi":
                s["strategy_profile"] = "multi"
                explain = "усредняет trend/momentum/breakout/mean_reversion"
            elif nxt == "best":
                s["strategy_profile"] = "best_dynamic"
                explain = "выбирает профиль по мини-бэктесту последних свечей"
            elif nxt == "max_profit":
                s["strategy_profile"] = "max_profit"
                explain = "расширяет цели и усиливает breakout/trend"
            else:
                s["strategy_profile"] = "auto_ai"
                explain = "адаптируется по PNL и мини-бэктесту"
            save_state(s)
            bot.send_message(message.chat.id, f"🤖 Генератор анализа: <b>{nxt}</b>\nИзменение реально влияет на веса, выбор профиля, entry/TP и фильтр сделки: {explain}.", reply_markup=main_keyboard())
            return

        if low.startswith("🎯 тейк") or low == "take":
            s["take_enabled"] = not s.get("take_enabled", True)
            save_state(s)
            bot.send_message(message.chat.id, f"Тейк: {'ВКЛ' if s['take_enabled'] else 'ВЫКЛ'}\nДиапазон зависит от старшего TF: 1h < 4h < 1d < 1w. Команда: take 0.5 4", reply_markup=main_keyboard())
            return
        if low.startswith("take "):
            p = low.split()
            if len(p) != 3:
                bot.send_message(message.chat.id, "Формат: take 0.5 4", reply_markup=main_keyboard())
                return
            s["take_min_profit_pct"] = float(p[1])
            s["take_max_profit_pct"] = float(p[2])
            s["take_enabled"] = True
            save_state(s)
            bot.send_message(message.chat.id, "Тейк обновлён.", reply_markup=main_keyboard())
            return

        if low.startswith("💼 сделки") or low == "trades":
            bot.send_message(message.chat.id, f"💼 Лимит сделок: <b>{s['daily_trades_limit']}</b> в сутки.\nФормат: <code>trades 10</code>", reply_markup=main_keyboard())
            return
        if low.startswith("trades "):
            s["daily_trades_limit"] = max(1, min(1000, int(low.split(maxsplit=1)[1])))
            save_state(s)
            bot.send_message(message.chat.id, f"Сделок/сутки: {s['daily_trades_limit']}", reply_markup=main_keyboard())
            return

        if low.startswith("🌏 азия") or low == "asia":
            s["session_asia"] = not s.get("session_asia", False)
            save_state(s)
            bot.send_message(message.chat.id, f"Азия 03:00 МСК: {'ВКЛ' if s['session_asia'] else 'ВЫКЛ'}", reply_markup=main_keyboard())
            return
        if low.startswith("🇺🇸 америка") or low == "america":
            s["session_america"] = not s.get("session_america", False)
            save_state(s)
            bot.send_message(message.chat.id, f"Америка 16:30 МСК: {'ВКЛ' if s['session_america'] else 'ВЫКЛ'}", reply_markup=main_keyboard())
            return

        if low.startswith("📨") or low in {"all signal", "one signal"}:
            if low == "all signal":
                s["signal_output_mode"] = "all"
            elif low == "one signal":
                s["signal_output_mode"] = "one"
            else:
                s["signal_output_mode"] = "one" if s.get("signal_output_mode") == "all" else "all"
            save_state(s)
            bot.send_message(message.chat.id, f"Режим сигналов: <b>{'all signal — кратко одним сообщением' if s['signal_output_mode']=='all' else 'one signal — подробно отдельными сообщениями'}</b>", reply_markup=main_keyboard())
            return

        if low in {"📊 профит", "profit", "/profit"}:
            bot.send_message(message.chat.id, profit_text(), reply_markup=main_keyboard())
            return
        if low in {"⛔ закрыть всё", "close all", "закрыть всё"}:
            close_all_trades(message.chat.id)
            return

        bot.send_message(message.chat.id, "Команда не распознана. Открой /settings.", reply_markup=main_keyboard())
    except Exception as e:
        print(traceback.format_exc(), flush=True)
        bot.send_message(message.chat.id, f"❌ Ошибка: {esc(str(e)[:600])}", reply_markup=main_keyboard())


if __name__ == "__main__":
    print(f"Crypto Futures Signal Bot v{VERSION} starting...", flush=True)
    print(f"DATA_DIR={DATA_DIR}", flush=True)
    threading.Thread(target=signal_loop, daemon=True).start()
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
