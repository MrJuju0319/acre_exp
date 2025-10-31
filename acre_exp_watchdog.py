#!/opt/spc-venv/bin/python3
# -*- coding: utf-8 -*-

import os, re, sys, time, json, argparse, signal, logging
import yaml
import requests
from bs4 import BeautifulSoup
from http.cookiejar import MozillaCookieJar
from typing import Dict

# === paho-mqtt v2.x, API V5 ===
try:
    from paho.mqtt import client as mqtt
    from paho.mqtt.client import CallbackAPIVersion
except Exception:
    print("[ERREUR] paho-mqtt v2.x requis : /opt/spc-venv/bin/pip install 'paho-mqtt>=2,<3'")
    sys.exit(1)

def load_cfg(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def ensure_dir(p):
    import pathlib
    pathlib.Path(p).mkdir(parents=True, exist_ok=True)

class SPCClient:
    def __init__(self, cfg: dict, debug: bool = False):
        spc = cfg.get("spc", {})
        self.host   = spc.get("host", "").rstrip("/")
        self.user   = spc.get("user", "")
        self.pin    = spc.get("pin", "")
        self.lang   = str(spc.get("language", 253))
        self.cache  = spc.get("session_cache_dir", "/var/lib/acre_exp")
        self.min_login_interval = int(spc.get("min_login_interval_sec", 60))
        self.debug = bool(spc.get("_debug", False)) or debug

        ensure_dir(self.cache)
        self.session_file = os.path.join(self.cache, "spc_session.json")
        self.cookie_file  = os.path.join(self.cache, "spc_cookies.jar")

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0 Safari/537.36",
            "Connection": "keep-alive",
        })
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

    def _get(self, url, referer=None):
        headers = {}
        if referer:
            headers["Referer"] = referer
        r = self.session.get(url, timeout=8, headers=headers, allow_redirects=True)
        r.raise_for_status()
        r.encoding = "utf-8"
        return r

    def _post(self, url, data, referer=None, allow_redirects=True):
        headers = {}
        if referer:
            headers["Referer"] = referer
        r = self.session.post(url, data=data, allow_redirects=allow_redirects, timeout=8, headers=headers)
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
        has_pass = ('name="password"' in low) or ('id="password'" in low) or ("id='password'" in low)
        return has_user and has_pass

    @staticmethod
    def _extract_state_text(td):
        txt = td.get_text(strip=True)
        if txt:
            return txt
        img = td.find("img")
        if img:
            alt = (img.get("alt") or "").strip()
            if alt: return alt
            title = (img.get("title") or "").strip()
            if title: return title
        return ""

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
                etat_txt = self._extract_state_text(tds[5])
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
            state = self._extract_state_text(tds[2])
            if label.lower().startswith("secteur"):
                m = re.match(r"^Secteur\s+(\d+)\s*:\s*(.+)$", label, re.I)
                if m:
                    num, nom = m.groups()
                    areas.append({"sid": num, "nom": nom, "etat_txt": state})
        return areas

    def __init_logging(self, debug: bool):
        logging.basicConfig(stream=sys.stderr, level=(logging.DEBUG if debug else logging.INFO),
                            format="%(levelname)s:%(message)s")

    def fetch(self):
        # placeholder; logging configuration is done in main()
        pass

    # Split fetch out of __init__ to keep code above tidy
    def fetch(self):
        sid = self.get_or_login()
        if not sid: return {"zones": [], "areas": []}

        def _fetch(page: str):
            url = f"{self.host}/secure.htm?session={sid}&page={page}"
            logging.debug("Requesting %s from: %s", page, url)
            r = self._get(url, referer=f"{self.host}/secure.htm?session={sid}&page=spc_home")
            logging.debug("%s page length: %d bytes", page, len(r.text))
            return sid, r

        sid, r_z = _fetch("status_zones")
        zones = self.parse_zones(r_z.text)
        logging.debug("Parsed zones count: %d", len(zones))
        if len(zones) == 0 and self._is_login_response(r_z.text, getattr(r_z, "url", ""), True):
            logging.debug("Zones parse empty + looks like login — re-login once")
            new_sid = self._do_login()
            if new_sid:
                sid = new_sid
                r_z = self._get(f"{self.host}/secure.htm?session={sid}&page=status_zones",
                                 referer=f"{self.host}/secure.htm?session={sid}&page=spc_home")
                zones = self.parse_zones(r_z.text)
                logging.debug("status_zones retry length: %d — parsed: %d", len(r_z.text), len(zones))

        sid, r_a = _fetch("spc_home")
        areas = self.parse_areas(r_a.text)
        logging.debug("Parsed areas count: %d", len(areas))
        if len(areas) == 0 and self._is_login_response(r_a.text, getattr(r_a, "url", ""), True):
            logging.debug("Areas parse empty + looks like login — re-login once")
            new_sid = self._do_login()
            if new_sid:
                sid = new_sid
                r_a = self._get(f"{self.host}/secure.htm?session={sid}&page=spc_home",
                                 referer=f"{self.host}/secure.htm?session={sid}&page=spc_home")
                areas = self.parse_areas(r_a.text)
                logging.debug("spc_home retry length: %d — parsed: %d", len(r_a.text), len(areas))

        self._save_cookies()
        self._save_session_cache(sid)
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

        self.client = mqtt.Client(
            client_id=self.client_id,
            protocol=self.protocol,
            callback_api_version=CallbackAPIVersion.V5,
        )

        def _on_connect(client, userdata, flags, reason_code, properties):
            ok = (reason_code == 0)
            self._set_conn(ok, reason_code)

        def _on_disconnect(client, userdata, reason_code, properties):
            self._unset_conn(reason_code)

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
        zid = SPCClient.zone_id_from_name(z["zname"])
        b   = SPCClient.zone_bin(z["etat_txt"])
        mq.pub(f"zones/{zid}/name", z["zname"])
        mq.pub(f"zones/{zid}/secteur", z["sect"])
        if b in (0,1):
            last_z[zid] = b
            mq.pub(f"zones/{zid}/state", b)

    for a in snap["areas"]:
        sid = a["sid"]
        s   = SPCClient.area_num(a["etat_txt"])
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

        for z in data["zones"]:
            zid = SPCClient.zone_id_from_name(z["zname"])
            b   = SPCClient.zone_bin(z["etat_txt"])
            if b not in (0,1): continue
            old = last_z.get(zid)
            if old is None or b != old:
                mq.pub(f"zones/{zid}/state", b)
                last_z[zid] = b
                if log_changes:
                    print(f"[{tick}] 🟡 Zone '{z['zname']}' → {b}")

        for a in data["areas"]:
            sid = a["sid"]
            s   = SPCClient.area_num(a["etat_txt"])
            if s < 0: continue
            old = last_a.get(sid)
            if old is None or s != old:
                mq.pub(f"secteurs/{sid}/state", s)
                last_a[sid] = s
                if log_changes:
                    print(f"[{tick}] 🔵 Secteur '{a['nom']}' → {s}")

        time.sleep(interval)

    mq.client.loop_stop()
    try:
        mq.client.disconnect()
    except Exception:
        pass
    print("[SPC→MQTT] Arrêt propre.")

if __name__ == "__main__":
    main()
