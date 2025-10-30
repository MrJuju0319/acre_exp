#!/opt/spc-venv/bin/python3
# -*- coding: utf-8 -*-

"""
ACRE SPC42 → MQTT watchdog (corrected)

This version addresses the issue of repeated login events from the SPC
controller by ensuring that the session ID and cookies are persisted and
reused across polls.  It also introduces a simple lock mechanism so that
only one watchdog instance runs at a time (preventing two processes from
invalidating each other’s sessions) and uses an exponential back‑off
strategy with jitter to avoid hammering the controller when login fails.

Key changes:

* **Persistent session and cookies** saved atomically with file locks.
* **Robust session validation**: we only log in again if the controller
  redirects us to ``login.htm``.  This matches the behaviour observed in
  your packet capture.
* **Exponential back‑off and jitter** applied when logins fail or occur too
  frequently, preventing stormy login loops.
* **Single instance lock** using ``/var/run/acre_exp.lock`` to avoid
  concurrent watchdogs stepping on each other.
* **MQTT Last Will and Testament** (LWT) publishes ``online``/``offline``
  status for the watchdog so you know if it crashes.

Additionally, the watchdog now fetches both zones and area statuses in a
single request to ``controller_status`` rather than two separate requests
(``status_zones`` and ``spc_home``).  This reduces load on the controller.

"""

import os
import re
import sys
import time
import json
import signal
import argparse
import tempfile
import fcntl
import contextlib
import random
import pathlib
import atexit
from typing import Dict

import yaml
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from http.cookiejar import MozillaCookieJar

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


# --- Helpers for safe file operations ---

@contextlib.contextmanager
def locked_file(path: str, mode: str = "r+"):
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, mode) as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield f
        finally:
            f.flush()
            os.fsync(f.fileno())
            fcntl.flock(f, fcntl.LOCK_UN)


def atomic_write(path: str, data: bytes, mode: int = 0o600) -> None:
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=directory)
    with os.fdopen(fd, "wb") as tmp:
        tmp.write(data)
        tmp.flush()
        os.fsync(tmp.fileno())
    os.replace(tmp_path, path)
    os.chmod(path, mode)


def ensure_dir(path: str) -> None:
    pathlib.Path(path).mkdir(parents=True, exist_ok=True)


def load_cfg(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# --- SPC Client (same improvements as in acre_exp_status) ---

class SPCClient:
    def __init__(self, cfg: dict) -> None:
        spc = cfg.get("spc", {})
        self.host = (spc.get("host") or "").rstrip("/")
        self.user = spc.get("user", "")
        self.pin = spc.get("pin", "")
        self.lang = str(spc.get("language", 253))
        self.cache = spc.get("session_cache_dir", "/var/lib/acre_exp")
        self.min_login_interval = int(spc.get("min_login_interval_sec", 60))
        self._last_login_fail = 0.0
        self._backoff = 0.0

        ensure_dir(self.cache)
        self.session_file = os.path.join(self.cache, "spc_session.json")
        self.cookie_file = os.path.join(self.cache, "spc_cookies.jar")

        self.session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST"]),
        )
        adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=10, pool_maxsize=20)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.headers.update({"Connection": "keep-alive", "User-Agent": "spc42-client/1.0"})

        self.cookiejar = MozillaCookieJar(self.cookie_file)
        self._load_cookies()
        atexit.register(self._save_cookies)

    # Cookies
    def _load_cookies(self) -> None:
        try:
            if os.path.exists(self.cookie_file):
                self.cookiejar.load(ignore_discard=True, ignore_expires=True)
            self.session.cookies = self.cookiejar
        except Exception:
            try:
                os.remove(self.cookie_file)
            except Exception:
                pass
            self.session.cookies = MozillaCookieJar()

    def _save_cookies(self) -> None:
        try:
            tmp = self.cookie_file + ".tmp"
            self.cookiejar.save(tmp, ignore_discard=True, ignore_expires=True)
            os.replace(tmp, self.cookie_file)
            os.chmod(self.cookie_file, 0o600)
        except Exception:
            pass

    # HTTP
    def _get(self, url: str):
        r = self.session.get(url, timeout=8)
        r.raise_for_status()
        self._save_cookies()
        r.encoding = "utf-8"
        return r

    def _post(self, url: str, data: dict, allow_redirects: bool = True):
        r = self.session.post(url, data=data, allow_redirects=allow_redirects, timeout=8)
        r.raise_for_status()
        self._save_cookies()
        r.encoding = "utf-8"
        return r

    # Session cache
    def _load_session_cache(self) -> dict:
        if not os.path.exists(self.session_file):
            return {}
        try:
            with locked_file(self.session_file, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_session_cache(self, sid: str) -> None:
        if not sid:
            return
        payload = {"host": self.host, "session": sid, "time": time.time()}
        atomic_write(self.session_file, json.dumps(payload).encode("utf-8"))

    def _last_login_too_recent(self) -> bool:
        data = self._load_session_cache()
        t = float(data.get("time", 0))
        jitter = random.uniform(0, self.min_login_interval * 0.2)
        return (time.time() - t) < (self.min_login_interval + jitter)

    @staticmethod
    def _extract_session(text_or_url: str) -> str:
        if not text_or_url:
            return ""
        m = re.search(r"[?&]session=([0-9A-Za-zx]+)", text_or_url)
        if m:
            return m.group(1)
        m = re.search(r"secure\.htm\?[^\"'>]*session=([0-9A-Za-zx]+)", text_or_url)
        return m.group(1) if m else ""

    def _session_valid(self, sid: str) -> bool:
        try:
            url = f"{self.host}/secure.htm?session={sid}&page=controller_status"
            r = self._get(url)
            low = r.text.lower()
            if "login.htm" in low or "mot de passe" in low or "identifiant" in low:
                return False
            return True
        except Exception:
            return False

    def _do_login(self) -> str:
        """
        Perform a login to the SPC controller and return a new session ID.

        This method initiates the login sequence by requesting the login page
        then posts the user credentials.  On success, the new session ID is
        cached and cookies are saved.  Note that we avoid using ``urljoin``
        here because ``urljoin`` is not imported in this module; instead we
        construct the URL manually.
        """
        try:
            # Initiate the login sequence by fetching the login page.  The
            # controller may set an initial cookie here.
            self._get(f"{self.host}/login.htm")
        except Exception:
            # Ignore errors fetching the login page; we'll attempt to login anyway.
            pass
        # Submit the user credentials.  The SPC controller returns a redirect
        # URL containing the new session ID.
        url = f"{self.host}/login.htm?action=login&language={self.lang}"
        r = self._post(url, {"userid": self.user, "password": self.pin}, allow_redirects=True)
        sid = self._extract_session(r.url) or self._extract_session(r.text)
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
        now = time.time()
        if now - self._last_login_fail < (self._backoff or 0):
            time.sleep(min(self._backoff, 60))
            if sid and self._session_valid(sid):
                return sid
        if self._last_login_too_recent():
            time.sleep(2)
            if sid and self._session_valid(sid):
                return sid
        new_sid = self._do_login()
        if new_sid:
            self._last_login_fail = 0.0
            self._backoff = 0.0
            return new_sid
        self._last_login_fail = now
        self._backoff = min((self._backoff or 2) * 2, 60)
        return sid or ""

    # Mapping and parsing
    @staticmethod
    def zone_bin(etat_txt: str) -> int:
        s = (etat_txt or "").lower()
        if "normal" in s:
            return 0
        if "activ" in s:
            return 1
        return -1

    @staticmethod
    def area_num(etat_txt: str) -> int:
        s = (etat_txt or "").lower()
        if "mhs" in s or "désarm" in s:
            return 0
        if "mes totale" in s:
            return 1
        if "mes partiel" in s:
            return 2
        return -1

    @staticmethod
    def zone_id_from_name(name: str) -> str:
        m = re.match(r"^\s*(\d+)\b", name or "")
        if m:
            return m.group(1)
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", name or "").strip("_").lower()
        return slug or "unknown"

    def _parse_status_page(self, html: str) -> Dict[str, list]:
        soup = BeautifulSoup(html, "html.parser")
        zones = []
        table = soup.find("table", {"class": "gridtable"})
        if table:
            for tr in table.find_all("tr"):
                tds = tr.find_all("td")
                if len(tds) >= 6:
                    zname = tds[0].get_text(strip=True)
                    sect = tds[1].get_text(strip=True)
                    etat_txt = tds[5].get_text(strip=True)
                    if zname:
                        zones.append({"zname": zname, "sect": sect, "etat_txt": etat_txt})
        areas = []
        for tr in soup.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) >= 3:
                label = tds[1].get_text(strip=True)
                state = tds[2].get_text(strip=True)
                if label.lower().startswith("secteur"):
                    m = re.match(r"^Secteur\s+(\d+)\s*:\s*(.+)$", label, re.I)
                    if m:
                        num, nom = m.groups()
                        areas.append({"sid": num, "nom": nom, "etat_txt": state})
        return {"zones": zones, "areas": areas}

    def fetch(self) -> dict:
        sid = self.get_or_login()
        if not sid:
            return {"zones": [], "areas": []}
        url = f"{self.host}/secure.htm?session={sid}&page=controller_status"
        html = self._get(url).text
        self._save_cookies()
        return self._parse_status_page(html)


# --- MQTT wrapper ---

class MQ:
    def __init__(self, cfg: dict) -> None:
        m = cfg.get("mqtt", {})
        self.host = m.get("host", "127.0.0.1")
        self.port = int(m.get("port", 1883))
        self.user = m.get("user", "")
        self.pwd = m.get("pass", "")
        self.base = m.get("base_topic", "spc").strip("/")
        self.qos = int(m.get("qos", 0))
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

        # Last Will and Testament (LWT)
        self.lwt_topic = f"{self.base}/status"
        self.client.will_set(self.lwt_topic, payload="offline", qos=self.qos, retain=True)

        self.connected = False
        if HAS_V2:
            def _on_connect(c, u, flags, rc, properties=None):
                self._set_conn(rc)
            def _on_disconnect(c, u, rc, properties=None):
                self._unset_conn()
        else:
            def _on_connect(c, u, flags, rc):
                self._set_conn(rc)
            def _on_disconnect(c, u, rc):
                self._unset_conn()
        self.client.on_connect = _on_connect
        self.client.on_disconnect = _on_disconnect

    def _set_conn(self, rc: int) -> None:
        self.connected = (rc == 0)
        print("[MQTT] Connecté" if self.connected else f"[MQTT] Connexion échouée rc={rc}")
        if self.connected:
            # Announce online status on connection
            self.pub("status", "online")

    def _unset_conn(self) -> None:
        self.connected = False
        print("[MQTT] Déconnecté")

    def connect(self) -> None:
        while True:
            try:
                self.client.connect(self.host, self.port, keepalive=30)
                self.client.loop_start()
                for _ in range(30):
                    if self.connected:
                        return
                    time.sleep(0.2)
            except Exception as e:
                print(f"[MQTT] Erreur: {e}")
            time.sleep(2)

    def pub(self, topic: str, payload: object) -> None:
        full_topic = f"{self.base}/{topic}".strip("/")
        try:
            self.client.publish(full_topic, payload=str(payload), qos=self.qos, retain=self.retain)
        except Exception as e:
            print(f"[MQTT] publish ERR {full_topic}: {e}")


# --- Main ---

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="/etc/acre_exp/config.yml")
    args = ap.parse_args()

    # Acquire a lock to prevent concurrent watchdog instances.
    lock_path = "/var/run/acre_exp.lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("[LOCK] Une autre instance est déjà en cours. Abandon.")
        sys.exit(0)

    cfg = load_cfg(args.config)
    wd_config = cfg.get("watchdog", {})
    interval = int(wd_config.get("refresh_interval", 2))
    log_changes = bool(wd_config.get("log_changes", True))

    spc = SPCClient(cfg)
    mq = MQ(cfg)

    print(f"[SPC→MQTT] Démarrage (refresh={interval}s) — Broker {mq.host}:{mq.port}")
    mq.connect()

    last_z: Dict[str, int] = {}
    last_a: Dict[str, int] = {}

    running = True

    def stop(*_: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    # Initial snapshot
    snapshot = spc.fetch()
    for z in snapshot["zones"]:
        zid = spc.zone_id_from_name(z["zname"])
        state_bin = spc.zone_bin(z["etat_txt"])
        mq.pub(f"zones/{zid}/name", z["zname"])
        mq.pub(f"zones/{zid}/secteur", z["sect"])
        if state_bin in (0, 1):
            last_z[zid] = state_bin
            mq.pub(f"zones/{zid}/state", state_bin)
    for a in snapshot["areas"]:
        sid = a["sid"]
        state_num = spc.area_num(a["etat_txt"])
        mq.pub(f"secteurs/{sid}/name", a["nom"])
        if state_num >= 0:
            last_a[sid] = state_num
            mq.pub(f"secteurs/{sid}/state", state_num)
    print("[SPC→MQTT] État initial publié.")

    # Main loop
    while running:
        tick = time.strftime("%H:%M:%S")
        try:
            data = spc.fetch()
        except Exception as e:
            print(f"[SPC] fetch ERR: {e}")
            time.sleep(interval)
            continue

        # Process zones
        for z in data["zones"]:
            zid = spc.zone_id_from_name(z["zname"])
            state_bin = spc.zone_bin(z["etat_txt"])
            if state_bin not in (0, 1):
                continue
            old = last_z.get(zid)
            if old is None or state_bin != old:
                mq.pub(f"zones/{zid}/state", state_bin)
                last_z[zid] = state_bin
                if log_changes:
                    print(f"[{tick}] 🟡 Zone '{z['zname']}' → {state_bin}")

        # Process areas (secteurs)
        for a in data["areas"]:
            sid = a["sid"]
            state_num = spc.area_num(a["etat_txt"])
            if state_num < 0:
                continue
            old = last_a.get(sid)
            if old is None or state_num != old:
                mq.pub(f"secteurs/{sid}/state", state_num)
                last_a[sid] = state_num
                if log_changes:
                    print(f"[{tick}] 🔵 Secteur '{a['nom']}' → {state_num}")

        time.sleep(interval)

    mq.client.loop_stop()
    mq.client.disconnect()
    print("[SPC→MQTT] Arrêt propre.")


if __name__ == "__main__":
    main()