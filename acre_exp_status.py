#!/opt/spc-venv/bin/python3
# -*- coding: utf-8 -*-

"""
ACRE SPC42 → JSON status
- Cookies/SID persistants (lock + écriture atomique)
- Retries HTTP + keep-alive
- Validation de session robuste
- Anti-tempête (jitter sur délai mini)
"""

import os, re, sys, json, time, argparse, tempfile, fcntl, contextlib, random, pathlib
from urllib.parse import urljoin
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from http.cookiejar import MozillaCookieJar
import yaml
import atexit

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

def ensure_dir(p):
    pathlib.Path(p).mkdir(parents=True, exist_ok=True)

def load_cfg(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

# ---------- Client SPC ----------
class SPCClient:
    def __init__(self, cfg: dict):
        spc = cfg.get("spc", {})
        self.host   = (spc.get("host") or "").rstrip("/")
        self.user   = spc.get("user", "")
        self.pin    = spc.get("pin", "")
        self.lang   = str(spc.get("language", 253))
        self.cache  = spc.get("session_cache_dir", "/var/lib/acre_exp")
        self.min_login_interval = int(spc.get("min_login_interval_sec", 60))
        self._last_login_fail = 0.0
        self._backoff = 0.0

        ensure_dir(self.cache)
        self.session_file = os.path.join(self.cache, "spc_session.json")
        self.cookie_file  = os.path.join(self.cache, "spc_cookies.jar")

        self.session = requests.Session()
        retry = Retry(
            total=3, backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST"])
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=20)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.headers.update({"Connection": "keep-alive", "User-Agent": "spc42-client/1.0"})

        self.cookiejar = MozillaCookieJar(self.cookie_file)
        self._load_cookies()
        atexit.register(self._save_cookies)

    # --- Cookies
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
            tmp = self.cookie_file + ".tmp"
            self.cookiejar.save(tmp, ignore_discard=True, ignore_expires=True)
            os.replace(tmp, self.cookie_file)
            os.chmod(self.cookie_file, 0o600)
        except Exception:
            pass

    # --- HTTP
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

    # --- Session cache
    def _load_session_cache(self):
        if not os.path.exists(self.session_file):
            return {}
        try:
            with locked_file(self.session_file, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_session_cache(self, sid):
        if not sid:
            return
        payload = {"host": self.host, "session": sid, "time": time.time()}
        atomic_write(self.session_file, json.dumps(payload).encode("utf-8"))

    def _last_login_too_recent(self):
        d = self._load_session_cache()
        t = float(d.get("time", 0))
        base = self.min_login_interval
        jitter = random.uniform(0, base * 0.2)
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
        """Validation robuste: pas de redirection login + indices de contenu protégé."""
        try:
            # 1) Page zones
            url = f"{self.host}/secure.htm?session={sid}&page=status_zones"
            r = self._get(url)
            low = r.text.lower()
            if "login.htm" in low or "mot de passe" in low or "identifiant" in low:
                return False
            if ("gridtable" in low) or ("page=status_zones" in low):
                return True
            # 2) Home protégée
            url2 = f"{self.host}/secure.htm?session={sid}&page=spc_home"
            r2 = self._get(url2)
            low2 = r2.text.lower()
            if "login.htm" in low2 or "mot de passe" in low2 or "identifiant" in low2:
                return False
            return True  # tolérant
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

    # --- Parsing
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

# ---------- CLI ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="/etc/acre_exp/config.yml")
    args = ap.parse_args()

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
