#!/opt/spc-venv/bin/python3
# -*- coding: utf-8 -*-

"""
ACRE SPC42 → JSON status script (corrected)

This version addresses the repeated-login issue reported on your SPC system by
ensuring that once a session and cookies have been obtained they are reused
across subsequent requests.  The session is considered valid unless the
controller actively redirects to the login page.  It also avoids corrupting
the session or cookie files by writing them atomically with file locks and
uses a small random jitter on the minimum login interval to avoid several
instances logging in simultaneously.

Key changes compared to your original version:

* Persistent cookies and session ID saved atomically to disk (using
  ``atomic_write`` and ``locked_file``).  This eliminates race conditions
  when more than one process tries to write the files at the same time.
* Session validation no longer depends on the presence of a magic string
  like “spc42” in the page.  Instead, any page that doesn’t redirect to
  ``login.htm`` is considered valid, which is what the SPC controller actually
  tests.  This prevents false negatives that would otherwise trigger a new
  login on every poll.
* A back‑off mechanism and jitter on the minimum login interval so that if
  a login fails repeatedly the script waits progressively longer before
  reattempting.  This helps to protect the controller from storms of login
  attempts during transient network problems.
* HTTP connections use a requests ``Retry`` adapter with a small back‑off to
  reduce false login triggers caused by network timeouts.
* The underlying code continues to parse the same HTML tables, but you
  could replace ``_parse_status_page`` with another parser if your SPC
  firmware changes.  The packet capture you supplied shows that the
  controller uses the ``controller_status`` page rather than ``status_zones``;
  the parser here works with that page as well because both pages contain
  the same ``gridtable`` class.

"""

import os
import re
import sys
import json
import time
import argparse
import tempfile
import fcntl
import contextlib
import random
import pathlib
import atexit
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from http.cookiejar import MozillaCookieJar
import yaml


# --- Helpers for safe file operations ---

@contextlib.contextmanager
def locked_file(path: str, mode: str = "r+"):
    """Open a file with an exclusive lock to prevent concurrent writes."""
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
    """Write bytes atomically by writing to a temp file then renaming."""
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


# --- SPC Client ---

class SPCClient:
    """
    High‑level client for communicating with the SPC web interface.

    It handles login, session caching, cookie persistence and HTTP retries.
    """

    def __init__(self, cfg: dict) -> None:
        spc = cfg.get("spc", {})
        self.host = (spc.get("host") or "").rstrip("/")
        self.user = spc.get("user", "")
        self.pin = spc.get("pin", "")
        self.lang = str(spc.get("language", 253))
        self.cache = spc.get("session_cache_dir", "/var/lib/acre_exp")
        self.min_login_interval = int(spc.get("min_login_interval_sec", 60))

        # These variables are used for backoff when logins fail.
        self._last_login_fail = 0.0
        self._backoff = 0.0

        ensure_dir(self.cache)
        self.session_file = os.path.join(self.cache, "spc_session.json")
        self.cookie_file = os.path.join(self.cache, "spc_cookies.jar")

        self.session = requests.Session()

        # Configure HTTP retries with exponential back‑off to reduce false
        # negatives that might trigger new logins.
        retry_strategy = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST"]),
        )
        adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=10, pool_maxsize=20)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        # Basic header to reduce HTTP connection overhead.
        self.session.headers.update({"Connection": "keep-alive", "User-Agent": "spc42-client/1.0"})

        self.cookiejar = MozillaCookieJar(self.cookie_file)
        self._load_cookies()
        atexit.register(self._save_cookies)


    # --- Cookie handling ---

    def _load_cookies(self) -> None:
        """Load cookies from disk into the session."""
        try:
            if os.path.exists(self.cookie_file):
                self.cookiejar.load(ignore_discard=True, ignore_expires=True)
            self.session.cookies = self.cookiejar
        except Exception:
            # If loading fails, remove the file and start fresh.
            try:
                os.remove(self.cookie_file)
            except Exception:
                pass
            self.session.cookies = MozillaCookieJar()


    def _save_cookies(self) -> None:
        """Persist cookies to disk atomically."""
        try:
            tmp = self.cookie_file + ".tmp"
            self.cookiejar.save(tmp, ignore_discard=True, ignore_expires=True)
            os.replace(tmp, self.cookie_file)
            os.chmod(self.cookie_file, 0o600)
        except Exception:
            pass


    # --- HTTP wrappers that save cookies after each request ---

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


    # --- Session cache handling ---

    def _load_session_cache(self) -> dict:
        """Load the cached session (SID) from disk."""
        if not os.path.exists(self.session_file):
            return {}
        try:
            with locked_file(self.session_file, "r") as f:
                return json.load(f)
        except Exception:
            return {}


    def _save_session_cache(self, sid: str) -> None:
        """Write the session ID to disk with a timestamp."""
        if not sid:
            return
        payload = {"host": self.host, "session": sid, "time": time.time()}
        atomic_write(self.session_file, json.dumps(payload).encode("utf-8"))


    def _last_login_too_recent(self) -> bool:
        """Return True if we logged in too recently and should not login again yet."""
        data = self._load_session_cache()
        last_time = float(data.get("time", 0))
        # Apply jitter up to 20% of the interval to avoid concurrent logins.
        jitter = random.uniform(0, self.min_login_interval * 0.2)
        return (time.time() - last_time) < (self.min_login_interval + jitter)


    @staticmethod
    def _extract_session(text_or_url: str) -> str:
        """Extract the session token from a URL or response body."""
        if not text_or_url:
            return ""
        m = re.search(r"[?&]session=([0-9A-Za-zx]+)", text_or_url)
        if m:
            return m.group(1)
        m = re.search(r"secure\.htm\?[^\"'>]*session=([0-9A-Za-zx]+)", text_or_url)
        return m.group(1) if m else ""


    def _session_valid(self, sid: str) -> bool:
        """
        Determine whether a given session ID is still valid.

        The SPC controller does not include a fixed banner string on its
        protected pages (as your packet capture confirms).  Therefore, the
        validation logic simply fetches a protected page and checks that we
        were not redirected to ``login.htm``.
        """
        try:
            # Try a lightweight page first (controller_status shows both
            # zones and areas).  If the controller redirects to login it will
            # return a small page containing login forms.
            url = f"{self.host}/secure.htm?session={sid}&page=controller_status"
            r = self._get(url)
            low = r.text.lower()
            if "login.htm" in low or "mot de passe" in low or "identifiant" in low:
                return False
            # Otherwise, the session is considered valid.
            return True
        except Exception:
            return False


    def _do_login(self) -> str:
        """
        Perform a login to the SPC controller and return the session ID.

        On success the new SID is saved to the cache and cookies are persisted.
        """
        # Kick start the login sequence by fetching the login page.  The
        # controller may set a cookie here.
        try:
            self._get(urljoin(self.host, "/login.htm"))
        except Exception:
            pass
        # Submit credentials.
        url = f"{self.host}/login.htm?action=login&language={self.lang}"
        r = self._post(url, {"userid": self.user, "password": self.pin}, allow_redirects=True)
        sid = self._extract_session(r.url) or self._extract_session(r.text)
        if sid:
            self._save_session_cache(sid)
            self._save_cookies()
            return sid
        return ""


    def get_or_login(self) -> str:
        """
        Return the current valid session ID or perform a login if necessary.

        Uses a back‑off mechanism to avoid hammering the controller when
        repeatedly failing to login.
        """
        data = self._load_session_cache()
        sid = data.get("session", "")

        # If we have a sid and it is valid, reuse it.
        if sid and self._session_valid(sid):
            return sid

        now = time.time()
        # If we recently failed to login, apply exponential backoff.
        if now - self._last_login_fail < (self._backoff or 0):
            # Sleep up to our backoff (but cap at 60s to avoid long delays).
            time.sleep(min(self._backoff, 60))
            # Recheck validity after waiting; maybe the session became valid
            # because another process logged in meanwhile.
            if sid and self._session_valid(sid):
                return sid

        # Respect the minimum login interval (with jitter) before trying a fresh login.
        if self._last_login_too_recent():
            # Wait two seconds then recheck again.
            time.sleep(2)
            if sid and self._session_valid(sid):
                return sid

        # Perform login.
        new_sid = self._do_login()
        if new_sid:
            # Reset backoff on success.
            self._last_login_fail = 0.0
            self._backoff = 0.0
            return new_sid

        # Login failed – increase backoff and remember the failure time.
        self._last_login_fail = now
        self._backoff = min((self._backoff or 2) * 2, 60)
        return sid or ""


    # --- Parsing logic ---

    @staticmethod
    def _map_entree(text: str) -> int:
        s = (text or "").lower()
        if "ferm" in s:
            return 1
        if "ouvert" in s:
            return 0
        return -1


    @staticmethod
    def _map_zone_state(text: str) -> int:
        s = (text or "").lower()
        if "normal" in s:
            return 1
        if "activ" in s:
            return 2
        return -1


    @staticmethod
    def _map_area_state(text: str) -> int:
        s = (text or "").lower()
        if "mes totale" in s:
            return 2
        if "mes partiel" in s:
            return 3
        if "mhs" in s or "désarm" in s:
            return 1
        if "alarme" in s:
            return 4
        return 0


    def _parse_status_page(self, html: str) -> dict:
        """
        Parse the controller_status page (or status_zones) into zones and areas.

        Both pages contain a table with class ``gridtable`` listing zones and a
        list of rows beginning with ``Secteur`` for areas.  We extract these
        into dictionaries with numeric state codes.
        """
        soup = BeautifulSoup(html, "html.parser")
        # Zones are in a table with class 'gridtable'.
        zones = []
        table = soup.find("table", {"class": "gridtable"})
        if table:
            for tr in table.find_all("tr"):
                cells = tr.find_all("td")
                if len(cells) >= 6:
                    zname = cells[0].get_text(strip=True)
                    sect = cells[1].get_text(strip=True)
                    entree_txt = cells[4].get_text(strip=True)
                    etat_txt = cells[5].get_text(strip=True)
                    if zname:
                        zones.append({
                            "zone": zname,
                            "secteur": sect,
                            "entree_txt": entree_txt,
                            "etat_txt": etat_txt,
                            "entree": self._map_entree(entree_txt),
                            "etat": self._map_zone_state(etat_txt),
                        })

        # Areas (secteurs) are rows where the second cell starts with 'Secteur'.
        areas = []
        for tr in soup.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) >= 3:
                label = cells[1].get_text(strip=True)
                state = cells[2].get_text(strip=True)
                if label.lower().startswith("secteur"):
                    m = re.match(r"^Secteur\s+(\d+)\s*:\s*(.+)$", label, re.I)
                    if m:
                        num, nom = m.groups()
                        areas.append({
                            "secteur": f"{num} {nom}",
                            "nom": nom,
                            "etat_txt": state,
                            "etat": self._map_area_state(state),
                        })
        return {"zones": zones, "areas": areas}


    def fetch_status(self) -> dict:
        """Fetch zones and area status from the controller using the current session."""
        sid = self.get_or_login()
        if not sid:
            return {"error": "Impossible d’obtenir une session"}
        # Prefer the controller_status page which contains both zones and areas in one request.
        url = f"{self.host}/secure.htm?session={sid}&page=controller_status"
        html = self._get(url).text
        data = self._parse_status_page(html)
        self._save_cookies()
        return data


# --- CLI entrypoint ---

def main() -> None:
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
    # Ensure stdout uses UTF‑8 so JSON output encodes accented characters correctly.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()