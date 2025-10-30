#!/opt/spc-venv/bin/python3
# -*- coding: utf-8 -*-

import os, re, sys, time, json, argparse, signal, tempfile, fcntl, contextlib, random
import yaml
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from http.cookiejar import MozillaCookieJar
from typing import Dict
import atexit

# --- MQTT (compat v1/v2) ---
try:
    from paho.mqtt import client as mqtt
    try:
        from paho.mqtt.client import CallbackAPIVersion
        HAS_V2 = True
    except Exception:
        HAS_V2 = False
except Exception:
    print("[ERREUR] paho-mqtt non installé : /opt/spc-venv/bin/pip install paho-mqtt")
    sys.exit(1)

# ---------- Utils fichiers sûrs ----------
@contextlib.contextmanager
def locked_file(path, mode="r+"):
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, mode) as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield f
        finally:
            f.flush()
            os.fsync(f.fileno())
            fcntl.flock(f, fcntl.LOCK_UN)

def atomic_write(path, data_bytes: bytes, mode=0o600):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=d)
    with os.fdopen(fd, "wb") as f:
        f.write(data_bytes)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    os.chmod(path, mode)

# ---------- Chargement YAML ----------
def load_cfg(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def ensure_dir(p):
    import pathlib
    pathlib.Path(p).mkdir(parents=True, exist_ok=True)

# ---------- Client SPC (tokens robustes) ----------
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
        retry = Retry(
            total=3, backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET","POST"])
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=20)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.headers.update({"Connection":"keep-alive","User-Agent":"spc42-client/1.0"})

        self.cookiejar = MozillaCookieJar(self.cookie_file)
        self._load_cookies()
        atexit.register(self._save_cookies)

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
            tmp = self.cookie_file + ".tmp"
            self.cookiejar.save(tmp, ignore_discard=True, ignore_expires=True)
            os.replace(tmp, self.cookie_file)
            os.chmod(self.cookie_file, 0o600)
        except Exception:
            pass

    def _get(self, url):
        r = self.session.get(url, timeout=8)
        r.raise_for_status()
        self._save_cookies()
        r.encoding = "utf-8"
        return r

    def _post(self, url, data, allow_redirects=True):
        r = self.session.post(url, data=data, allow_redirects=allow_redirects, timeout=8)
        r.raise_for_status()
        self._save_cookies()
        r.encoding = "utf-8"
        return r

    def _load_session_cache(self):
        if not os.path.exists(self.session_file):
            return {}
        try:
            with locked_file(self.session_file, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_session_cache(self, sid):
        if not sid: return
        payload = {"host": self.host, "session": sid, "time": time.time()}
        atomic_write(self.session_file, json.dumps(payload).encode("utf-8"))

    def _last_login_too_recent(self):
        d = self._load_session_cache()
        t = float(d.get("time", 0))
        base = self.min_login_interval
        jitter = random.uniform(0, base*0.2)
        return (time.time() - t) < (base + jitter)

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
    """Validation robuste: pas de redirection login + présence de contenu 'zones'."""
    try:
        # 1) Appel d'une page protégée (status_zones) avec le sid
        url = f"{self.host}/secure.htm?session={sid}&page=status_zones"
        r = self._get(url)

        # 2) Si on a été redirigé vers login, la session est invalide
        low = r.text.lower()
        if "login.htm" in low or "mot de passe" in low or "identifiant" in low:
            return False

        # 3) Chercher un indice HTML réel de la page "zones"
        #    -> présence d'un tableau "gridtable" ou de l'URL 'page=status_zones' dans le HTML
        if ("gridtable" in low) or ("page=status_zones" in low):
            return True

        # 4) Dernière chance : la home protégée sans redirection
        url2 = f"{self.host}/secure.htm?session={sid}&page=spc_home"
        r2 = self._get(url2)
        low2 = r2.text.lower()
        if "login.htm" in low2 or "mot de passe" in low2 or "identifiant" in low2:
            return False
        # On tolère tant que ce n'est PAS une page de login
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

        # double-check avant relogin (évite faux négatifs)
        if sid and not self._last_login_too_recent():
            time.sleep(1.0)
            if self._session_valid(sid):
                return sid

        if self._last_login_too_recent():
            time.sleep(2)
            if sid and self._session_valid(sid):
                return sid
        return self._do_login()

    # mapping
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
        return {"zones": self.parse_zones(z_html), "areas": self.parse_areas(a_html)}

# ---------- MQTT ----------
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
            self.client = mqtt.Client(
                callback_api_version=CallbackAPIVersion.VERSION2,
                client_id=self.client_id,
                protocol=mqtt.MQTTv311,
                transport="tcp",
            )
        else:
            self.client = mqtt.Client(client_id=self.client_id, clean_session=True, userdata=None, protocol=mqtt.MQTTv311)

        if self.user:
            self.client.username_pw_set(self.user, self.pwd)

        # LWT online/offline
        self.lwt_topic = f"{self.base}/status"
        self.client.will_set(self.lwt_topic, payload="offline", qos=self.qos, retain=True)

        self.connected = False
        if HAS_V2:
            def _on_connect(c, u, flags, rc, properties=None): self._set_conn(rc)
            def _on_disconnect(c, u, rc, properties=None): self._unset_conn()
        else:
            def _on_connect(c, u, flags, rc): self._set_conn(rc)
            def _on_disconnect(c, u, rc): self._unset_conn()
        self.client.on_connect = _on_connect
        self.client.on_disconnect = _on_disconnect

    def _set_conn(self, rc):
        self.connected = (rc == 0)
        print("[MQTT] Connecté" if self.connected else f"[MQTT] Connexion échouée rc={rc}")
        if self.connected:
            self.pub("status", "online")

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

# ---------- Main loop ----------
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

