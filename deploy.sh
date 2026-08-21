#!/bin/bash
# ============================================================
#  Trading Bot – Script d'installation automatique (OVH/VPS)
#  Usage : bash deploy.sh
# ============================================================
set -e

REPO="https://github.com/allureluxe/Bot.git"
INSTALL_DIR="$HOME/Bot"
SERVICE_NAME="trading-bot"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}   Trading Bot – Installation OVH       ${NC}"
echo -e "${GREEN}========================================${NC}\n"

# ── 1. Dépendances système ────────────────────────────────
echo -e "${YELLOW}[1/6] Installation des dépendances système...${NC}"
apt-get update -qq
apt-get install -y -qq python3 python3-pip git curl

# ── 2. Cloner / mettre à jour le repo ────────────────────
echo -e "${YELLOW}[2/6] Récupération du code depuis GitHub...${NC}"
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "  → Repo existant, mise à jour..."
    git -C "$INSTALL_DIR" pull --ff-only
else
    git clone "$REPO" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# ── 3. Packages Python ───────────────────────────────────
echo -e "${YELLOW}[3/6] Installation des packages Python...${NC}"
pip3 install -q -r requirements.txt

# ── 4. Fichier .env ──────────────────────────────────────
echo -e "${YELLOW}[4/6] Configuration des clés API...${NC}"

if [ -f "$INSTALL_DIR/.env" ]; then
    echo "  → Fichier .env existant – conservé."
else
    echo ""
    echo -e "${GREEN}Entrez vos clés API (elles ne seront PAS affichées) :${NC}"

    read -rp "  BOT_TOKEN Telegram       : " BOT_TOKEN
    read -rsp "  BINANCE_API_KEY          : " BINANCE_API_KEY
    echo ""
    read -rsp "  BINANCE_SECRET_KEY       : " BINANCE_SECRET_KEY
    echo ""

    cat > "$INSTALL_DIR/.env" <<EOF
BOT_TOKEN=${BOT_TOKEN}
BINANCE_API_KEY=${BINANCE_API_KEY}
BINANCE_SECRET_KEY=${BINANCE_SECRET_KEY}
EOF
    chmod 600 "$INSTALL_DIR/.env"
    echo "  → Fichier .env créé avec succès."
fi

# ── 5. Service systemd (démarrage auto + redémarrage) ────
echo -e "${YELLOW}[5/6] Création du service systemd...${NC}"

PYTHON_PATH=$(which python3)

cat > /etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=Binance Scalping Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
ExecStart=${PYTHON_PATH} ${INSTALL_DIR}/trading_bot.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ${SERVICE_NAME}
systemctl restart ${SERVICE_NAME}

# ── 6. Vérification ──────────────────────────────────────
echo -e "${YELLOW}[6/6] Vérification du statut...${NC}"
sleep 3

if systemctl is-active --quiet ${SERVICE_NAME}; then
    echo -e "\n${GREEN}✅ Bot démarré avec succès !${NC}"
    echo -e "${GREEN}   Il se relancera automatiquement au reboot.${NC}\n"
else
    echo -e "\n${RED}❌ Le bot n'a pas démarré. Vérifiez les logs :${NC}"
    echo -e "   journalctl -u ${SERVICE_NAME} -n 30\n"
    exit 1
fi

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Commandes utiles                      ${NC}"
echo -e "${GREEN}========================================${NC}"
echo -e "  Voir les logs en direct :"
echo -e "    ${YELLOW}journalctl -u ${SERVICE_NAME} -f${NC}"
echo -e "  Arrêter le bot :"
echo -e "    ${YELLOW}systemctl stop ${SERVICE_NAME}${NC}"
echo -e "  Redémarrer le bot :"
echo -e "    ${YELLOW}systemctl restart ${SERVICE_NAME}${NC}"
echo -e "  Statut :"
echo -e "    ${YELLOW}systemctl status ${SERVICE_NAME}${NC}"
echo ""
echo -e "${GREEN}→ Envoyez /status sur Telegram pour vérifier !${NC}\n"
