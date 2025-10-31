#!/opt/spc-venv/bin/python3
# -*- coding: utf-8 -*-

import os, re, sys, json, time, pathlib, argparse, logging
import requests
from bs4 import BeautifulSoup
from http.cookiejar import MozillaCookieJar
from urllib.parse import urljoin
import yaml

def load_cfg(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def ensure_dir(p):
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
            self.session.cookies = MozillaCookieJar()

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
        has_pass = ('name="password"' in low) or ('id="password"' in low) or ("id='password'" in low)
        return has_user and has_pass

    def _do_login(self):
        logging.debug("Performing login…")
        try:
            self._get(urljoin(self.host, "/login.htm"))
        except Exception:
            pass
        url = f"{self.host}/login.htm?action=login&language={self.lang}"
        r = self._post(url, {"userid": self.user, "password": self.pin}, allow_redirects=True)
        sid = self._extract_session(r.url) or self._extract_session(r.text)
        logging.debug("Login got SID=%s", sid or "(none)")
        if sid:
            self._save_session_cache(sid)
            self._save_cookies()
            return sid
        return ""

    def get_or_login(self):
        d = self._load_session_cache()
        sid = d.get("session", "")
        if sid:
            return sid
        return self._do_login()

    @staticmethod
    def _extract_state_text(td):
        if td is None:
            return ""
        # privilégier le texte brut présent dans la cellule, en supprimant les
        # éventuels espaces ou sauts de ligne superflus.
        pieces = [s.strip() for s in td.stripped_strings if s.strip()]
        if pieces:
            return " ".join(pieces)

        # certains états peuvent être représentés via une icône ou un attribut.
        for tag_name in ("img", "span", "i"):
            node = td.find(tag_name)
            if not node:
                continue
            for attr in ("alt", "title", "data-state"):
                val = (node.get(attr) or "").strip()
                if val:
                    return val

        # à défaut, tenter les attributs directement sur la cellule.
        for attr in ("data-state", "title", "aria-label"):
            val = (td.get(attr) or "").strip()
            if val:
                return val
        return ""

    @staticmethod
    def _map_entree(txt):
        s = (txt or "").strip().lower()
        if not s:
            return -1
        if "isol" in s: return 2
        if "inhib" in s: return 3
        if "ferm" in s: return 0
        if "ouvr" in s: return 1
        return -1

    @staticmethod
    def _map_zone_state(txt):
        s = (txt or "").strip().lower()
        if not s:
            return -1
        if "isol" in s: return 2
        if "inhib" in s: return 3
        if "activ" in s or "alarm" in s or "alarme" in s: return 1
        if "normal" in s or "repos" in s: return 0
        if "trouble" in s or "defaut" in s or "défaut" in s: return 4
        return -1

    @staticmethod
    def zone_id_from_name(name: str) -> str:
        m = re.match(r"^\s*(\d+)\b", name or "")
        if m:
            return m.group(1)
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", name or "").strip("_").lower()
        return slug or "unknown"

    @staticmethod
    def _map_area_state(txt):
        s = (txt or "").lower()
        if "mes totale" in s: return 2
        if "mes partiel" in s: return 3
        if "mhs" in s or "désarm" in s: return 1
        if "alarme" in s: return 4
        return 0

    def _secure_url(self, sid: str, page: str) -> str:
        return f"{self.host}/secure.htm?session={sid}&page={page}"

    @staticmethod
    def _collect_form_values(form):
        data = {}
        for inp in form.find_all("input"):
            name = inp.get("name")
            if not name:
                continue
            itype = (inp.get("type") or "").lower()
            if itype in ("submit", "button", "image"):
                continue
            if itype == "checkbox":
                if inp.has_attr("checked"):
                    data[name] = inp.get("value", "on")
                continue
            data[name] = inp.get("value", "")

        for sel in form.find_all("select"):
            name = sel.get("name")
            if not name:
                continue
            option = sel.find("option", selected=True) or sel.find("option")
            if option:
                data[name] = option.get("value", option.get_text(strip=True))
        return data

    def _refresh_from_form(self, sid: str, page: str, response, base_url: str):
        try:
            soup = BeautifulSoup(response.text, "html.parser")
        except Exception:
            return None

        form = soup.find("form")
        if not form:
            return None

        action = form.get("action") or base_url
        target = urljoin(base_url, action)
        if "session=" not in target:
            sep = "&" if "?" in target else "?"
            target = f"{target}{sep}session={sid}"

        data = self._collect_form_values(form)

        try:
            return self._post(target, data, referer=base_url)
        except Exception:
            return None

    def _load_page_with_refresh(self, sid: str, page: str, referer_page: str = None):
        base_url = self._secure_url(sid, page)
        referer_url = self._secure_url(sid, referer_page or page)
        response = self._get(base_url, referer=referer_url)

        new_sid = self._extract_session(getattr(response, "url", "")) or sid
        if new_sid and new_sid != sid:
            sid = new_sid
            base_url = self._secure_url(sid, page)

        refreshed = self._refresh_from_form(sid, page, response, base_url)
        if refreshed is not None:
            response = refreshed
            new_sid = (self._extract_session(getattr(response, "url", "")) or
                       self._extract_session(getattr(response, "text", "")) or sid)
            if new_sid and new_sid != sid:
                sid = new_sid

        return sid, response

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
                entree_txt = self._extract_state_text(tds[4])
                etat_txt   = self._extract_state_text(tds[5])
                if zname:
                    zones.append({
                        "zone": zname,
                        "secteur": sect,
                        "entree_txt": entree_txt,
                        "etat_txt": etat_txt,
                        "entree": self._map_entree(entree_txt),
                        "etat":   self._map_zone_state(etat_txt),
                        "id":     self.zone_id_from_name(zname),
                    })
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
                    areas.append({
                        "secteur": f"{num} {nom}",
                        "nom": nom,
                        "etat_txt": state,
                        "etat": self._map_area_state(state),
                        "sid": num,
                    })
        return areas

    def fetch_status(self):
        sid = self.get_or_login()
        if not sid:
            return {"error": "Impossible d’obtenir une session"}

        attempts = 0
        zones = []
        areas = []
        while attempts < 2:
            sid, r_z = self._load_page_with_refresh(sid, "status_zones", referer_page="spc_home")
            logging.debug("Requesting zones from: %s (len=%d)", getattr(r_z, "url", "?"), len(getattr(r_z, "text", "")))
            if self._is_login_response(getattr(r_z, "text", ""), getattr(r_z, "url", ""), True):
                logging.debug("Zones page returned login — retrying after re-login")
                new_sid = self._do_login()
                if new_sid:
                    sid = new_sid
                    attempts += 1
                    continue
                break
            zones = self.parse_zones(r_z.text)

            sid, r_a = self._load_page_with_refresh(sid, "system_summary", referer_page="spc_home")
            logging.debug("Requesting areas from: %s (len=%d)", getattr(r_a, "url", "?"), len(getattr(r_a, "text", "")))
            if self._is_login_response(getattr(r_a, "text", ""), getattr(r_a, "url", ""), True):
                logging.debug("Areas page returned login — retrying after re-login")
                new_sid = self._do_login()
                if new_sid:
                    sid = new_sid
                    attempts += 1
                    continue
                break
            areas = self.parse_areas(r_a.text)
            break

        self._save_cookies()
        self._save_session_cache(sid)
        return {"zones": zones, "areas": areas}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", default="/etc/acre_exp/config.yml")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(stream=sys.stderr, level=(logging.DEBUG if args.debug else logging.WARNING),
                        format="%(levelname)s:%(message)s")

    try:
        cfg = load_cfg(args.config)
        client = SPCClient(cfg, debug=args.debug)
        data = client.fetch_status()
        sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    except Exception as e:
        sys.stdout.write(json.dumps({"error": str(e)}) + "\n")

if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
