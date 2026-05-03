from __future__ import annotations
THRESHOLD_VALUES = [60, 70, 75, 80, 85, 90, 95]

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

VERSION = "0.15"
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
SCAN_PROGRESS_EVERY = int(os.getenv("SCAN_PROGRESS_EVERY", "50"))
SUPER_ALERT_COOLDOWN_SECONDS = int(os.getenv("SUPER_ALERT_COOLDOWN_SECONDS", "1800"))
NEWS_REFRESH_SECONDS = int(os.getenv("NEWS_REFRESH_SECONDS", "900"))
NEWS_SIGNAL_REFRESH_SECONDS = int(os.getenv("NEWS_SIGNAL_REFRESH_SECONDS", "300"))
NEWS_CACHE_LIMIT = int(os.getenv("NEWS_CACHE_LIMIT", "60"))
# Срок действия новостей в сигналах:
# 0–3 часа — полное влияние; 3–6 часов — половинное; старше 6 часов — не учитываются.
NEWS_STRONG_SECONDS = int(os.getenv("NEWS_STRONG_SECONDS", str(3 * 60 * 60)))
NEWS_EXPIRE_SECONDS = int(os.getenv("NEWS_EXPIRE_SECONDS", str(6 * 60 * 60)))
MSK_OFFSET = 3

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")
start_time = time.time()
state_lock = threading.RLock()

TF_LIST = ["15m", "1h", "4h", "1d", "1w"]
TF_PAIRS = [("15m", "1h"), ("15m", "4h"), ("1h", "4h"), ("4h", "1d"), ("1d", "1w")]
ANALYSIS_MODES = ["multi", "best", "max_profit", "auto_ai"]
INTERNAL_PROFILES = ["trend", "momentum", "breakout", "mean_reversion"]
SIGNAL_THRESHOLDS = [60, 70, 75, 80, 85, 90, 95]
STABLE_BASES = {"USDT", "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "PYUSD", "USDE", "USD1", "EURC", "EURS"}

DEFAULT_STATE: Dict[str, Any] = {
    "exchange": "mexc",
    "symbols": ["BTC/USDT:USDT", "ETH/USDT:USDT"],
    "lower_tf": "15m",
    "higher_tf": "1h",
    "auto_signals": False,
    "autotrade": False,
    "adaptive_improvement": False,
    "news_enabled": True,
    "news_cache": [],
    "news_cache_times": {},
    "news_seen": [],
    "news_last_update": 0,
    "news_bias_value": 0,
    "news_bias_label": "нейтральный",
    "news_bias_delta_pct": 0.0,
    "take_enabled": True,
    "take_min_profit_pct": 0.3,
    "take_max_profit_pct": 3.0,
    "take_auto_by_tf": True,
    "analysis_mode": "multi",
    "strategy_profile": "trend",
    "signal_output_mode": "top10",  # all = одно короткое сообщение, one = подробно отдельными сообщениями, top10 = 10 лучших
    "daily_trades_limit": 5,
    "daily_trades_count": 0,
    "daily_trades_date": "",
    "session_asia": True,
    "session_america": True,
    "last_top_exchange": "mexc",
    "admin_id": None,
    "price_count": 5,
    "super_trade_enabled": False,
    "signal_threshold_pct": 90,
    "last_super_alerts": {},
    "language": "ru",
}

api_input_sessions: Dict[int, Dict[str, str]] = {}
top_input_sessions: Dict[int, str] = {}
signal_jobs: Dict[int, float] = {}


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


def reset_state(preserve_admin: bool = True, preserve_language: bool = True) -> Dict[str, Any]:
    old = load_state()
    state = DEFAULT_STATE.copy()
    if preserve_admin:
        state["admin_id"] = old.get("admin_id")
    if preserve_language:
        state["language"] = old.get("language", DEFAULT_STATE.get("language", "ru"))
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


def safe_send_message(chat_id: int, text: str, **kwargs):
    """Отправка сообщений без падения на HTML/таймаутах Telegram."""
    try:
        return bot.send_message(chat_id, text, **kwargs)
    except Exception as e:
        msg = str(e)
        print(f"send_message error: {msg}", flush=True)
        # Ошибка entity parse чаще всего из-за случайного < или >. Повторяем без HTML.
        try:
            clean = re.sub(r"<[^>]+>", "", text)
            kwargs.pop("parse_mode", None)
            return bot.send_message(chat_id, clean, **kwargs)
        except Exception as e2:
            print(f"send_message retry error: {e2}", flush=True)
            return None


def safe_send_photo(chat_id: int, photo_path: str, caption: str = "", **kwargs):
    try:
        with open(photo_path, "rb") as f:
            return bot.send_photo(chat_id, f, caption=caption, **kwargs)
    except Exception as e:
        msg = str(e)
        print(f"send_photo error: {msg}", flush=True)
        # Если Telegram не принял caption, шлём фото без подписи + текст отдельно.
        try:
            with open(photo_path, "rb") as f:
                bot.send_photo(chat_id, f, **{k: v for k, v in kwargs.items() if k != "reply_markup"})
            return safe_send_message(chat_id, caption, reply_markup=kwargs.get("reply_markup"))
        except Exception as e2:
            print(f"send_photo retry error: {e2}", flush=True)
            return safe_send_message(chat_id, caption, reply_markup=kwargs.get("reply_markup"))


def is_en() -> bool:
    return load_state().get("language") == "en"


def onoff(v: bool) -> str:
    return "✅" if v else "❌"



def main_keyboard() -> types.ReplyKeyboardMarkup:
    s = load_state()
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    mode_label = {"all": "all signal", "one": "one signal", "top10": "10 signal"}.get(s.get("signal_output_mode", "top10"), "10 signal")
    threshold = int(s.get("signal_threshold_pct", 90))
    lang = s.get("language", "ru")
    if lang == "en":
        kb.add("📡 Signal", "⚙️ Settings")
        kb.add("📈 MEXC top+", "📈 BINGX top+")
        kb.add(f"🏦 Exchange: {s['exchange'].upper()}", f"⏱ TF {s['lower_tf']}/{s['higher_tf']}")
        kb.add(f"📰 News {onoff(s['news_enabled'])}", f"🤖 Generator: {s['analysis_mode']}")
        kb.add(f"🧠 Improvement {onoff(s['adaptive_improvement'])}", f"💼 Trades: {s['daily_trades_limit']}/day")
        kb.add(f"🌏 Asia {onoff(s['session_asia'])}", f"🇺🇸 America {onoff(s['session_america'])}")
        kb.add(f"🎯 Max Take {onoff(s['take_enabled'])}", f"⚡ Autotrade {onoff(s['autotrade'])}")
        kb.add(f"🚨 Super Deal {onoff(s.get('super_trade_enabled', False))}", f"💲 Price top-{s.get('price_count', 5)}")
        kb.add(f"📨 {mode_label}", f"🎚 Threshold {threshold}%")
        kb.add("🌐 Language EN", "📊 Profit")
        kb.add("🔑 API keys", "⛔ Close all")
        kb.add("🏓 Ping", "♻️ Reset")
        kb.add("🗑 delete all")
    else:
        kb.add("📡 Signal", "⚙️ Настройки")
        kb.add("📈 MEXC top+", "📈 BINGX top+")
        kb.add(f"🏦 Биржа: {s['exchange'].upper()}", f"⏱ TF {s['lower_tf']}/{s['higher_tf']}")
        kb.add(f"📰 Новости {onoff(s['news_enabled'])}", f"🤖 Генератор: {s['analysis_mode']}")
        kb.add(f"🧠 Улучшения {onoff(s['adaptive_improvement'])}", f"💼 Сделки: {s['daily_trades_limit']}/сут")
        kb.add(f"🌏 Азия {onoff(s['session_asia'])}", f"🇺🇸 Америка {onoff(s['session_america'])}")
        kb.add(f"🎯 Тейк макс {onoff(s['take_enabled'])}", f"⚡ Автоторговля {onoff(s['autotrade'])}")
        kb.add(f"🚨 Супер сделка {onoff(s.get('super_trade_enabled', False))}", f"💲 Цена top-{s.get('price_count', 5)}")
        kb.add(f"📨 {mode_label}", f"🎚 Порог {threshold}%")
        kb.add("🌐 Language", "📊 Профит")
        kb.add("🔑 API ключи", "⛔ Закрыть всё")
        kb.add("🏓 Ping", "♻️ Сброс")
        kb.add("🗑 delete all")
    return kb

def settings_text() -> str:
    s = load_state()
    if s.get("language") == "en":
        lines = [
            f"⚙️ <b>Settings v{VERSION}</b>",
            "Language: <b>English</b>",
            f"Exchange: <b>{esc(s['exchange'])}</b>",
            f"Coins: <b>{len(s['symbols'])}</b>",
            f"TF: <b>{esc(s['lower_tf'])}</b> / <b>{esc(s['higher_tf'])}</b>",
            f"Signal mode: <b>{ {'all': 'all signal — short summary', 'one': 'one signal — detailed separate messages', 'top10': '10 signal — only 10 best setups'}.get(s.get('signal_output_mode', 'top10')) }</b>",
            f"Threshold: <b>{int(s.get('signal_threshold_pct', 90))}%</b>",
            f"Auto signals: <b>{'ON' if s['auto_signals'] else 'OFF'}</b>",
            f"Autotrade: <b>{'ON' if s['autotrade'] else 'OFF'}</b> ({'LIVE' if LIVE_TRADING_ENABLED else 'PAPER'})",
            f"Super deal: <b>{'ON' if s.get('super_trade_enabled') else 'OFF'}</b>",
            f"Improvement: <b>{'ON' if s['adaptive_improvement'] else 'OFF'}</b>",
            f"News: <b>{'ON' if s['news_enabled'] else 'OFF'}</b> | background: <b>{esc(s.get('news_bias_label', 'neutral'))}</b> ({float(s.get('news_bias_delta_pct') or 0):+.1f}%)",
            f"Max Take: <b>{'ON' if s['take_enabled'] else 'OFF'}</b>, user range {s['take_min_profit_pct']}% / {s['take_max_profit_pct']}%, TF auto: {'ON' if s.get('take_auto_by_tf') else 'OFF'}",
            f"Analysis: <b>{esc(s['analysis_mode'])}</b>, profile: <b>{esc(s['strategy_profile'])}</b>",
            f"Trades/day: <b>{s['daily_trades_limit']}</b>, today: <b>{s['daily_trades_count']}</b>",
            f"Asia 03:00 MSK: <b>{'ON' if s['session_asia'] else 'OFF'}</b>",
            f"America 16:30 MSK: <b>{'ON' if s['session_america'] else 'OFF'}</b>",
            f"Price: <b>top-{s.get('price_count', 5)}</b>",
            f"Admin: <b>{esc(s.get('admin_id') or 'not assigned')}</b>",
            f"API: <b>{esc(api_status_short())}</b>",
            "",
            "Commands:",
            "<code>signal btc</code> / <code>btc</code>",
            "<code>mexc top-100</code> / <code>bingx top-200</code> / <code>top-50</code>",
            "<code>new sol</code> / <code>delete sol</code> / <code>delete all</code>",
            "<code>tf 15m 4h</code> / <code>take 0.5 4</code> / <code>trades 10</code>",
            "<code>price top-10</code> / <code>price 3</code>",
            "<code>all signal</code> / <code>one signal</code> / <code>10 signal</code>",
            "<code>threshold 70</code> / Threshold button",
            "<code>auto on</code> / <code>auto off</code>",
            "<code>api mexc</code> / <code>api bingx</code> / <code>api status</code>",
        ]
        return "\n".join(lines)
    lines = [
        f"⚙️ <b>Настройки v{VERSION}</b>",
        "Язык: <b>Русский</b>",
        f"Биржа: <b>{esc(s['exchange'])}</b>",
        f"Монет: <b>{len(s['symbols'])}</b>",
        f"TF: <b>{esc(s['lower_tf'])}</b> / <b>{esc(s['higher_tf'])}</b>",
        f"Режим сигналов: <b>{ {'all': 'all signal — кратко одним сообщением', 'one': 'one signal — подробно отдельными сообщениями', 'top10': '10 signal — только 10 лучших'}.get(s.get('signal_output_mode', 'top10')) }</b>",
        f"Порог сигнала: <b>{int(s.get('signal_threshold_pct', 90))}%</b>",
        f"Автосигналы: <b>{'ВКЛ' if s['auto_signals'] else 'ВЫКЛ'}</b>",
        f"Автоторговля: <b>{'ВКЛ' if s['autotrade'] else 'ВЫКЛ'}</b> ({'LIVE' if LIVE_TRADING_ENABLED else 'PAPER'})",
        f"Супер сделка: <b>{'ВКЛ' if s.get('super_trade_enabled') else 'ВЫКЛ'}</b>",
        f"Улучшения: <b>{'ВКЛ' if s['adaptive_improvement'] else 'ВЫКЛ'}</b>",
        f"Новости: <b>{'ВКЛ' if s['news_enabled'] else 'ВЫКЛ'}</b> | фон: <b>{esc(s.get('news_bias_label', 'нейтральный'))}</b> ({float(s.get('news_bias_delta_pct') or 0):+.1f}%)",
        f"Тейк макс: <b>{'ВКЛ' if s['take_enabled'] else 'ВЫКЛ'}</b>, user range {s['take_min_profit_pct']}% / {s['take_max_profit_pct']}%, TF auto: {'ВКЛ' if s.get('take_auto_by_tf') else 'ВЫКЛ'}",
        f"Анализ: <b>{esc(s['analysis_mode'])}</b>, профиль: <b>{esc(s['strategy_profile'])}</b>",
        f"Сделок/сутки: <b>{s['daily_trades_limit']}</b>, сегодня: <b>{s['daily_trades_count']}</b>",
        f"Азия 03:00 МСК: <b>{'ВКЛ' if s['session_asia'] else 'ВЫКЛ'}</b>",
        f"Америка 16:30 МСК: <b>{'ВКЛ' if s['session_america'] else 'ВЫКЛ'}</b>",
        f"Цена: <b>top-{s.get('price_count', 5)}</b>",
        f"Админ: <b>{esc(s.get('admin_id') or 'не назначен')}</b>",
        f"API: <b>{esc(api_status_short())}</b>",
        "",
        "Команды:",
        "<code>signal btc</code> / <code>btc</code>",
        "<code>mexc top-100</code> / <code>bingx top-200</code> / <code>top-50</code>",
        "<code>new sol</code> / <code>delete sol</code> / <code>delete all</code>",
        "<code>tf 15m 4h</code> / <code>take 0.5 4</code> / <code>trades 10</code>",
        "<code>price top-10</code> / <code>price 3</code>",
        "<code>all signal</code> / <code>one signal</code> / <code>10 signal</code>",
        "<code>threshold 70</code> / кнопка Порог",
        "<code>auto on</code> / <code>auto off</code>",
        "<code>api mexc</code> / <code>api bingx</code> / <code>api status</code>",
    ]
    return "\n".join(lines)

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


def fetch_crypto_news(limit: int = 20) -> List[str]:
    """Русскоязычные и крупные крипто-источники. Без API-ключей."""
    urls = [
        "https://forklog.com/feed",
        "https://bits.media/rss/",
        "https://cointelegraph.com/rss/tag/bitcoin",
        "https://cointelegraph.com/rss",
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
                t = html.unescape(t)
                if not t or "RSS" in t or "Cointelegraph.com News" in t:
                    continue
                if not any(normalize_news_title(x) == normalize_news_title(t) for x in out):
                    out.append(t)
        except Exception as e:
            print(f"news source error {url}: {e}", flush=True)
    return out[:limit]


def normalize_news_title(title: str) -> str:
    title = re.sub(r"\s+", " ", str(title or "").strip().lower())
    title = re.sub(r"[^a-zа-я0-9%$ ]+", "", title)
    return title[:220]


def news_freshness_weight(ts: float, now: Optional[float] = None) -> float:
    """0–3ч = 1.0, 3–6ч = 0.5, старше 6ч = 0.0."""
    now = time.time() if now is None else now
    if not ts:
        return 0.0
    age = max(0.0, now - float(ts))
    if age <= NEWS_STRONG_SECONDS:
        return 1.0
    if age <= NEWS_EXPIRE_SECONDS:
        return 0.5
    return 0.0


def format_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h:
        return f"{h}ч {m}м"
    return f"{m}м"


def analyze_news_background(headlines: List[str], item_times: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """
    Возвращает общий новостной фон.
    Если переданы item_times, новости имеют срок действия:
    0–3 часа — полное влияние; 3–6 часов — половина; старше 6 часов — только в списке, но не в расчёте сигнала.
    delta_pct — на сколько процентов модель может сдвинуть расчётную успешность.
    Плюс помогает LONG, минус помогает SHORT. Это оценка, не гарантия.
    """
    positive_words = [
        "рост", "раст", "быч", "позитив", "одобр", "запуск", "принял", "легализ", "институцион",
        "etf", "rally", "surge", "pump", "bull", "bullish", "adoption", "approve", "approval",
        "record", "inflow", "buy", "покуп", "снижен", "rate cut", "blackrock", "накоп",
    ]
    negative_words = [
        "паден", "обвал", "дамп", "медвеж", "негатив", "взлом", "хак", "эксплойт", "ликвидац",
        "запрет", "иск", "штраф", "расслед", "банкрот", "отток", "распрод", "войн", "санкц",
        "hack", "exploit", "ban", "lawsuit", "sec sues", "crackdown", "selloff", "dump", "liquidation",
        "bankrupt", "outflow", "rate hike", "fine",
    ]
    high_impact_words = [
        "trump", "трамп", "musk", "маск", "elon", "илон", "sec", "фрс", "fed", "etf",
        "blackrock", "binance", "coinbase", "tether", "usdt", "hack", "взлом", "exploit",
    ]

    now = time.time()
    score = 0.0
    hits_pos: List[str] = []
    hits_neg: List[str] = []
    important: List[str] = []
    fresh_count = weak_count = expired_count = 0
    effective_items: List[str] = []
    oldest_effective_age = 0.0
    newest_effective_age: Optional[float] = None

    for h in headlines[:20]:
        low = h.lower()
        key = normalize_news_title(h)
        weight = 1.0
        if item_times is not None:
            ts = float(item_times.get(key) or 0)
            age = max(0.0, now - ts) if ts else NEWS_EXPIRE_SECONDS + 1
            weight = news_freshness_weight(ts, now)
            if weight >= 1.0:
                fresh_count += 1
            elif weight > 0:
                weak_count += 1
            else:
                expired_count += 1
            if weight <= 0:
                continue
            effective_items.append(h)
            oldest_effective_age = max(oldest_effective_age, age)
            newest_effective_age = age if newest_effective_age is None else min(newest_effective_age, age)

        p = sum(1 for w in positive_words if w in low)
        n = sum(1 for w in negative_words if w in low)
        hi = any(w in low for w in high_impact_words)
        mult = 1.6 if hi else 1.0
        score += (p - n) * mult * weight
        if p:
            hits_pos.append(h)
        if n:
            hits_neg.append(h)
        if hi:
            important.append(h)

    score = max(-12.0, min(12.0, score))
    delta = round(max(-6.0, min(6.0, score * 0.55)), 1)
    if score >= 4:
        label = "резко позитивный"
    elif score >= 1:
        label = "позитивный"
    elif score <= -4:
        label = "резко негативный"
    elif score <= -1:
        label = "негативный"
    else:
        if item_times is not None and expired_count and not (fresh_count or weak_count):
            label = "нейтральный, свежих новостей нет"
        else:
            label = "нейтральный"

    if item_times is None:
        freshness_label = "без ограничения срока"
    elif fresh_count:
        freshness_label = f"свежие до 3ч: {fresh_count}, 3–6ч: {weak_count}"
    elif weak_count:
        freshness_label = f"только 3–6ч: {weak_count}, влияние 50%"
    else:
        freshness_label = "свежих нет, фон не учитывается"

    return {
        "value": int(round(score)),
        "delta_pct": delta,
        "label": label,
        "positive_count": len(hits_pos),
        "negative_count": len(hits_neg),
        "important_count": len(important),
        "important": important[:5],
        "fresh_count": fresh_count,
        "weak_count": weak_count,
        "expired_count": expired_count,
        "freshness_label": freshness_label,
        "effective_items": effective_items[:10],
        "newest_effective_age_sec": newest_effective_age,
        "oldest_effective_age_sec": oldest_effective_age,
    }

def update_news_cache(force: bool = False) -> Dict[str, Any]:
    """Обновляет новости: новые заголовки кладутся сверху, старые остаются на месте.
    Для сигналов учитываются только новости до 6 часов: 0–3ч = 100%, 3–6ч = 50%, старше = 0%.
    """
    s = load_state()
    now = time.time()
    old_items = [str(x) for x in (s.get("news_cache") or []) if str(x).strip()]
    old_times = dict(s.get("news_cache_times") or {})
    fallback_ts = float(s.get("news_last_update") or 0)
    if fallback_ts:
        for item in old_items:
            old_times.setdefault(normalize_news_title(item), fallback_ts)

    if not force and old_items and now - float(s.get("news_last_update") or 0) < NEWS_REFRESH_SECONDS:
        bg = analyze_news_background(old_items, old_times)
        return {"items": old_items, "new_items": [], "item_times": old_times, **bg}

    fetched = fetch_crypto_news(NEWS_CACHE_LIMIT)
    old_norm = {normalize_news_title(x) for x in old_items}
    new_items = [x for x in fetched if normalize_news_title(x) and normalize_news_title(x) not in old_norm]

    merged: List[str] = []
    seen = set()
    merged_times: Dict[str, float] = {}
    new_keys = {normalize_news_title(x) for x in new_items}
    for item in new_items + old_items:
        key = normalize_news_title(item)
        if key and key not in seen:
            seen.add(key)
            merged.append(item)
            merged_times[key] = now if key in new_keys else float(old_times.get(key) or now)
        if len(merged) >= NEWS_CACHE_LIMIT:
            break

    if not merged:
        merged = fetched[:NEWS_CACHE_LIMIT]
        merged_times = {normalize_news_title(x): now for x in merged}

    bg = analyze_news_background(merged, merged_times)
    s["news_cache"] = merged
    s["news_cache_times"] = merged_times
    s["news_seen"] = list(seen)[:NEWS_CACHE_LIMIT]
    s["news_last_update"] = now
    s["news_bias_value"] = bg["value"]
    s["news_bias_label"] = bg["label"]
    s["news_bias_delta_pct"] = bg["delta_pct"]
    save_state(s)
    return {"items": merged, "new_items": new_items, "item_times": merged_times, **bg}

def get_news_for_signal(force: bool = False) -> Dict[str, Any]:
    """Перед сигналом реально проверяет новости, но не чаще NEWS_SIGNAL_REFRESH_SECONDS.
    Старые новости остаются в списке, но если им больше 6 часов — не влияют на сигнал.
    """
    s = load_state()
    cached = [str(x) for x in (s.get("news_cache") or []) if str(x).strip()]
    times = dict(s.get("news_cache_times") or {})
    fallback_ts = float(s.get("news_last_update") or 0)
    if fallback_ts:
        for item in cached:
            times.setdefault(normalize_news_title(item), fallback_ts)
    age = time.time() - float(s.get("news_last_update") or 0)
    if force or not cached or age > NEWS_SIGNAL_REFRESH_SECONDS:
        return update_news_cache(force=True)
    bg = analyze_news_background(cached, times)
    return {"items": cached, "new_items": [], "item_times": times, **bg}

def news_bias(headlines: List[str]) -> int:
    return int(analyze_news_background(headlines).get("value", 0))


def format_news_panel(info: Dict[str, Any], enabled: bool) -> str:
    delta = float(info.get("delta_pct") or 0)
    delta_text = f"{delta:+.1f}%"
    freshness = str(info.get("freshness_label") or "")
    msg = (
        f"📰 <b>Новости: {'ВКЛ' if enabled else 'ВЫКЛ'}</b>\n"
        f"Фон: <b>{esc(info.get('label', 'нейтральный'))}</b> | влияние на сигнал: <b>{delta_text}</b>\n"
        f"⏳ Срок действия: <b>0–3ч = 100%, 3–6ч = 50%, старше 6ч = 0%</b>\n"
        f"Свежесть: <b>{esc(freshness)}</b>\n"
    )
    if enabled:
        msg += "Фильтр новостей будет реально проверяться перед сигналом и автоторговлей.\n"
    else:
        msg += "Новостной фон не будет учитываться в сигналах и автоторговле.\n"
    if info.get("new_items"):
        msg += f"\n✅ Новые новости добавлены сверху: <b>{len(info['new_items'])}</b>\n"
    else:
        msg += "\nℹ️ Новых новостей нет — список не изменился.\n"
    items = info.get("items") or []
    msg += "\n<b>Последние новости:</b>\n"
    msg += "\n".join("• " + esc(x) for x in items[:10]) if items else "Новости не получены."
    return msg


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
    # Символы, загруженные через top+, уже имеют точное имя биржи. Не грузим markets лишний раз.
    if "/" in symbol and symbol.upper().endswith(":USDT"):
        symbol = symbol
    else:
        symbol = resolve_symbol(ex, symbol)
    lo = fetch_ohlcv(ex, symbol, s["lower_tf"])
    hi = fetch_ohlcv(ex, symbol, s["higher_tf"])
    lf = features(lo)
    hf = features(hi)
    headlines: List[str] = []
    nb = 0
    news_info: Dict[str, Any] = {}
    if s.get("news_enabled"):
        # Перед каждым сигналом новости реально проверяются, но через кэш,
        # чтобы при анализе 100–500 монет бот не зависал на RSS-запросах.
        news_info = get_news_for_signal(force=False)
        headlines = list(news_info.get("effective_items") or news_info.get("items") or [])[:6]
        nb = int(news_info.get("value") or 0)

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
    confidence = round(max(long_pct, short_pct), 1)
    direction = "LONG" if long_pct >= 55 else "SHORT" if short_pct >= 55 else "NEUTRAL"
    entry, stop, tp = build_entry_plan(direction, lf, s, mode)

    # Score 1–10: внутренний рейтинг качества setup.
    # В v0.15 score больше не разгоняется до 10 только из-за L/S 100/0,
    # иначе режим "Супер сделка" спамил почти по любой монете.
    sep = abs(long_pct - short_pct)
    trend_aligned = (
        direction == "LONG" and lf["slope"] > 0 and hf["ema20"] > hf["ema50"] and lf["ema20"] > lf["ema50"]
    ) or (
        direction == "SHORT" and lf["slope"] < 0 and hf["ema20"] < hf["ema50"] and lf["ema20"] < lf["ema50"]
    )
    momentum_aligned = (direction == "LONG" and lf["macd_hist"] > 0) or (direction == "SHORT" and lf["macd_hist"] < 0)
    rsi_ok = (direction == "LONG" and 42 <= lf["rsi"] <= 67) or (direction == "SHORT" and 33 <= lf["rsi"] <= 58)
    volume_confirm = lf["vol"] > lf["vol_sma"] * 1.12

    risk = abs(entry - stop)
    reward_mid = abs(tp[1] - entry) if len(tp) > 1 else abs(tp[0] - entry)
    rr = reward_mid / risk if risk > 0 else 0.0
    entry_distance_pct = abs(lf["price"] - entry) / max(lf["price"], 1e-12) * 100

    score = 1.0 + sep / 20.0
    if direction != "NEUTRAL":
        if trend_aligned:
            score += 0.9
        if momentum_aligned:
            score += 0.55
        if rsi_ok:
            score += 0.45
        if volume_confirm:
            score += 0.45
        if rr >= 1.25:
            score += 0.65
        if entry_distance_pct <= max(0.45, lf.get("atr_pct", 0.3) * 1.4):
            score += 0.35
        if mode in {"best", "auto_ai", "max_profit"}:
            score += 0.2
    news_adjustment = 0.0
    news_label = "выкл"
    news_delta = 0.0
    if s.get("news_enabled"):
        if not news_info:
            news_info = get_news_for_signal(force=False)
        news_delta = float(news_info.get("delta_pct") or 0.0)
        news_label = str(news_info.get("label") or "нейтральный")
        # Позитивный фон усиливает LONG и ослабляет SHORT; негативный — наоборот.
        if direction == "LONG":
            news_adjustment = news_delta
        elif direction == "SHORT":
            news_adjustment = -news_delta
        else:
            news_adjustment = -abs(news_delta) * 0.25
        if news_adjustment > 0:
            score += min(0.6, news_adjustment / 8.0)
        elif news_adjustment < 0:
            score += max(-0.8, news_adjustment / 7.0)

    score = round(max(1.0, min(10.0, score)), 1)

    # Расчётная проходимость не является гарантией.
    # Для "Супер сделки" теперь нужны одновременно:
    # 1) LONG/SHORT, 2) проходимость 95–97%, 3) score >= 7,
    # 4) нормальный риск/профит, 5) подтверждение трендом.
    raw_success = confidence * 0.72 + score * 2.5
    if trend_aligned:
        raw_success += 2.2
    if momentum_aligned:
        raw_success += 1.0
    if rsi_ok:
        raw_success += 0.8
    if volume_confirm:
        raw_success += 0.8
    if rr >= 1.25:
        raw_success += 1.2
    if s.get("news_enabled"):
        raw_success += news_adjustment
    success_pct = round(min(97.0, max(50.0, raw_success)), 1)

    is_super = bool(
        direction != "NEUTRAL"
        and 95.0 <= success_pct <= 97.0
        and score >= 7.0
        and confidence >= 92.0
        and rr >= 1.25
        and trend_aligned
    )

    chart = draw_chart(symbol, lo, direction, long_pct, short_pct, entry, stop, tp) if with_chart else None
    return {
        "symbol": symbol,
        "exchange": s["exchange"],
        "direction": direction,
        "long_pct": long_pct,
        "short_pct": short_pct,
        "confidence": confidence,
        "success_pct": success_pct,
        "score": score,
        "risk_reward": round(rr, 2),
        "entry_distance_pct": round(entry_distance_pct, 3),
        "is_super": is_super,
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
        "news_enabled": bool(s.get("news_enabled")),
        "news_label": news_label,
        "news_delta_pct": round(news_delta, 1),
        "news_adjustment_pct": round(news_adjustment, 1),
        "news_freshness_label": str(news_info.get("freshness_label", "")) if news_info else "",
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
    close = o["close"][-180:]
    x = np.arange(len(close))
    e20 = ema(close, 20)
    e50 = ema(close, 50)
    slope, st, en = linreg_line(close, 80)
    n = min(80, len(close))
    tx = np.arange(len(close) - n, len(close))
    trend = np.linspace(st, en, n)

    fig, ax = plt.subplots(figsize=(20, 11), dpi=190)
    draw_candles(ax, {k: v[-180:] for k, v in o.items() if k in {"open", "high", "low", "close", "volume"}})
    ax.plot(x, e20, label="EMA20", linewidth=2.2)
    ax.plot(x, e50, label="EMA50", linewidth=2.2)
    ax.plot(tx, trend, linestyle="--", label="Наклонный уровень", linewidth=2.6)

    # Жирные линии входа, стопа и тейков + крупные подписи справа.
    ax.axhline(entry, label=f"ENTRY {entry:.6g}", linewidth=3.3)
    ax.text(len(close) + 1.5, entry, f"ENTRY {entry:.6g}", va="center", fontsize=11, fontweight="bold")
    ax.axhline(stop, linestyle="--", label=f"STOP {stop:.6g}", linewidth=3.3)
    ax.text(len(close) + 1.5, stop, f"STOP {stop:.6g}", va="center", fontsize=11, fontweight="bold")
    for i, t in enumerate(tp, 1):
        ax.axhline(t, linestyle=":", label=f"TP{i} {t:.6g}", linewidth=3.0)
        ax.text(len(close) + 1.5, t, f"TP{i} {t:.6g}", va="center", fontsize=11, fontweight="bold")

    ax.set_title(f"{symbol} | {direction} | LONG {long_pct}% / SHORT {short_pct}%", fontsize=18, fontweight="bold")
    ax.grid(True, alpha=0.26)
    ax.legend(fontsize=11, loc="best")
    ax.set_xlim(-2, len(close) + 24)
    ax.tick_params(axis="both", labelsize=11)
    fig.tight_layout()
    p = CHART_DIR / (re.sub(r"[^A-Za-z0-9_.-]+", "_", symbol) + f"_{int(time.time())}.png")
    fig.savefig(p, bbox_inches="tight")
    plt.close(fig)
    return str(p)


def direction_icon(direction: str) -> str:
    # Только цвет позиции без стрелок, чтобы не загромождать ALL SIGNAL.
    if direction == "LONG":
        return "🟢"
    if direction == "SHORT":
        return "🔴"
    return "⚪"


def format_signal(sig: Dict[str, Any]) -> str:
    tp = sig["tp"]
    reasons = "\n".join("• " + esc(r) for r in sig["reasons"])
    lang = load_state().get("language", "ru")
    news = ""
    if sig.get("headlines"):
        adj = float(sig.get("news_adjustment_pct") or 0.0)
        freshness = sig.get("news_freshness_label", "")
        if lang == "en":
            news = (
                f"\n\n📰 <b>News background:</b> {esc(sig.get('news_label', 'neutral'))} "
                f"| impact on this setup: <b>{adj:+.1f}%</b>\n"
                f"⏳ {esc(freshness)}\n"
                + "\n".join("• " + esc(h) for h in sig["headlines"][:4])
            )
        else:
            news = (
                f"\n\n📰 <b>Новостной фон:</b> {esc(sig.get('news_label', 'нейтральный'))} "
                f"| влияние на эту сделку: <b>{adj:+.1f}%</b>\n"
                f"⏳ {esc(freshness)}\n"
                + "\n".join("• " + esc(h) for h in sig["headlines"][:4])
            )
    super_line = "\n🚨 <b>SUPER DEAL</b>" if (sig.get("is_super") and lang == "en") else "\n🚨 <b>СУПЕР СДЕЛКА</b>" if sig.get("is_super") else ""
    if lang == "en":
        return (
            f"📡 <b>Signal {esc(sig['symbol'])}</b>{super_line}\n"
            f"Exchange: <b>{esc(sig['exchange'])}</b> | TF: <b>{esc(sig['lower_tf'])}/{esc(sig['higher_tf'])}</b>\n"
            f"Direction: <b>{direction_icon(sig['direction'])} {sig['direction']}</b>\n"
            f"Long/Short: <b>{sig['long_pct']}%</b> / <b>{sig['short_pct']}%</b>\n"
            f"Estimated success: <b>{sig.get('success_pct', sig.get('confidence', 0))}%</b> | Score: <b>{sig.get('score', 0)}/10</b>\n\n"
            f"💵 Price: <code>{sig['price']:.8g}</code>\n"
            f"🎯 Favorable entry: <code>{sig['entry']:.8g}</code>\n"
            f"🛑 Stop-loss: <code>{sig['stop']:.8g}</code>\n"
            f"✅ TP1: <code>{tp[0]:.8g}</code>\n"
            f"✅ TP2: <code>{tp[1]:.8g}</code>\n"
            f"✅ TP3: <code>{tp[2]:.8g}</code>\n\n"
            f"RSI: <b>{sig['rsi']:.1f}</b> | ATR: <b>{sig['atr']:.8g}</b> ({sig['atr_pct']:.2f}%)\n"
            f"Support/Resistance: <code>{sig['support']:.8g}</code> / <code>{sig['resistance']:.8g}</code>\n"
            f"Mode: <b>{esc(sig['mode'])}</b>, profile: <b>{esc(sig['profile'])}</b>\n\n"
            f"🧩 <b>Factors:</b>\n{reasons}{news}\n\n"
            "⚠️ Not financial advice. Risk management is mandatory. Percentages are model estimates, not guarantees."
        )
    return (
        f"📡 <b>Signal {esc(sig['symbol'])}</b>{super_line}\n"
        f"Биржа: <b>{esc(sig['exchange'])}</b> | TF: <b>{esc(sig['lower_tf'])}/{esc(sig['higher_tf'])}</b>\n"
        f"Направление: <b>{direction_icon(sig['direction'])} {sig['direction']}</b>\n"
        f"Long/Short: <b>{sig['long_pct']}%</b> / <b>{sig['short_pct']}%</b>\n"
        f"Расчётная проходимость: <b>{sig.get('success_pct', sig.get('confidence', 0))}%</b> | Score: <b>{sig.get('score', 0)}/10</b>\n\n"
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
        "⚠️ Не финсовет. Риск обязателен. Проценты — оценка модели, не гарантия."
    )


def format_signal_brief(sig: Dict[str, Any]) -> str:
    tp = sig["tp"]
    base = base_from_symbol(sig["symbol"])
    emoji = direction_icon(sig["direction"])
    super_mark = " 🚨" if sig.get("is_super") else ""
    if load_state().get("language") == "en":
        return (
            f"{emoji} <b>{esc(base)}</b>{super_mark} {sig['direction']} "
            f"L/S {sig['long_pct']}/{sig['short_pct']} | Success {sig.get('success_pct', 0)}% | Score {sig.get('score', 0)}/10 | "
            + (f"News {float(sig.get('news_adjustment_pct') or 0):+.1f}% | " if sig.get('news_enabled') else "")
            + f"Entry <code>{sig['entry']:.6g}</code> | SL <code>{sig['stop']:.6g}</code> | "
            f"TP <code>{tp[0]:.6g}</code>/<code>{tp[1]:.6g}</code>/<code>{tp[2]:.6g}</code>"
        )
    return (
        f"{emoji} <b>{esc(base)}</b>{super_mark} {sig['direction']} "
        f"L/S {sig['long_pct']}/{sig['short_pct']} | Усп. {sig.get('success_pct', 0)}% | Score {sig.get('score', 0)}/10 | "
        + (f"News {float(sig.get('news_adjustment_pct') or 0):+.1f}% | " if sig.get('news_enabled') else "")
        + f"Entry <code>{sig['entry']:.6g}</code> | SL <code>{sig['stop']:.6g}</code> | "
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
        if len(buf) + len(line) + 1 > 3200:
            safe_send_message(chat_id, buf, reply_markup=main_keyboard())
            time.sleep(0.35)
            buf = ""
        buf += line + "\n"
    if buf.strip():
        safe_send_message(chat_id, buf, reply_markup=main_keyboard())


def send_signal(chat_id: int, symbol: str) -> None:
    safe_send_message(chat_id, f"⏳ Анализирую {esc(symbol)}...", reply_markup=main_keyboard())
    sig = build_signal(symbol, True)
    caption = format_signal(sig)
    p = sig.get("chart_path")
    if p and Path(p).exists():
        safe_send_photo(chat_id, p, caption=caption, reply_markup=main_keyboard())
    else:
        safe_send_message(chat_id, caption, reply_markup=main_keyboard())
    execute_trade_if_allowed(chat_id, sig)


def signal_sort_key(sig: Dict[str, Any]) -> Tuple[float, float, float, float]:
    # Главная сортировка — по расчётной проходимости, затем score.
    # Так в ALL SIGNAL сверху всегда будут монеты с максимальным % успешности.
    return (
        float(sig.get("success_pct", 0)),
        float(sig.get("score", 0)),
        float(sig.get("confidence", 0)),
        float(sig.get("is_super", False)),
    )


def passes_signal_threshold(sig: Dict[str, Any], state: Optional[Dict[str, Any]] = None) -> bool:
    st = state or load_state()
    threshold = float(st.get("signal_threshold_pct", 90))
    return float(sig.get("success_pct", 0)) >= threshold


def should_alert_super(sig: Dict[str, Any]) -> bool:
    if not sig.get("is_super"):
        return False
    s = load_state()
    key = f"{sig.get('exchange')}:{sig.get('symbol')}:{sig.get('direction')}"
    last = (s.get("last_super_alerts") or {}).get(key, 0)
    now = time.time()
    if now - float(last or 0) < SUPER_ALERT_COOLDOWN_SECONDS:
        return False
    alerts = dict(s.get("last_super_alerts") or {})
    alerts[key] = now
    # чистка старых ключей
    alerts = {k: v for k, v in alerts.items() if now - float(v or 0) < 24 * 3600}
    s["last_super_alerts"] = alerts
    save_state(s)
    return True


def send_super_alert(chat_id: int, sig: Dict[str, Any]) -> None:
    if not load_state().get("super_trade_enabled"):
        return
    if not should_alert_super(sig):
        return
    text = "🚨 <b>СУПЕР СДЕЛКА</b>\n" + format_signal_brief(sig)
    safe_send_message(chat_id, text, reply_markup=main_keyboard())


def send_all_signals_worker(chat_id: int) -> None:
    try:
        s = load_state()
        symbols = list(s.get("symbols", []))[:MAX_SIGNAL_SCAN]
        threshold = float(s.get("signal_threshold_pct", 90))
        mode = s.get("signal_output_mode", "top10")
        if not symbols:
            safe_send_message(chat_id, "Список монет пуст. Нажми MEXC top+ / BINGX top+ или new BTC.", reply_markup=main_keyboard())
            return

        if s.get("news_enabled"):
            info = get_news_for_signal(force=True)
            print(f"news before scan: {info.get('label')} delta={info.get('delta_pct')} new={len(info.get('new_items') or [])}", flush=True)

        scan_note = "10 лучших" if mode == "top10" else "подробно" if mode == "one" else "кратко"
        safe_send_message(
            chat_id,
            f"⏳ Анализ {len(symbols)} монет запущен в фоне: {scan_note}. Фильтр: успешность от {threshold:.0f}%.",
            reply_markup=main_keyboard(),
        )

        results: List[Dict[str, Any]] = []
        errors: List[str] = []
        for i, sym in enumerate(symbols, 1):
            try:
                sig = build_signal(sym, with_chart=False)
                if passes_signal_threshold(sig, s):
                    results.append(sig)
                    send_super_alert(chat_id, sig)
                    execute_trade_if_allowed(chat_id, sig)
            except Exception as e:
                errors.append(f"❌ <b>{esc(base_from_symbol(sym))}</b>: {esc(str(e)[:160])}")
            if i % SCAN_PROGRESS_EVERY == 0:
                safe_send_message(chat_id, f"⏳ Сканирование: {i}/{len(symbols)} | прошло фильтр: {len(results)}", reply_markup=main_keyboard())

        # Сортируем строго по максимальной расчётной успешности, затем score.
        results.sort(key=signal_sort_key, reverse=True)

        if mode == "top10":
            results = results[:10]

        if not results:
            safe_send_message(
                chat_id,
                f"⚪ Нет сигналов выше порога {threshold:.0f}%. Попробуй снизить порог кнопкой 🎚 Порог или командой <code>threshold 70</code>.",
                reply_markup=main_keyboard(),
            )
            if errors:
                send_text_chunks(chat_id, "Ошибки по части монет:\n", errors[:30])
            return

        if mode == "one":
            safe_send_message(
                chat_id,
                f"📨 ONE SIGNAL: подробно по {len(results)} монетам, отсортировано по успешности сверху вниз. Графики только у первых {MAX_ONE_SIGNAL_CHARTS}.",
                reply_markup=main_keyboard(),
            )
            for idx, sig in enumerate(results, 1):
                try:
                    if idx <= MAX_ONE_SIGNAL_CHARTS:
                        detailed = build_signal(sig["symbol"], with_chart=True)
                        # сохраняем сортировочные поля из первого расчёта на случай небольшой разницы тиков
                        sig = detailed
                    if sig.get("chart_path") and Path(sig["chart_path"]).exists() and idx <= MAX_ONE_SIGNAL_CHARTS:
                        safe_send_photo(chat_id, sig["chart_path"], caption=format_signal(sig), reply_markup=main_keyboard())
                    else:
                        safe_send_message(chat_id, format_signal(sig), reply_markup=main_keyboard())
                    time.sleep(0.45)
                except Exception as e:
                    safe_send_message(chat_id, f"❌ {esc(sig.get('symbol'))}: {esc(str(e)[:250])}", reply_markup=main_keyboard())
            return

        lines = [format_signal_brief(sig) for sig in results]
        lines.extend(errors[:50])
        title = "10 SIGNAL" if mode == "top10" else "ALL SIGNAL"
        header = (
            f"📡 <b>{title}</b> | {esc(s['exchange']).upper()} | TF {esc(s['lower_tf'])}/{esc(s['higher_tf'])} | "
            f"монет: {len(symbols)} | фильтр: от {threshold:.0f}% | показано: {len(results)}\n"
            f"Сверху — максимальная расчётная успешность, затем Score.\n\n"
        )
        send_text_chunks(chat_id, header, lines)
    finally:
        signal_jobs.pop(chat_id, None)


def send_all_signals(chat_id: int) -> None:
    if chat_id in signal_jobs and time.time() - signal_jobs[chat_id] < 600:
        safe_send_message(chat_id, "⏳ Анализ уже идёт. Дождись результата или повтори позже.", reply_markup=main_keyboard())
        return
    signal_jobs[chat_id] = time.time()
    threading.Thread(target=send_all_signals_worker, args=(chat_id,), daemon=True).start()
    safe_send_message(chat_id, "✅ Задача анализа запущена в фоне.", reply_markup=main_keyboard())

def load_top(chat_id: int, exchange_name: str, n: int) -> None:
    n = max(1, min(1000, int(n)))
    lang = load_state().get("language", "ru")
    bot.send_message(chat_id, f"⏳ Loading top-{n} {exchange_name.upper()} futures..." if lang == "en" else f"⏳ Загружаю top-{n} {exchange_name.upper()} futures...", reply_markup=main_keyboard())
    symbols = fetch_top_symbols(exchange_name, n)
    s = load_state()
    s["exchange"] = exchange_name
    s["last_top_exchange"] = exchange_name
    s["symbols"] = symbols  # если MEXC загружен — BINGX список удаляется, и наоборот
    save_state(s)
    if lang == "en":
        bot.send_message(chat_id, f"✅ {exchange_name.upper()} top-{n}: loaded {len(symbols)} coins.\nFirst 10:\n" + "\n".join(symbols[:10]), reply_markup=main_keyboard())
    else:
        bot.send_message(chat_id, f"✅ {exchange_name.upper()} top-{n}: загружено {len(symbols)} монет.\nПервые 10:\n" + "\n".join(symbols[:10]), reply_markup=main_keyboard())


def reset_to_default_profile(chat_id: int) -> None:
    old = load_state()
    lang = old.get("language", "ru")
    admin_id = old.get("admin_id")
    state = DEFAULT_STATE.copy()
    state["admin_id"] = admin_id
    state["language"] = lang
    state.update({
        "exchange": "mexc",
        "last_top_exchange": "mexc",
        "signal_threshold_pct": 90,
        "news_enabled": True,
        "session_asia": True,
        "session_america": True,
        "price_count": 5,
        "analysis_mode": "multi",
        "strategy_profile": "multi",
        "take_enabled": True,
        "signal_output_mode": "top10",
    })
    save_state(state)
    bot.send_message(
        chat_id,
        "♻️ Reset done. Loading MEXC top-150..." if lang == "en" else "♻️ Настройки сброшены. Загружаю MEXC top-150...",
        reply_markup=main_keyboard(),
    )
    try:
        symbols = fetch_top_symbols("mexc", 150)
        s = load_state()
        s["exchange"] = "mexc"
        s["last_top_exchange"] = "mexc"
        s["symbols"] = symbols
        save_state(s)
        bot.send_message(
            chat_id,
            f"✅ MEXC top-150 loaded: {len(symbols)} coins." if lang == "en" else f"✅ MEXC top-150 загружен: {len(symbols)} монет.",
            reply_markup=main_keyboard(),
        )
    except Exception as e:
        s = load_state()
        s["symbols"] = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
        save_state(s)
        bot.send_message(
            chat_id,
            f"⚠️ Could not load MEXC top-150: {esc(str(e)[:250])}. Fallback BTC/ETH saved." if lang == "en" else f"⚠️ Не удалось загрузить MEXC top-150: {esc(str(e)[:250])}. Сохранил fallback BTC/ETH.",
            reply_markup=main_keyboard(),
        )


def fetch_price_top(count: int) -> List[Tuple[str, float, float]]:
    """
    Возвращает [(symbol, price, change_pct)].
    1) Если есть CMC_API_KEY — берём CoinMarketCap top по market cap.
    2) Если ключа нет — берём CoinGecko public API, чтобы не ловить Binance 451.
    3) Если CoinGecko недоступен — fallback через выбранную биржу MEXC/BingX по quoteVolume.
    """
    count = max(1, min(100, int(count)))
    cmc_key = os.getenv("CMC_API_KEY", "").strip()

    if cmc_key:
        url = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest"
        params = {"start": "1", "limit": str(min(500, count + 50)), "convert": "USD"}
        headers = {"X-CMC_PRO_API_KEY": cmc_key, "Accept": "application/json"}
        r = requests.get(url, params=params, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json().get("data", [])
        out = []
        for item in data:
            sym = str(item.get("symbol", "?")).upper()
            if sym in STABLE_BASES:
                continue
            q = item.get("quote", {}).get("USD", {})
            out.append((sym, float(q.get("price") or 0), float(q.get("percent_change_24h") or 0)))
            if len(out) >= count:
                break
        return out

    # Public fallback без ключа: CoinGecko. Не используем Binance, чтобы не получать 451 по региону.
    try:
        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": str(min(250, count + 50)),
            "page": "1",
            "sparkline": "false",
            "price_change_percentage": "24h",
        }
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        rows = []
        for item in r.json():
            sym = str(item.get("symbol", "?")).upper()
            if sym in STABLE_BASES:
                continue
            rows.append((
                sym,
                float(item.get("current_price") or 0),
                float(item.get("price_change_percentage_24h") or 0),
            ))
            if len(rows) >= count:
                break
        if rows:
            return rows
    except Exception as e:
        print(f"CoinGecko price fallback error: {e}", flush=True)

    # Последний fallback: выбранная биржа MEXC/BingX через ccxt.
    s = load_state()
    exchange_name = str(s.get("exchange", "mexc")).lower()
    ex = make_exchange(exchange_name)
    markets = ex.load_markets()
    tickers = ex.fetch_tickers()
    rows = []
    for sym, m in markets.items():
        if m.get("quote", "").upper() != "USDT" or not m.get("active", True):
            continue
        base = str(m.get("base") or sym.split("/")[0]).upper()
        if base in STABLE_BASES:
            continue
        t = tickers.get(sym) or {}
        price = t.get("last") or t.get("close")
        pct = t.get("percentage")
        vol = t.get("quoteVolume")
        try:
            price = float(price or 0)
            pct = float(pct or 0)
            vol = float(vol or 0)
        except Exception:
            continue
        if price <= 0:
            continue
        rows.append((vol, base, price, pct))

    rows.sort(reverse=True, key=lambda x: x[0])
    return [(base, price, pct) for _, base, price, pct in rows[:count]]


def price_text(count: int) -> str:
    rows = fetch_price_top(count)
    source = "CoinMarketCap" if os.getenv("CMC_API_KEY") else "CoinGecko/биржа"
    lines = [f"💲 <b>Цена top-{len(rows)}</b> | {esc(source)}"]
    for base, price, pct in rows:
        marker = "🟢" if pct >= 0 else "🔴"
        lines.append(f"{marker} <b>{esc(base.lower())}</b> — <code>{price:.8g}</code> USD ({pct:+.2f}%)")
    return "\n".join(lines)



def signal_loop() -> None:
    while True:
        try:
            s = load_state()
            auto_chats = [x.strip() for x in os.getenv("AUTO_SIGNAL_CHAT_IDS", "").split(",") if x.strip()]
            if not auto_chats and s.get("admin_id"):
                auto_chats = [str(s["admin_id"])]
            need_scan = (s.get("auto_signals") or s.get("super_trade_enabled")) and within_enabled_sessions() and auto_chats
            if need_scan:
                symbols = list(s.get("symbols", []))[:MAX_SIGNAL_SCAN]
                for chat_id in auto_chats:
                    lines: List[str] = []
                    for i, symbol in enumerate(symbols, 1):
                        try:
                            sig = build_signal(symbol, with_chart=False)
                            if s.get("super_trade_enabled"):
                                send_super_alert(int(chat_id), sig)
                            if s.get("auto_signals") and sig["direction"] != "NEUTRAL" and passes_signal_threshold(sig, s):
                                lines.append(format_signal_brief(sig))
                                execute_trade_if_allowed(int(chat_id), sig)
                        except Exception as e:
                            print("auto signal error", symbol, e, flush=True)
                    if s.get("auto_signals") and lines:
                        # автосигналы тоже сортируем лучшими вверх приблизительно по тексту уже без пересчёта
                        send_text_chunks(int(chat_id), "📡 <b>AUTO ALL SIGNAL</b>\n\n", lines)
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
        # Дублируем обработку /start внутри общего handler, чтобы Telegram/TeleBot
        # точно назначал первого админа даже если команда попала сюда.
        if low in {"/start", "/help"}:
            return start(message)

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

        if low in {"⚙️ настройки", "⚙️ settings", "settings", "/settings"}:
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

        if low in {"♻️ сброс", "♻️ reset", "reset", "/reset"}:
            reset_to_default_profile(message.chat.id)
            return

        if low.startswith("🌐 language") or low in {"language", "язык", "lang"}:
            s["language"] = "ru" if s.get("language") == "en" else "en"
            save_state(s)
            bot.send_message(
                message.chat.id,
                "🌐 Language switched to English." if s["language"] == "en" else "🌐 Язык переключён на русский.",
                reply_markup=main_keyboard(),
            )
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

        if low.startswith("exchange ") or low.startswith("🏦 биржа") or low.startswith("🏦 exchange"):
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

        if low in {"📰 новости", "📰 news", "news", "/news"} or low.startswith("📰 новости") or low.startswith("📰 news"):
            s["news_enabled"] = not s.get("news_enabled", False)
            save_state(s)
            info = update_news_cache(force=True)
            safe_send_message(message.chat.id, format_news_panel(info, s["news_enabled"]), reply_markup=main_keyboard())
            return
        if low in {"news on", "news off", "news refresh"}:
            if low == "news refresh":
                pass
            else:
                s["news_enabled"] = low.endswith("on")
                save_state(s)
            info = update_news_cache(force=True)
            safe_send_message(message.chat.id, format_news_panel(info, s["news_enabled"]), reply_markup=main_keyboard())
            return

        if low in {"auto on", "auto off"}:
            s["auto_signals"] = low.endswith("on")
            save_state(s)
            bot.send_message(message.chat.id, f"Автосигналы: {'ВКЛ' if s['auto_signals'] else 'ВЫКЛ'}", reply_markup=main_keyboard())
            return

        if low.startswith("⚡ автоторговля") or low.startswith("⚡ autotrade") or low in {"autotrade"}:
            s["autotrade"] = not s.get("autotrade", False)
            save_state(s)
            key, secret = get_api_credentials(s.get("exchange", "mexc"))
            note = "" if key and secret else "\n⚠️ API ключи текущей биржи не заданы. Для LIVE используй: api mexc / api bingx."
            bot.send_message(message.chat.id, f"Автоторговля: {'ВКЛ' if s['autotrade'] else 'ВЫКЛ'} ({'LIVE' if LIVE_TRADING_ENABLED else 'PAPER'}){note}", reply_markup=main_keyboard())
            return

        if low.startswith("🧠 улучшения") or low.startswith("🧠 improvement") or low in {"improve", "improvement"}:
            s["adaptive_improvement"] = not s.get("adaptive_improvement", False)
            save_state(s)
            bot.send_message(message.chat.id, f"Улучшения: {'ВКЛ' if s['adaptive_improvement'] else 'ВЫКЛ'}\nЕсли последние paper-сделки в минусе, auto_ai/best будет менять профиль по мини-бэктесту.", reply_markup=main_keyboard())
            return

        if low.startswith("🤖 генератор") or low.startswith("🤖 generator") or low in {"generator"}:
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

        if low.startswith("🎯 тейк макс") or low.startswith("🎯 тейк") or low.startswith("🎯 max take") or low == "take":
            s["take_enabled"] = not s.get("take_enabled", True)
            save_state(s)
            safe_send_message(message.chat.id, f"Тейк макс: {'ВКЛ' if s['take_enabled'] else 'ВЫКЛ'}\nДиапазон зависит от старшего TF: 1h &lt; 4h &lt; 1d &lt; 1w. Команда: take 0.5 4", reply_markup=main_keyboard())
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
            bot.send_message(message.chat.id, "Тейк макс обновлён.", reply_markup=main_keyboard())
            return

        if low.startswith("💼 сделки") or low.startswith("💼 trades") or low == "trades":
            bot.send_message(message.chat.id, f"💼 Лимит сделок: <b>{s['daily_trades_limit']}</b> в сутки.\nФормат: <code>trades 10</code>", reply_markup=main_keyboard())
            return
        if low.startswith("trades "):
            s["daily_trades_limit"] = max(1, min(1000, int(low.split(maxsplit=1)[1])))
            save_state(s)
            bot.send_message(message.chat.id, f"Сделок/сутки: {s['daily_trades_limit']}", reply_markup=main_keyboard())
            return

        if low.startswith("🌏 азия") or low.startswith("🌏 asia") or low == "asia":
            s["session_asia"] = not s.get("session_asia", False)
            save_state(s)
            bot.send_message(message.chat.id, f"Азия 03:00 МСК: {'ВКЛ' if s['session_asia'] else 'ВЫКЛ'}", reply_markup=main_keyboard())
            return
        if low.startswith("🇺🇸 америка") or low.startswith("🇺🇸 america") or low == "america":
            s["session_america"] = not s.get("session_america", False)
            save_state(s)
            bot.send_message(message.chat.id, f"Америка 16:30 МСК: {'ВКЛ' if s['session_america'] else 'ВЫКЛ'}", reply_markup=main_keyboard())
            return

        if low.startswith("📨") or low in {"all signal", "one signal", "10 signal", "top signal", "top10 signal"}:
            if low == "all signal":
                s["signal_output_mode"] = "all"
            elif low == "one signal":
                s["signal_output_mode"] = "one"
            elif low in {"10 signal", "top signal", "top10 signal"}:
                s["signal_output_mode"] = "top10"
            else:
                cycle = ["all", "top10", "one"]
                cur = s.get("signal_output_mode", "top10")
                s["signal_output_mode"] = cycle[(cycle.index(cur) + 1) % len(cycle)] if cur in cycle else "top10"
            save_state(s)
            label = {"all": "all signal — кратко одним сообщением", "one": "one signal — подробно отдельными сообщениями", "top10": "10 signal — только 10 лучших по успешности"}.get(s["signal_output_mode"], "all signal")
            bot.send_message(message.chat.id, f"Режим сигналов: <b>{label}</b>", reply_markup=main_keyboard())
            return

        if low.startswith("🎚 порог") or low.startswith("🎚 threshold") or low.startswith("порог") or low.startswith("threshold"):
            mt_thr = re.search(r"(60|70|75|80|85|90|95)", low)
            if mt_thr:
                s["signal_threshold_pct"] = int(mt_thr.group(1))
            else:
                cur = int(s.get("signal_threshold_pct", 90))
                idx = SIGNAL_THRESHOLDS.index(cur) if cur in SIGNAL_THRESHOLDS else 0
                s["signal_threshold_pct"] = SIGNAL_THRESHOLDS[(idx + 1) % len(SIGNAL_THRESHOLDS)]
            save_state(s)
            bot.send_message(message.chat.id, f"🎚 Порог сигналов: <b>{s['signal_threshold_pct']}%</b>\nВ ALL/ONE/10 SIGNAL будут показаны только монеты с расчётной успешностью от этого значения.", reply_markup=main_keyboard())
            return

        if low.startswith("💲 цена") or low.startswith("💲 price") or low in {"price", "/price", "цена"}:
            safe_send_message(message.chat.id, price_text(int(load_state().get("price_count", 5))), reply_markup=main_keyboard())
            return
        mt_price = re.match(r"^price\s+(?:top[-\s]?)?(\d+)$", low)
        if mt_price:
            s["price_count"] = max(1, min(100, int(mt_price.group(1))))
            save_state(s)
            safe_send_message(message.chat.id, f"💲 Количество монет для кнопки Цена: top-{s['price_count']}", reply_markup=main_keyboard())
            return

        if low.startswith("🚨 супер сделка") or low.startswith("🚨 super deal") or low in {"super", "super deal", "супер сделка"}:
            s["super_trade_enabled"] = not s.get("super_trade_enabled", False)
            save_state(s)
            safe_send_message(message.chat.id, f"🚨 Супер сделка: {'ВКЛ' if s['super_trade_enabled'] else 'ВЫКЛ'}\nПри setup с расчётной проходимостью 95–97% и score от 7 бот пришлёт срочное уведомление.", reply_markup=main_keyboard())
            return

        if low in {"📊 профит", "📊 profit", "profit", "/profit"}:
            safe_send_message(message.chat.id, profit_text(), reply_markup=main_keyboard())
            return
        if low in {"⛔ закрыть всё", "⛔ close all", "close all", "закрыть всё"}:
            close_all_trades(message.chat.id)
            return

        # Если в чат отправить просто название монеты — это как signal btc.
        # Не перехватываем служебные слова и длинные фразы.
        service_words = {
            "price", "signal", "settings", "ping", "news", "take", "trades", "auto", "api",
            "reset", "delete", "top", "mexc", "bingx", "threshold", "порог", "цена", "новости",
            "сделки", "сброс", "биржа", "super", "profit", "close", "language", "lang", "язык"
        }
        if re.fullmatch(r"[a-zA-Z0-9]{2,12}", text) and low not in service_words:
            send_signal(message.chat.id, text)
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
