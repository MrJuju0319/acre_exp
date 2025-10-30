#!/usr/bin/env bash
set -euo pipefail

# =============================
# ACRE SPC42 → MQTT installer
# =============================

# --- couleurs ---
C_RESET="\033[0m"
C_GREEN="\033[1;32m"
C_YELLOW="\033[1;33m"
C_BLUE="\033[1;34m"
C_RED="\033[1;31m"

# --- chemins / noms ---
VENV_DIR="/opt/spc-venv"
ETC_DIR="/etc/acre_exp"
STATE_DIR="/var/lib/acre_exp"
BIN_STATUS="/usr/local/bin/acre_exp_status.py"
BIN_WATCHDOG="/usr/local/bin/acre_exp_watchdog.py"
SERVICE_FILE="/etc/systemd/system/acre-exp-watchdog.service"
CFG_FILE="${ETC_DIR}/config.yml"

# --- flags ---
ASSUME_YES="${ASSUME_YES:-false}"

usage() {
  cat <<EOF
Usage: $0 [--yes]
  --yes    : exécution non-interactive (utilise les variables d'env si définies)

Variables supportées en non-interactif:
  SPC_HOST, SPC_USER, SPC_PIN, SPC_LANG, MIN_LOGIN_INTERVAL
  MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASS, MQTT_BASE_TOPIC, MQTT_CLIENT_ID, MQTT_QOS, MQTT_RETAIN
  WD_REFRESH, WD_LOG_CHANGES
EOF
}

if [[ "${1:-}" == "--help" ]]; then usage; exit 0; fi
if [[ "${1:-}" == "--yes" ]]; then ASSUME_YES=true; fi

# --- root check ---
if [[ $EUID -ne 0 ]]; then
  echo -e "${C_RED}[ERREUR]${C_RESET} Ce script doit être exécuté en root."
  exit 1
fi

# --- helpers ---
ask() {
  local prompt="$1"; local default="$2"; local var
  if [[ "$ASSUME_YES" == "true" ]]; then
    echo -e "${C_BLUE}${prompt}${C_RESET} (${default})"
    echo "$default"
    return 0
  fi
  read -rp "$(echo -e ${C_BLUE}${prompt}${C_RESET}" [${default}] : ")" var || true
  echo "${var:-$default}"
}

confirm() {
  local prompt="$1"
  if [[ "$ASSUME_YES" == "true" ]]; then
    return 0
  fi
  read -rp "$(echo -e ${C_BLUE}${prompt}${C_RESET} [o/N] : )" yn || true
  [[ "${yn,,}" == "o" || "${yn,,}" == "oui" || "${yn,,}" == "y" || "${yn,,}" == "yes" ]]
}

line() { echo -e "${C_YELLOW}------------------------------------------------------------${C_RESET}"; }

# --- dépendances OS ---
echo -e "${C_GREEN}>>> Vérification des paquets système...${C_RESET}"
PKGS=(python3 python3-venv python3-pip jq)
if command -v apt-get >/dev/null 2>&1; then
  MISSING=()
  for p in "${PKGS[@]}"; do
    dpkg -s "$p" >/dev/null 2>&1 || MISSING+=("$p")
  done
  if (( ${#MISSING[@]} )); then
    echo -e "${C_YELLOW}Installation de: ${MISSING[*]}${C_RESET}"
    apt-get update -y
    apt-get install -y "${MISSING[@]}"
  fi
else
  echo -e "${C_YELLOW}[ATTENTION] Pas d'APT détecté. Assure-toi d'avoir:${C_RESET} ${PKGS[*]}"
fi

# --- venv ---
echo -e "${C_GREEN}>>> Préparation du venv Python: ${VENV_DIR}${C_RESET}"
if [[ ! -d "$VENV_DIR" ]]; then
  python3 -m venv "$VENV_DIR"
fi
"${VENV_DIR}/bin/python" -m pip install --upgrade pip >/dev/null

echo -e "${C_GREEN}>>> Installation des dépendances Python (requests, bs4, pyyaml, paho-mqtt)...${C_RESET}"
"${VENV_DIR}/bin/pip" install --quiet requests beautifulsoup4 pyyaml paho-mqtt

# --- dossiers ---
echo -e "${C_GREEN}>>> Création des dossiers${C_RESET} ${ETC_DIR} ${STATE_DIR}"
mkdir -p "$ETC_DIR" "$STATE_DIR"
chmod 755 "$ETC_DIR" "$STATE_DIR"

# --- saisie interactive config (si pas déjà présente) ---
write_cfg=true
if [[ -f "$CFG_FILE" ]]; then
  echo -e "${C_YELLOW}Config existante détectée: ${CFG_FILE}${C_RESET}"
  if ! confirm "Souhaites-tu la régénérer ? (sinon on garde l'existante)"; then
    write_cfg=false
  fi
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
  SPC_LANG="$(ask "Langue (253 = Langue utilisateur, 2 = Français, 0 = English)" "$SPC_LANG_DEFAULT")"
  MIN_LOGIN_INTERVAL="$(ask "Délai minimum entre relogins (sec)" "$MIN_LOGIN_INTERVAL_DEFAULT")"

  MQTT_HOST="$(ask "MQTT hôte" "$MQTT_HOST_DEFAULT")"
  MQTT_PORT="$(ask "MQTT port" "$MQTT_PORT_DEFAULT")"
  MQTT_USER="$(ask "MQTT user (laisser vide si N/A)" "$MQTT_USER_DEFAULT")"
  MQTT_PASS="$(ask "MQTT pass (laisser vide si N/A)" "$MQTT_PASS_DEFAULT")"
  MQTT_BASE_TOPIC="$(ask "MQTT base topic" "$MQTT_BASE_DEFAULT")"
  MQTT_CLIENT_ID="$(ask "MQTT client_id" "$MQTT_CLIENT_ID_DEFAULT")"
  MQTT_QOS="$(ask "MQTT QoS (0/1/2)" "$MQTT_QOS_DEFAULT")"
  MQTT_RETAIN="$(ask "MQTT retain (true/false)" "$MQTT_RETAIN_DEFAULT")"

  WD_REFRESH="$(ask "Intervalle de refresh watchdog (sec)" "$WD_REFRESH_DEFAULT")"
  WD_LOG_CHANGES="$(ask "Logs des changements (true/false)" "$WD_LOG_DEFAULT")"

  line
  echo -e "${C_GREEN}>>> Écriture du fichier de config : ${CFG_FILE}${C_RESET}"
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

watchdog:
  refresh_interval: ${WD_REFRESH}
  log_changes: ${WD_LOG_CHANGES}
YAML
  chmod 640 "$CFG_FILE"
else
  echo -e "${C_GREEN}>>> On conserve la configuration existante.${C_RESET}"
fi

# --- installer scripts Python COMPLETS ---

echo -e "${C_GREEN}>>> Installation du script JSON : ${BIN_STATUS}${C_RESET}"
cat > "$BIN_STATUS" <<'PY'
#!/opt/spc-venv/bin/python3
# -*- coding: utf-8 -*-

import os, re, sys, json, time, pathlib, argparse
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from http.cookiejar import MozillaCookieJar
import yaml

def load_cfg(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def ensure_dir(p):
    pathlib.Path(p).mkdir(parents=True, exist_ok=True)

class SPCClient:
    def __init__(self, cfg: dict):
        spc = cfg.get("spc", {})
        self.host   = spc.get("host", "").rstrip("/")
        self.user   = spc.get("user", "")
        self.pin    = spc.get("pin", "")
        self.lang   = str(spc.get("language", 253))
        self.cache  = spc.get("session_cache_dir", "/var/lib/acre_exp")
        self.min_login_interval = int(spc.get("min_login_interval_sec", 60))

        ensure_dir(self.cache)
        self.session_file = os.path.join(self.cache, "spc_session.json")
        self.cookie_file  = os.path.join(self.cache, "spc_cookies.jar")

        self.session = requests.Session()
        self.cookiejar = MozillaCookieJar(self.cookie_file)
        self._load_cookies()

    # --- cookies
    def _load_cookies(self):
        try:
            if os.path.exists(self.cookie_file):
                self.cookiejar.load(ignore_discard=True, ignore_expires=True)
            self.session.cookies = self.cookiejar
        except Exception:
            try: os.remove(self.cookie_file)
            except Exception: pass
            self.session.cookies = MozillaCookieJar()

    def _save_cookies(self):
        try:
            self.cookiejar.save(ignore_discard=True, ignore_expires=True)
        except Exception:
            pass

    # --- http
    def _get(self, url):
        r = self.session.get(url, timeout=8)
        r.raise_for_status()
        r.encoding = "utf-8"
        return r

    def _post(self, url, data, allow_redirects=True):
        r = self.session.post(url, data=data, allow_redirects=allow_redirects, timeout=8)
        r.raise_for_status()
        r.encoding = "utf-8"
        return r

    # --- session cache
    def _load_session_cache(self):
        if not os.path.exists(self.session_file):
            return {}
        try:
            with open(self.session_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_session_cache(self, sid):
        try:
            with open(self.session_file, "w", encoding="utf-8") as f:
                json.dump({"session": sid, "time": time.time()}, f)
        except Exception:
            pass

    def _last_login_too_recent(self):
        d = self._load_session_cache()
        t = d.get("time", 0)
        return (time.time() - float(t)) < self.min_login_interval

    @staticmethod
    def _extract_session(text_or_url):
        if not text_or_url:
            return ""
        m = re.search(r"[?&]session=([0-9A-Za-zx]+)", text_or_url)
        if m:
            return m.group(1)
        m = re.search(r"secure\.htm\?[^\"'>]*session=([0-9A-Za-zx]+)", text_or_url)
        return m.group(1) if m else ""

    def _session_valid(self, sid):
        try:
            url = f"{self.host}/secure.htm?session={sid}&page=spc_home"
            r = self._get(url)
            low = r.text.lower()
            if "login.htm" in low or "mot de passe" in low or "identifiant" in low:
                return False
            if "spc42" not in r.text:
                return False
            return True
        except Exception:
            return False

    def _do_login(self):
        try:
            self._get(urljoin(self.host, "/login.htm"))
        except Exception:
            pass
        url = f"{self.host}/login.htm?action=login&language={self.lang}"
        r = self._post(url, {"userid": self.user, "password": self.pin}, allow_redirects=True)
        sid = self._extract_session(r.url) or self._extract_session(r.text)
        if sid:
            self._save_session_cache(sid)
            self._save_cookies()
            return sid
        return ""

    def get_or_login(self):
        d = self._load_session_cache()
        sid = d.get("session", "")
        if sid and self._session_valid(sid):
            return sid

        if self._last_login_too_recent():
            time.sleep(2)
            if sid and self._session_valid(sid):
                return sid

        return self._do_login()

    # --- parsing helpers
    @staticmethod
    def _map_entree(txt):
        s = (txt or "").lower()
        if "ferm" in s: return 1
        if "ouvert" in s: return 0
        return -1

    @staticmethod
    def _map_zone_state(txt):
        s = (txt or "").lower()
        if "normal" in s: return 1
        if "activ"  in s: return 2
        return -1

    @staticmethod
    def _map_area_state(txt):
        s = (txt or "").lower()
        if "mes totale" in s: return 2
        if "mes partiel" in s: return 3
        if "mhs" in s or "désarm" in s: return 1
        if "alarme" in s: return 4
        return 0

    def parse_zones(self, html):
        soup = BeautifulSoup(html, "html.parser")
        grid = soup.find("table", {"class": "gridtable"})
        zones = []
        if not grid:
            return zones
        for tr in grid.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) >= 6:
                zname = tds[0].get_text(strip=True)
                sect  = tds[1].get_text(strip=True)
                entree_txt = tds[4].get_text(strip=True)
                etat_txt   = tds[5].get_text(strip=True)
                if zname:
                    zones.append({
                        "zone": zname,
                        "secteur": sect,
                        "entree_txt": entree_txt,
                        "etat_txt": etat_txt,
                        "entree": self._map_entree(entree_txt),
                        "etat":   self._map_zone_state(etat_txt),
                    })
        return zones

    def parse_areas(self, html):
        soup = BeautifulSoup(html, "html.parser")
        areas = []
        for tr in soup.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 3: continue
            label = tds[1].get_text(strip=True)
            state = tds[2].get_text(strip=True)
            if label.lower().startswith("secteur"):
                m = re.match(r"^Secteur\s+(\d+)\s*:\s*(.+)$", label, re.I)
                if m:
                    num, nom = m.groups()
                    areas.append({
                        "secteur": f"{num} {nom}",
                        "nom": nom,
                        "etat_txt": state,
                        "etat": self._map_area_state(state)
                    })
        return areas

    def fetch_status(self):
        sid = self.get_or_login()
        if not sid:
            return {"error": "Impossible d’obtenir une session"}

        z_html = self._get(f"{self.host}/secure.htm?session={sid}&page=status_zones").text
        zones  = self.parse_zones(z_html)

        a_html = self._get(f"{self.host}/secure.htm?session={sid}&page=spc_home").text
        areas  = self.parse_areas(a_html)

        self._save_cookies()
        return {"zones": zones, "areas": areas}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", default="/etc/acre_exp/config.yml")
    args = parser.parse_args()

    try:
        cfg = load_cfg(args.config)
        client = SPCClient(cfg)
        data = client.fetch_status()
        print(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}))

if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
PY
chmod 755 "$BIN_STATUS"

echo -e "${C_GREEN}>>> Installation du watchdog MQTT : ${BIN_WATCHDOG}${C_RESET}"
cat > "$BIN_WATCHDOG" <<'PY'
#!/opt/spc-venv/bin/python3
# -*- coding: utf-8 -*-

import os, re, sys, time, json, argparse, signal
import yaml
import requests
from bs4 import BeautifulSoup
from http.cookiejar import MozillaCookieJar
from typing import Dict

# --- MQTT (paho v2 sans warning) ---
try:
    from paho.mqtt import client as mqtt
    try:
        # paho-mqtt >= 2.0
        from paho.mqtt.client import CallbackAPIVersion
        HAS_V2 = True
    except Exception:
        HAS_V2 = False
except Exception:
    print("[ERREUR] paho-mqtt non installé : /opt/spc-venv/bin/pip install paho-mqtt")
    sys.exit(1)

def load_cfg(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def ensure_dir(p):
    import pathlib
    pathlib.Path(p).mkdir(parents=True, exist_ok=True)

class SPCClient:
    def __init__(self, cfg: dict):
        spc = cfg.get("spc", {})
        self.host   = spc.get("host", "").rstrip("/")
        self.user   = spc.get("user", "")
        self.pin    = spc.get("pin", "")
        self.lang   = str(spc.get("language", 253))
        self.cache  = spc.get("session_cache_dir", "/var/lib/acre_exp")
        self.min_login_interval = int(spc.get("min_login_interval_sec", 60))

        ensure_dir(self.cache)
        self.session_file = os.path.join(self.cache, "spc_session.json")
        self.cookie_file  = os.path.join(self.cache, "spc_cookies.jar")

        self.session = requests.Session()
        self.cookiejar = MozillaCookieJar(self.cookie_file)
        self._load_cookies()

    def _load_cookies(self):
        try:
            if os.path.exists(self.cookie_file):
                self.cookiejar.load(ignore_discard=True, ignore_expires=True)
            self.session.cookies = self.cookiejar
        except Exception:
            try: os.remove(self.cookie_file)
            except Exception: pass
            from http.cookiejar import MozillaCookieJar as MCJ
            self.session.cookies = MCJ()

    def _save_cookies(self):
        try:
            self.cookiejar.save(ignore_discard=True, ignore_expires=True)
        except Exception:
            pass

    def _get(self, url):
        r = self.session.get(url, timeout=8)
        r.raise_for_status()
        r.encoding = "utf-8"
        return r

    def _post(self, url, data, allow_redirects=True):
        r = self.session.post(url, data=data, allow_redirects=allow_redirects, timeout=8)
        r.raise_for_status()
        r.encoding = "utf-8"
        return r

    def _load_session_cache(self):
        if not os.path.exists(self.session_file):
            return {}
        try:
            with open(self.session_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_session_cache(self, sid):
        try:
            with open(self.session_file, "w", encoding="utf-8") as f:
                json.dump({"session": sid, "time": time.time()}, f)
        except Exception:
            pass

    def _last_login_too_recent(self):
        d = self._load_session_cache()
        t = d.get("time", 0)
        return (time.time() - float(t)) < self.min_login_interval

    @staticmethod
    def _extract_session(text_or_url):
        if not text_or_url:
            return ""
        m = re.search(r"[?&]session=([0-9A-Za-zx]+)", text_or_url)
        if m:
            return m.group(1)
        m = re.search(r"secure\.htm\?[^\"'>]*session=([0-9A-Za-zx]+)", text_or_url)
        return m.group(1) if m else ""

    def _session_valid(self, sid):
        try:
            url = f"{self.host}/secure.htm?session={sid}&page=spc_home"
            r = self._get(url)
            low = r.text.lower()
            if "login.htm" in low or "mot de passe" in low or "identifiant" in low:
                return False
            if "spc42" not in r.text:
                return False
            return True
        except Exception:
            return False

    def _do_login(self):
        try:
            self._get(f"{self.host}/login.htm")
        except Exception:
            pass
        url = f"{self.host}/login.htm?action=login&language={self.lang}"
        r = self._post(url, {"userid": self.user, "password": self.pin}, allow_redirects=True)
        sid = self._extract_session(r.url) or self._extract_session(r.text)
        if sid:
            self._save_session_cache(sid)
            self._save_cookies()
            return sid
        return ""

    def get_or_login(self):
        d = self._load_session_cache()
        sid = d.get("session", "")
        if sid and self._session_valid(sid):
            return sid
        if self._last_login_too_recent():
            time.sleep(2)
            if sid and self._session_valid(sid):
                return sid
        return self._do_login()

    @staticmethod
    def zone_bin(etat_txt: str) -> int:
        s = (etat_txt or "").lower()
        if "normal" in s: return 0
        if "activ"  in s: return 1
        return -1

    @staticmethod
    def area_num(etat_txt: str) -> int:
        s = (etat_txt or "").lower()
        if "mhs" in s or "désarm" in s: return 0
        if "mes totale" in s: return 1
        if "mes partiel" in s: return 2
        return -1

    @staticmethod
    def zone_id_from_name(name: str) -> str:
        m = re.match(r"^\s*(\d+)\b", name or "")
        if m: return m.group(1)
        import re as _re
        slug = _re.sub(r"[^a-zA-Z0-9]+", "_", name or "").strip("_").lower()
        return slug or "unknown"

    def parse_zones(self, html):
        soup = BeautifulSoup(html, "html.parser")
        grid = soup.find("table", {"class": "gridtable"})
        zones = []
        if not grid: return zones
        for tr in grid.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) >= 6:
                zname = tds[0].get_text(strip=True)
                sect  = tds[1].get_text(strip=True)
                etat_txt = tds[5].get_text(strip=True)
                if zname:
                    zones.append({"zname": zname, "sect": sect, "etat_txt": etat_txt})
        return zones

    def parse_areas(self, html):
        soup = BeautifulSoup(html, "html.parser")
        areas = []
        for tr in soup.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 3: continue
            label = tds[1].get_text(strip=True)
            state = tds[2].get_text(strip=True)
            if label.lower().startswith("secteur"):
                m = re.match(r"^Secteur\s+(\d+)\s*:\s*(.+)$", label, re.I)
                if m:
                    num, nom = m.groups()
                    areas.append({"sid": num, "nom": nom, "etat_txt": state})
        return areas

    def fetch(self):
        sid = self.get_or_login()
        if not sid:
            return {"zones": [], "areas": []}
        z_html = self._get(f"{self.host}/secure.htm?session={sid}&page=status_zones").text
        a_html = self._get(f"{self.host}/secure.htm?session={sid}&page=spc_home").text
        self._save_cookies()
        return {
            "zones": self.parse_zones(z_html),
            "areas": self.parse_areas(a_html)
        }

class MQ:
    def __init__(self, cfg: dict):
        m = cfg.get("mqtt", {})
        self.host = m.get("host", "127.0.0.1")
        self.port = int(m.get("port", 1883))
        self.user = m.get("user", "")
        self.pwd  = m.get("pass", "")
        self.base = m.get("base_topic", "spc").strip("/")
        self.qos  = int(m.get("qos", 0))
        self.retain = bool(m.get("retain", True))
        self.client_id = m.get("client_id", "spc42-watchdog")

        if HAS_V2:
            # API v2 -> plus de warning
            self.client = mqtt.Client(
                callback_api_version=CallbackAPIVersion.VERSION2,
                client_id=self.client_id,
                protocol=mqtt.MQTTv311,
                transport="tcp",
            )
        else:
            # fallback: ancienne signature (peut afficher un warning)
            self.client = mqtt.Client(client_id=self.client_id, clean_session=True, userdata=None, protocol=mqtt.MQTTv311)

        if self.user:
            self.client.username_pw_set(self.user, self.pwd)

        self.connected = False
        if HAS_V2:
            # on_connect(client, userdata, flags, rc, properties=None)
            def _on_connect(c, u, flags, rc, properties=None):
                self._set_conn(rc)
            def _on_disconnect(c, u, rc, properties=None):
                self._unset_conn()
        else:
            # v1
            def _on_connect(c, u, flags, rc):
                self._set_conn(rc)
            def _on_disconnect(c, u, rc):
                self._unset_conn()

        self.client.on_connect = _on_connect
        self.client.on_disconnect = _on_disconnect

    def _set_conn(self, rc):
        self.connected = (rc == 0)
        print("[MQTT] Connecté" if self.connected else f"[MQTT] Connexion échouée rc={rc}")

    def _unset_conn(self):
        self.connected = False
        print("[MQTT] Déconnecté")

    def connect(self):
        while True:
            try:
                self.client.connect(self.host, self.port, keepalive=30)
                self.client.loop_start()
                for _ in range(30):
                    if self.connected: return
                    time.sleep(0.2)
            except Exception as e:
                print(f"[MQTT] Erreur: {e}")
            time.sleep(2)

    def pub(self, topic, payload):
        full = f"{self.base}/{topic}".strip("/")
        try:
            self.client.publish(full, payload=str(payload), qos=self.qos, retain=self.retain)
        except Exception as e:
            print(f"[MQTT] publish ERR {full}: {e}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="/etc/acre_exp/config.yml")
    args = ap.parse_args()

    cfg = load_cfg(args.config)
    wd  = cfg.get("watchdog", {})
    interval = int(wd.get("refresh_interval", 2))
    log_changes = bool(wd.get("log_changes", True))

    spc = SPCClient(cfg)
    mq  = MQ(cfg)

    print(f"[SPC→MQTT] Démarrage (refresh={interval}s) — Broker {mq.host}:{mq.port}")
    mq.connect()

    last_z: Dict[str, int] = {}
    last_a: Dict[str, int] = {}

    running = True
    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    # Snapshot initial
    snap = spc.fetch()
    for z in snap["zones"]:
        zid = spc.zone_id_from_name(z["zname"])
        b   = spc.zone_bin(z["etat_txt"])
        mq.pub(f"zones/{zid}/name", z["zname"])
        mq.pub(f"zones/{zid}/secteur", z["sect"])
        if b in (0,1):
            last_z[zid] = b
            mq.pub(f"zones/{zid}/state", b)

    for a in snap["areas"]:
        sid = a["sid"]
        s   = spc.area_num(a["etat_txt"])
        mq.pub(f"secteurs/{sid}/name", a["nom"])
        if s >= 0:
            last_a[sid] = s
            mq.pub(f"secteurs/{sid}/state", s)

    print("[SPC→MQTT] État initial publié.")

    while running:
        tick = time.strftime("%H:%M:%S")
        try:
            data = spc.fetch()
        except Exception as e:
            print(f"[SPC] fetch ERR: {e}")
            time.sleep(interval)
            continue

        # zones
        for z in data["zones"]:
            zid = spc.zone_id_from_name(z["zname"])
            b   = spc.zone_bin(z["etat_txt"])
            if b not in (0,1): continue
            old = last_z.get(zid)
            if old is None or b != old:
                mq.pub(f"zones/{zid}/state", b)
                last_z[zid] = b
                if log_changes:
                    print(f"[{tick}] 🟡 Zone '{z['zname']}' → {b}")

        # secteurs
        for a in data["areas"]:
            sid = a["sid"]
            s   = spc.area_num(a["etat_txt"])
            if s < 0: continue
            old = last_a.get(sid)
            if old is None or s != old:
                mq.pub(f"secteurs/{sid}/state", s)
                last_a[sid] = s
                if log_changes:
                    print(f"[{tick}] 🔵 Secteur '{a['nom']}' → {s}")

        time.sleep(interval)

    mq.client.loop_stop()
    mq.client.disconnect()
    print("[SPC→MQTT] Arrêt propre.")

if __name__ == "__main__":
    main()
PY
chmod 755 "$BIN_WATCHDOG"

# --- systemd unit ---
echo -e "${C_GREEN}>>> Installation du service systemd : ${SERVICE_FILE}${C_RESET}"
cat > "$SERVICE_FILE" <<SYSTEMD
[Unit]
Description=ACRE SPC42 -> MQTT Watchdog (zones + secteurs)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=${BIN_WATCHDOG} -c ${CFG_FILE}
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

User=root
Group=root

[Install]
WantedBy=multi-user.target
SYSTEMD

# --- reload + start ---
echo -e "${C_GREEN}>>> Activation du service...${C_RESET}"
systemctl daemon-reload
systemctl enable --now acre-exp-watchdog.service

line
echo -e "${C_GREEN}Installation terminée !${C_RESET}"
echo -e "• Status service : ${C_YELLOW}systemctl status acre-exp-watchdog.service${C_RESET}"
echo -e "• Logs en direct : ${C_YELLOW}journalctl -u acre-exp-watchdog.service -f -n 100${C_RESET}"
echo -e "• Test JSON      : ${C_YELLOW}${BIN_STATUS} -c ${CFG_FILE} | jq .${C_RESET}"
echo -e "• MQTT (exemple) : ${C_YELLOW}mosquitto_sub -h \${MQTT_HOST:-127.0.0.1} -t '\${MQTT_BASE_TOPIC:-acre_XXX}/#' -v${C_RESET}"
line
