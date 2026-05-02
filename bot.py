from __future__ import annotations
import base64, hashlib, json, math, os, re, threading, time, traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import ccxt
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import requests
import telebot
from telebot import types
from cryptography.fernet import Fernet, InvalidToken

VERSION = '0.01'
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN') or os.getenv('BOT_TOKEN')
if not TOKEN:
    raise RuntimeError('TELEGRAM_BOT_TOKEN не установлен')

DATA_DIR = Path(os.getenv('RAILWAY_VOLUME_MOUNT_PATH') or os.getenv('DATA_DIR') or '.').resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / 'state.json'
TRADES_FILE = DATA_DIR / 'trades.json'
API_KEYS_FILE = DATA_DIR / 'api_keys.enc'
SECRET_KEY_FILE = DATA_DIR / 'bot_secret.key'
CHART_DIR = DATA_DIR / 'charts'
CHART_DIR.mkdir(parents=True, exist_ok=True)

SIGNAL_LOOP_SECONDS = int(os.getenv('SIGNAL_LOOP_SECONDS', '300'))
LIVE_TRADING_ENABLED = os.getenv('ALLOW_LIVE_TRADING', 'false').lower() == 'true'
DEFAULT_NOTIONAL_USDT = float(os.getenv('ORDER_AMOUNT_USDT', '10'))
MSK_OFFSET = 3

bot = telebot.TeleBot(TOKEN, parse_mode='HTML')
start_time = time.time()
state_lock = threading.RLock()

DEFAULT_STATE: Dict[str, Any] = {
    'exchange': 'mexc',
    'symbols': ['BTC/USDT:USDT', 'ETH/USDT:USDT'],
    'lower_tf': '15m',
    'higher_tf': '1h',
    'auto_signals': False,
    'autotrade': False,
    'adaptive_improvement': False,
    'news_enabled': False,
    'take_enabled': True,
    'take_min_profit_pct': 0.3,
    'take_max_profit_pct': 3.0,
    'analysis_mode': 'multi',
    'strategy_profile': 'multi',
    'daily_trades_limit': 5,
    'daily_trades_count': 0,
    'daily_trades_date': '',
    'session_asia': False,
    'session_america': False,
    'paper_trades': [],
    'admin_id': None,
}

def load_json(path: Path, default: Any) -> Any:
    if not path.exists(): return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default

def save_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)

def load_state() -> Dict[str, Any]:
    with state_lock:
        data = load_json(STATE_FILE, {})
        merged = DEFAULT_STATE.copy()
        if isinstance(data, dict): merged.update(data)
        return merged

def save_state(state: Dict[str, Any]) -> None:
    with state_lock: save_json(STATE_FILE, state)

def reset_state(preserve_admin: bool=True) -> Dict[str, Any]:
    old = load_state()
    state = DEFAULT_STATE.copy()
    if preserve_admin:
        state['admin_id'] = old.get('admin_id')
    save_state(state)
    return state

def load_trades() -> List[Dict[str, Any]]:
    data = load_json(TRADES_FILE, [])
    return data if isinstance(data, list) else []

def save_trades(trades: List[Dict[str, Any]]) -> None:
    save_json(TRADES_FILE, trades[-5000:])

def esc(x: Any) -> str:
    import html
    return html.escape(str(x), quote=False)

def main_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add('📡 Signal', '⚙️ Настройки')
    kb.add('📈 MEXC top-100', '📈 top-200')
    kb.add('📰 Новости', '🤖 Генератор анализа')
    kb.add('🧠 Улучшения', '💼 Сделки')
    kb.add('🌏 Азия', '🇺🇸 Америка')
    kb.add('🎯 Тейк', '⚡ Автоторговля')
    kb.add('🔑 API ключи')
    kb.add('🏓 Ping', '♻️ Сброс')
    kb.add('🗑 delete all')
    return kb

def settings_text() -> str:
    s = load_state()
    return (f"⚙️ <b>Настройки v{VERSION}</b>\n"
        f"Биржа: <b>{esc(s['exchange'])}</b>\n"
        f"Монет: <b>{len(s['symbols'])}</b>\n"
        f"TF: <b>{esc(s['lower_tf'])}</b> / <b>{esc(s['higher_tf'])}</b>\n"
        f"Автосигналы: <b>{'ВКЛ' if s['auto_signals'] else 'ВЫКЛ'}</b>\n"
        f"Автоторговля: <b>{'ВКЛ' if s['autotrade'] else 'ВЫКЛ'}</b> ({'LIVE' if LIVE_TRADING_ENABLED else 'PAPER'})\n"
        f"Улучшения: <b>{'ВКЛ' if s['adaptive_improvement'] else 'ВЫКЛ'}</b>\n"
        f"Новости: <b>{'ВКЛ' if s['news_enabled'] else 'ВЫКЛ'}</b>\n"
        f"Тейк: <b>{'ВКЛ' if s['take_enabled'] else 'ВЫКЛ'}</b>, {s['take_min_profit_pct']}% / {s['take_max_profit_pct']}%\n"
        f"Анализ: <b>{esc(s['analysis_mode'])}</b>, профиль: <b>{esc(s['strategy_profile'])}</b>\n"
        f"Сделок/сутки: <b>{s['daily_trades_limit']}</b>, сегодня: <b>{s['daily_trades_count']}</b>\n"
        f"Азия 03:00 МСК: <b>{'ВКЛ' if s['session_asia'] else 'ВЫКЛ'}</b>\n"
        f"Америка 16:30 МСК: <b>{'ВКЛ' if s['session_america'] else 'ВЫКЛ'}</b>\n"
        f"Админ: <b>{esc(s.get('admin_id') or 'не назначен')}</b>\n"
        f"API: <b>{esc(api_status_short())}</b>\n\n"
        "<code>signal BTC/USDT</code>\n<code>new SOL/USDT</code>\n<code>delete SOL/USDT</code>\n"
        "<code>delete all</code>\n<code>mexc top-100</code> / <code>top-200</code>\n"
        "<code>exchange mexc</code> / <code>exchange bingx</code>\n<code>tf 15m 1h</code>\n"
        "<code>auto on</code> / <code>auto off</code>\n<code>trades 10</code>\n<code>take 0.5 3</code>\n"
        "<code>api mexc</code> / <code>api bingx</code> / <code>api status</code> / <code>api delete mexc</code>")


def get_cipher() -> Fernet:
    """Локальное шифрование API-ключей. Ключ хранится в Railway Volume."""
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
        data = json.loads(get_cipher().decrypt(raw).decode('utf-8'))
        return data if isinstance(data, dict) else {}
    except (InvalidToken, Exception) as e:
        print(f'api keys decrypt/load error: {e}', flush=True)
        return {}

def save_api_keys(data: Dict[str, Dict[str, str]]) -> None:
    API_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False).encode('utf-8')
    tmp = API_KEYS_FILE.with_suffix(API_KEYS_FILE.suffix + '.tmp')
    tmp.write_bytes(get_cipher().encrypt(payload))
    tmp.replace(API_KEYS_FILE)

def set_api_credentials(exchange: str, api_key: str, api_secret: str) -> None:
    exchange = exchange.lower().strip()
    if exchange not in {'mexc', 'bingx'}:
        raise ValueError('Поддерживаются только mexc или bingx')
    data = load_api_keys()
    data[exchange] = {'api_key': api_key.strip(), 'api_secret': api_secret.strip(), 'updated_at': str(int(time.time()))}
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
    key = data.get('api_key') or os.getenv(f'{exchange.upper()}_API_KEY', '')
    secret = data.get('api_secret') or os.getenv(f'{exchange.upper()}_API_SECRET', '')
    return key.strip(), secret.strip()

def mask_secret(value: str) -> str:
    if not value:
        return 'нет'
    if len(value) <= 8:
        return value[:2] + '***'
    return value[:4] + '…' + value[-4:]

def api_status_short() -> str:
    parts = []
    data = load_api_keys()
    for ex in ['mexc', 'bingx']:
        key, secret = get_api_credentials(ex)
        source = 'chat' if ex in data else 'env' if key and secret else 'нет'
        parts.append(f'{ex.upper()}={source}')
    return ', '.join(parts)

def api_status_text() -> str:
    lines = ['🔑 <b>API ключи</b>']
    data = load_api_keys()
    for ex in ['mexc', 'bingx']:
        key, secret = get_api_credentials(ex)
        source = 'загружены через чат' if ex in data else 'из Railway Variables' if key and secret else 'не заданы'
        lines.append(f"{ex.upper()}: <b>{source}</b> | key: <code>{esc(mask_secret(key))}</code> | secret: <code>{esc(mask_secret(secret))}</code>")
    lines.append('')
    lines.append('Команды:')
    lines.append('<code>api mexc</code> — добавить/заменить ключи MEXC')
    lines.append('<code>api bingx</code> — добавить/заменить ключи BingX')
    lines.append('<code>api status</code> — статус')
    lines.append('<code>api delete mexc</code> — удалить ключи')
    lines.append('<code>cancel</code> — отменить ввод')
    lines.append('')
    lines.append('⚠️ Для безопасности бот удаляет сообщения с ключами, если Telegram разрешит удалить их.')
    return '\n'.join(lines)

api_input_sessions: Dict[int, Dict[str, str]] = {}

def is_admin(chat_id: int) -> bool:
    s = load_state()
    return str(s.get('admin_id') or '') == str(chat_id)

def ensure_admin_claim(chat_id: int) -> Tuple[bool, str]:
    s = load_state()
    if not s.get('admin_id'):
        s['admin_id'] = int(chat_id)
        save_state(s)
        return True, '✅ Ты назначен админом, потому что первым отправил /start.'
    if str(s.get('admin_id')) == str(chat_id):
        return True, '✅ Ты админ.'
    return False, '⛔ Админ уже назначен. Управление ботом закрыто для этого чата.'

def admin_only(message) -> bool:
    if is_admin(message.chat.id):
        return True
    bot.send_message(message.chat.id, '⛔ Доступ запрещён. Админом становится первый чат, который отправил /start.', reply_markup=main_keyboard())
    return False

def safe_delete_user_message(message) -> None:
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass

def make_exchange(name: Optional[str]=None, private: bool=False):
    s = load_state(); exchange_name = (name or s['exchange']).lower()
    if exchange_name not in {'mexc','bingx'}: raise ValueError('Поддерживаются mexc или bingx')
    cls = getattr(ccxt, exchange_name)
    cfg: Dict[str, Any] = {'enableRateLimit': True, 'timeout': 20000, 'options': {'defaultType': 'swap'}}
    if private:
        key, secret = get_api_credentials(exchange_name)
        if not key or not secret:
            raise RuntimeError(f'{exchange_name.upper()} API ключи не заданы. Используй команду api {exchange_name}')
        cfg.update({'apiKey': key, 'secret': secret})
    return cls(cfg)

def normalize_symbol(raw: str) -> str:
    raw = raw.strip().upper().replace('-', '/').replace('_','/')
    if '/' not in raw: raw = raw + '/USDT'
    if raw.endswith('/USDT') and ':USDT' not in raw: raw += ':USDT'
    return raw

def resolve_symbol(exchange, symbol: str) -> str:
    target = normalize_symbol(symbol); markets = exchange.load_markets()
    if target in markets: return target
    base = target.split('/')[0]
    for sym, m in markets.items():
        if m.get('base','').upper()==base and m.get('quote','').upper()=='USDT' and (m.get('swap') or m.get('future') or m.get('contract')):
            return sym
    spot = f'{base}/USDT'
    if spot in markets: return spot
    raise ValueError(f'Символ не найден: {symbol}')

def fetch_top_symbols(exchange_name: str, limit: int) -> List[str]:
    ex = make_exchange(exchange_name); markets = ex.load_markets(); tickers = ex.fetch_tickers(); rows=[]
    for sym, m in markets.items():
        if not (m.get('swap') or m.get('future') or m.get('contract')): continue
        if m.get('quote','').upper()!='USDT' or not m.get('active', True): continue
        t = tickers.get(sym) or {}; vol = t.get('quoteVolume')
        if vol is None: vol = float(t.get('baseVolume') or 0) * float(t.get('last') or t.get('close') or 0)
        try: vol = float(vol or 0)
        except Exception: vol = 0
        rows.append((vol, sym))
    rows.sort(reverse=True, key=lambda x: x[0])
    return [sym for _, sym in rows[:limit]]

def ema(v: np.ndarray, period: int) -> np.ndarray:
    if len(v)==0: return v
    alpha = 2/(period+1); out = np.empty_like(v, dtype=float); out[0]=v[0]
    for i in range(1,len(v)): out[i]=alpha*v[i]+(1-alpha)*out[i-1]
    return out

def rsi(v: np.ndarray, period:int=14)->np.ndarray:
    if len(v)<period+1: return np.full_like(v,50.0,dtype=float)
    d=np.diff(v); out=np.full_like(v,50.0,dtype=float); up=max(d[:period].mean(),0); down=max(-d[:period].mean(),0)
    for i in range(period+1,len(v)):
        delta=d[i-1]; up=(up*(period-1)+max(delta,0))/period; down=(down*(period-1)+max(-delta,0))/period
        out[i]=100 if down==0 else 100-100/(1+up/down)
    return out

def atr(h,l,c,period:int=14)->np.ndarray:
    tr=[h[0]-l[0]]
    for i in range(1,len(c)): tr.append(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])))
    return ema(np.array(tr,dtype=float), period)

def macd(v):
    line=ema(v,12)-ema(v,26); sig=ema(line,9); return line, sig, line-sig

def linreg_line(v: np.ndarray, n:int=60):
    sample=v[-min(n,len(v)):]; x=np.arange(len(sample))
    if len(sample)<2: return 0.0, float(v[-1]), float(v[-1])
    slope, intercept=np.polyfit(x, sample, 1); return float(slope), float(intercept), float(slope*(len(sample)-1)+intercept)

def fetch_ohlcv(exchange, symbol: str, timeframe: str, limit:int=220):
    data = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    if len(data)<60: raise RuntimeError(f'Недостаточно свечей: {symbol} {timeframe}')
    arr=np.array(data,dtype=float)
    return {'ts':arr[:,0], 'open':arr[:,1], 'high':arr[:,2], 'low':arr[:,3], 'close':arr[:,4], 'volume':arr[:,5]}

def features(o):
    close=o['close']; high=o['high']; low=o['low']; vol=o['volume']; macd_line, macd_sig, macd_hist=macd(close); slope,start,end=linreg_line(close,60)
    look=min(80,len(close)); atr14=atr(high,low,close,14); ema20=ema(close,20); ema50=ema(close,50); ema200=ema(close,200)
    return {'price':float(close[-1]), 'ema20':float(ema20[-1]), 'ema50':float(ema50[-1]), 'ema200':float(ema200[-1]),
            'rsi':float(rsi(close,14)[-1]), 'atr':float(atr14[-1]), 'macd_hist':float(macd_hist[-1]),
            'support':float(np.min(low[-look:])), 'resistance':float(np.max(high[-look:])), 'slope':float(slope),
            'trend_start':start, 'trend_end':end, 'vol':float(vol[-1]), 'vol_sma':float(np.mean(vol[-30:]))}

def fetch_crypto_news(limit:int=8)->List[str]:
    urls=['https://www.coindesk.com/arc/outboundfeeds/rss/','https://cointelegraph.com/rss']; out=[]
    for url in urls:
        try:
            r=requests.get(url, timeout=8, headers={'User-Agent':'Mozilla/5.0'})
            if r.status_code!=200: continue
            titles=re.findall(r'<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>', r.text, re.I|re.S)
            for a,b in titles:
                t=re.sub(r'\s+',' ',(a or b).strip())
                if t and 'CoinDesk' not in t and 'Cointelegraph' not in t and t not in out: out.append(t)
        except Exception: pass
    return out[:limit]

def news_bias(headlines: List[str])->int:
    text=' '.join(headlines).lower(); bull=['approval','etf','bull','rally','surge','adoption','rate cut','trump','musk']; bear=['hack','lawsuit','ban','crackdown','selloff','liquidation','exploit','rate hike']
    return max(-5,min(5,sum(text.count(w) for w in bull)-sum(text.count(w) for w in bear)))

def weights(profile:str):
    return {'multi':(1,1,1,1),'trend':(1.5,1.1,.6,1),'mean_reversion':(.7,.8,1.6,.7),'breakout':(1,1.2,.5,1.7),'max_profit':(1.2,1.3,.5,1.6),'auto_ai':(1.1,1.2,.8,1.2)}.get(profile,(1,1,1,1))

def adaptive_profile():
    s=load_state()
    if not s.get('adaptive_improvement'): return s.get('strategy_profile','multi')
    closed=[t for t in load_trades() if 'pnl_pct' in t][-30:]
    if len(closed)<10: return s.get('strategy_profile','multi')
    avg=sum(float(t.get('pnl_pct',0)) for t in closed)/len(closed)
    if avg<0:
        cycle=['multi','trend','breakout','mean_reversion','max_profit']; cur=s.get('strategy_profile','multi')
        s['strategy_profile']=cycle[(cycle.index(cur)+1)%len(cycle)] if cur in cycle else 'multi'; save_state(s)
    return s.get('strategy_profile','multi')

def build_signal(symbol: str, with_chart=True):
    s=load_state(); ex=make_exchange(s['exchange']); symbol=resolve_symbol(ex, symbol)
    lo=fetch_ohlcv(ex, symbol, s['lower_tf']); hi=fetch_ohlcv(ex, symbol, s['higher_tf']); lf=features(lo); hf=features(hi)
    profile=adaptive_profile(); mode=s.get('analysis_mode','multi'); tw,mw,meanw,bw=weights(mode if mode in {'max_profit','auto_ai'} else profile)
    long=short=0.0; reasons=[]
    def L(p,r):
        nonlocal long; long+=p; reasons.append('🟢 '+r)
    def S(p,r):
        nonlocal short; short+=p; reasons.append('🔴 '+r)
    L(18*tw,'HTF EMA20>EMA50') if hf['ema20']>hf['ema50'] else S(18*tw,'HTF EMA20<EMA50')
    L(12*tw,'Цена выше EMA200 старший TF') if hf['price']>hf['ema200'] else S(12*tw,'Цена ниже EMA200 старший TF')
    L(12*tw,'LTF EMA20>EMA50') if lf['ema20']>lf['ema50'] else S(12*tw,'LTF EMA20<EMA50')
    L(10*mw,'MACD положительный') if lf['macd_hist']>0 else S(10*mw,'MACD отрицательный')
    if lf['rsi']<32: L(10*meanw, f"RSI перепроданность {lf['rsi']:.1f}")
    elif lf['rsi']>68: S(10*meanw, f"RSI перекупленность {lf['rsi']:.1f}")
    elif lf['rsi']>55: L(5*mw, f"RSI импульс {lf['rsi']:.1f}")
    elif lf['rsi']<45: S(5*mw, f"RSI слабость {lf['rsi']:.1f}")
    if lf['price']>lf['resistance']-.25*lf['atr']: L(8*bw,'Цена у сопротивления/пробой')
    if lf['price']<lf['support']+.25*lf['atr']: S(8*bw,'Цена у поддержки/пробой вниз')
    L(7*tw,'Наклонный уровень вверх') if lf['slope']>0 else S(7*tw,'Наклонный уровень вниз')
    if lf['vol']>lf['vol_sma']*1.2: L(5*mw,'Объём выше среднего') if lf['price']>lf['ema20'] else S(5*mw,'Объём выше среднего вниз')
    headlines=[]
    if s.get('news_enabled'):
        headlines=fetch_crypto_news(6); nb=news_bias(headlines); L(nb,'Новости позитивные') if nb>0 else S(abs(nb),'Новости негативные') if nb<0 else None
    total=max(long+short,1); long_pct=round(long/total*100,1); short_pct=round(short/total*100,1)
    direction='LONG' if long_pct>=55 else 'SHORT' if short_pct>=55 else 'NEUTRAL'
    entry=lf['price']; av=max(lf['atr'], entry*.002)
    if direction=='SHORT': stop=entry+1.5*av; tp=[entry-av, entry-2*av, entry-3*av]
    else: stop=entry-1.5*av; tp=[entry+av, entry+2*av, entry+3*av]
    if s.get('take_enabled'):
        mn=float(s.get('take_min_profit_pct',.3))/100; mx=float(s.get('take_max_profit_pct',3))/100
        if direction=='SHORT': tp=[min(tp[0],entry*(1-mn)), (entry*(1-mn)+entry*(1-mx))/2, max(tp[2],entry*(1-mx))]
        else: tp=[max(tp[0],entry*(1+mn)), (entry*(1+mn)+entry*(1+mx))/2, min(tp[2],entry*(1+mx))]
    chart=draw_chart(symbol, lo, direction, long_pct, short_pct, entry, stop, tp) if with_chart else None
    return {'symbol':symbol,'exchange':s['exchange'],'direction':direction,'long_pct':long_pct,'short_pct':short_pct,'entry':entry,'stop':stop,'tp':tp,'price':entry,'rsi':lf['rsi'],'atr':lf['atr'],'support':lf['support'],'resistance':lf['resistance'],'profile':profile,'mode':mode,'reasons':reasons[:8],'headlines':headlines[:5],'chart_path':chart}

def draw_chart(symbol,o,direction,long_pct,short_pct,entry,stop,tp):
    close=o['close'][-140:]; x=np.arange(len(close)); e20=ema(close,20); e50=ema(close,50); slope,st,en=linreg_line(close,60); n=min(60,len(close)); tx=np.arange(len(close)-n,len(close)); trend=np.linspace(st,en,n)
    plt.figure(figsize=(14,8), dpi=150); plt.plot(x,close,label='Price'); plt.plot(x,e20,label='EMA20'); plt.plot(x,e50,label='EMA50'); plt.plot(tx,trend,'--',label='Trendline')
    plt.axhline(entry,label=f'Entry {entry:.6g}'); plt.axhline(stop,linestyle='--',label=f'SL {stop:.6g}')
    for i,t in enumerate(tp,1): plt.axhline(t,linestyle=':',label=f'TP{i} {t:.6g}')
    plt.title(f'{symbol} | {direction} | LONG {long_pct}% / SHORT {short_pct}%'); plt.grid(True,alpha=.25); plt.legend(fontsize=8); plt.tight_layout()
    p=CHART_DIR/(re.sub(r'[^A-Za-z0-9_.-]+','_',symbol)+f'_{int(time.time())}.png'); plt.savefig(p); plt.close(); return str(p)

def format_signal(sig):
    tp=sig['tp']; reasons='\n'.join('• '+esc(r) for r in sig['reasons']); news=''
    if sig.get('headlines'): news='\n\n📰 <b>Новости:</b>\n'+'\n'.join('• '+esc(h) for h in sig['headlines'][:3])
    return (f"📡 <b>Signal {esc(sig['symbol'])}</b>\nБиржа: <b>{esc(sig['exchange'])}</b>\nНаправление: <b>{sig['direction']}</b>\nLong/Short: <b>{sig['long_pct']}%</b> / <b>{sig['short_pct']}%</b>\n\n"
            f"💵 Цена: <code>{sig['price']:.8g}</code>\n🎯 Entry: <code>{sig['entry']:.8g}</code>\n🛑 Stop-loss: <code>{sig['stop']:.8g}</code>\n✅ TP1: <code>{tp[0]:.8g}</code>\n✅ TP2: <code>{tp[1]:.8g}</code>\n✅ TP3: <code>{tp[2]:.8g}</code>\n\n"
            f"RSI: <b>{sig['rsi']:.1f}</b> | ATR: <b>{sig['atr']:.8g}</b>\nSupport/Resistance: <code>{sig['support']:.8g}</code> / <code>{sig['resistance']:.8g}</code>\nРежим: <b>{esc(sig['mode'])}</b>, профиль: <b>{esc(sig['profile'])}</b>\n\n🧩 <b>Факторы:</b>\n{reasons}{news}\n\n⚠️ Не финсовет. Риск обязателен.")

def today_msk(): return datetime.utcfromtimestamp(time.time()+MSK_OFFSET*3600).strftime('%Y-%m-%d')
def can_open_trade():
    s=load_state(); day=today_msk()
    if s.get('daily_trades_date')!=day: s['daily_trades_date']=day; s['daily_trades_count']=0; save_state(s)
    return int(s.get('daily_trades_count',0))<int(s.get('daily_trades_limit',5))
def mark_trade_used():
    s=load_state(); day=today_msk()
    if s.get('daily_trades_date')!=day: s['daily_trades_date']=day; s['daily_trades_count']=0
    s['daily_trades_count']=int(s.get('daily_trades_count',0))+1; save_state(s)

def execute_trade_if_allowed(chat_id:int, sig:Dict[str,Any]):
    s=load_state()
    if not s.get('autotrade') or sig['direction']=='NEUTRAL' or max(sig['long_pct'],sig['short_pct'])<62: return
    if not can_open_trade(): bot.send_message(chat_id,'💼 Лимит сделок на сутки исчерпан.',reply_markup=main_keyboard()); return
    side='buy' if sig['direction']=='LONG' else 'sell'; amount=DEFAULT_NOTIONAL_USDT/sig['entry']
    if not LIVE_TRADING_ENABLED:
        tr=load_trades(); tr.append({'ts':time.time(),'mode':'paper','exchange':sig['exchange'],'symbol':sig['symbol'],'direction':sig['direction'],'entry':sig['entry'],'stop':sig['stop'],'tp':sig['tp'],'notional_usdt':DEFAULT_NOTIONAL_USDT}); save_trades(tr); mark_trade_used()
        bot.send_message(chat_id, f"🧾 PAPER trade: {esc(sig['symbol'])} {sig['direction']} ~{DEFAULT_NOTIONAL_USDT} USDT. LIVE выключен.", reply_markup=main_keyboard()); return
    ex=make_exchange(sig['exchange'], private=True); order=ex.create_order(sig['symbol'],'market',side,amount); mark_trade_used(); bot.send_message(chat_id, f"⚡ LIVE order:\n<code>{esc(order)}</code>", reply_markup=main_keyboard())

def within_enabled_sessions():
    s=load_state()
    if not s.get('session_asia') and not s.get('session_america'): return True
    now=datetime.utcfromtimestamp(time.time()+MSK_OFFSET*3600); minutes=now.hour*60+now.minute; window=4*60
    return bool((s.get('session_asia') and 180<=minutes<=180+window) or (s.get('session_america') and 990<=minutes<=990+window))

def signal_loop():
    while True:
        try:
            s=load_state()
            auto_chats = [x.strip() for x in os.getenv('AUTO_SIGNAL_CHAT_IDS','').split(',') if x.strip()]
            if not auto_chats and s.get('admin_id'):
                auto_chats = [str(s['admin_id'])]
            if s.get('auto_signals') and within_enabled_sessions() and auto_chats:
                for symbol in s.get('symbols',[])[:30]:
                    try:
                        sig=build_signal(symbol, with_chart=False)
                        if sig['direction']!='NEUTRAL' and max(sig['long_pct'],sig['short_pct'])>=65:
                            for chat_id in auto_chats:
                                bot.send_message(int(chat_id), format_signal(sig), reply_markup=main_keyboard()); execute_trade_if_allowed(int(chat_id), sig)
                    except Exception as e: print('auto signal error', symbol, e, flush=True)
            time.sleep(SIGNAL_LOOP_SECONDS)
        except Exception as e: print('signal_loop error', e, flush=True); time.sleep(30)

@bot.message_handler(commands=['start','help'])
def start(message): bot.send_message(message.chat.id, "👋 <b>Crypto Futures Signal Bot v0.01</b>\n\nЖми 📡 Signal или отправь <code>signal BTC/USDT</code>.", reply_markup=main_keyboard())

@bot.message_handler(func=lambda m: True)
def handle(message):
    text=(message.text or '').strip(); low=text.lower().strip(); s=load_state()
    try:
        # Пошаговый ввод API-ключей через чат
        pending = api_input_sessions.get(message.chat.id)
        if pending:
            if low in {'cancel', 'отмена', '/cancel'}:
                api_input_sessions.pop(message.chat.id, None)
                safe_delete_user_message(message)
                bot.send_message(message.chat.id, '❌ Ввод API ключей отменён.', reply_markup=main_keyboard())
                return
            if not admin_only(message):
                api_input_sessions.pop(message.chat.id, None)
                return
            step = pending.get('step')
            exchange = pending.get('exchange', 'mexc')
            if step == 'api_key':
                pending['api_key'] = text.strip()
                pending['step'] = 'api_secret'
                safe_delete_user_message(message)
                bot.send_message(message.chat.id, f'🔐 {exchange.upper()}: теперь отправь API Secret. Сообщение с ключом я попытался удалить.', reply_markup=main_keyboard())
                return
            if step == 'api_secret':
                api_key = pending.get('api_key', '').strip()
                api_secret = text.strip()
                safe_delete_user_message(message)
                if not api_key or not api_secret:
                    api_input_sessions.pop(message.chat.id, None)
                    bot.send_message(message.chat.id, '❌ Пустой API Key или Secret. Повтори: api mexc / api bingx', reply_markup=main_keyboard())
                    return
                set_api_credentials(exchange, api_key, api_secret)
                api_input_sessions.pop(message.chat.id, None)
                bot.send_message(message.chat.id, f'✅ API ключи для {exchange.upper()} сохранены в зашифрованном файле. Key: <code>{esc(mask_secret(api_key))}</code>', reply_markup=main_keyboard())
                return

        # Первый /start назначает админа; остальные команды только для админа.
        if low not in {'/start', '/help'} and not is_admin(message.chat.id):
            bot.send_message(message.chat.id, '⛔ Сначала админ должен отправить /start. Админом становится первый чат.', reply_markup=main_keyboard())
            return
        if low in {'🏓 ping','ping','/ping'}:
            st=time.perf_counter(); bot.get_me(); ms=int((time.perf_counter()-st)*1000); up=int(time.time()-start_time)
            bot.send_message(message.chat.id, f"🏓 Ping: <b>{ms} ms</b>\n⏱ Uptime: <b>{up//3600}h {(up%3600)//60}m</b>\n🔢 Version: <b>{VERSION}</b>", reply_markup=main_keyboard()); return
        if low in {'⚙️ настройки','settings','/settings'}: bot.send_message(message.chat.id, settings_text(), reply_markup=main_keyboard()); return
        if low in {'🔑 api ключи', 'api', '/api'}:
            bot.send_message(message.chat.id, api_status_text(), reply_markup=main_keyboard()); return
        if low in {'api status', '/api_status'}:
            bot.send_message(message.chat.id, api_status_text(), reply_markup=main_keyboard()); return
        if low in {'api mexc', 'api bingx'}:
            exchange = low.split()[-1]
            api_input_sessions[message.chat.id] = {'exchange': exchange, 'step': 'api_key'}
            bot.send_message(message.chat.id, f'🔑 {exchange.upper()}: отправь API Key следующим сообщением. Для отмены: <code>cancel</code>\n\n⚠️ Создавай ключ без прав вывода средств.', reply_markup=main_keyboard()); return
        if low.startswith('api delete '):
            exchange = low.split(maxsplit=2)[2].strip().lower()
            if exchange not in {'mexc', 'bingx'}:
                bot.send_message(message.chat.id, 'Формат: api delete mexc или api delete bingx', reply_markup=main_keyboard()); return
            existed = delete_api_credentials(exchange)
            bot.send_message(message.chat.id, f"{'✅ Удалены' if existed else 'ℹ️ Не были сохранены'} ключи {exchange.upper()} из chat-хранилища.", reply_markup=main_keyboard()); return
        if low in {'♻️ сброс','reset','/reset'}: reset_state(); bot.send_message(message.chat.id,'♻️ Настройки сброшены.',reply_markup=main_keyboard()); return
        if low in {'📡 signal','signal','/signal'}: send_signal(message.chat.id, s.get('symbols',['BTC/USDT:USDT'])[0]); return
        if low.startswith('signal '): send_signal(message.chat.id, text.split(maxsplit=1)[1]); return
        if low.startswith('exchange '):
            ex=low.split(maxsplit=1)[1].strip();
            if ex not in {'mexc','bingx'}: bot.send_message(message.chat.id,'Биржа: mexc или bingx',reply_markup=main_keyboard()); return
            s['exchange']=ex; save_state(s); bot.send_message(message.chat.id,f'Биржа изменена: {ex}',reply_markup=main_keyboard()); return
        if low.startswith('tf '):
            p=low.split();
            if len(p)!=3: bot.send_message(message.chat.id,'Формат: tf 15m 1h',reply_markup=main_keyboard()); return
            s['lower_tf']=p[1]; s['higher_tf']=p[2]; save_state(s); bot.send_message(message.chat.id,f"TF: {p[1]} / {p[2]}",reply_markup=main_keyboard()); return
        if low in {'📈 mexc top-100','mexc top-100','/mexc_top_100'}: load_top(message.chat.id,'mexc',100); return
        mt=re.match(r'^(?:📈\s*)?top[-\s]?(\d+)$', low)
        if mt: load_top(message.chat.id, s['exchange'], max(1,min(1000,int(mt.group(1))))); return
        if low.startswith('new '):
            sym=normalize_symbol(text.split(maxsplit=1)[1]);
            if sym not in s['symbols']: s['symbols'].append(sym)
            save_state(s); bot.send_message(message.chat.id,f'Добавлено: {esc(sym)}',reply_markup=main_keyboard()); return
        if low in {'🗑 delete all','delete all'}: s['symbols']=[]; save_state(s); bot.send_message(message.chat.id,'Список очищен.',reply_markup=main_keyboard()); return
        if low.startswith('delete '):
            sym=normalize_symbol(text.split(maxsplit=1)[1]); before=len(s['symbols']); s['symbols']=[x for x in s['symbols'] if x.upper()!=sym.upper()]; save_state(s); bot.send_message(message.chat.id,f"Удалено: {before-len(s['symbols'])}",reply_markup=main_keyboard()); return
        if low in {'📰 новости','news','/news'}:
            h=fetch_crypto_news(10); bot.send_message(message.chat.id, '📰 <b>Crypto news</b>\n'+'\n'.join('• '+esc(x) for x in h) if h else 'Новости не получены.', reply_markup=main_keyboard()); return
        if low in {'news on','news off'}: s['news_enabled']=low.endswith('on'); save_state(s); bot.send_message(message.chat.id,f"Новости: {'ВКЛ' if s['news_enabled'] else 'ВЫКЛ'}",reply_markup=main_keyboard()); return
        if low in {'auto on','auto off'}: s['auto_signals']=low.endswith('on'); save_state(s); bot.send_message(message.chat.id,f"Автосигналы: {'ВКЛ' if s['auto_signals'] else 'ВЫКЛ'}",reply_markup=main_keyboard()); return
        if low in {'⚡ автоторговля','autotrade'}:
            s['autotrade']=not s.get('autotrade',False); save_state(s)
            key, secret = get_api_credentials(s.get('exchange','mexc'))
            note = '' if key and secret else '\n⚠️ API ключи текущей биржи не заданы. Для LIVE используй: api mexc / api bingx.'
            bot.send_message(message.chat.id,f"Автоторговля: {'ВКЛ' if s['autotrade'] else 'ВЫКЛ'} ({'LIVE' if LIVE_TRADING_ENABLED else 'PAPER'}){note}",reply_markup=main_keyboard()); return
        if low in {'🧠 улучшения','improve'}: s['adaptive_improvement']=not s.get('adaptive_improvement',False); save_state(s); bot.send_message(message.chat.id,f"Улучшения: {'ВКЛ' if s['adaptive_improvement'] else 'ВЫКЛ'}",reply_markup=main_keyboard()); return
        if low in {'🤖 генератор анализа','generator'}:
            modes=['multi','best','max_profit','auto_ai']; cur=s.get('analysis_mode','multi'); nxt=modes[(modes.index(cur)+1)%len(modes)] if cur in modes else 'multi'; s['analysis_mode']=nxt; s['strategy_profile']='trend' if nxt=='best' else nxt; save_state(s); bot.send_message(message.chat.id,f'Генератор анализа: {nxt}',reply_markup=main_keyboard()); return
        if low in {'🎯 тейк','take'}: s['take_enabled']=not s.get('take_enabled',True); save_state(s); bot.send_message(message.chat.id,f"Тейк: {'ВКЛ' if s['take_enabled'] else 'ВЫКЛ'}",reply_markup=main_keyboard()); return
        if low.startswith('take '):
            p=low.split(); s['take_min_profit_pct']=float(p[1]); s['take_max_profit_pct']=float(p[2]); s['take_enabled']=True; save_state(s); bot.send_message(message.chat.id,'Тейк обновлён.',reply_markup=main_keyboard()); return
        if low in {'💼 сделки','trades'}: bot.send_message(message.chat.id,f"Лимит сделок: {s['daily_trades_limit']}\nФормат: trades 10",reply_markup=main_keyboard()); return
        if low.startswith('trades '): s['daily_trades_limit']=max(1,min(1000,int(low.split(maxsplit=1)[1]))); save_state(s); bot.send_message(message.chat.id,f"Сделок/сутки: {s['daily_trades_limit']}",reply_markup=main_keyboard()); return
        if low in {'🌏 азия','asia'}: s['session_asia']=not s.get('session_asia',False); save_state(s); bot.send_message(message.chat.id,f"Азия: {'ВКЛ' if s['session_asia'] else 'ВЫКЛ'}",reply_markup=main_keyboard()); return
        if low in {'🇺🇸 америка','america'}: s['session_america']=not s.get('session_america',False); save_state(s); bot.send_message(message.chat.id,f"Америка: {'ВКЛ' if s['session_america'] else 'ВЫКЛ'}",reply_markup=main_keyboard()); return
        bot.send_message(message.chat.id,'Команда не распознана. Открой /settings.',reply_markup=main_keyboard())
    except Exception as e:
        print(traceback.format_exc(), flush=True); bot.send_message(message.chat.id, f'❌ Ошибка: {esc(str(e)[:500])}', reply_markup=main_keyboard())

def send_signal(chat_id:int, symbol:str):
    bot.send_message(chat_id, f'⏳ Анализирую {esc(symbol)}...', reply_markup=main_keyboard()); sig=build_signal(symbol, True); caption=format_signal(sig); p=sig.get('chart_path')
    if p and Path(p).exists():
        with open(p,'rb') as f: bot.send_photo(chat_id, f, caption=caption, reply_markup=main_keyboard())
    else: bot.send_message(chat_id, caption, reply_markup=main_keyboard())
    execute_trade_if_allowed(chat_id, sig)

def load_top(chat_id:int, exchange_name:str, n:int):
    bot.send_message(chat_id, f'⏳ Загружаю top-{n} {exchange_name.upper()} futures...', reply_markup=main_keyboard()); symbols=fetch_top_symbols(exchange_name,n); s=load_state(); s['exchange']=exchange_name; s['symbols']=symbols; save_state(s); bot.send_message(chat_id, f"✅ Загружено {len(symbols)} монет.\nПервые 10:\n"+'\n'.join(symbols[:10]), reply_markup=main_keyboard())

if __name__ == '__main__':
    print(f'Crypto Futures Signal Bot v{VERSION} starting...', flush=True)
    print(f'DATA_DIR={DATA_DIR}', flush=True)
    threading.Thread(target=signal_loop, daemon=True).start()
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
