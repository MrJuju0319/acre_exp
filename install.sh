#!/usr/bin/env bash
set -euo pipefail

# =============================
# ACRE SPC42 → MQTT installer
# =============================

C_RESET="\033[0m"; C_GREEN="\033[1;32m"; C_YELLOW="\033[1;33m"; C_BLUE="\033[1;34m"; C_RED="\033[1;31m"

REPO_URL="${REPO_URL:-https://github.com/MrJuju0319/acre_exp.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
SRC_DIR="/usr/local/src/acre_exp"
VENV_DIR="/opt/spc-venv"
ETC_DIR="/etc/acre_exp"
STATE_DIR="/var/lib/acre_exp"
BIN_STATUS="/usr/local/bin/acre_exp_status.py"
BIN_WATCHDOG="/usr/local/bin/acre_exp_watchdog.py"
SERVICE_FILE="/etc/systemd/system/acre-exp-watchdog.service"
CFG_FILE="${ETC_DIR}/config.yml"

ASSUME_YES="${ASSUME_YES:-false}"
MODE="${1:-}"   # --install | --update | --help

usage() {
  cat <<EOF
Usage:
  $0 --install [--yes]
  $0 --update

Variables optionnelles (exportables avant exécution) :
  REPO_URL, REPO_BRANCH
  SPC_HOST, SPC_USER, SPC_PIN, SPC_LANG, MIN_LOGIN_INTERVAL
  MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASS, MQTT_BASE_TOPIC, MQTT_CLIENT_ID, MQTT_QOS, MQTT_RETAIN
  WD_REFRESH, WD_LOG_CHANGES
EOF
}

if [[ "${MODE}" == "--help" || -z "${MODE}" ]]; then usage; exit 0; fi
if [[ $EUID -ne 0 ]]; then echo -e "${C_RED}[ERREUR]${C_RESET} Exécute en root."; exit 1; fi

ask() { local prompt="$1"; local def="$2"; local var; if [[ "$ASSUME_YES" == "true" ]]; then echo -e "${C_BLUE}${prompt}${C_RESET} (${def})"; echo "$def"; return 0; fi; read -rp "$(echo -e ${C_BLUE}${prompt}${C_RESET}" [${def}] : ")" var || true; echo "${var:-$def}"; }
confirm() { local p="$1"; if [[ "$ASSUME_YES" == "true" ]]; then return 0; fi; read -rp "$(echo -e ${C_BLUE}${p}${C_RESET} [o/N] : )" yn || true; [[ "${yn,,}" == o || "${yn,,}" == oui || "${yn,,}" == y || "${yn,,}" == yes ]]; }
line() { echo -e "${C_YELLOW}------------------------------------------------------------${C_RESET}"; }

normalize_repo_files() {
  # 1) Convertir CRLF -> LF
  find "$SRC_DIR" -type f \( -name "*.sh" -o -name "*.py" -o -name "*.service" \) -exec sed -i 's/\r$//' {} +

  # 2) Retirer un éventuel BOM UTF-8 sur les fichiers exécutables + service
  for f in install.sh acre_exp_status.py acre_exp_watchdog.py acre-exp-watchdog.service; do
    local p="$SRC_DIR/$f"
    [[ -f "$p" ]] || continue
    awk 'NR==1{sub(/^\xef\xbb\xbf/,"")}{print}' "$p" > "$p.tmp" && mv "$p.tmp" "$p"
  done
}

echo -e "${C_GREEN}>>> Vérification des paquets système...${C_RESET}"
PKGS=(git python3 python3-venv python3-pip jq)
if command -v apt-get >/dev/null 2>&1; then
  MISSING=(); for p in "${PKGS[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || MISSING+=("$p"); done
  if (( ${#MISSING[@]} )); then apt-get update -y; apt-get install -y "${MISSING[@]}"; fi
else
  echo -e "${C_YELLOW}[ATTENTION] Pas d'APT détecté. Assure-toi d'avoir:${C_RESET} ${PKGS[*]}"
fi

mkdir -p "$ETC_DIR" "$STATE_DIR" "$(dirname "$SRC_DIR")"
chmod 755 "$ETC_DIR" "$STATE_DIR"

# --- Clone ou pull ---
if [[ ! -d "$SRC_DIR/.git" ]]; then
  echo -e "${C_GREEN}>>> Clonage du dépôt${C_RESET} ${REPO_URL} (${REPO_BRANCH})"
  git clone --branch "$REPO_BRANCH" --depth 1 "$REPO_URL" "$SRC_DIR"
else
  echo -e "${C_GREEN}>>> Mise à jour du dépôt${C_RESET} $SRC_DIR"
  git -C "$SRC_DIR" fetch --depth 1 origin "$REPO_BRANCH"
  git -C "$SRC_DIR" reset --hard "origin/${REPO_BRANCH}"
fi

# --- Normalisation CRLF/BOM — auto-heal ---
normalize_repo_files

# --- venv ---
echo -e "${C_GREEN}>>> Préparation du venv Python:${C_RESET} ${VENV_DIR}"
if [[ ! -d "$VENV_DIR" ]]; then python3 -m venv "$VENV_DIR"; fi
"${VENV_DIR}/bin/python" -m pip install --upgrade pip >/dev/null

echo -e "${C_GREEN}>>> Installation deps Python (requests, bs4, pyyaml, paho-mqtt v2.x)${C_RESET}"
"${VENV_DIR}/bin/pip" install --quiet requests beautifulsoup4 pyyaml "paho-mqtt>=2,<3"

# --- Sanity check paho v2 + API V5 ---
"${VENV_DIR}/bin/python" - <<'PY'
import paho.mqtt.client as m
from paho.mqtt.client import CallbackAPIVersion as C
assert hasattr(C, "V5"), "CallbackAPIVersion.V5 indisponible (paho-mqtt v2 attendu)"
print("paho-mqtt OK:", m.__version__)
PY

# --- Config (uniquement en --install) ---
if [[ "${MODE}" == "--install" ]]; then
  write_cfg=true
  if [[ -f "$CFG_FILE" ]]; then
    echo -e "${C_YELLOW}Config existante détectée: ${CFG_FILE}${C_RESET}"
    if ! confirm "Régénérer la configuration ? (sinon on garde l'existante)"; then write_cfg=false; fi
  fi
  if [[ "$write_cfg" == "true" ]]; then
    echo -e "${C_GREEN}>>> Paramétrage...${C_RESET}"
    SPC_HOST_DEFAULT="${SPC_HOST:-http://192.168.1.100}"
    SPC_USER_DEFAULT="${SPC_USER:-Engineer}"
    SPC_PIN_DEFAULT="${SPC_PIN:-1111}"
    SPC_LANG_DEFAULT="${SPC_LANG:-253}"
    MIN_LOGIN_INTERVAL_DEFAULT="${MIN_LOGIN_INTERVAL:-60}"

    MQTT_HOST_DEFAULT="${MQTT_HOST:-127.0.0.1}"
    MQTT_PORT_DEFAULT="${MQTT_PORT:-1883}"
    MQTT_USER_DEFAULT="${MQTT_USER:-}"
    MQTT_PASS_DEFAULT="${MQTT_PASS:-}"
    MQTT_BASE_DEFAULT="${MQTT_BASE_TOPIC:-acre_XXX}"
    MQTT_CLIENT_ID_DEFAULT="${MQTT_CLIENT_ID:-acre-exp}"
    MQTT_QOS_DEFAULT="${MQTT_QOS:-0}"
    MQTT_RETAIN_DEFAULT="${MQTT_RETAIN:-true}"

    WD_REFRESH_DEFAULT="${WD_REFRESH:-2}"
    WD_LOG_DEFAULT="${WD_LOG_CHANGES:-true}"

    SPC_HOST="$(ask "Adresse de la centrale (http://IP:PORT)" "$SPC_HOST_DEFAULT")"
    SPC_USER="$(ask "Code utilisateur (ID Web)" "$SPC_USER_DEFAULT")"
    SPC_PIN="$(ask "Mot de passe / PIN" "$SPC_PIN_DEFAULT")"
    SPC_LANG="$(ask "Langue (253=Def user, 2=FR, 0=EN)" "$SPC_LANG_DEFAULT")"
    MIN_LOGIN_INTERVAL="$(ask "Délai minimum entre relogins (sec)" "$MIN_LOGIN_INTERVAL_DEFAULT")"

    MQTT_HOST="$(ask "MQTT hôte" "$MQTT_HOST_DEFAULT")"
    MQTT_PORT="$(ask "MQTT port" "$MQTT_PORT_DEFAULT")"
    MQTT_USER="$(ask "MQTT user (vide si N/A)" "$MQTT_USER_DEFAULT")"
    MQTT_PASS="$(ask "MQTT pass (vide si N/A)" "$MQTT_PASS_DEFAULT")"
    MQTT_BASE_TOPIC="$(ask "MQTT base topic" "$MQTT_BASE_DEFAULT")"
    MQTT_CLIENT_ID="$(ask "MQTT client_id" "$MQTT_CLIENT_ID_DEFAULT")"
    MQTT_QOS="$(ask "MQTT QoS (0/1/2)" "$MQTT_QOS_DEFAULT")"
    MQTT_RETAIN="$(ask "MQTT retain (true/false)" "$MQTT_RETAIN_DEFAULT")"

    WD_REFRESH="$(ask "Intervalle de refresh watchdog (sec)" "$WD_REFRESH_DEFAULT")"
    WD_LOG_CHANGES="$(ask "Logs des changements (true/false)" "$WD_LOG_DEFAULT")"

    echo -e "${C_GREEN}>>> Écriture: ${CFG_FILE}${C_RESET}"
    cat > "$CFG_FILE" <<YAML
spc:
  host: "${SPC_HOST}"
  user: "${SPC_USER}"
  pin: "${SPC_PIN}"
  language: ${SPC_LANG}
  session_cache_dir: "${STATE_DIR}"
  min_login_interval_sec: ${MIN_LOGIN_INTERVAL}

mqtt:
  host: "${MQTT_HOST}"
  port: ${MQTT_PORT}
  user: "${MQTT_USER}"
  pass: "${MQTT_PASS}"
  base_topic: "${MQTT_BASE_TOPIC}"
  client_id: "${MQTT_CLIENT_ID}"
  qos: ${MQTT_QOS}
  retain: ${MQTT_RETAIN}
  # protocol: v311 | v5  (défaut v311)

watchdog:
  refresh_interval: ${WD_REFRESH}
  log_changes: ${WD_LOG_CHANGES}
YAML
    chmod 640 "$CFG_FILE"
  else
    echo -e "${C_GREEN}>>> On conserve la configuration existante.${C_RESET}"
  fi
fi

# --- Installation des scripts (copie) ---
echo -e "${C_GREEN}>>> Installation des scripts${C_RESET}"
install -m 0755 "$SRC_DIR/acre_exp_status.py"   "$BIN_STATUS"
install -m 0755 "$SRC_DIR/acre_exp_watchdog.py" "$BIN_WATCHDOG"

# --- Shebangs vers le venv (sans options) ---
sed -i "1s|^#!.*python.*$|#!${VENV_DIR}/bin/python3|" "$BIN_STATUS"
sed -i "1s|^#!.*python.*$|#!${VENV_DIR}/bin/python3|" "$BIN_WATCHDOG"

# --- Service systemd ---
echo -e "${C_GREEN}>>> Installation du service systemd${C_RESET}"
cat > "$SERVICE_FILE" <<'SYSTEMD'
[Unit]
Description=ACRE SPC42 -> MQTT Watchdog (zones + secteurs)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/acre_exp_watchdog.py -c /etc/acre_exp/config.yml
Restart=always
RestartSec=3

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ProtectKernelModules=true
ProtectKernelTunables=true
ProtectControlGroups=true
LockPersonality=true
MemoryDenyWriteExecute=true
CapabilityBoundingSet=
AmbientCapabilities=
ReadWritePaths=/var/lib/acre_exp /etc/acre_exp

User=root
Group=root

[Install]
WantedBy=multi-user.target
SYSTEMD

systemctl daemon-reload
if [[ "${MODE}" == "--install" ]]; then
  systemctl enable --now acre-exp-watchdog.service
elif [[ "${MODE}" == "--update" ]]; then
  systemctl restart acre-exp-watchdog.service
fi

line
echo -e "${C_GREEN}OK.${C_RESET}  Service: ${C_YELLOW}systemctl status acre-exp-watchdog.service${C_RESET}"
echo -e "Logs:     ${C_YELLOW}journalctl -u acre-exp-watchdog.service -f -n 100${C_RESET}"
echo -e "Test JSON:${C_YELLOW}${BIN_STATUS} -c ${CFG_FILE} | jq .${C_RESET}"
echo -e "MQTT sub: ${C_YELLOW}mosquitto_sub -h \${MQTT_HOST:-127.0.0.1} -t '\${MQTT_BASE_TOPIC:-acre_XXX}/#' -v${C_RESET}"
line
