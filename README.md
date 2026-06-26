# PulseFX Gold — Bot de trading XAUUSD

Bot de scalp automatisé sur le **Gold (XAUUSD)** avec deux modes :
- **Signal-only** → analyse technique + alertes Telegram (Railway/Linux)
- **Signal + exécution MT5** → trade automatique sur MetaTrader 5 (Windows local)

---

## Architecture

```
TwelveData API
    │
    ▼
Analyse technique (M5 + H1)
    │  RSI, MACD, EMA9, Bollinger, Volume, Divergences
    ▼
Score de confluence
    │  ≥ 25 → BUY/SELL
    ▼
Filtre qualité
    │  RANGE, News, Cooldown, Anti-périmé, Stop journalier
    ▼
Signal Telegram ──────────────────────────────────┐
    │                                              │
    ▼                                          Notification
Exécution MT5 (si dispo)                    TP1 / TP2 / SL
    │  Spread check, Lot sizing (1% risque)
    ▼
Suivi positions
    │  TP/SL auto, fermeture anticipée 50%
    ▼
Bilan quotidien 20h UTC
```

---

## Prérequis

### Python
```
Python 3.9+
```

### Packages
```bash
pip install requests numpy pandas python-dotenv
# Sur Windows uniquement (pour l'exécution MT5) :
pip install MetaTrader5
```

Ou via `requirements.txt` :
```bash
pip install -r requirements.txt
```

---

## Installation

### 1. Cloner / copier le fichier
```
pulsefx_gold_mt5.py
requirements.txt
.env
```

### 2. Configurer le `.env`

```env
# Telegram
TELEGRAM_BOT_TOKEN=7xxxxxxxxx:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_CHAT_ID=-100xxxxxxxxxx
CHANNEL_NAME=PulseFX Gold VIP

# TwelveData
TWELVEDATA_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# MT5 — optionnel (laisser vide si Railway/Linux)
MT5_LOGIN=12345678
MT5_PASSWORD=MonMotDePasse
MT5_SERVER=ICMarkets-Demo
```

> **Note :** Si MT5_LOGIN/PASSWORD/SERVER sont absents, le bot démarre en mode **signal-only** automatiquement.

### 3. Lancer
```bash
python pulsefx_gold_mt5.py
```

---

## Déploiement Railway (signal-only)

### `Procfile`
```
worker: python pulsefx_gold_mt5.py
```

### `railway.toml`
```toml
[build]
builder = "NIXPACKS"

[deploy]
startCommand = "python pulsefx_gold_mt5.py"
restartPolicyType = "ON_FAILURE"
restartPolicyMaxRetries = 10
```

### Variables d'environnement Railway
Ajouter dans **Settings → Variables** :
```
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
CHANNEL_NAME
TWELVEDATA_API_KEY
```
Ne pas mettre MT5_LOGIN/PASSWORD/SERVER sur Railway — le bot tourne en signal-only.

---

## Logique de signal

### Sessions actives (UTC)
| Session | Horaires UTC |
|---------|-------------|
| 🇬🇧 London | 08h00 – 12h00 |
| 🇺🇸 New York | 13h00 – 17h00 |

Le bot est **silencieux en dehors de ces fenêtres** et le weekend.

### Indicateurs utilisés (M5 + H1)
| Indicateur | Rôle | Points |
|-----------|------|--------|
| RSI (14) | Survendu/Suracheté | ±25 pts |
| Divergence RSI | Signal fort | ±25 pts |
| MACD Histogram | Momentum | ±20 pts (+5 si croisement) |
| MA50 / MA200 | Tendance long terme | ±20 pts |
| Bollinger Bands | Extrêmes de prix | ±10 pts |
| EMA9 | Micro-tendance | ±10 pts |
| Volume | Confirmation | ±10 pts |

**Score minimum pour signal : 25**

Le score M5 est ajusté par le contexte H1 :
- H1 confirme → score x1.2
- H1 contredit → score x0.8

### Filtres de qualité
- **RANGE** → signal ignoré si marché sans tendance
- **News** → pause ±45 min autour de NFP, Fed, CPI, PCE
- **Cooldown** → 45 min minimum entre deux signaux
- **Anti-périmé** → vérifie que le prix n'a pas déjà dérivé > 50% vers le SL
- **Stop journalier** → bot suspendu après 2 SL dans la journée

### SL/TP (pips fixes Gold)
```
SL  = 15 pips (1.5$)
TP1 = 20 pips (2.0$)   → R/R 1:1.33
TP2 = 35 pips (3.5$)   → R/R 1:2.33
```
*1 pip XAUUSD = 0.1$*

---

## Exécution MT5

Disponible uniquement sur **Windows avec MetaTrader 5 installé**.

### Paramètres MT5
```python
RISK_PERCENT          = 1      # % du solde risqué par trade
MAX_SPREAD_POINTS_GOLD = 50    # spread max accepté (points)
PROFIT_CLOSE_THRESHOLD = 0.5   # fermeture anticipée à 50% du chemin vers TP
MONITOR_SLEEP_SECONDS  = 4     # fréquence de surveillance des positions
```

### Gestion des positions
- **TP1 atteint** → fermeture + notification
- **TP2 atteint** → fermeture + notification
- **SL touché** → fermeture + notification
- **Fermeture anticipée** → si profit > 50% du chemin vers TP

### Lot sizing automatique
```
Lot = (Solde × 1%) / (SL_pips × valeur_tick)
```
Respecte les limites `volume_min`, `volume_max`, `volume_step` du broker.

---

## Commandes Telegram

| Commande | Action |
|---------|--------|
| `/status` | Affiche les signaux XAUUSD ouverts |
| `/stats` | Performance globale (TP / SL / win rate) |
| `/pause` | Suspend le bot |
| `/resume` | Reprend le bot |

Les boutons **✅ Pris** / **❌ Ignoré** sont disponibles sur chaque signal.

---

## Messages automatiques

| Heure UTC | Message |
|----------|---------|
| Au démarrage | Confirmation lancement + mode actif |
| Chaque signal | Alerte complète (entry, SL, TP1, TP2, R/R, RSI, session) |
| TP1 / TP2 atteint | Notification + action recommandée |
| SL touché | Notification |
| 19h30 | "Aucun signal qualifié" si journée vide |
| 20h00 | Bilan quotidien (signaux, win rate) |

---

## Structure des fichiers

```
pulsefx_gold_mt5.py   ← bot principal
requirements.txt      ← dépendances Python
.env                  ← variables d'environnement (ne pas commiter)
Procfile              ← Railway
railway.toml          ← Railway
README.md             ← ce fichier
```

---

## Variables d'environnement — récapitulatif

| Variable | Obligatoire | Description |
|---------|-------------|-------------|
| `TELEGRAM_BOT_TOKEN` | ✅ | Token BotFather |
| `TELEGRAM_CHAT_ID` | ✅ | ID du canal/groupe |
| `CHANNEL_NAME` | ⬜ | Nom affiché dans les messages |
| `TWELVEDATA_API_KEY` | ✅ | Clé API TwelveData |
| `MT5_LOGIN` | ⬜ | Numéro de compte MT5 |
| `MT5_PASSWORD` | ⬜ | Mot de passe MT5 |
| `MT5_SERVER` | ⬜ | Serveur broker MT5 |

---

## Troubleshooting

**Le bot ne se lance pas**
→ Vérifier que `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` et `TWELVEDATA_API_KEY` sont bien définis.

**Pas de signaux**
→ Normal hors sessions (08-12h et 13-17h UTC) et le weekend.
→ Vérifier que le marché n'est pas en RANGE et que le score atteint 25.

**MT5 non connecté**
→ Vérifier que MetaTrader 5 est installé et ouvert, et que les credentials `.env` sont corrects.
→ Sur Railway/Linux, MT5 n'est pas disponible — le bot tourne en signal-only automatiquement.

**Spread trop large**
→ Le bot ignore automatiquement les trades si spread > 50 points. Régler `MAX_SPREAD_POINTS_GOLD` selon ton broker.
