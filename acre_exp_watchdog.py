#!/opt/spc-venv/bin/python3
# -*- coding: utf-8 -*-

import os, re, sys, time, json, argparse, signal, logging, warnings
import yaml
import requests
from bs4 import BeautifulSoup
from http.cookiejar import MozillaCookieJar
from typing import Dict

from acre_exp_status import SPCClient as StatusSPCClient

# paho-mqtt v2.x (API V5) recommandé — compatibilité assurée avec v1.x
try:
    from paho.mqtt import client as mqtt
except Exception:
    print("[ERREUR] paho-mqtt non disponible : /opt/spc-venv/bin/pip install 'paho-mqtt>=2,<3'")
    sys.exit(1)

try:
    from paho.mqtt.client import CallbackAPIVersion
except Exception:
    CallbackAPIVersion = None  # paho-mqtt < 1.6

def load_cfg(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def ensure_dir(p):
    import pathlib
    pathlib.Path(p).mkdir(parents=True, exist_ok=True)

class SPCClient(StatusSPCClient):
    def __init__(self, cfg: dict, debug: bool = False):
        super().__init__(cfg, debug)

    def _last_login_too_recent(self) -> bool:
        try:
            data = self._load_session_cache()
            last = float(data.get("time", 0) or 0)
        except Exception:
            last = 0.0
        delta = time.time() - last
        too_recent = delta < self.min_login_interval
        if too_recent and self.debug:
            logging.debug("Dernière tentative de login il y a %.1fs — attente min %ss", delta, self.min_login_interval)
        return too_recent

    def _session_valid(self, sid: str) -> bool:
        if not sid:
            return False
        try:
            url = f"{self.host}/secure.htm?session={sid}&page=spc_home"
            r = self._get(url, referer=f"{self.host}/secure.htm?session={sid}&page=spc_home")
        except Exception:
            if self.debug:
                logging.debug("Validation session %s impossible (erreur requête)", sid, exc_info=True)
            return False

        if self._is_login_response(r.text, getattr(r, "url", ""), True):
            if self.debug:
                logging.debug("Session %s invalide : page de login renvoyée", sid)
            return False

        if self.debug:
            logging.debug("Session %s toujours valide", sid)
        return True

    def _do_login(self) -> str:
        if self.debug:
            logging.debug("Connexion SPC…")
        try:
            self._get(f"{self.host}/login.htm")
        except Exception:
            if self.debug:
                logging.debug("Pré-chargement login.htm échoué", exc_info=True)
        url = f"{self.host}/login.htm?action=login&language={self.lang}"
        try:
            r = self._post(
                url,
                {"userid": self.user, "password": self.pin},
                allow_redirects=True,
                referer=f"{self.host}/login.htm",
            )
        except Exception:
            if self.debug:
                logging.debug("POST login échoué", exc_info=True)
            return ""

        sid = self._extract_session(getattr(r, "url", "")) or self._extract_session(r.text)
        if self.debug:
            logging.debug("Login SID=%s", sid or "(aucun)")
        if sid:
            self._save_session_cache(sid)
            self._save_cookies()
            return sid
        return ""

    def get_or_login(self) -> str:
        data = self._load_session_cache()
        sid = data.get("session", "")
        if sid and self._session_valid(sid):
            return sid

        if self._last_login_too_recent():
            time.sleep(2)
            if sid and self._session_valid(sid):
                return sid

        return self._do_login()

    @staticmethod
    def _extract_session(text_or_url):
        if not text_or_url:
            return ""
        m = re.search(r"[?&]session=([0-9A-Za-zx]+)", text_or_url)
        if m:
            return m.group(1)
        m = re.search(r"secure\.htm\?[^\"'>]*session=([0-9A-Za-zx]+)", text_or_url)
        return m.group(1) if m else ""

    @staticmethod
    def _is_login_response(resp_text: str, resp_url: str, expect_table: bool) -> bool:
        if resp_url and "login.htm" in resp_url.lower():
            return True
        if not expect_table:
            return False
        low = resp_text.lower()
        has_user = ('name="userid"' in low) or ('id="userid"' in low) or ("id='userid'" in low)
        # >>> BUG FIX: quotes corrigés (pas de " au milieu) <<<
        has_pass = ('name="password"' in low) or ('id="password"' in low) or ("id='password'" in low)
        return has_user and has_pass

    @staticmethod
    def _normalize_state_text(txt: str) -> str:
        return (txt or "").strip().lower()

    @classmethod
    def zone_bin(cls, zone) -> int:
        if isinstance(zone, dict):
            etat = zone.get("etat")
            if isinstance(etat, int):
                if etat <= 1:
                    return 0 if etat == 1 else -1
                if etat >= 2:
                    return 1
            etat_txt = zone.get("etat_txt")
        else:
            etat_txt = zone

        s = cls._normalize_state_text(etat_txt)
        if "normal" in s:
            return 0
        if "activ" in s or "alarm" in s or "alarme" in s or "trouble" in s:
            return 1
        return -1

    @classmethod
    def area_num(cls, area) -> int:
        if isinstance(area, dict):
            etat = area.get("etat")
            if isinstance(etat, int) and etat >= 0:
                return etat
            etat_txt = area.get("etat_txt")
        else:
            etat_txt = area

        s = cls._normalize_state_text(etat_txt)
        if "mes totale" in s:
            return 2
        if "mes partiel" in s:
            return 3
        if "alarme" in s:
            return 4
        if "mhs" in s or "désarm" in s:
            return 1
        return 0

    @staticmethod
    def zone_id_from_name(zone) -> str:
        if isinstance(zone, dict):
            name = zone.get("zone") or zone.get("zname") or zone.get("name") or ""
        else:
            name = zone or ""
        m = re.match(r"^\s*(\d+)\b", name)
        if m:
            return m.group(1)
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()
        return slug or "unknown"

    @staticmethod
    def zone_name(zone) -> str:
        if isinstance(zone, dict):
            return zone.get("zone") or zone.get("zname") or ""
        return str(zone or "")

    @staticmethod
    def zone_sector(zone) -> str:
        if isinstance(zone, dict):
            return zone.get("secteur") or zone.get("sect") or ""
        return ""

    @staticmethod
    def zone_input(zone) -> int:
        if isinstance(zone, dict):
            entree = zone.get("entree")
            if isinstance(entree, int) and entree in (0, 1):
                return entree
            entree_txt = zone.get("entree_txt")
        else:
            entree_txt = zone

        s = SPCClient._normalize_state_text(entree_txt)
        if "ferm" in s:
            return 1
        if "ouvert" in s or "alarm" in s:
            return 0
        return -1

    @staticmethod
    def area_id(area) -> str:
        if isinstance(area, dict):
            sid = area.get("sid")
            if sid:
                return str(sid).strip()
            label = area.get("secteur") or ""
            m = re.match(r"^\s*(\d+)\b", label)
            if m:
                return m.group(1)
            name = area.get("nom")
            if name:
                return SPCClient.zone_id_from_name(name)
        return ""

    def fetch(self):
        data = super().fetch_status()
        if not isinstance(data, dict):
            return {"zones": [], "areas": []}
        if "error" in data:
            raise RuntimeError(data["error"])

        zones = data.get("zones", [])
        for z in zones:
            if isinstance(z, dict):
                if not z.get("id"):
                    z["id"] = self.zone_id_from_name(z)

        areas = data.get("areas", [])
        for a in areas:
            if isinstance(a, dict):
                sid = self.area_id(a)
                if sid:
                    a.setdefault("sid", sid)

        return {"zones": zones, "areas": areas}

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
        proto = str(m.get("protocol", "v311")).lower()
        self.protocol = mqtt.MQTTv5 if proto in ("v5", "mqttv5", "5") else mqtt.MQTTv311

        client_kwargs = {
            "client_id": self.client_id,
            "protocol": self.protocol,
        }

        callback_version = None
        if CallbackAPIVersion is not None:
            for attr in ("V5", "V311", "V3"):
                ver = getattr(CallbackAPIVersion, attr, None)
                if ver is not None:
                    callback_version = ver
                    client_kwargs["callback_api_version"] = ver
                    break
        if callback_version is None:
            print("[MQTT] Attention : API callbacks V3 utilisée (paho-mqtt ancien)")

        with warnings.catch_warnings():
            if callback_version is None:
                warnings.filterwarnings(
                    "ignore",
                    message="Callback API version 1 is deprecated, update to latest version",
                    category=DeprecationWarning,
                    module="paho.mqtt.client",
                )
            self.client = mqtt.Client(**client_kwargs)

        def _normalize_reason_code(code):
            if code is None:
                return 0
            value = getattr(code, "value", code)
            try:
                return int(value)
            except Exception:
                return 0

        def _on_connect(client, userdata, flags, reason_code=0, *rest):
            rc = _normalize_reason_code(reason_code)
            self._set_conn(rc == 0, rc)

        def _on_disconnect(client, userdata, reason_code=0, *rest):
            rc = _normalize_reason_code(reason_code)
            self._unset_conn(rc)

        if self.user:
            self.client.username_pw_set(self.user, self.pwd)

        self.connected = False
        self.client.on_connect = _on_connect
        self.client.on_disconnect = _on_disconnect

    def _set_conn(self, ok: bool, rc: int):
        self.connected = ok
        print("[MQTT] Connecté" if ok else f"[MQTT] Connexion échouée rc={rc}")

    def _unset_conn(self, rc: int):
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

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="/etc/acre_exp/config.yml",
                    help="chemin vers le fichier de configuration YAML")
    ap.add_argument("--debug", action="store_true",
                    help="activer les logs détaillés (HTTP, parse, relogin)")
    args = ap.parse_args()

    logging.basicConfig(stream=sys.stderr, level=(logging.DEBUG if args.debug else logging.INFO),
                        format="%(levelname)s:%(message)s")

    cfg = load_cfg(args.config)
    wd  = cfg.get("watchdog", {})
    interval = int(wd.get("refresh_interval", 2))
    log_changes = bool(wd.get("log_changes", True))

    spc = SPCClient(cfg, debug=args.debug)
    mq  = MQ(cfg)

    print(f"[SPC→MQTT] Démarrage (refresh={interval}s) — Broker {mq.host}:{mq.port}")
    mq.connect()

    last_z: Dict[str, int] = {}
    last_z_in: Dict[str, int] = {}
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
        zid = SPCClient.zone_id_from_name(z)
        zname = SPCClient.zone_name(z)
        if not zid or not zname:
            continue
        mq.pub(f"zones/{zid}/name", zname)
        mq.pub(f"zones/{zid}/secteur", SPCClient.zone_sector(z))
        b = SPCClient.zone_bin(z)
        if b in (0, 1):
            last_z[zid] = b
            mq.pub(f"zones/{zid}/state", b)
        entree = SPCClient.zone_input(z)
        if entree in (0, 1):
            last_z_in[zid] = entree
            mq.pub(f"zones/{zid}/entree", entree)

    for a in snap["areas"]:
        sid = SPCClient.area_id(a)
        if not sid:
            continue
        mq.pub(f"secteurs/{sid}/name", a.get("nom", ""))
        s = SPCClient.area_num(a)
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

        for z in data["zones"]:
            zid = SPCClient.zone_id_from_name(z)
            zname = SPCClient.zone_name(z)
            if not zid or not zname:
                continue
            b = SPCClient.zone_bin(z)
            if b not in (0, 1):
                continue
            old = last_z.get(zid)
            if old is None or b != old:
                mq.pub(f"zones/{zid}/state", b)
                last_z[zid] = b
                if log_changes:
                    print(f"[{tick}] 🟡 Zone '{zname}' → {b}")

            entree = SPCClient.zone_input(z)
            if entree in (0, 1):
                old_in = last_z_in.get(zid)
                if old_in is None or entree != old_in:
                    mq.pub(f"zones/{zid}/entree", entree)
                    last_z_in[zid] = entree
                    if log_changes:
                        state_txt = "fermée" if entree == 1 else "ouverte"
                        print(f"[{tick}] 🟢 Entrée zone '{zname}' → {state_txt}")

        for a in data["areas"]:
            sid = SPCClient.area_id(a)
            if not sid:
                continue
            s = SPCClient.area_num(a)
            old = last_a.get(sid)
            if old is None or s != old:
                mq.pub(f"secteurs/{sid}/state", s)
                last_a[sid] = s
                if log_changes:
                    print(f"[{tick}] 🔵 Secteur '{a.get('nom', sid)}' → {s}")

        time.sleep(interval)

    mq.client.loop_stop()
    try:
        mq.client.disconnect()
    except Exception:
        pass
    print("[SPC→MQTT] Arrêt propre.")

if __name__ == "__main__":
    main()
