# PulseFX Gold V2 — 100% MetaTrader 5

Bot de scalp automatisé sur **XAUUSD** — données, signaux et exécution via MT5 uniquement.
TwelveData supprimé. Prix en temps réel depuis le broker, zéro latence.

---

## Architecture

```
MetaTrader 5 (broker)
    │
    ├── Bougies M5  (200 candles)
    └── Bougies H1  (100 candles)
              │
              ▼
    Scoring technique M5 + H1
    RSI · MACD · EMA9 · Bollinger · Volume · Divergences
              │
              ▼
    Score ≥ 25 → BUY / SELL
              │
    Filtres : RANGE · News · Cooldown · Stop journalier
              │
         ┌────┴────┐
         ▼         ▼
   Signal Telegram  Exécution MT5
   (entry/SL/TP)   (lot auto, spread check)
         │
         ▼
   Suivi TP1 · TP2 · SL (tick MT5)
   Fermeture anticipée 50% TP
         │
         ▼
   Bilan quotidien 20h UTC
```

---

## Prérequis

- Windows (MT5 Python API Windows uniquement)
- MetaTrader 5 installé et ouvert
- Python 3.9+

```bash
pip install MetaTrader5 requests numpy python-dotenv
```

---

## Configuration

Crée un fichier `.env` à la racine :

```env
TELEGRAM_BOT_TOKEN=7xxxxxxxxx:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_CHAT_ID=-100xxxxxxxxxx
CHANNEL_NAME=PulseFX Gold VIP

MT5_LOGIN=12345678
MT5_PASSWORD=MonMotDePasse
MT5_SERVER=ICMarkets-Demo
```

---

## Lancement

```bash
python pulsefx_gold_v2.py
```

MT5 doit être **ouvert et connecté** avant de lancer le script.

---

## Paramètres clés

| Paramètre | Valeur | Description |
|-----------|--------|-------------|
| `SL_PIPS` | 15 | Stop Loss en pips |
| `TP1_PIPS` | 20 | TP1 en pips |
| `TP2_PIPS` | 35 | TP2 en pips |
| `RISK_PERCENT` | 1 | % du solde risqué par trade |
| `MIN_SCORE` | 25 | Score minimum pour signal |
| `COOLDOWN_MIN` | 45 | Minutes entre deux signaux |
| `MAX_DAILY_SL` | 2 | SL max par jour avant arrêt |
| `MAX_SPREAD_POINTS` | 50 | Spread max accepté (points) |
| `PROFIT_CLOSE_THRESHOLD` | 0.5 | Fermeture anticipée à 50% du TP |
| `GOLD_HOURS` (UTC) | 8-12h, 13-17h | Sessions London + New York |

---

## Indicateurs & Scoring

| Indicateur | Signal BUY | Signal SELL | Points |
|-----------|-----------|------------|--------|
| RSI < 30 | Survendu | — | +25 |
| RSI > 70 | — | Suracheté | −25 |
| Divergence RSI haussière | ✅ | — | +25 |
| Divergence RSI baissière | — | ✅ | −25 |
| MACD histogram positif | ✅ | — | +20 |
| MACD croisement | ✅ | — | +5 |
| Prix > MA50 > MA200 | ✅ | — | +20 |
| EMA9 confirme | ✅ | ✅ | +10 |
| Prix sous BB basse | ✅ | — | +10 |
| Volume fort (>1.5x) | ✅ | ✅ | +10 |

Le score M5 est multiplié selon le contexte H1 :
- H1 dans le même sens → ×1.2
- H1 neutre → ×1.0
- H1 opposé → ×0.8

---

## Commandes Telegram

| Commande | Réponse |
|---------|---------|
| `/status` | Signaux XAUUSD en cours |
| `/stats` | TP · SL · Win rate global |
| `/solde` | Solde / Equity / Marge libre MT5 |
| `/pause` | Suspend le bot |
| `/resume` | Reprend le bot |

Boutons sur chaque signal : **✅ Pris** / **❌ Ignoré**

---

## Gestion des positions MT5

Le bot surveille en continu ses positions (magic=999001) :

- **TP1 atteint** → notification Telegram + action recommandée (move SL breakeven)
- **TP2 atteint** → fermeture + notification
- **SL touché** → fermeture + notification + compteur daily SL
- **Fermeture anticipée** → si profit ≥ 50% du chemin vers TP

Lot calculé automatiquement :
```
Lot = (Solde × 1%) / (SL_pips × valeur_tick)
```

---

## Fichiers

```
pulsefx_gold_v2.py    ← bot principal
requirements.txt      ← dépendances
.env                  ← config (ne pas commiter)
README.md             ← ce fichier
```

---

## Troubleshooting

**`Connexion MT5 échouée`**
→ Vérifie que MT5 est ouvert et que login/password/server sont corrects dans `.env`

**`Spread trop large`**
→ Normal en dehors des sessions. Augmenter `MAX_SPREAD_POINTS` si le broker a un spread structurellement plus élevé.

**Pas de trades**
→ Vérifier que l'heure UTC est dans les sessions (8-12h ou 13-17h), que le marché n'est pas RANGE, et que le score atteint 25.

**`TRADE_RETCODE` erreur**
→ Le broker peut refuser un filling mode. Le bot détecte automatiquement le mode supporté (FOK/IOC/RETURN).
