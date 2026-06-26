# -*- coding: utf-8 -*-
# PulseFX Gold V2 — 100% MT5
# Données + signaux + exécution via MetaTrader 5 uniquement
# Actif : XAUUSD

import MetaTrader5 as mt5
import datetime
import time
import os
import sys
import logging
import requests
import json
import numpy as np
from threading import Thread
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("PulseFX-Gold")

# ─── CONFIG ────────────────────────────────────────────────────────────────────
TG_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
CHANNEL_NAME = os.environ.get("CHANNEL_NAME", "PulseFX Gold VIP")
MT5_LOGIN    = int(os.environ.get("MT5_LOGIN", 0))
MT5_PASSWORD = os.environ.get("MT5_PASSWORD", "")
MT5_SERVER   = os.environ.get("MT5_SERVER", "")

for var, val in [
    ("TELEGRAM_BOT_TOKEN", TG_BOT_TOKEN),
    ("TELEGRAM_CHAT_ID",   TG_CHAT_ID),
    ("MT5_LOGIN",          str(MT5_LOGIN)),
    ("MT5_PASSWORD",       MT5_PASSWORD),
    ("MT5_SERVER",         MT5_SERVER),
]:
    if not val or val == "0":
        log.error(f"Variable manquante : {var}")
        sys.exit(1)

DISCLAIMER = "⚠️ Il ne s'agit en aucun cas d'un conseil financier mais uniquement d'une alerte à titre informatif."

# ─── GOLD — PARAMÈTRES ────────────────────────────────────────────────────────
SYMBOL        = "XAUUSD"
TIMEFRAME_M5  = mt5.TIMEFRAME_M5
TIMEFRAME_H1  = mt5.TIMEFRAME_H1
CANDLES_M5    = 200
CANDLES_H1    = 100

# Sessions actives (UTC)
GOLD_HOURS    = list(range(8, 12)) + list(range(13, 17))

# Signal
MIN_SCORE     = 25
MIN_SCORE_H1  = 8
COOLDOWN_MIN  = 45
MAX_DAILY_SL  = 2

# SL/TP fixes en pips (1 pip XAUUSD = 0.1$)
PIP           = 0.1
SL_PIPS       = 15
TP1_PIPS      = 20
TP2_PIPS      = 35

# MT5 exécution
RISK_PERCENT           = 1      # % solde risqué par trade
MAX_SPREAD_POINTS      = 50     # spread max en points
PROFIT_CLOSE_THRESHOLD = 0.5    # fermeture anticipée à 50% du chemin vers TP
MONITOR_SLEEP          = 4      # secondes entre chaque check positions
MAGIC                  = 999001

# ─── ÉTAT GLOBAL ───────────────────────────────────────────────────────────────
STATE_FILE      = "pulsefx_state.json"
BOT_PAUSED      = False
_last_update_id = 0

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
            t = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if abs((now - t).total_seconds()) <= 2700:
                return True, label
    return False, ""

# ─── SESSION ──────────────────────────────────────────────────────────────────
def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

def get_session(now):
    h = now.hour
    if 8  <= h < 12: return "🇬🇧 London"
    elif 13 <= h < 17: return "🇺🇸 New York"
    elif 17 <= h < 20: return "🌆 NY Late"
    else:              return "🌙 Hors session"

def get_validity():
    now    = utcnow()
    expiry = min(
        now.replace(hour=22, minute=0, second=0, microsecond=0),
        now + datetime.timedelta(minutes=60)
    )
    if expiry <= now:
        return "⏳ Valide maintenant"
    delta  = int((expiry - now).total_seconds() / 60)
    urgence = "🔴 URGENT" if delta <= 10 else "🟡 Rapidement" if delta <= 30 else "🟢 Tu as le temps"
    return f"{urgence} — Valide jusqu'à {expiry.strftime('%H:%M UTC')} ({delta} min)"

# ─── PERSISTANCE ──────────────────────────────────────────────────────────────
def save_state(open_signals, daily_signals, stats):
    try:
        data = {
            "open_signals":  {k: {**v, "time": v["time"].isoformat()} for k, v in open_signals.items()},
            "daily_signals": daily_signals,
            "stats":         stats,
        }
        with open(STATE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        log.error(f"save_state: {e}")

def load_state():
    try:
        if not os.path.exists(STATE_FILE):
            return {}, [], {"total": 0, "tp": 0, "sl": 0}
        with open(STATE_FILE) as f:
            data = json.load(f)
        open_signals = {
            k: {**v, "time": datetime.datetime.fromisoformat(v["time"])}
            for k, v in data.get("open_signals", {}).items()
        }
        return open_signals, data.get("daily_signals", []), data.get("stats", {"total": 0, "tp": 0, "sl": 0})
    except Exception as e:
        log.error(f"load_state: {e}")
        return {}, [], {"total": 0, "tp": 0, "sl": 0}

# ─── CONNEXION MT5 ────────────────────────────────────────────────────────────
def connect_mt5():
    if not mt5.initialize(login=MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER):
        raise Exception(f"Connexion MT5 échouée : {mt5.last_error()}")
    info = mt5.account_info()
    log.info(f"✅ MT5 connecté : {info.name} | Solde : {info.balance} {info.currency}")

# ─── DONNÉES MT5 ──────────────────────────────────────────────────────────────
def fetch_candles_mt5(timeframe, count):
    rates = mt5.copy_rates_from_pos(SYMBOL, timeframe, 0, count)
    if rates is None or len(rates) == 0:
        log.error(f"Pas de données MT5 ({timeframe})")
        return None
    closes  = np.array([r["close"]  for r in rates], dtype=float)
    highs   = np.array([r["high"]   for r in rates], dtype=float)
    lows    = np.array([r["low"]    for r in rates], dtype=float)
    volumes = np.array([r["tick_volume"] for r in rates], dtype=float)
    return closes, highs, lows, volumes

def get_current_price():
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        return None
    return (tick.ask + tick.bid) / 2

# ─── INDICATEURS ──────────────────────────────────────────────────────────────
def ema_series(arr, span):
    k = 2 / (span + 1)
    r = [arr[0]]
    for v in arr[1:]:
        r.append(v * k + r[-1] * (1 - k))
    return np.array(r)

def compute_rsi_series(closes, period=14):
    delta    = np.diff(closes)
    gain     = np.where(delta > 0, delta, 0.0)
    loss     = np.where(delta < 0, -delta, 0.0)
    avg_gain = np.mean(gain[:period])
    avg_loss = np.mean(loss[:period])
    rsi_vals = []
    for i in range(period, len(delta)):
        avg_gain = (avg_gain * (period - 1) + gain[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i]) / period
        rsi_vals.append(100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss)))
    return np.array(rsi_vals)

def detect_rsi_divergence(closes, rsi_s, lookback=20):
    if len(rsi_s) < lookback or len(closes) < lookback:
        return None
    pw = closes[-lookback:]
    rw = rsi_s[-lookback:]
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

def compute_macd_hist(closes):
    macd   = ema_series(closes, 12) - ema_series(closes, 26)
    signal = ema_series(macd, 9)
    return float(macd[-1] - signal[-1]), float(macd[-2] - signal[-2])

def compute_atr(highs, lows, closes, period=14):
    tr = np.maximum(highs[1:] - lows[1:],
         np.maximum(abs(highs[1:] - closes[:-1]),
                    abs(lows[1:]  - closes[:-1])))
    return float(np.mean(tr[-period:]))

def compute_bollinger(closes, period=20):
    ma  = np.mean(closes[-period:])
    std = np.std(closes[-period:])
    return ma - 2*std, ma + 2*std

def compute_volume_confirm(volumes):
    if volumes is None or len(volumes) < 20 or np.mean(volumes) == 0:
        return True, 0
    avg = float(np.mean(volumes[-20:]))
    rec = float(np.mean(volumes[-3:]))
    ratio = rec / avg if avg > 0 else 1
    return ratio >= 0.8, round(ratio, 2)

def compute_trend(closes):
    slope = (closes[-1] - closes[-20]) / closes[-20] * 100
    return ("TREND" if abs(slope) > 0.5 else "RANGE"), round(slope, 3)

# ─── SCORE TECHNIQUE ──────────────────────────────────────────────────────────
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
    ema9       = ema_series(closes, 9)
    ema9_val   = float(ema9[-1])
    above_ema9 = price > ema9_val

    score, reasons = 0, []

    # RSI (25 pts)
    if rsi < 30:    score += 25; reasons.append(f"RSI {rsi:.1f} survendu ✅")
    elif rsi > 70:  score -= 25; reasons.append(f"RSI {rsi:.1f} suracheté ❌")
    elif rsi < 45:  score += 12; reasons.append(f"RSI {rsi:.1f} zone basse")
    elif rsi > 55:  score -= 12; reasons.append(f"RSI {rsi:.1f} zone haute")

    # Divergence RSI (25 pts)
    if divergence == "bullish":  score += 25; reasons.append("📐 Divergence RSI haussière ✅")
    elif divergence == "bearish": score -= 25; reasons.append("📐 Divergence RSI baissière ❌")

    # MACD (20 pts + 5 bonus croisement)
    if macd_h > 0:
        score += 20; reasons.append("MACD haussier")
        if macd_prev <= 0: score += 5; reasons.append("MACD croisement haussier ✅")
    elif macd_h < 0:
        score -= 20; reasons.append("MACD baissier")
        if macd_prev >= 0: score -= 5; reasons.append("MACD croisement baissier ❌")

    # MAs (20 pts)
    if price > ma50 > ma200:   score += 20; reasons.append("Prix > MA50 > MA200 ✅")
    elif price < ma50 < ma200: score -= 20; reasons.append("Prix < MA50 < MA200 ❌")
    elif price > ma200:        score += 8
    elif price < ma200:        score -= 8

    # Bollinger (10 pts)
    if price <= bb_low:  score += 10; reasons.append("Prix sous BB basse")
    elif price >= bb_up: score -= 10; reasons.append("Prix sur BB haute")

    # EMA9 (10 pts)
    if above_ema9 and score > 0:     score += 10; reasons.append("EMA9 haussière ✅")
    elif not above_ema9 and score < 0: score -= 10; reasons.append("EMA9 baissière ✅")
    elif above_ema9 and score < 0:   score = int(score * 0.85); reasons.append("EMA9 contre signal ⚠️")
    elif not above_ema9 and score > 0: score = int(score * 0.85); reasons.append("EMA9 contre signal ⚠️")

    # Volume
    if not vol_ok:
        score = int(score * 0.8); reasons.append(f"⚠️ Volume faible ({vol_ratio}x)")
    elif vol_ratio >= 1.5:
        score += 10; reasons.append(f"📊 Volume fort ({vol_ratio}x) ✅")

    # Filtres bloquants
    if rsi > 75 and score > 0: return None, None, None
    if rsi < 25 and score < 0: return None, None, None

    indics = {
        "price": price, "rsi": round(rsi, 1), "divergence": divergence,
        "macd_hist": round(macd_h, 4), "vol_ratio": vol_ratio, "vol_ok": vol_ok,
        "above_ema9": above_ema9, "market_type": market_type,
        "ma50": round(ma50, 2), "ma200": round(ma200, 2),
    }
    return score, reasons, indics

# ─── GÉNÉRATION SIGNAL ────────────────────────────────────────────────────────
def generate_signal():
    # M5 — signal principal
    res_m5 = fetch_candles_mt5(TIMEFRAME_M5, CANDLES_M5)
    if res_m5 is None: return None
    score_m5, reasons_m5, ind_m5 = score_candles(*res_m5)
    if score_m5 is None: return None

    dir_m5 = "BUY" if score_m5 >= MIN_SCORE else "SELL" if score_m5 <= -MIN_SCORE else None
    if dir_m5 is None:
        log.info(f"[XAUUSD] Score M5={score_m5} RSI={ind_m5['rsi']} → pas de signal")
        return None

    # H1 — contexte
    res_h1 = fetch_candles_mt5(TIMEFRAME_H1, CANDLES_H1)
    if res_h1 is None:
        score_h1, reasons_h1, ind_h1 = 0, [], {"rsi": "N/A"}
    else:
        score_h1, reasons_h1, ind_h1 = score_candles(*res_h1)
        if score_h1 is None:
            score_h1, reasons_h1, ind_h1 = 0, [], {"rsi": "N/A"}

    dir_h1 = "BUY" if score_h1 >= MIN_SCORE_H1 else "SELL" if score_h1 <= -MIN_SCORE_H1 else "NEUTRE"

    if dir_h1 == dir_m5:
        score_m5 = int(score_m5 * 1.2); h1_note = "H1 confirme ✅"
    elif dir_h1 not in (dir_m5, "NEUTRE"):
        score_m5 = int(score_m5 * 0.8); h1_note = "H1 contredit ⚠️"
    else:
        h1_note = "H1 neutre"

    log.info(f"[XAUUSD] {dir_m5} score={score_m5} RSI_M5={ind_m5['rsi']} RSI_H1={ind_h1.get('rsi','N/A')} | {h1_note}")

    if abs(score_m5) < MIN_SCORE:
        log.info(f"[XAUUSD] Signal annulé après pénalité H1 (score={score_m5})")
        return None

    # Prix live depuis le tick MT5
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None: return None
    price = (tick.ask + tick.bid) / 2

    if dir_m5 == "BUY":
        tp1 = round(price + TP1_PIPS * PIP, 2)
        tp2 = round(price + TP2_PIPS * PIP, 2)
        sl  = round(price - SL_PIPS  * PIP, 2)
    else:
        tp1 = round(price - TP1_PIPS * PIP, 2)
        tp2 = round(price - TP2_PIPS * PIP, 2)
        sl  = round(price + SL_PIPS  * PIP, 2)

    spread      = 2 * PIP
    entry_low   = round(price - spread, 2)
    entry_high  = round(price + spread, 2)

    all_reasons = [f"M5: {r}" for r in reasons_m5[:4]] + [f"H1: {r}" for r in reasons_h1[:3]]
    if ind_m5["divergence"]:
        all_reasons.insert(0, f"📐 Divergence RSI {ind_m5['divergence']} 🔥")

    return {
        "direction":   dir_m5,
        "price":       round(price, 2),
        "entry_low":   entry_low,
        "entry_high":  entry_high,
        "tp1": tp1, "tp2": tp2, "sl": sl,
        "rsi_m5":      ind_m5["rsi"],
        "rsi_h1":      ind_h1.get("rsi", "N/A"),
        "divergence":  ind_m5["divergence"],
        "vol_ratio":   ind_m5["vol_ratio"],
        "vol_ok":      ind_m5["vol_ok"],
        "above_ema9":  ind_m5["above_ema9"],
        "market_type": ind_m5["market_type"],
        "score":       score_m5,
        "score_h1":    score_h1,
        "reasons":     all_reasons,
    }

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────
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

def answer_callback(cb_id, text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": cb_id, "text": text}, timeout=5
        )
    except Exception:
        pass

def notify_signal(sig, ticket=None):
    arrow      = "🟢" if sig["direction"] == "BUY" else "🔴"
    div_line   = f"📐 Divergence RSI {sig['divergence']} 🔥\n" if sig["divergence"] else ""
    vol_icon   = "✅" if sig["vol_ok"] else "⚠️"
    ema9_icon  = "↗️" if sig["above_ema9"] else "↘️"
    mkt_icon   = "📈" if sig["market_type"] == "TREND" else "↔️"
    sl_dist    = abs(sig["price"] - sig["sl"])
    tp1_dist   = abs(sig["tp1"]  - sig["price"])
    rr         = round(tp1_dist / sl_dist, 1) if sl_dist > 0 else 0
    session    = get_session(utcnow())
    mt5_line   = f"🤖 <b>Trade MT5 ouvert</b> (ticket #{ticket})\n\n" if ticket else ""

    msg = (
        f"<b>{CHANNEL_NAME}</b>\n\n"
        + mt5_line
        + f"{arrow} <b>{sig['direction']} XAUUSD</b>  |  {session}\n\n"
        + f"📍 <b>ENTRY :</b> {sig['entry_low']}–{sig['entry_high']}\n"
        + f"🛑 <b>SL :</b> {sig['sl']}\n"
        + f"🎯 <b>TP1 :</b> {sig['tp1']}\n"
        + f"🎯 <b>TP2 :</b> {sig['tp2']}\n"
        + f"⚖️ <b>R/R TP1 :</b> 1:{rr}\n\n"
        + div_line
        + f"📊 Volume : {sig['vol_ratio']}x {vol_icon} | {ema9_icon} EMA9\n"
        + f"{mkt_icon} Marché : {sig['market_type']} | RSI M5/H1 : {sig['rsi_m5']}/{sig['rsi_h1']}\n\n"
        + f"⏱ {get_validity()}\n\n"
        + f"<i>{DISCLAIMER}</i>"
    )
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Pris",   "callback_data": f"taken_{sig['direction']}"},
        {"text": "❌ Ignoré", "callback_data": f"ignored_{sig['direction']}"},
    ]]}
    send_telegram(msg, reply_markup=keyboard)
    log.info(f"✅ Signal {sig['direction']} XAUUSD | score={sig['score']}")

def notify_tp(sig, current, tp_num):
    tp_val    = sig[f"tp{tp_num}"]
    entry_mid = round((sig["entry_low"] + sig["entry_high"]) / 2, 2)
    action = (
        f"• Ferme 30-50% de ta position\n"
        f"• Déplace SL au breakeven ({entry_mid})\n"
        f"• Laisse courir vers TP2 : {sig['tp2']}"
    ) if tp_num == 1 else "🏆 Objectif final — ferme tout !"

    send_telegram(
        f"<b>{CHANNEL_NAME}</b>\n\n"
        f"✅ <b>TP{tp_num} ATTEINT — {sig['direction']} XAUUSD</b>\n\n"
        f"📥 Entrée : {sig['entry_low']}–{sig['entry_high']}\n"
        f"📤 Prix actuel : {current}\n"
        f"🎯 TP{tp_num} : {tp_val} ✅\n\n"
        f"🔔 <b>Action :</b>\n{action}\n\n"
        f"<i>{DISCLAIMER}</i>"
    )

def notify_sl(sig, current):
    send_telegram(
        f"<b>{CHANNEL_NAME}</b>\n\n"
        f"❌ <b>SL TOUCHÉ — {sig['direction']} XAUUSD</b>\n\n"
        f"📥 Entrée : {sig['entry_low']}–{sig['entry_high']}\n"
        f"📤 Prix actuel : {current}\n"
        f"🛑 SL : {sig['sl']} ❌\n\n"
        f"<i>{DISCLAIMER}</i>"
    )

def notify_startup():
    info = mt5.account_info()
    send_telegram(
        f"<b>{CHANNEL_NAME}</b>\n\n"
        f"🚀 <b>PulseFX Gold démarré</b>\n\n"
        f"🕐 {utcnow().strftime('%d/%m/%Y %H:%M')} UTC\n"
        f"🥇 Actif : XAUUSD\n"
        f"🏦 Compte : {info.name} | {info.balance:.2f} {info.currency}\n"
        f"📊 Scoring M5+H1 | RSI Divergence | MACD | EMA9 | Volume\n"
        f"🎯 TP1={TP1_PIPS} pips | TP2={TP2_PIPS} pips | SL={SL_PIPS} pips\n"
        f"⚡ Exécution automatique MT5\n\n"
        f"Commandes :\n"
        f"/status — signaux ouverts\n"
        f"/stats — performance\n"
        f"/solde — solde du compte\n"
        f"/pause — suspendre\n"
        f"/resume — reprendre\n\n"
        f"<i>{DISCLAIMER}</i>"
    )

def send_daily_recap(daily_signals, stats):
    now   = utcnow().strftime("%d/%m/%Y")
    total = len(daily_signals)
    wr    = round(stats["tp"] / stats["total"] * 100, 1) if stats["total"] > 0 else 0
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
        f"🏆 Win rate : {wr}% ({stats['tp']} TP / {stats['sl']} SL)\n\n"
        f"{lines}\n\n"
        f"<i>{DISCLAIMER}</i>"
    )

# ─── COMMANDES TELEGRAM ───────────────────────────────────────────────────────
def poll_commands(open_signals, stats):
    global BOT_PAUSED, _last_update_id
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getUpdates",
            params={"offset": _last_update_id + 1, "timeout": 5},
            timeout=10
        )
        for upd in r.json().get("result", []):
            _last_update_id = upd["update_id"]

            cb = upd.get("callback_query", {})
            if cb:
                parts  = cb.get("data", "").split("_")
                action = parts[0] if parts else ""
                if action == "taken":
                    answer_callback(cb["id"], "✅ Bon trade !")
                    stats["total"] += 1
                elif action == "ignored":
                    answer_callback(cb["id"], "❌ Ignoré noté")
                continue

            msg     = upd.get("message", {})
            text    = msg.get("text", "").strip().lower()
            chat_id = str(msg.get("chat", {}).get("id", ""))
            if chat_id not in TG_CHAT_ID and TG_CHAT_ID not in chat_id:
                continue

            if text == "/status":
                if not open_signals:
                    send_telegram("📭 Aucun signal XAUUSD ouvert.")
                else:
                    lines = []
                    for _, entry in open_signals.items():
                        sig = entry["sig"]
                        age = round((utcnow() - entry["time"]).total_seconds() / 60)
                        lines.append(f"• {sig['direction']} | TP1:{sig['tp1']} SL:{sig['sl']} | {age} min")
                    send_telegram("📊 <b>Signaux ouverts :</b>\n\n" + "\n".join(lines))

            elif text == "/stats":
                t  = stats["total"]
                tp = stats["tp"]
                sl = stats["sl"]
                wr = round(tp / t * 100, 1) if t > 0 else 0
                send_telegram(
                    f"📈 <b>Performance PulseFX Gold</b>\n\n"
                    f"Total : {t} | ✅ TP : {tp} | ❌ SL : {sl}\n"
                    f"🏆 Win rate : {wr}%"
                )

            elif text == "/solde":
                info = mt5.account_info()
                if info:
                    send_telegram(
                        f"💰 <b>Compte MT5</b>\n\n"
                        f"Solde : {info.balance:.2f} {info.currency}\n"
                        f"Equity : {info.equity:.2f} {info.currency}\n"
                        f"Marge libre : {info.margin_free:.2f} {info.currency}"
                    )

            elif text == "/pause":
                BOT_PAUSED = True
                send_telegram("⏸ <b>Bot suspendu.</b> /resume pour reprendre.")

            elif text == "/resume":
                BOT_PAUSED = False
                send_telegram("▶️ <b>Bot repris.</b>")

    except Exception as e:
        log.error(f"poll_commands: {e}")

# ─── SUIVI TP/SL (prix MT5) ───────────────────────────────────────────────────
def monitor_signals(open_signals, stats, save_cb):
    while True:
        time.sleep(10)
        if not open_signals:
            continue
        to_close = []
        for key, entry in list(open_signals.items()):
            sig    = entry["sig"]
            is_buy = sig["direction"] == "BUY"
            tick   = mt5.symbol_info_tick(SYMBOL)
            if tick is None:
                continue
            current = tick.ask if is_buy else tick.bid

            tp1_hit = current >= sig["tp1"] if is_buy else current <= sig["tp1"]
            tp2_hit = current >= sig["tp2"] if is_buy else current <= sig["tp2"]
            sl_hit  = current <= sig["sl"]  if is_buy else current >= sig["sl"]

            if tp2_hit:
                notify_tp(sig, round(current, 2), 2)
                stats["tp"] += 1; stats["total"] += 1
                to_close.append(key)
            elif tp1_hit and not entry.get("tp1_notified"):
                notify_tp(sig, round(current, 2), 1)
                entry["tp1_notified"] = True
                stats["tp"] += 1; stats["total"] += 1
            elif sl_hit:
                notify_sl(sig, round(current, 2))
                stats["sl"] += 1; stats["total"] += 1
                stats["daily_sl"] = stats.get("daily_sl", 0) + 1
                if stats["daily_sl"] >= MAX_DAILY_SL:
                    send_telegram(
                        f"🛑 <b>Stop journalier</b>\n{MAX_DAILY_SL} SL touchés. "
                        f"Bot suspendu jusqu'à 07h UTC."
                    )
                to_close.append(key)

        for k in to_close:
            open_signals.pop(k, None)
        if to_close:
            save_cb()

# ─── EXÉCUTION MT5 ────────────────────────────────────────────────────────────
def get_filling_mode():
    info  = mt5.symbol_info(SYMBOL)
    if info is None: return mt5.ORDER_FILLING_IOC
    flags = info.filling_mode
    if flags & 2: return mt5.ORDER_FILLING_IOC
    if flags & 1: return mt5.ORDER_FILLING_FOK
    if flags & 4: return mt5.ORDER_FILLING_RETURN
    return mt5.ORDER_FILLING_IOC

def spread_ok():
    info = mt5.symbol_info(SYMBOL)
    tick = mt5.symbol_info_tick(SYMBOL)
    if info is None or tick is None or info.point == 0:
        return False
    spread = (tick.ask - tick.bid) / info.point
    if spread > MAX_SPREAD_POINTS:
        log.warning(f"Spread trop large : {spread:.1f} pts")
        return False
    return True

def calculate_lot(sl_price, entry_price):
    account  = mt5.account_info()
    info     = mt5.symbol_info(SYMBOL)
    if account is None or info is None:
        return 0.01
    sl_pips    = abs(entry_price - sl_price) / info.point
    tick_value = info.trade_tick_value or 1
    lot = (account.balance * RISK_PERCENT / 100) / (sl_pips * tick_value)
    lot = max(lot, info.volume_min)
    lot = min(lot, info.volume_max)
    step = info.volume_step or 0.01
    return round(round(lot / step) * step, 2)

def open_trade(sig):
    info = mt5.symbol_info(SYMBOL)
    if info is None or info.trade_mode != mt5.SYMBOL_TRADE_MODE_FULL:
        log.warning("Symbole non tradable")
        return None
    if not info.visible:
        mt5.symbol_select(SYMBOL, True)
    if not spread_ok():
        log.info("Spread trop large — trade annulé")
        return None

    tick       = mt5.symbol_info_tick(SYMBOL)
    if tick is None: return None
    is_buy     = sig["direction"] == "BUY"
    order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
    price      = tick.ask if is_buy else tick.bid
    lot        = calculate_lot(sig["sl"], price)

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       SYMBOL,
        "volume":       lot,
        "type":         order_type,
        "price":        price,
        "sl":           sig["sl"],
        "tp":           sig["tp1"],
        "deviation":    50,
        "magic":        MAGIC,
        "comment":      "PulseFX-Gold",
        "type_filling": get_filling_mode(),
    }
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        log.info(f"✅ MT5 trade ouvert : {sig['direction']} {lot} lots @ {price} | ticket={result.order}")
        return result.order
    else:
        log.error(f"MT5 trade échoué : retcode={result.retcode if result else 'None'} | {result.comment if result else ''}")
        return None

def monitor_trades_mt5():
    """Surveillance des positions MT5 ouvertes par le bot."""
    while True:
        try:
            positions = mt5.positions_get(symbol=SYMBOL)
            if positions:
                for pos in positions:
                    if pos.magic != MAGIC:
                        continue
                    is_buy  = pos.type == mt5.ORDER_TYPE_BUY
                    tick    = mt5.symbol_info_tick(SYMBOL)
                    if tick is None: continue
                    current = tick.ask if is_buy else tick.bid
                    tp      = pos.tp
                    sl      = pos.sl
                    entry   = pos.price_open
                    tp_dist = abs(tp - entry) if tp != 0 else 0

                    closed = False; label = ""

                    if is_buy and tp and current >= tp:
                        closed = close_position_mt5(pos); label = f"TP atteint à {round(current,2)} ✅"
                    elif not is_buy and tp and current <= tp:
                        closed = close_position_mt5(pos); label = f"TP atteint à {round(current,2)} ✅"
                    elif is_buy and sl and current <= sl:
                        closed = close_position_mt5(pos); label = f"SL touché à {round(current,2)} ❌"
                    elif not is_buy and sl and current >= sl:
                        closed = close_position_mt5(pos); label = f"SL touché à {round(current,2)} ❌"
                    elif pos.profit > 0 and tp_dist > 0:
                        ratio = (current - entry) / (tp - entry) if is_buy else (entry - current) / (entry - tp)
                        if ratio >= PROFIT_CLOSE_THRESHOLD:
                            closed = close_position_mt5(pos)
                            label  = f"Fermeture anticipée @ {round(current,2)} (+{pos.profit:.2f}$) ⚡"

                    if closed:
                        send_telegram(
                            f"<b>{CHANNEL_NAME}</b>\n\n"
                            f"<b>{'BUY' if is_buy else 'SELL'} XAUUSD — Position fermée</b>\n"
                            f"🎫 Ticket #{pos.ticket}\n"
                            f"{label}\n\n"
                            f"<i>{DISCLAIMER}</i>"
                        )
        except Exception as e:
            log.error(f"monitor_trades_mt5: {e}")
        time.sleep(MONITOR_SLEEP)

def close_position_mt5(pos):
    is_buy     = pos.type == mt5.ORDER_TYPE_BUY
    close_type = mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY
    tick       = mt5.symbol_info_tick(SYMBOL)
    price      = tick.bid if is_buy else tick.ask
    result     = mt5.order_send({
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       SYMBOL,
        "volume":       pos.volume,
        "type":         close_type,
        "position":     pos.ticket,
        "price":        price,
        "deviation":    10,
        "magic":        MAGIC,
        "comment":      "PulseFX-Close",
        "type_filling": get_filling_mode(),
    })
    ok = result and result.retcode == mt5.TRADE_RETCODE_DONE
    if ok:  log.info(f"✅ Position {pos.ticket} fermée à {price}")
    else:   log.error(f"❌ Fermeture {pos.ticket} échouée : {result.retcode if result else 'None'}")
    return ok

# ─── BOUCLE PRINCIPALE ────────────────────────────────────────────────────────
def main():
    global BOT_PAUSED
    log.info("🚀 PulseFX Gold démarrage...")

    connect_mt5()
    open_signals, daily_signals, stats = load_state()
    notify_startup()

    def save_cb():
        save_state(open_signals, daily_signals, stats)

    # Threads
    Thread(target=monitor_signals,   args=(open_signals, stats, save_cb), daemon=True, name="monitor-signal").start()
    Thread(target=monitor_trades_mt5, daemon=True, name="monitor-mt5").start()

    last_signal    = None
    last_cmd_poll  = utcnow()
    last_recap_day = -1

    while True:
        now = utcnow()

        # Poll commandes Telegram toutes les 10s
        if (now - last_cmd_poll).total_seconds() >= 10:
            poll_commands(open_signals, stats)
            last_cmd_poll = now

        # Message "aucun signal" à 19h30
        if now.hour == 19 and 30 <= now.minute < 45 and now.day != last_recap_day:
            if len(daily_signals) == 0 and stats.get("daily_sl", 0) == 0:
                send_telegram(
                    "📭 <b>Aucun signal qualifié aujourd'hui</b>\n\n"
                    "Confluence insuffisante. 🔄 Reprise demain dès 07h UTC."
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
            time.sleep(60)
            continue

        # Weekend
        if now.weekday() >= 5:
            log.info("Weekend — pause 10 min")
            time.sleep(600)
            continue

        # Fenêtre news
        in_news, news_label = is_news_window(now)
        if in_news:
            log.info(f"News ({news_label}) — pause 5 min")
            time.sleep(300)
            continue

        # Stop journalier
        if stats.get("daily_sl", 0) >= MAX_DAILY_SL:
            log.info("Stop journalier actif")
            time.sleep(300)
            continue

        # Cooldown
        if last_signal and (now - last_signal).total_seconds() < COOLDOWN_MIN * 60:
            remaining = int(COOLDOWN_MIN * 60 - (now - last_signal).total_seconds())
            log.info(f"Cooldown — {remaining}s restantes")
        else:
            candle_str = now.strftime("%H:") + str((now.minute // 5) * 5).zfill(2)
            log.info(f"--- Scan XAUUSD {candle_str} UTC ---")
            try:
                sig = generate_signal()
                if sig:
                    if sig["market_type"] == "RANGE":
                        log.info("Marché RANGE — signal ignoré")
                    else:
                        ticket = open_trade(sig)
                        notify_signal(sig, ticket=ticket)
                        last_signal = now
                        open_signals[SYMBOL] = {"sig": sig, "time": now}
                        daily_signals.append({
                            "direction": sig["direction"],
                            "price":     sig["price"],
                            "tp1":       sig["tp1"],
                            "sl":        sig["sl"],
                        })
                        save_cb()
                        time.sleep(3)
            except Exception as e:
                log.error(f"[XAUUSD] {e}")

        # Attente prochaine bougie M5
        now2     = utcnow()
        next_min = ((now2.minute // 5) + 1) * 5
        if next_min >= 60:
            next_candle = now2.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)
        else:
            next_candle = now2.replace(minute=next_min, second=0, microsecond=0)
        wait = max((next_candle - utcnow()).total_seconds(), 10)
        log.info(f"Prochain scan dans {int(wait)}s")
        time.sleep(wait)

if __name__ == "__main__":
    main()
