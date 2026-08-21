# Bot Trading Avancé 🤖

Un bot Telegram qui analyse les marchés financiers en temps réel en utilisant des indicateurs techniques (EMA et RSI).

## Caractéristiques

- 📊 Analyse technique multi-timeframe (1min, 5min, 15min, 30min, 1h)
- 📈 Indicateurs: EMA (20, 50) et RSI (14)
- 🎯 Signaux de trading: BUY, SELL, WAIT
- 🔄 Signaux confirmés (consensus multi-timeframe)
- ⚡ Requêtes asynchrones pour performance
- 🔒 Gestion sécurisée des variables d'environnement
- 📝 Logging complet et gestion d'erreurs

## Actifs supportés

- 🥇 **Gold** - XAU/USD
- 💱 **EUR/USD** - Forex
- ₿ **Bitcoin** - BTC/USD

## Installation

### Prérequis

- Python 3.8+
- Compte Telegram avec bot token
- Clé API Twelve Data

### Étapes

1. **Cloner le repository**
```bash
git clone https://github.com/allureluxe/Bot.git
cd Bot
```

2. **Créer un environnement virtuel**
```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
# ou
venv\Scripts\activate  # Windows
```

3. **Installer les dépendances**
```bash
pip install -r requirements.txt
```

4. **Configurer les variables d'environnement**
```bash
cp .env.example .env
```

Éditer `.env` et ajouter:
```
BOT_TOKEN=votre_token_telegram
TWELVE_API_KEY=votre_clé_api_twelve_data
```

5. **Lancer le bot**
```bash
python main.py
```

## Utilisation

Sur Telegram, utilisez les commandes:

- `/start` - Affiche l'aide
- `/gold` - Analyse XAU/USD
- `/eurusd` - Analyse EUR/USD
- `/btc` - Analyse BTC/USD

## Méthodologie

### Signaux
- **BUY** ✅: Prix > EMA20 > EMA50 ET RSI > 55
- **SELL** ❌: Prix < EMA20 < EMA50 ET RSI < 45
- **WAIT** ⏸️: Autres cas

### Confirmation
- Signal global confirmé si consensus ≥ 3 timeframes
- Sinon: WAIT

## Sécurité ⚠️

- ✅ Pas de secrets en dur dans le code
- ✅ Variables d'environnement via `.env`
- ✅ `.gitignore` pour `.env`
- ✅ Gestion d'erreurs robuste
- ✅ Timeout sur les requêtes API

## Structure du projet

```
Bot/
├── main.py              # Code principal du bot
├── requirements.txt     # Dépendances Python
├── .env.example        # Exemple de configuration
├── .gitignore          # Fichiers à ignorer
└── README.md           # Ce fichier
```

## Logs

Les logs incluent:
- ✅ Démarrage/arrêt du bot
- ⚠️ Erreurs API
- 📊 Analyses effectuées
- ❌ Problèmes de données

## Performance

- ⚡ Requêtes asynchrones (non-bloquantes)
- 🔄 Timeout 10s par requête API
- 📦 Données en cache via API
- ⚙️ Optimisé pour VPS/serveurs

## Limitations

- ⏱️ Limite de taux API Twelve Data (à vérifier)
- 📊 Historique limité à 100 bougies par défaut
- 🌍 Dépend de la disponibilité de l'API

## Déploiement 24/7 (Railway / Render) 🚀

Pour que le bot réponde **en permanence** à `/start`, il doit tourner sur un **vrai serveur**, pas seulement dans GitHub Actions (qui s'arrête après quelques heures).

### Option 1 — Railway (recommandé, gratuit)

1. Va sur [railway.app](https://railway.app) et connecte ton compte GitHub
2. Clique **New Project → Deploy from GitHub Repo → `allureluxe/Bot`**
3. Dans **Variables**, ajoute :
   - `BOT_TOKEN` = ton token Telegram
   - `BINANCE_API_KEY` = ta clé Binance
   - `BINANCE_SECRET_KEY` = ton secret Binance
   - `TWELVE_API_KEY` = ta clé Twelve Data (si tu utilises `main.py`)
4. Dans **Settings → Start Command**, mets : `python trading_bot.py`
5. Clique **Deploy** → le bot tourne 24/7 automatiquement

### Option 2 — Render (gratuit)

1. Va sur [render.com](https://render.com) → **New → Background Worker**
2. Connecte ton repo GitHub `allureluxe/Bot`
3. **Build Command** : `pip install -r requirements.txt`
4. **Start Command** : `python trading_bot.py`
5. Ajoute les variables d'environnement dans l'onglet **Environment**
6. Clique **Create Background Worker**

### Option 3 — GitHub Actions (temporaire, redémarre toutes les 5h)

Le workflow `.github/workflows/bot.yml` est configuré pour relancer le bot toutes les 5 heures automatiquement. C'est suffisant pour tester, mais **pas recommandé pour un usage permanent** car il y a des délais entre les redémarrages.

### Variables d'environnement requises

| Variable | Description |
|---|---|
| `BOT_TOKEN` | Token du bot Telegram (via [@BotFather](https://t.me/BotFather)) |
| `BINANCE_API_KEY` | Clé API Binance |
| `BINANCE_SECRET_KEY` | Secret Binance |
| `TWELVE_API_KEY` | Clé API Twelve Data (pour `main.py`) |



Pour signaler des bugs ou proposer des améliorations, ouvrez une issue sur GitHub.

## Licence

MIT

---

**⚡ Bot créé avec python-telegram-bot et Twelve Data API**
