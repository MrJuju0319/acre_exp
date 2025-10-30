#!/opt/spc-venv/bin/python3
# -*- coding: utf-8 -*-

import os, re, sys, time, json, argparse, signal
import yaml
import requests
from bs4 import BeautifulSoup
from http.cookiejar import MozillaCookieJar
from typing import Dict, Optional

# MQTT compatible v1/v2 (évite l’erreur d’args)
try:
    from paho.mqtt import client as mqtt
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
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/118.0 Safari/537.36",
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

    @staticmethod
    def _looks_like_login_page(html: str) -> bool:
        low = html.lower()
        return ("login.htm" in low) or ("mot de passe" in low) or ("identifiant" in low)

    def _do_login(self) -> str:
        if self.debug:
            print("[DEBUG] Performing login…")
        try:
            self._get(f"{self.host}/login.htm")
        except Exception:
            pass
        url = f"{self.host}/login.htm?action=login&language={self.lang}"
        r = self._post(url, {"userid": self.user, "password": self.pin}, allow_redirects=True)
        sid = self._extract_session(r.url) or self._extract_session(r.text)
        if self.debug:
            print(f"[DEBUG] Login got SID={sid or '(none)'}")
        if sid:
            self._save_session_cache(sid)
            self._save_cookies()
            return sid
        return ""

    def get_or_login(self) -> str:
        d = self._load_session_cache()
        sid = d.get("session", "")
        if sid:
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

    @staticmethod
    def _extract_state_text(td):
        txt = td.get_text(strip=True)
        if txt:
            return txt
        img = td.find("img")
        if img:
            alt = (img.get("alt") or "").strip()
            if alt:
                return alt
            title = (img.get("title") or "").strip()
            if title:
                return title
        return ""

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

    def fetch(self):
        sid = self.get_or_login()
        if not sid:
            return {"zones": [], "areas": []}

        def _secure(page: str) -> (str, Optional[requests.Response]):
            url = f"{self.host}/secure.htm?session={sid}&page={page}"
            if self.debug:
                print(f"[DEBUG] Using SID={sid}")
                print(f"[DEBUG] Requesting {page} from: {url}")
            r = self._get(url, referer=f"{self.host}/secure.htm?session={sid}&page=spc_home")
            if self.debug:
                print(f"[DEBUG] {page} page length: {len(r.text)} bytes")
            if self._looks_like_login_page(r.text):
                if self.debug:
                    print("[DEBUG] Looks like login page — re-login")
                new_sid = self._do_login()
                if not new_sid:
                    return "", None
                url2 = f"{self.host}/secure.htm?session={new_sid}&page={page}"
                r = self._get(url2, referer=f"{self.host}/secure.htm?session={new_sid}&page=spc_home")
                if self.debug:
                    print(f"[DEBUG] {page} retry length: {len(r.text)} bytes")
                self._save_cookies()
                self._save_session_cache(new_sid)
                return new_sid, r
            return sid, r

        sid, z = _secure("status_zones")
        if not z:
            return {"zones": [], "areas": []}
        zones = self.parse_zones(z.text)
        if self.debug:
            print(f"[DEBUG] Parsed zones count: {len(zones)}")
            if zones:
                print(f"[DEBUG] First zone: {zones[0]}")

        sid, a = _secure("spc_home")
        if not a:
            return {"zones": zones, "areas": []}
        areas = self.parse_areas(a.text)
        if self.debug:
            print(f"[DEBUG] Parsed areas count: {len(areas)}")
            if areas:
                print(f"[DEBUG] First area: {areas[0]}")

        self._save_cookies()
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

        # signature compatible v1/v2
        self.client = mqtt.Client(client_id=self.client_id, protocol=mqtt.MQTTv311)
        if self.user:
            self.client.username_pw_set(self.user, self.pwd)
        self.connected = False

        def _on_connect(client, userdata, flags, rc, *args, **kwargs):
            self.connected = (rc == 0)
            print("[MQTT] Connecté" if self.connected else f"[MQTT] Connexion échouée rc={rc}")

        def _on_disconnect(client, userdata, rc, *args, **kwargs):
            self.connected = False
            print("[MQTT] Déconnecté")

        self.client.on_connect = _on_connect
        self.client.on_disconnect = _on_disconnect

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
    try:
        mq.client.disconnect()
    except Exception:
        pass
    print("[SPC→MQTT] Arrêt propre.")

if __name__ == "__main__":
    main()
