# Bot de scalping Binance Spot

Bot de trading autonome : il scanne le marché en continu, ouvre des positions,
et **ferme réellement** ses positions sur stop loss, take profit ou stop
suiveur. Le dimensionnement des lots est progressif quand ça gagne, régressif
quand ça perd.

---

## Pourquoi les anciens bots ne prenaient jamais de position

Le dépôt contenait deux bots. Voici, vérifié dans le code, pourquoi aucune
position n'a jamais été ouverte.

| # | Problème | Fichier | Conséquence |
|---|---|---|---|
| 1 | **Aucune boucle de trading.** `/trade` faisait **un seul** passage puis s'arrêtait. | `trading_bot.py` | Il fallait retaper `/trade` à la main pour chaque tentative. « 10 à 1000 trades par jour » était impossible. |
| 2 | **Stop loss et take profit jamais exécutés.** `STOP_LOSS_PERCENT` et `TAKE_PROFIT_PERCENT` étaient affichés dans les messages mais n'apparaissaient dans aucun calcul. | `trading_bot.py` | Une position ouverte ne se refermait jamais, quoi qu'il arrive. |
| 3 | **RSI calculé sur les mauvaises bougies.** La boucle `for i in range(1, period + 1)` ne lisait que les **15 plus anciennes** valeurs du tampon. Avec 100 bougies, le RSI datait de ~85 minutes. | `main.py`, `trading_bot.py` | Le filtre RSI réagissait à un marché qui n'existait plus. |
| 4 | **Binance bloque GitHub Actions (HTTP 451).** Les runners GitHub sont aux États-Unis, zone restreinte pour Binance. | `.github/workflows/bot.yml` | Toutes les requêtes échouaient. Le bot tournait sans jamais joindre Binance. |
| 5 | **Positions perdues à chaque redémarrage.** `active_trades` était un dictionnaire en mémoire, et le workflow redémarrait toutes les 5 heures. | `trading_bot.py` | Une position ouverte devenait orpheline et invendable, car `SELL` exigeait que le symbole soit dans `active_trades`. |
| 6 | **Univers de trading quasi vide.** `QUOTE_ASSET = "USDC"` combiné à `MIN_QUOTE_VOLUME = 1 000 000`. Les paires USDC sont bien moins liquides que les USDT. | `trading_bot.py` | Très peu de paires, parfois zéro, passaient le filtre. |
| 7 | **Conditions d'entrée trop strictes.** `prix > EMA20 > EMA50 ET RSI > 55` exigé simultanément sur 2 des 3 timeframes — avec le RSI faux du point 3. | `trading_bot.py` | Signal quasiment jamais atteint. |
| 8 | **Horodatage en heure locale.** `datetime.now()` au lieu de l'heure UTC de Binance. | `trading_bot.py` | Erreur `-1021 recvWindow` dès que le serveur n'est pas en UTC. |
| 9 | **Une session HTTP créée par requête**, des centaines de requêtes séquentielles par scan. | `trading_bot.py` | Bannissement pour dépassement de quota (`-1003`). |
| 10 | **Aucun money management.** Montant fixe borné entre 5 et 25 USDC, sans lien avec la distance au stop. | `trading_bot.py` | Le risque réel variait du simple au décuple d'un trade à l'autre. |

Les points 3 et 8 sont **corrigés** dans `main.py` et `trading_bot.py`. Le reste
demandait une réécriture : c'est `run_scalper.py`.

---

## Démarrage rapide

```bash
git clone https://github.com/allureluxe/Bot.git
cd Bot
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### 1. Vérifier que tout est joignable

```bash
python run_scalper.py --check
```

Ce diagnostic teste, dans l'ordre : cohérence de la configuration, joignabilité
de Binance, synchronisation d'horloge, taille de l'univers de trading, validité
des clés API, permissions, et solde suffisant. **Chaque échec est expliqué en
clair avec la façon de le corriger.** Plus jamais un bot silencieux dont on
ignore pourquoi il ne fait rien.

### 2. Vérifier que la stratégie déclenche

```bash
python run_scalper.py --backtest
```

Rejoue la stratégie sur les 1000 dernières bougies des 8 paires les plus
liquides et affiche le nombre de trades, la fréquence par jour, le taux de
réussite et le drawdown maximum. **Si le résultat est 0 trade, le backtest vous
dit exactement quelle condition bloque.**

### 3. Lancer en simulation

```bash
python run_scalper.py
```

Avec `DRY_RUN=true` (valeur par défaut), aucun ordre n'est envoyé à Binance :
le bot utilise les vrais prix du marché mais un capital fictif. **Laissez-le
tourner quelques jours ainsi et regardez `state/trades.csv` avant d'envisager
de l'argent réel.**

### 4. Passer en réel

Mettez `DRY_RUN=false` dans `.env`, et seulement une fois les étapes 1 à 3
concluantes.

---

## Comment il trade

### Sélection des paires

À chaque cycle, l'univers est reconstruit : paires cotées en `QUOTE_ASSET`,
statut `TRADING`, hors tokens à effet de levier, volume 24 h supérieur à
`MIN_QUOTE_VOLUME`, triées par liquidité décroissante et limitées à
`MAX_UNIVERSE`. Le spread bid/ask est vérifié juste avant l'analyse — un spread
large mange tout le gain d'un scalp.

### Entrée : un score, pas un couperet

L'ancien bot exigeait que **toutes** les conditions soient vraies en même
temps. Ici chaque condition vaut un point sur 6 :

1. EMA rapide au-dessus de l'EMA lente
2. Prix au-dessus de l'EMA rapide
3. Croisement haussier récent (ou prix au-dessus de l'EMA lente)
4. RSI dans la zone d'achat (`RSI_LONG_MIN` à `RSI_LONG_MAX`)
5. Volume supérieur à la moyenne (`VOLUME_SPIKE_MULT`)
6. Momentum positif sur 3 bougies

L'entrée se déclenche dès `MIN_ENTRY_SCORE`. **C'est le réglage d'agressivité :
`3` déclenche beaucoup, `5` très peu.**

Deux filtres restent bloquants quel que soit le score : la volatilité doit être
entre `MIN_ATR_PCT` et `MAX_ATR_PCT`, et la tendance sur `TREND_TIMEFRAME` doit
être haussière si `REQUIRE_TREND_FILTER=true`.

### Sortie : quatre mécanismes qui s'exécutent vraiment

Une boucle indépendante relit le prix de chaque position toutes les
`MONITOR_INTERVAL_SECONDS` (2 s par défaut) et envoie un ordre de vente au
marché dès qu'un seuil est franchi :

- **Stop loss** — `SL_ATR_MULT × ATR` sous l'entrée, borné entre `MIN_SL_PCT` et `MAX_SL_PCT`
- **Take profit** — `TP_ATR_MULT × ATR` au-dessus de l'entrée
- **Point mort** — dès `BREAKEVEN_AT_R` atteint, le stop remonte au-dessus du prix d'entrée, frais compris
- **Stop suiveur** — au-delà de `TRAIL_ACTIVATE_R`, le stop suit le plus haut atteint
- **Timeout** — au-delà de `MAX_HOLD_SECONDS`, la position est soldée : un scalp qui traîne n'est plus un scalp

Le stop ne redescend jamais.

### Le seuil de rentabilité, souvent oublié

Binance prélève 0,1 % à l'achat **et** 0,1 % à la vente, soit **0,2 % par
aller-retour**. Un scalp qui vise 0,15 % de gain est perdant même quand il
touche son objectif. Le bot refuse donc tout trade dont le take profit est
inférieur à `FEE_RATE × 2 × FEE_SAFETY_MULT`.

C'est aussi ce qui limite le nombre de trades réellement rentables par jour :
1000 allers-retours coûtent 200 % du capital engagé en frais. Réduire
`MIN_ENTRY_SCORE` augmente la fréquence, mais les frais augmentent d'autant.
Le backtest chiffre exactement ce compromis (colonne `Frais`).

---

## Money management

### Le lot découle du risque, pas l'inverse

```
quantité = (capital × risque%) / distance au stop
```

Une perte coûte le même pourcentage du capital, que le stop soit à 0,3 % ou à
1,2 %. L'ancien bot prenait un montant fixe de 5 à 25 USDC sans regarder le
stop : le risque réel variait du simple au décuple.

### Progressif

- **Le lot suit le capital.** Le risque étant un pourcentage, la taille grandit
  mécaniquement quand le compte grandit (capitalisation).
- **Séries gagnantes.** Chaque gain consécutif multiplie le risque par
  `WIN_STREAK_STEP` (1,15 par défaut), plafonné à `MAX_STREAK_MULT`.

### Régressif

- **Séries perdantes.** Chaque perte consécutive multiplie le risque par
  `LOSS_STREAK_STEP` (0,70 par défaut), avec un plancher `MIN_STREAK_MULT`.
  La configuration **refuse de démarrer** si `LOSS_STREAK_STEP ≥ 1` : augmenter
  la mise après une perte (martingale) est le moyen le plus rapide de vider un
  compte.
- **Capital élevé, risque réduit.** `EQUITY_DECAY` fait décroître le risque en
  pourcentage à mesure que le capital grossit. Mettre `0` pour un risque
  constant.
- **Drawdown.** Au-delà de `DAILY_SOFT_LOSS_PCT` de perte sur la journée, le
  risque est divisé par deux automatiquement.

Le risque final est toujours borné entre `MIN_RISK_PCT` et `MAX_RISK_PCT`.

### Coupe-circuits

| Garde-fou | Variable | Effet |
|---|---|---|
| Perte journalière maximale | `DAILY_MAX_LOSS_PCT` | Arrêt total des entrées jusqu'au lendemain |
| Objectif journalier | `DAILY_PROFIT_TARGET_PCT` | Arrêt une fois l'objectif atteint (`0` = désactivé) |
| Pertes consécutives | `MAX_CONSECUTIVE_LOSSES` | Pause de sécurité |
| Positions simultanées | `MAX_OPEN_POSITIONS` | Limite l'exposition |
| Exposition totale | `MAX_TOTAL_EXPOSURE_PCT` | Garde toujours des liquidités |
| Taille d'une position | `MAX_POSITION_PCT` | Aucune position ne peut dominer le compte |
| Cooldown après perte | `SYMBOL_COOLDOWN_SECONDS` | Évite l'acharnement sur une paire |

Les compteurs journaliers se remettent à zéro au changement de jour UTC.

---

## Déploiement 24/7

**GitHub Actions ne peut pas faire tourner ce bot.** Binance répond HTTP 451
aux adresses IP des runners, et chaque run repart d'un conteneur vierge. Le
workflow du dépôt a été converti en CI : il lance les tests, rien d'autre.

Il faut un serveur qui tourne en permanence, dans une région non restreinte par
Binance (Europe, Asie). Un VPS à quelques euros par mois suffit.

### systemd (recommandé)

```bash
sudo cp deploy/scalper.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now scalper
journalctl -u scalper -f
```

Le bot recharge son état au démarrage : un redémarrage ne perd aucune position.

### Docker

```bash
docker compose up -d
docker compose logs -f
```

Le volume `./state` doit être monté, sinon les positions sont perdues au
redémarrage — exactement le défaut de l'ancienne version.

---

## Sécurité des clés API

Sur Binance, dans les paramètres de la clé :

- Activer **uniquement** « Enable Spot & Margin Trading »
- **Ne jamais** activer « Enable Withdrawals »
- Restreindre l'accès à l'adresse IP de votre serveur

Le fichier `.env` est dans `.gitignore`. Ne le commitez jamais. Si une clé a
été exposée, révoquez-la immédiatement sur Binance.

---

## Suivi

- `state/trades.csv` — journal de chaque trade : entrée, sortie, PnL, R
  multiple, frais, motif de sortie, durée
- `state/scalper_state.json` — positions ouvertes, séries, statistiques du jour
- Toutes les 5 minutes, une ligne d'état résume capital, positions, signaux et
  performance du jour
- Si aucun signal n'apparaît, le bot **affiche les motifs de refus** pour que
  vous sachiez quel réglage assouplir

Notifications Telegram optionnelles à chaque entrée et sortie : renseignez
`BOT_TOKEN` et `TELEGRAM_CHAT_ID`.

---

## Réglage de l'agressivité

| Objectif | Réglage |
|---|---|
| Plus de trades | Baisser `MIN_ENTRY_SCORE` (4 → 3) |
| Beaucoup plus de trades | `REQUIRE_TREND_FILTER=false` |
| Plus de paires surveillées | Baisser `MIN_QUOTE_VOLUME`, monter `MAX_UNIVERSE` |
| Trades plus courts | Baisser `TP_ATR_MULT` et `MAX_HOLD_SECONDS` |
| Réaction plus rapide | Baisser `SCAN_INTERVAL_SECONDS` |
| Moins de risque | Baisser `BASE_RISK_PCT` et `MAX_OPEN_POSITIONS` |

Testez chaque changement avec `--backtest` avant de l'appliquer en réel.

---

## Tests

```bash
python -m unittest discover -s tests -t . -v
```

79 tests couvrent les indicateurs (dont une valeur de référence Wilder et une
régression sur le bug du RSI), le dimensionnement, les coupe-circuits, la
persistance, et le cycle complet du moteur : scan → ouverture → surveillance →
clôture, en simulation comme en mode ordres réels.

---

## Limites, dites franchement

- **Aucun bot ne garantit un profit.** Celui-ci applique une méthode de façon
  disciplinée ; il ne prédit pas le marché.
- **Les frais dominent le scalping.** À 0,2 % par aller-retour, la fréquence
  élevée est un coût, pas un avantage. Un bot qui fait 1000 trades par jour
  n'est pas meilleur qu'un bot qui en fait 20.
- **Long uniquement.** Binance Spot ne permet pas de vendre à découvert. En
  marché baissier, le bot reste majoritairement à l'écart — c'est voulu.
- **Le backtest est optimiste** malgré ses hypothèses prudentes : il ne modélise
  ni le slippage réel ni la profondeur du carnet d'ordres.
- **Le stop est logiciel.** Si le serveur tombe, le stop ne s'exécute pas.
  L'état est persisté et le stop reprend au redémarrage, mais un VPS fiable
  reste indispensable.
- **Ne risquez que ce que vous pouvez perdre.**

---

## Anciens bots

`main.py` (analyse Twelve Data sur XAU/USD, EUR/USD, BTC/USD via Telegram) et
`trading_bot.py` sont conservés, avec le bug de RSI corrigé. `trading_bot.py`
est remplacé par `run_scalper.py` et ne devrait plus être utilisé pour trader :
les problèmes 1, 2, 5, 6, 7, 9 et 10 du tableau ci-dessus y sont toujours
présents par conception.

## Licence

MIT
