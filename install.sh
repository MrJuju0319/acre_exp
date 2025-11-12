#!/usr/bin/env -S bash -euo pipefail

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
  WD_REFRESH, WD_CONTROLLER_REFRESH, WD_LOG_CHANGES
  WD_INFO_ZONES, WD_INFO_SECTEURS, WD_INFO_DOORS, WD_INFO_OUTPUTS
  WD_CTRL_ZONES, WD_CTRL_SECTEURS, WD_CTRL_DOORS, WD_CTRL_OUTPUTS
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

ensure_controller_refresh_config() {
  [[ -f "$CFG_FILE" ]] || return 0
  if grep -q "^[[:space:]]*controller_refresh_interval:" "$CFG_FILE"; then
    return 0
  fi

  echo -e "${C_GREEN}>>> Mise à jour config: ajout watchdog.controller_refresh_interval${C_RESET}"
  local tmp
  tmp="$(mktemp)"
  awk '
    BEGIN { added = 0; in_wd = 0; }
    {
      if ($0 ~ /^[[:space:]]*watchdog:/) { in_wd = 1 }
      else if (in_wd && $0 ~ /^[^[:space:]]/) {
        if (!added) { print "  controller_refresh_interval: 60"; added = 1 }
        in_wd = 0
      }

      print

      if (in_wd && $0 ~ /refresh_interval:/ && !added) {
        print "  controller_refresh_interval: 60"
        added = 1
      }
    }
    END {
      if (in_wd && !added) {
        print "  controller_refresh_interval: 60"
      }
    }
  ' "$CFG_FILE" >"$tmp"

  mv "$tmp" "$CFG_FILE"
  chmod 640 "$CFG_FILE"
}

ensure_watchdog_feature_flags() {
  [[ -f "$CFG_FILE" ]] || return 0

  local result
  result="$(python3 - "$CFG_FILE" <<'PY' 2>/dev/null
import sys

try:
    import yaml
except Exception:
    sys.exit(0)

path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
except FileNotFoundError:
    sys.exit(0)

changed = False
if not isinstance(data, dict):
    data = {}
    changed = True

watchdog = data.get("watchdog")
if not isinstance(watchdog, dict):
    watchdog = {}
    data["watchdog"] = watchdog
    changed = True

changed_flag = [changed]

def ensure_section(name: str) -> None:
    section = watchdog.get(name)
    if not isinstance(section, dict):
        section = {}
        watchdog[name] = section
        changed_flag[0] = True
    for key in ("zones", "secteurs", "doors", "outputs"):
        if key not in section:
            section[key] = True
            changed_flag[0] = True

ensure_section("information")
ensure_section("controle")

if changed_flag[0]:
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(
            data,
            fh,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )
    print("UPDATED", end="")
else:
    print("UNCHANGED", end="")
PY
)"

  local status=$?
  if [[ $status -ne 0 ]]; then
    result=""
  fi

  if [[ "$result" == "UPDATED" ]]; then
    echo -e "${C_GREEN}>>> Mise à jour config: ajout watchdog.information/controle${C_RESET}"
    chmod 640 "$CFG_FILE"
  fi
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

echo -e "${C_GREEN}>>> Installation deps Python (requests, bs4, pyyaml, paho-mqtt >=2,<3)${C_RESET}"
"${VENV_DIR}/bin/pip" install --quiet --upgrade requests beautifulsoup4 pyyaml "paho-mqtt>=2,<3"

# --- Sanity check paho v2 + API V5 ---
"${VENV_DIR}/bin/python" - <<'PY'
from importlib import metadata

version = metadata.version("paho-mqtt")
major = int(version.split(".")[0])
assert major >= 2, f"paho-mqtt v2 requis, version détectée: {version}"
print("paho-mqtt OK:", version)
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
    DEFAULT_SPC_HOST_VALUE="${SPC_HOST:-http://192.168.1.100}"
    if [[ "${DEFAULT_SPC_HOST_VALUE}" =~ ^https?:// ]]; then
      SPC_SCHEME_DEFAULT="${DEFAULT_SPC_HOST_VALUE%%://*}"
      SPC_HOST_DEFAULT="${DEFAULT_SPC_HOST_VALUE#*://}"
    else
      SPC_SCHEME_DEFAULT="${SPC_SCHEME:-http}"
      SPC_HOST_DEFAULT="${DEFAULT_SPC_HOST_VALUE}"
    fi
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
    WD_CONTROLLER_REFRESH_DEFAULT="${WD_CONTROLLER_REFRESH:-60}"
    WD_LOG_DEFAULT="${WD_LOG_CHANGES:-true}"

    WD_INFO_ZONES_DEFAULT="${WD_INFO_ZONES:-true}"
    WD_INFO_SECTEURS_DEFAULT="${WD_INFO_SECTEURS:-true}"
    WD_INFO_DOORS_DEFAULT="${WD_INFO_DOORS:-true}"
    WD_INFO_OUTPUTS_DEFAULT="${WD_INFO_OUTPUTS:-true}"

    WD_CTRL_ZONES_DEFAULT="${WD_CTRL_ZONES:-true}"
    WD_CTRL_SECTEURS_DEFAULT="${WD_CTRL_SECTEURS:-true}"
    WD_CTRL_DOORS_DEFAULT="${WD_CTRL_DOORS:-true}"
    WD_CTRL_OUTPUTS_DEFAULT="${WD_CTRL_OUTPUTS:-true}"

    SPC_SCHEME="$(ask "Protocole de la centrale (http/https)" "$SPC_SCHEME_DEFAULT")"
    SPC_SCHEME="${SPC_SCHEME,,}"
    if [[ ! "${SPC_SCHEME}" =~ ^https?$ ]]; then
      SPC_SCHEME="${SPC_SCHEME_DEFAULT}"
    fi
    SPC_HOST_INPUT="$(ask "Adresse de la centrale (IP ou nom DNS)" "$SPC_HOST_DEFAULT")"
    if [[ "${SPC_HOST_INPUT}" =~ ^https?:// ]]; then
      SPC_HOST="${SPC_HOST_INPUT}"
    else
      SPC_HOST="${SPC_SCHEME}://${SPC_HOST_INPUT}"
    fi
    SPC_USER="$(ask "Code utilisateur (ID Web)" "$SPC_USER_DEFAULT")"
    SPC_PIN="$(ask "Mot de passe / PIN" "$SPC_PIN_DEFAULT")"
    SPC_LANG="$(ask "Langue (253=FR, 0=EN)" "$SPC_LANG_DEFAULT")"
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
    WD_CONTROLLER_REFRESH="$(ask "Intervalle refresh état centrale (sec)" "$WD_CONTROLLER_REFRESH_DEFAULT")"
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
  controller_refresh_interval: ${WD_CONTROLLER_REFRESH}
  log_changes: ${WD_LOG_CHANGES}
  information:
    zones: ${WD_INFO_ZONES_DEFAULT}
    secteurs: ${WD_INFO_SECTEURS_DEFAULT}
    doors: ${WD_INFO_DOORS_DEFAULT}
    outputs: ${WD_INFO_OUTPUTS_DEFAULT}
  controle:
    zones: ${WD_CTRL_ZONES_DEFAULT}
    secteurs: ${WD_CTRL_SECTEURS_DEFAULT}
    doors: ${WD_CTRL_DOORS_DEFAULT}
    outputs: ${WD_CTRL_OUTPUTS_DEFAULT}
YAML
    chmod 640 "$CFG_FILE"
  else
    echo -e "${C_GREEN}>>> On conserve la configuration existante.${C_RESET}"
  fi
fi

ensure_controller_refresh_config
ensure_watchdog_feature_flags

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


