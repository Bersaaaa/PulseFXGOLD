# -*- coding: utf-8 -*-
# PulseFX GOLD — Fusion V1
# Cerveau : bot.py (TwelveData, scoring riche, Telegram)
# Bras     : botmt5_scalp_m5.py (exécution MT5, gestion positions)
# Actif    : XAUUSD uniquement

import datetime
import time
import os
import sys
import logging
import requests
import json
import numpy as np
from threading import Thread

# MT5 — import conditionnel (pas disponible sur Railway/Linux sans Wine)
try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("PulseFX-Gold")

# ─── CONFIG ────────────────────────────────────────────────────────────────────
TG_BOT_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID     = os.environ.get("TELEGRAM_CHAT_ID", "")
CHANNEL_NAME   = os.environ.get("CHANNEL_NAME", "PulseFX Gold VIP")
TWELVEDATA_KEY = os.environ.get("TWELVEDATA_API_KEY", "")

# MT5 credentials (optionnel — si absents, le bot tourne en mode signal-only)
MT5_LOGIN    = os.environ.get("MT5_LOGIN")
MT5_PASSWORD = os.environ.get("MT5_PASSWORD")
MT5_SERVER   = os.environ.get("MT5_SERVER")

EXECUTE_MT5  = bool(MT5_AVAILABLE and MT5_LOGIN and MT5_PASSWORD and MT5_SERVER)

for var, val in [
    ("TELEGRAM_BOT_TOKEN", TG_BOT_TOKEN),
    ("TELEGRAM_CHAT_ID",   TG_CHAT_ID),
    ("TWELVEDATA_API_KEY", TWELVEDATA_KEY),
]:
    if not val:
        log.error(f"Variable manquante : {var}")
        sys.exit(1)

def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

DISCLAIMER = "⚠️ Il ne s'agit en aucun cas d'un conseil financier mais uniquement d'une alerte à titre informatif."

# ─── GOLD UNIQUEMENT ───────────────────────────────────────────────────────────
SYMBOL_TD  = "XAU/USD"    # TwelveData ticker
SYMBOL_MT5 = "XAUUSD"     # MT5 ticker
ASSET_NAME = "XAUUSD"
ASSET_DEC  = 2

# Sessions Gold optimales (UTC)
GOLD_HOURS = list(range(8, 12)) + list(range(13, 17))  # London 8-12h + NY 13-17h

# ─── PARAMÈTRES SIGNAL ────────────────────────────────────────────────────────
MIN_SCORE      = 25
MIN_SCORE_H1   = 8
SCALP_INTERVAL = "5min"
SCALP_OUTPUTSIZE = 100

COOLDOWN_MIN  = 45
MAX_DAILY_SL  = 2
SIGNAL_VALIDITY_MIN = 60

# ─── PARAMÈTRES TRADE MT5 ─────────────────────────────────────────────────────
RISK_PERCENT          = 1          # % du solde par trade
ATR_PERIOD            = 14
MONITOR_SLEEP_SECONDS = 4
PROFIT_CLOSE_THRESHOLD = 0.5       # fermeture anticipée à 50% du chemin vers TP
MAX_SPREAD_POINTS_GOLD = 50        # spread max XAUUSD en points

# ─── ÉTAT GLOBAL ───────────────────────────────────────────────────────────────
STATE_FILE = "/tmp/pulsefx_gold_state.json"
BOT_PAUSED = False
_last_update_id = 0

# ─── PERSISTANCE JSON ──────────────────────────────────────────────────────────
def save_state(open_signals, daily_signals, stats):
    try:
        data = {
            "open_signals":  {
                k: {**v, "time": v["time"].isoformat()}
                for k, v in open_signals.items()
            },
            "daily_signals": daily_signals,
            "stats":         stats,
            "saved_at":      utcnow().isoformat(),
        }
        with open(STATE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        log.error(f"save_state: {e}")

def load_state():
    try:
        if not os.path.exists(STATE_FILE):
            return {}, [], {"total": 0, "tp1": 0, "sl": 0}
        with open(STATE_FILE) as f:
            data = json.load(f)
        open_signals = {
            k: {**v, "time": datetime.datetime.fromisoformat(v["time"])}
            for k, v in data.get("open_signals", {}).items()
        }
        return open_signals, data.get("daily_signals", []), data.get("stats", {"total": 0, "tp1": 0, "sl": 0})
    except Exception as e:
        log.error(f"load_state: {e}")
        return {}, [], {"total": 0, "tp1": 0, "sl": 0}

# ─── NEWS MACRO ────────────────────────────────────────────────────────────────
RECURRING_NEWS = [
    (4, 12, 30, "NFP US"),
    (2, 18,  0, "Fed Decision"),
    (1,  9,  0, "CPI Zone Euro"),
    (2, 13, 30, "CPI US"),
    (4, 13, 30, "PCE US"),
]

def is_news_window(now):
    for dow, h, m, label in RECURRING_NEWS:
        if now.weekday() == dow:
            news_time = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if abs((now - news_time).total_seconds()) <= 2700:
                return True, label
    return False, ""

# ─── SESSION ──────────────────────────────────────────────────────────────────
def get_gold_session(now):
    h = now.hour
    if 8 <= h < 12:   return "🇬🇧 London"
    elif 13 <= h < 17: return "🇺🇸 New York"
    elif 17 <= h < 20: return "🌆 NY Late"
    else:              return "🌙 Hors session"

def get_signal_validity(sent_at):
    now        = utcnow()
    expiry_by_market = now.replace(hour=22, minute=0, second=0, microsecond=0)
    expiry_by_time   = now + datetime.timedelta(minutes=SIGNAL_VALIDITY_MIN)
    expiry = min(expiry_by_market, expiry_by_time)
    if expiry <= now:
        return "⏳ Valide maintenant"
    delta_min  = int((expiry - now).total_seconds() / 60)
    expiry_str = expiry.strftime("%H:%M UTC")
    urgence = "🔴 URGENT" if delta_min <= 10 else "🟡 Rapidement" if delta_min <= 30 else "🟢 Tu as le temps"
    return urgence + " — Valide jusqu'à " + expiry_str + " (" + str(delta_min) + " min)"

# ─── TWELVE DATA ───────────────────────────────────────────────────────────────
def fetch_candles(symbol, interval="15min", outputsize=200):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol":     symbol,
        "interval":   interval,
        "outputsize": outputsize,
        "apikey":     TWELVEDATA_KEY,
        "format":     "JSON",
    }
    try:
        r    = requests.get(url, params=params, timeout=15)
        data = r.json()
        if data.get("status") == "error":
            log.error(f"[{symbol}/{interval}] {data.get('message')}")
            return None
        values = data.get("values", [])
        if not values:
            return None
        closes  = np.array([float(v["close"])  for v in reversed(values)])
        highs   = np.array([float(v["high"])   for v in reversed(values)])
        lows    = np.array([float(v["low"])    for v in reversed(values)])
        volumes = np.array([float(v.get("volume", 0)) for v in reversed(values)])
        return closes, highs, lows, volumes
    except Exception as e:
        log.error(f"[{symbol}/{interval}] Exception: {e}")
        return None

def fetch_price_only():
    res = fetch_candles(SYMBOL_TD, "5min", 5)
    if res is None:
        return None
    return float(res[0][-1])

def is_signal_still_valid(sig, fresh_price):
    is_buy    = sig["direction"] == "BUY"
    entry_mid = (sig["entry_low"] + sig["entry_high"]) / 2
    sl_dist   = abs(sig["sl"] - entry_mid)
    if sl_dist == 0:
        return True
    drift = (entry_mid - fresh_price) if is_buy else (fresh_price - entry_mid)
    return (drift / sl_dist) < 0.5

# ─── INDICATEURS ───────────────────────────────────────────────────────────────
def compute_rsi_series(closes, period=14):
    rsi_vals = []
    delta    = np.diff(closes)
    gain     = np.where(delta > 0, delta, 0.0)
    loss     = np.where(delta < 0, -delta, 0.0)
    avg_gain = np.mean(gain[:period])
    avg_loss = np.mean(loss[:period])
    for i in range(period, len(delta)):
        avg_gain = (avg_gain * (period - 1) + gain[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i]) / period
        rsi_vals.append(100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss)))
    return np.array(rsi_vals)

def detect_rsi_divergence(closes, rsi_series, lookback=20):
    if len(rsi_series) < lookback or len(closes) < lookback:
        return None
    pw = closes[-lookback:]
    rw = rsi_series[-lookback:]
    ph = [(i, pw[i]) for i in range(1, len(pw)-1) if pw[i] > pw[i-1] and pw[i] > pw[i+1]]
    pl = [(i, pw[i]) for i in range(1, len(pw)-1) if pw[i] < pw[i-1] and pw[i] < pw[i+1]]
    rh = [(i, rw[i]) for i in range(1, len(rw)-1) if rw[i] > rw[i-1] and rw[i] > rw[i+1]]
    rl = [(i, rw[i]) for i in range(1, len(rw)-1) if rw[i] < rw[i-1] and rw[i] < rw[i+1]]
    if len(ph) >= 2 and len(rh) >= 2:
        if ph[-1][1] > ph[-2][1] and rh[-1][1] < rh[-2][1]:
            return "bearish"
    if len(pl) >= 2 and len(rl) >= 2:
        if pl[-1][1] < pl[-2][1] and rl[-1][1] > rl[-2][1]:
            return "bullish"
    return None

def ema_series(arr, span):
    k = 2 / (span + 1)
    r = [arr[0]]
    for v in arr[1:]:
        r.append(v * k + r[-1] * (1 - k))
    return np.array(r)

def compute_ema9(closes):
    ema9  = ema_series(closes, 9)
    price = float(closes[-1])
    return float(ema9[-1]), price > float(ema9[-1])

def compute_macd_hist(closes):
    macd   = ema_series(closes, 12) - ema_series(closes, 26)
    signal = ema_series(macd, 9)
    return float(macd[-1] - signal[-1]), float(macd[-2] - signal[-2])

def compute_atr(highs, lows, closes, period=14):
    tr = np.maximum(highs[1:] - lows[1:],
         np.maximum(abs(highs[1:] - closes[:-1]),
                    abs(lows[1:]  - closes[:-1])))
    return float(np.mean(tr[-period:]))

def compute_atr_dynamic(highs, lows, closes):
    return compute_atr(highs, lows, closes, period=5)

def compute_bollinger(closes, period=20):
    ma  = np.mean(closes[-period:])
    std = np.std(closes[-period:])
    return ma - 2*std, ma + 2*std

def compute_volume_confirm(volumes):
    if volumes is None or len(volumes) < 20 or np.mean(volumes) == 0:
        return True, 0
    avg_vol    = float(np.mean(volumes[-20:]))
    recent_vol = float(np.mean(volumes[-3:]))
    ratio      = recent_vol / avg_vol if avg_vol > 0 else 1
    return ratio >= 0.8, round(ratio, 2)

def compute_trend(closes):
    slope = (closes[-1] - closes[-20]) / closes[-20] * 100
    return ("TREND" if abs(slope) > 0.5 else "RANGE"), round(slope, 3)

# ─── SCORE TECHNIQUE ───────────────────────────────────────────────────────────
def score_candles(closes, highs, lows, volumes):
    price      = float(closes[-1])
    rsi_s      = compute_rsi_series(closes)
    rsi        = float(rsi_s[-1])
    divergence = detect_rsi_divergence(closes, rsi_s)
    macd_h, macd_prev = compute_macd_hist(closes)
    ma50       = float(np.mean(closes[-50:]))
    ma200      = float(np.mean(closes[-200:])) if len(closes) >= 200 else ma50
    bb_low, bb_up = compute_bollinger(closes)
    vol_ok, vol_ratio = compute_volume_confirm(volumes)
    market_type, slope = compute_trend(closes)
    ema9_val, price_above_ema9 = compute_ema9(closes)

    score, reasons = 0, []

    # RSI niveau
    if rsi < 30:    score += 25; reasons.append(f"RSI {rsi:.1f} survendu ✅")
    elif rsi > 70:  score -= 25; reasons.append(f"RSI {rsi:.1f} suracheté ❌")
    elif rsi < 45:  score += 12; reasons.append(f"RSI {rsi:.1f} zone basse")
    elif rsi > 55:  score -= 12; reasons.append(f"RSI {rsi:.1f} zone haute")

    # RSI divergence
    if divergence == "bullish":  score += 25; reasons.append("📐 Divergence RSI haussière ✅")
    elif divergence == "bearish": score -= 25; reasons.append("📐 Divergence RSI baissière ❌")

    # MACD
    if macd_h > 0:
        score += 20; reasons.append("MACD haussier")
        if macd_prev <= 0: score += 5; reasons.append("MACD vient de croiser ✅")
    elif macd_h < 0:
        score -= 20; reasons.append("MACD baissier")
        if macd_prev >= 0: score -= 5; reasons.append("MACD vient de croiser ❌")

    # MAs
    if price > ma50 > ma200:   score += 20; reasons.append("Prix > MA50 > MA200 ✅")
    elif price < ma50 < ma200: score -= 20; reasons.append("Prix < MA50 < MA200 ❌")
    elif price > ma200:        score += 8
    elif price < ma200:        score -= 8

    # Bollinger
    if price <= bb_low:  score += 10; reasons.append("Prix sous BB basse")
    elif price >= bb_up: score -= 10; reasons.append("Prix sur BB haute")

    # EMA9
    if price_above_ema9 and score > 0:    score += 10; reasons.append("EMA9 haussière ✅")
    elif not price_above_ema9 and score < 0: score -= 10; reasons.append("EMA9 baissière ✅")
    elif price_above_ema9 and score < 0:  score = int(score * 0.85); reasons.append("EMA9 contre signal ⚠️")
    elif not price_above_ema9 and score > 0: score = int(score * 0.85); reasons.append("EMA9 contre signal ⚠️")

    # Volume
    if not vol_ok:
        score = int(score * 0.8)
        reasons.append("⚠️ Volume faible (" + str(vol_ratio) + "x)")
    elif vol_ratio >= 1.5:
        score += 10; reasons.append("📊 Volume fort (" + str(vol_ratio) + "x) ✅")

    # Filtres RSI extrêmes bloquants
    if rsi > 75 and score > 0: return None, None, None
    if rsi < 25 and score < 0: return None, None, None

    indics = {
        "price": price, "rsi": round(rsi, 1), "divergence": divergence,
        "macd_hist": round(macd_h, 5), "macd_cross": macd_prev <= 0 < macd_h or macd_prev >= 0 > macd_h,
        "ma50": round(ma50, 5), "ma200": round(ma200, 5),
        "bb_low": round(bb_low, 5), "bb_up": round(bb_up, 5),
        "vol_ratio": vol_ratio, "vol_ok": vol_ok,
        "ema9": round(ema9_val, 5), "price_above_ema9": price_above_ema9,
        "market_type": market_type, "slope": slope,
    }
    return score, reasons, indics

# ─── GÉNÉRATION SIGNAL ────────────────────────────────────────────────────────
def generate_signal():
    res_m5 = fetch_candles(SYMBOL_TD, SCALP_INTERVAL, SCALP_OUTPUTSIZE)
    if res_m5 is None: return None
    score_m5, reasons_m5, ind_m5 = score_candles(*res_m5)
    if score_m5 is None: return None

    dir_m5 = "BUY" if score_m5 >= MIN_SCORE else "SELL" if score_m5 <= -MIN_SCORE else None
    if dir_m5 is None:
        log.info(f"[XAUUSD] Score 5min={score_m5} RSI={ind_m5['rsi']} → pas de signal")
        return None

    time.sleep(1)
    res_h1 = fetch_candles(SYMBOL_TD, "1h", 100)
    if res_h1 is None:
        score_h1, reasons_h1, ind_h1 = 0, [], {"rsi": "N/A", "divergence": None}
    else:
        score_h1, reasons_h1, ind_h1 = score_candles(*res_h1)
        if score_h1 is None:
            score_h1, reasons_h1, ind_h1 = 0, [], {"rsi": "N/A", "divergence": None}

    dir_h1 = "BUY" if score_h1 >= MIN_SCORE_H1 else "SELL" if score_h1 <= -MIN_SCORE_H1 else "NEUTRE"

    if dir_h1 == dir_m5:
        score_m5 = int(score_m5 * 1.2); h1_note = "H1 confirme ✅"
    elif dir_h1 not in (dir_m5, "NEUTRE"):
        score_m5 = int(score_m5 * 0.8); h1_note = "H1 contredit ⚠️"
    else:
        h1_note = "H1 neutre"

    log.info(f"[XAUUSD] {dir_m5} score={score_m5} RSI5m={ind_m5['rsi']} H1={ind_h1['rsi']} | {h1_note}")

    if abs(score_m5) < MIN_SCORE:
        log.info(f"[XAUUSD] Signal annulé après pénalité H1 (score={score_m5})")
        return None

    price = ind_m5["price"]
    atr   = compute_atr_dynamic(res_m5[1], res_m5[2], res_m5[0])

    # SL/TP GOLD — pips fixes (1 pip = 0.1$)
    PIP      = 0.1
    SL_PIPS  = 15
    TP1_PIPS = 20
    TP2_PIPS = 35

    if dir_m5 == "BUY":
        tp1 = round(price + TP1_PIPS * PIP, ASSET_DEC)
        tp2 = round(price + TP2_PIPS * PIP, ASSET_DEC)
        sl  = round(price - SL_PIPS  * PIP, ASSET_DEC)
    else:
        tp1 = round(price - TP1_PIPS * PIP, ASSET_DEC)
        tp2 = round(price - TP2_PIPS * PIP, ASSET_DEC)
        sl  = round(price + SL_PIPS  * PIP, ASSET_DEC)

    spread     = 2 * PIP
    entry_low  = round(price - spread, ASSET_DEC)
    entry_high = round(price + spread, ASSET_DEC)

    all_reasons = [f"5min: {r}" for r in reasons_m5[:4]] + [f"H1: {r}" for r in reasons_h1[:4]]
    div = ind_m5["divergence"]
    if div:
        all_reasons.insert(0, f"📐 Divergence RSI {div} 🔥")

    return {
        "name": ASSET_NAME, "ticker_td": SYMBOL_TD, "ticker_mt5": SYMBOL_MT5,
        "direction": dir_m5, "price": price,
        "entry_low": entry_low, "entry_high": entry_high,
        "tp1": tp1, "tp2": tp2, "tp3": tp2, "sl": sl,
        "dec": ASSET_DEC, "atr": round(atr, ASSET_DEC),
        "rsi_m5": ind_m5["rsi"],
        "rsi_h1": ind_h1["rsi"] if isinstance(ind_h1, dict) else "N/A",
        "divergence": div,
        "macd_hist": ind_m5["macd_hist"],
        "vol_ratio": ind_m5["vol_ratio"], "vol_ok": ind_m5["vol_ok"],
        "price_above_ema9": ind_m5.get("price_above_ema9", True),
        "market_type": ind_m5["market_type"],
        "score": score_m5, "score_h1": score_h1,
        "reasons": all_reasons,
    }

# ─── TELEGRAM ──────────────────────────────────────────────────────────────────
def send_telegram(text, reply_markup=None):
    url     = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            log.error(f"Telegram {r.status_code}: {r.text}")
        return r.json().get("result", {}).get("message_id")
    except Exception as e:
        log.error(f"Telegram: {e}")
        return None

def answer_callback(callback_query_id, text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=5,
        )
    except Exception:
        pass

def notify_signal(sig, mt5_ticket=None):
    arrow       = "🔴" if sig["direction"] == "SELL" else "🟢"
    div_line    = "📐 Divergence RSI " + str(sig["divergence"]) + " 🔥\n" if sig["divergence"] else ""
    vol_icon    = "✅" if sig["vol_ok"] else "⚠️"
    market_icon = "📈" if sig["market_type"] == "TREND" else "↔️"
    ema9_icon   = "↗️" if sig.get("price_above_ema9") else "↘️"
    sl_dist     = abs(sig["price"] - sig["sl"])
    tp1_dist    = abs(sig["tp1"]   - sig["price"])
    rr          = round(tp1_dist / sl_dist, 1) if sl_dist > 0 else 0
    session     = get_gold_session(utcnow())
    mt5_line    = f"🤖 <b>Trade MT5 ouvert</b> (ticket #{mt5_ticket})\n\n" if mt5_ticket else ""

    msg = (
        "<b>" + CHANNEL_NAME + "</b>\n\n"
        + mt5_line
        + arrow + " <b>" + sig["direction"] + " " + sig["name"] + "</b>"
        + "  |  " + session + "\n\n"
        + "📍 <b>ENTRY:</b> " + str(sig["entry_low"]) + "-" + str(sig["entry_high"]) + "\n"
        + "🛑 <b>SL:</b> " + str(sig["sl"]) + "\n"
        + "🎯 <b>TP1:</b> " + str(sig["tp1"]) + "\n"
        + "🎯 <b>TP2:</b> " + str(sig["tp2"]) + "\n"
        + "⚖️ <b>R/R TP1:</b> 1:" + str(rr) + "\n\n"
        + div_line
        + "📊 Volume : " + str(sig["vol_ratio"]) + "x " + vol_icon + " | "
        + ema9_icon + " EMA9\n"
        + market_icon + " Marché : " + sig["market_type"]
        + " | RSI 5m/H1 : " + str(sig["rsi_m5"]) + "/" + str(sig["rsi_h1"]) + "\n\n"
        + "⏱ " + get_signal_validity(utcnow()) + "\n\n"
        + "<i>" + DISCLAIMER + "</i>"
    )

    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Pris",   "callback_data": "taken_XAUUSD_"  + sig["direction"]},
            {"text": "❌ Ignoré", "callback_data": "ignored_XAUUSD_" + sig["direction"]},
        ]]
    }
    send_telegram(msg, reply_markup=keyboard)
    log.info("✅ Signal : " + sig["direction"] + " XAUUSD | score=" + str(sig["score"]))

def notify_tp_hit(sig, current, tp_num):
    tp_val    = sig[f"tp{tp_num}"]
    entry_mid = round((sig["entry_low"] + sig["entry_high"]) / 2, sig.get("dec", 2))
    if tp_num == 1:
        action = (
            "🔔 <b>Action recommandée :</b>\n"
            "• Ferme 30-50% de ta position\n"
            "• Déplace ton SL au breakeven (" + str(entry_mid) + ")\n"
            "• Laisse le reste courir vers TP2 : " + str(sig["tp2"])
        )
    elif tp_num == 2:
        action = "🏆 Objectif final atteint — ferme tout !"
    else:
        action = "🏆 Objectif final atteint — ferme tout !"

    send_telegram(
        "<b>" + CHANNEL_NAME + "</b>\n\n"
        "✅ <b>TP" + str(tp_num) + " ATTEINT — " + sig["direction"] + " " + sig["name"] + "</b>\n\n"
        "📥 Entrée : " + str(sig["entry_low"]) + "-" + str(sig["entry_high"]) + "\n"
        "📤 Prix actuel : " + str(current) + "\n"
        "🎯 TP" + str(tp_num) + " : " + str(tp_val) + " ✅\n\n"
        + action + "\n\n"
        "<i>" + DISCLAIMER + "</i>"
    )

def notify_sl_hit(sig, current):
    send_telegram(
        f"<b>{CHANNEL_NAME}</b>\n\n"
        f"❌ <b>SL TOUCHÉ — {sig['direction']} {sig['name']}</b>\n\n"
        f"📥 Entrée : {sig['entry_low']}-{sig['entry_high']}\n"
        f"📤 Prix actuel : {current}\n"
        f"🛑 SL : {sig['sl']} ❌\n\n"
        f"<i>{DISCLAIMER}</i>"
    )

def notify_startup():
    mode = "🤖 Signal + exécution MT5" if EXECUTE_MT5 else "📡 Signal-only (pas de connexion MT5)"
    send_telegram(
        f"<b>{CHANNEL_NAME}</b>\n\n"
        f"🚀 <b>PulseFX Gold démarré</b>\n\n"
        f"🕐 {utcnow().strftime('%d/%m/%Y %H:%M')} UTC\n"
        f"🥇 Actif : XAUUSD uniquement\n"
        f"📊 Scoring M5+H1 | Divergence RSI | Volume | EMA9\n"
        f"🎯 TP1/TP2 | SL fixe 15 pips\n"
        f"⚡ {mode}\n\n"
        f"Commandes :\n"
        f"/status — signaux ouverts\n"
        f"/stats — performance\n"
        f"/pause — suspendre\n"
        f"/resume — reprendre\n\n"
        f"<i>{DISCLAIMER}</i>"
    )

def send_daily_recap(daily_signals, stats):
    now   = utcnow().strftime("%d/%m/%Y")
    total = len(daily_signals)
    wr    = round(stats["tp1"] / stats["total"] * 100, 1) if stats["total"] > 0 else 0
    if total == 0:
        send_telegram(f"<b>{CHANNEL_NAME}</b>\n\n📊 <b>Bilan {now}</b>\n\nAucun signal aujourd'hui.")
        return
    buys  = sum(1 for s in daily_signals if s["direction"] == "BUY")
    lines = "\n".join(
        f"• {s['direction']} @ {s['price']} → TP1:{s['tp1']} SL:{s['sl']}"
        for s in daily_signals
    )
    send_telegram(
        f"<b>{CHANNEL_NAME}</b>\n\n"
        f"📊 <b>Bilan XAUUSD {now}</b>\n\n"
        f"📨 Signaux : {total} | 🟢 BUY : {buys} | 🔴 SELL : {total-buys}\n"
        f"🏆 Win rate : {wr}% ({stats['tp1']} TP / {stats['sl']} SL)\n\n"
        f"{lines}\n\n"
        f"<i>{DISCLAIMER}</i>"
    )

# ─── COMMANDES TELEGRAM ────────────────────────────────────────────────────────
def poll_commands(open_signals, stats):
    global BOT_PAUSED, _last_update_id
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getUpdates"
        r   = requests.get(url, params={"offset": _last_update_id + 1, "timeout": 5}, timeout=10)
        updates = r.json().get("result", [])
        for upd in updates:
            _last_update_id = upd["update_id"]

            callback = upd.get("callback_query", {})
            if callback:
                cb_data = callback.get("data", "")
                cb_id   = callback.get("id", "")
                parts   = cb_data.split("_")
                action  = parts[0] if parts else ""
                if action == "taken":
                    answer_callback(cb_id, "✅ Bon trade !")
                    stats["total"] += 1
                elif action == "ignored":
                    answer_callback(cb_id, "❌ Ignoré noté")
                continue

            msg    = upd.get("message", {})
            text   = msg.get("text", "").strip().lower()
            chat_id = str(msg.get("chat", {}).get("id", ""))
            if chat_id not in TG_CHAT_ID and TG_CHAT_ID not in chat_id:
                continue

            if text == "/status":
                if not open_signals:
                    send_telegram("📭 Aucun signal XAUUSD ouvert.")
                else:
                    lines = []
                    for t, entry in open_signals.items():
                        sig = entry["sig"]
                        age = round((utcnow() - entry["time"]).total_seconds() / 60)
                        lines.append(f"• {sig['direction']} | TP1:{sig['tp1']} SL:{sig['sl']} | {age} min")
                    send_telegram("📊 <b>Signaux ouverts XAUUSD :</b>\n\n" + "\n".join(lines))

            elif text == "/stats":
                total = stats["total"]
                tp1   = stats["tp1"]
                sl    = stats["sl"]
                wr    = round(tp1 / total * 100, 1) if total > 0 else 0
                send_telegram(
                    f"📈 <b>Performance PulseFX Gold</b>\n\n"
                    f"Total signaux : {total}\n"
                    f"✅ TP atteint : {tp1}\n"
                    f"❌ SL touché : {sl}\n"
                    f"🏆 Win rate : {wr}%"
                )

            elif text == "/pause":
                BOT_PAUSED = True
                send_telegram("⏸ <b>Bot suspendu.</b> Envoie /resume pour reprendre.")

            elif text == "/resume":
                BOT_PAUSED = False
                send_telegram("▶️ <b>Bot repris.</b>")

    except Exception as e:
        log.error(f"poll_commands: {e}")

# ─── SUIVI SIGNAUX TwelveData (sans MT5) ──────────────────────────────────────
def monitor_signals(open_signals, stats, save_cb):
    while True:
        time.sleep(300)
        if not open_signals:
            continue
        to_close = []
        for ticker, entry in list(open_signals.items()):
            sig     = entry["sig"]
            is_buy  = sig["direction"] == "BUY"
            current = fetch_price_only()
            if current is None:
                continue

            tp1_hit = current >= sig["tp1"] if is_buy else current <= sig["tp1"]
            tp2_hit = current >= sig["tp2"] if is_buy else current <= sig["tp2"]
            sl_hit  = current <= sig["sl"]  if is_buy else current >= sig["sl"]

            if tp2_hit:
                notify_tp_hit(sig, current, 2)
                stats["tp1"] += 1; stats["total"] += 1
                to_close.append(ticker)
            elif tp1_hit and not entry.get("tp1_notified"):
                notify_tp_hit(sig, current, 1)
                entry["tp1_notified"] = True
                stats["tp1"] += 1; stats["total"] += 1
            elif sl_hit:
                notify_sl_hit(sig, current)
                stats["sl"] += 1; stats["total"] += 1
                stats["daily_sl"] = stats.get("daily_sl", 0) + 1
                if stats["daily_sl"] >= MAX_DAILY_SL:
                    send_telegram(
                        "🛑 <b>Stop journalier atteint</b>\n"
                        + str(MAX_DAILY_SL) + " SL touchés. Bot suspendu jusqu'à 07h UTC."
                    )
                to_close.append(ticker)

        for t in to_close:
            open_signals.pop(t, None)
        save_cb()

# ─── MT5 — EXÉCUTION ──────────────────────────────────────────────────────────
def connect_mt5():
    if not MT5_AVAILABLE:
        raise Exception("MetaTrader5 non disponible")
    if not mt5.initialize(login=int(MT5_LOGIN), password=MT5_PASSWORD, server=MT5_SERVER):
        raise Exception(f"Erreur connexion MT5 : {mt5.last_error()}")
    info = mt5.account_info()
    log.info(f"✅ MT5 connecté : {info.name} | Solde : {info.balance}")

def get_filling_mode_mt5(symbol):
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        return mt5.ORDER_FILLING_IOC
    flags = symbol_info.filling_mode
    if flags & 2: return mt5.ORDER_FILLING_IOC
    elif flags & 1: return mt5.ORDER_FILLING_FOK
    elif flags & 4: return mt5.ORDER_FILLING_RETURN
    return mt5.ORDER_FILLING_IOC

def spread_ok_mt5():
    symbol_info = mt5.symbol_info(SYMBOL_MT5)
    tick        = mt5.symbol_info_tick(SYMBOL_MT5)
    if symbol_info is None or tick is None or symbol_info.point == 0:
        return False
    spread_pts = (tick.ask - tick.bid) / symbol_info.point
    if spread_pts > MAX_SPREAD_POINTS_GOLD:
        log.warning(f"Spread trop large : {spread_pts:.1f}pts > {MAX_SPREAD_POINTS_GOLD}pts")
        return False
    return True

def calculate_lot_mt5(sl_pips):
    account_info = mt5.account_info()
    symbol_info  = mt5.symbol_info(SYMBOL_MT5)
    if account_info is None or symbol_info is None or sl_pips <= 0:
        return 0.01
    tick_value = symbol_info.trade_tick_value or 1
    lot = (account_info.balance * RISK_PERCENT / 100) / (sl_pips * tick_value)
    lot = max(lot, symbol_info.volume_min)
    lot = min(lot, symbol_info.volume_max)
    step = symbol_info.volume_step or 0.01
    lot  = round(lot / step) * step
    return round(lot, 2)

def open_trade_mt5(sig):
    """Ouvre un trade sur MT5 et retourne le ticket, ou None en cas d'échec."""
    if not EXECUTE_MT5:
        return None
    try:
        symbol_info = mt5.symbol_info(SYMBOL_MT5)
        if symbol_info is None:
            log.error("MT5 : symbol_info None")
            return None
        if not symbol_info.trade_mode == mt5.SYMBOL_TRADE_MODE_FULL:
            log.warning("MT5 : symbole non tradable")
            return None
        if not symbol_info.visible:
            mt5.symbol_select(SYMBOL_MT5, True)
        if not spread_ok_mt5():
            log.info("MT5 : spread trop large, trade annulé")
            return None

        tick       = mt5.symbol_info_tick(SYMBOL_MT5)
        if tick is None:
            log.error("MT5 : pas de tick")
            return None

        order_type = mt5.ORDER_TYPE_BUY if sig["direction"] == "BUY" else mt5.ORDER_TYPE_SELL
        price      = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid
        sl_pips    = abs(sig["price"] - sig["sl"]) / symbol_info.point
        lot        = calculate_lot_mt5(sl_pips)

        request = {
            "action":      mt5.TRADE_ACTION_DEAL,
            "symbol":      SYMBOL_MT5,
            "volume":      lot,
            "type":        order_type,
            "price":       price,
            "sl":          sig["sl"],
            "tp":          sig["tp1"],   # TP1 par défaut (géré manuellement pour TP2)
            "deviation":   50,
            "magic":       999001,
            "comment":     "PulseFX-Gold",
            "type_filling": get_filling_mode_mt5(SYMBOL_MT5),
        }
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            log.info(f"✅ MT5 trade ouvert : {sig['direction']} {lot} lots à {price} | ticket={result.order}")
            return result.order
        else:
            log.error(f"MT5 trade échoué : retcode={result.retcode if result else 'None'} | {result.comment if result else ''}")
            return None
    except Exception as e:
        log.error(f"open_trade_mt5: {e}")
        return None

def close_position_mt5(pos):
    close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    tick = mt5.symbol_info_tick(SYMBOL_MT5)
    price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask
    request = {
        "action":      mt5.TRADE_ACTION_DEAL,
        "symbol":      SYMBOL_MT5,
        "volume":      pos.volume,
        "type":        close_type,
        "position":    pos.ticket,
        "price":       price,
        "deviation":   10,
        "magic":       999001,
        "comment":     "PulseFX-Close",
        "type_filling": get_filling_mode_mt5(SYMBOL_MT5),
    }
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        log.info(f"✅ MT5 position {pos.ticket} fermée à {price}")
        return True, price
    else:
        log.error(f"❌ MT5 fermeture échouée {pos.ticket} : {result.retcode if result else 'None'}")
        return False, price

def monitor_trades_mt5():
    """Thread MT5 — surveille TP/SL/fermeture anticipée des positions PulseFX."""
    while True:
        try:
            positions = mt5.positions_get(symbol=SYMBOL_MT5)
            if positions:
                for pos in positions:
                    if pos.magic != 999001:
                        continue
                    direction    = "BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL"
                    tick         = mt5.symbol_info_tick(SYMBOL_MT5)
                    if tick is None:
                        continue
                    current      = tick.ask if direction == "BUY" else tick.bid
                    tp           = pos.tp
                    sl           = pos.sl
                    open_price   = pos.price_open
                    tp_distance  = abs(tp - open_price) if tp != 0 else 0

                    closed = False; result_text = ""; notify_text = ""

                    if direction == "BUY" and tp != 0 and current >= tp:
                        closed, price = close_position_mt5(pos)
                        result_text = "TP ATTEINT ✅"; notify_text = f"🎯 TP atteint à {price}"
                    elif direction == "SELL" and tp != 0 and current <= tp:
                        closed, price = close_position_mt5(pos)
                        result_text = "TP ATTEINT ✅"; notify_text = f"🎯 TP atteint à {price}"
                    elif direction == "BUY" and sl != 0 and current <= sl:
                        closed, price = close_position_mt5(pos)
                        result_text = "SL TOUCHÉ ❌"; notify_text = f"🛑 SL touché à {price}"
                    elif direction == "SELL" and sl != 0 and current >= sl:
                        closed, price = close_position_mt5(pos)
                        result_text = "SL TOUCHÉ ❌"; notify_text = f"🛑 SL touché à {price}"
                    elif pos.profit > 0 and tp_distance > 0:
                        ratio = (current - open_price) / (tp - open_price) if direction == "BUY" \
                               else (open_price - current) / (open_price - tp)
                        if ratio >= PROFIT_CLOSE_THRESHOLD:
                            closed, price = close_position_mt5(pos)
                            result_text = "PROFIT EARLY ✅"; notify_text = f"⚡ Fermeture anticipée à {price} (+{pos.profit:.2f}$)"

                    if closed:
                        send_telegram(
                            f"<b>{CHANNEL_NAME}</b>\n\n"
                            f"<b>{result_text} — {direction} XAUUSD</b>\n"
                            f"🎫 Ticket #{pos.ticket}\n"
                            f"{notify_text}\n\n"
                            f"<i>{DISCLAIMER}</i>"
                        )
        except Exception as e:
            log.error(f"monitor_trades_mt5: {e}")
        time.sleep(MONITOR_SLEEP_SECONDS)

# ─── BOUCLE PRINCIPALE ────────────────────────────────────────────────────────
def main():
    global BOT_PAUSED
    log.info("🚀 PulseFX Gold démarrage...")

    if EXECUTE_MT5:
        try:
            connect_mt5()
            Thread(target=monitor_trades_mt5, daemon=True, name="mt5-monitor").start()
            log.info("🤖 Mode : Signal + exécution MT5")
        except Exception as e:
            log.warning(f"MT5 non disponible — mode signal-only : {e}")

    open_signals, daily_signals, stats = load_state()
    notify_startup()

    def save_cb():
        save_state(open_signals, daily_signals, stats)

    # Thread suivi TP/SL via TwelveData (toujours actif)
    Thread(target=monitor_signals, args=(open_signals, stats, save_cb), daemon=True, name="monitor-td").start()

    last_signal   = {}
    last_cmd_poll = utcnow()
    last_recap_day = -1

    while True:
        now = utcnow()

        # Poll commandes Telegram
        if (now - last_cmd_poll).total_seconds() >= 10:
            poll_commands(open_signals, stats)
            last_cmd_poll = now

        # Message "aucun signal" à 19h30
        if now.hour == 19 and 30 <= now.minute < 45 and now.day != last_recap_day:
            if len(daily_signals) == 0 and stats.get("daily_sl", 0) == 0:
                send_telegram(
                    "📭 <b>Aucun signal qualifié aujourd'hui</b>\n\n"
                    "Confluence insuffisante (score ≥25 requis, hors RANGE).\n"
                    "🔄 Reprise demain dès 07h UTC."
                )

        # Bilan quotidien à 20h
        if now.hour == 20 and now.minute < 15 and now.day != last_recap_day:
            send_daily_recap(daily_signals, stats)
            daily_signals.clear()
            stats["daily_sl"] = 0
            last_recap_day = now.day
            save_cb()

        if BOT_PAUSED:
            time.sleep(30)
            continue

        # Hors sessions Gold
        if now.hour not in GOLD_HOURS:
            time.sleep(300)
            continue

        # Weekend
        if now.weekday() >= 5:
            log.info("[XAUUSD] Marché fermé weekend")
            time.sleep(600)
            continue

        # Fenêtre news
        in_news, news_label = is_news_window(now)
        if in_news:
            log.info(f"Fenêtre news ({news_label}) — pause 5 min")
            time.sleep(300)
            continue

        # Stop journalier
        if stats.get("daily_sl", 0) >= MAX_DAILY_SL:
            log.info("[XAUUSD] Stop journalier actif")
            time.sleep(300)
            continue

        # Cooldown
        last = last_signal.get(SYMBOL_TD)
        if last and (now - last).total_seconds() < COOLDOWN_MIN * 60:
            log.info(f"[XAUUSD] Cooldown ({COOLDOWN_MIN} min)")
        else:
            candle_min = (now.minute // 5) * 5
            log.info(f"--- Scan XAUUSD {now.strftime('%H:')+str(candle_min).zfill(2)} UTC ---")
            try:
                sig = generate_signal()
                if sig:
                    # Filtre RANGE
                    if sig["market_type"] == "RANGE":
                        log.info("[XAUUSD] Marché RANGE — signal ignoré")
                    else:
                        # Anti-signal périmé
                        fresh = fetch_price_only()
                        if fresh is not None and not is_signal_still_valid(sig, fresh):
                            log.info(f"[XAUUSD] Signal périmé (prix {fresh} trop proche SL {sig['sl']})")
                        else:
                            # Exécution MT5 si disponible
                            mt5_ticket = open_trade_mt5(sig) if EXECUTE_MT5 else None
                            notify_signal(sig, mt5_ticket=mt5_ticket)
                            last_signal[SYMBOL_TD]   = now
                            open_signals[SYMBOL_TD]  = {"sig": sig, "time": now}
                            daily_signals.append({
                                "direction": sig["direction"],
                                "price": sig["price"],
                                "tp1": sig["tp1"],
                                "sl": sig["sl"],
                            })
                            save_cb()
                            time.sleep(3)
            except Exception as e:
                log.error(f"[XAUUSD] {e}")

        # Attendre prochaine bougie 5min
        now2     = utcnow()
        next_min = ((now2.minute // 5) + 1) * 5
        if next_min >= 60:
            next_candle = now2.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)
        else:
            next_candle = now2.replace(minute=next_min, second=0, microsecond=0)
        wait = (next_candle - utcnow()).total_seconds()
        log.info(f"Prochain scan dans {int(wait)}s")
        time.sleep(max(wait, 10))

if __name__ == "__main__":
    main()
