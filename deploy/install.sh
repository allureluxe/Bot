#!/usr/bin/env bash
#
# Installation du bot de scalping sur un serveur Debian / Ubuntu (OVH, Hetzner...).
#
#   bash deploy/install.sh
#
# Le script ne demarre PAS le bot : il prepare tout, verifie que Binance est
# joignable depuis ce serveur, puis affiche les deux commandes finales.
#
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/allureluxe/Bot.git}"
BRANCH="${BRANCH:-claude/trading-bots-scalping-rindfe}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/scalper}"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m[OK]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[STOP]\033[0m %s\n' "$*" >&2; exit 1; }

# --- 1. Verification geographique -----------------------------------------
# C'est le test le plus important : Binance renvoie HTTP 451 depuis les pays
# restreints (Etats-Unis, Canada...). Inutile d'installer si c'est le cas.
say "Verification : Binance est-il joignable depuis ce serveur ?"
command -v curl >/dev/null 2>&1 || die "curl est absent. Installez-le : sudo apt install -y curl"

HTTP_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 https://api.binance.com/api/v3/ping || true)"
HTTP_CODE="$(printf '%s' "$HTTP_CODE" | tr -cd '0-9')"
[ -z "$HTTP_CODE" ] && HTTP_CODE="000"

case "$HTTP_CODE" in
  200)
    ok "Binance repond (HTTP 200). Ce serveur est dans une region autorisee."
    ;;
  451)
    die "Binance BLOQUE ce serveur (HTTP 451).
      L'adresse IP est dans une region restreinte. Chez OVH, cela arrive avec
      les datacenters hors Europe (Canada/BHS, notamment).
      Solution : utilisez un serveur dans un datacenter europeen
      (Gravelines/GRA, Roubaix/RBX, Strasbourg/SBG, Limburg/LIM)."
    ;;
  000)
    die "Aucune reponse de Binance (timeout ou DNS).
      Verifiez la connectivite sortante du serveur et le pare-feu."
    ;;
  *)
    warn "Reponse inattendue de Binance : HTTP $HTTP_CODE. On continue, mais
      lancez 'python run_scalper.py --check' apres l'installation pour en
      avoir le coeur net."
    ;;
esac

# --- 2. Dependances systeme ------------------------------------------------
say "Installation des dependances systeme"
if command -v apt-get >/dev/null 2>&1; then
  SUDO=""
  [ "$(id -u)" -ne 0 ] && SUDO="sudo"
  $SUDO apt-get update -qq || warn "apt-get update partiellement en echec (depot tiers ?), on continue"
  $SUDO apt-get install -y -qq git python3 python3-venv python3-pip
  ok "git et python3 installes"
else
  warn "apt-get introuvable : installez git et python3 (>= 3.9) manuellement."
fi

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "Python 3.9 minimum requis (version detectee : $(python3 --version))"
ok "$(python3 --version)"

# --- 3. Recuperation du code ----------------------------------------------
say "Recuperation du code dans $INSTALL_DIR"
if [ -d "$INSTALL_DIR/.git" ]; then
  git -C "$INSTALL_DIR" fetch origin "$BRANCH"
  git -C "$INSTALL_DIR" checkout "$BRANCH"
  git -C "$INSTALL_DIR" pull origin "$BRANCH"
  ok "Depot mis a jour"
else
  git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
  ok "Depot clone"
fi

cd "$INSTALL_DIR"

# --- 4. Environnement Python ----------------------------------------------
say "Creation de l'environnement Python"
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet -r requirements.txt
ok "Dependances Python installees"

# --- 5. Configuration ------------------------------------------------------
if [ -f .env ]; then
  ok ".env existant conserve (aucune valeur ecrasee)"
else
  cp .env.example .env
  chmod 600 .env
  ok ".env cree depuis .env.example (DRY_RUN=true par defaut)"
fi

# --- 6. Tests --------------------------------------------------------------
say "Execution de la suite de tests"
if ./venv/bin/python -m unittest discover -s tests -t . >/dev/null 2>&1; then
  ok "Tous les tests passent"
else
  warn "Des tests echouent : lancez './venv/bin/python -m unittest discover -s tests -t . -v'"
fi

# --- 7. Unit systemd pretes a l'emploi ------------------------------------
say "Generation du service systemd"
cat > /tmp/scalper.service <<UNIT
[Unit]
Description=Bot de scalping Binance
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(id -un)
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
ExecStart=$INSTALL_DIR/venv/bin/python run_scalper.py
Restart=always
RestartSec=15
TimeoutStopSec=30
KillSignal=SIGTERM
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT
ok "Service genere dans /tmp/scalper.service"

# --- 8. Diagnostic ---------------------------------------------------------
say "Diagnostic de demarrage"
./venv/bin/python run_scalper.py --check || true

cat <<FIN

==================================================================
  INSTALLATION TERMINEE
==================================================================

Le bot n'est PAS encore demarre. Etapes restantes :

1. Verifier la strategie sur l'historique reel :
     cd $INSTALL_DIR && ./venv/bin/python run_scalper.py --backtest

2. Lancer en SIMULATION (aucun ordre reel, DRY_RUN=true par defaut) :
     ./venv/bin/python run_scalper.py

3. Pour le faire tourner en permanence :
     sudo cp /tmp/scalper.service /etc/systemd/system/
     sudo systemctl daemon-reload
     sudo systemctl enable --now scalper
     journalctl -u scalper -f

4. Passer en REEL seulement apres plusieurs jours de simulation
   concluants : editez $INSTALL_DIR/.env, mettez DRY_RUN=false et
   renseignez BINANCE_API_KEY / BINANCE_SECRET_KEY, puis :
     sudo systemctl restart scalper

Le journal des trades est dans $INSTALL_DIR/state/trades.csv
==================================================================
FIN
