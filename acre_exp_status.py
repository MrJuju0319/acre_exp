#!/opt/spc-venv/bin/python3
# -*- coding: utf-8 -*-

import os, re, sys, json, time, pathlib, argparse, logging, unicodedata
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

    def _reset_session_state(self):
        try:
            self.session.cookies.clear()
        except Exception:
            pass
        self.cookiejar = MozillaCookieJar(self.cookie_file)
        self.session.cookies = self.cookiejar
        for path in (self.session_file, self.cookie_file):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
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
        if has_user and has_pass:
            return True
        return "utilisateur déconnecté" in low

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
        sid = self._do_login()
        if sid:
            return sid
        logging.warning("SPC: échec connexion initiale, purge du cache et nouvel essai")
        self._reset_session_state()
        return self._do_login()

    @staticmethod
    def _normalize_label(text: str) -> str:
        if not text:
            return ""
        normalized = unicodedata.normalize("NFKD", text)
        return "".join(ch for ch in normalized if not unicodedata.combining(ch)).lower().strip()

    @staticmethod
    def _attr_values(node):
        if not node:
            return []
        values = []
        for _, val in node.attrs.items():
            if not val:
                continue
            if isinstance(val, (list, tuple, set)):
                values.extend(str(v) for v in val if v)
            else:
                values.append(str(val))
        return values

    @staticmethod
    def _guess_zone_state_label(token: str) -> str:
        norm = SPCClient._normalize_label(token)
        if not norm:
            return ""
        if any(k in norm for k in ("ferm", "close", "ferme", "locked", "normal")):
            return "Fermée"
        if any(k in norm for k in ("ouvr", "open", "unlock")):
            return "Ouverte"
        if any(k in norm for k in ("isol", "isole", "isolee", "isolation", "separe")):
            return "Isolée"
        if any(k in norm for k in ("inhib", "bypass", "shunt")):
            return "Inhibée"
        if any(k in norm for k in ("trou", "fault", "defaut", "defa", "anomal")):
            return "Trouble"
        if any(k in norm for k in ("alarm", "alarme", "alert")):
            return "Alarme"
        if any(k in norm for k in ("vert", "green")):
            return "Fermée"
        if any(k in norm for k in ("roug", "red")):
            return "Ouverte"
        if any(k in norm for k in ("orang", "amber")):
            return "Isolée"
        if any(k in norm for k in ("bleu", "blue")):
            return "Inhibée"
        return ""

    @staticmethod
    def _guess_area_state_label(token: str) -> str:
        norm = SPCClient._normalize_label(token)
        if not norm:
            return ""
        if any(k in norm for k in ("mes partiel b", "mes partielle b", "partiel b", "partielle b", "partial b", "part b")):
            return "MES Partielle B"
        if any(k in norm for k in ("mes partiel a", "mes partielle a", "partiel a", "partielle a", "partial a", "part a")):
            return "MES Partielle A"
        if any(k in norm for k in ("mes part", "partiel", "partial", "part")):
            return "MES Partielle"
        if any(k in norm for k in ("mes totale", "total", "totale", "tot")):
            return "MES Totale"
        if any(k in norm for k in ("mhs", "desarm", "desactive", "off", "ready")):
            return "MHS"
        if any(k in norm for k in ("alarm", "alarme", "alert")):
            return "Alarme"
        if any(k in norm for k in ("trou", "fault", "defaut", "defa")):
            return "Trouble"
        return ""

    @staticmethod
    def _find_column(headers, keywords, default=None):
        for idx, label in enumerate(headers or []):
            for kw in keywords:
                if kw in label:
                    return idx
        return default

    @staticmethod
    def _extract_state_text(td):
        if td is None:
            return ""

        # 1) tenter directement le texte brut (BeautifulSoup gère les balises
        # <font> et autres en fournissant la concaténation des textes).
        try:
            text = td.get_text(" ", strip=True)
            if text:
                return text
        except Exception:
            pass

        # 2) repli équivalent mais en éliminant les chaînes vides.
        pieces = []
        try:
            pieces = [s.strip() for s in td.stripped_strings if s.strip()]
        except Exception:
            pieces = []
        if pieces:
            return " ".join(pieces)

        # 3) certains états peuvent être représentés via une icône ou un
        # attribut.
        for tag_name in ("img", "span", "i", "font"):
            node = td.find(tag_name)
            if not node:
                continue
            txt = (node.get_text(" ", strip=True) or "").strip()
            if txt:
                return txt
            for attr in ("alt", "title", "data-state"):
                val = (node.get(attr) or "").strip()
                if val:
                    return val

        # 4) à défaut, tenter les attributs directement sur la cellule.
        for attr in ("data-state", "title", "aria-label"):
            val = (td.get(attr) or "").strip()
            if val:
                return val

        for attr_val in SPCClient._attr_values(td):
            guess = SPCClient._guess_zone_state_label(attr_val)
            if guess:
                return guess
            attr_val = attr_val.strip()
            if attr_val:
                return attr_val

        for child in td.find_all(True):
            for attr_val in SPCClient._attr_values(child):
                guess = SPCClient._guess_zone_state_label(attr_val)
                if guess:
                    return guess
                attr_val = attr_val.strip()
                if attr_val:
                    return attr_val
        return ""

    @staticmethod
    def _color_hint(td):
        if td is None:
            return ""
        node = td.find("font") or td.find("span") or td
        color = (node.get("color") or "").lower()
        if color:
            return color
        style = (node.get("style") or "").lower()
        if "color" in style:
            m = re.search(r"color\s*:\s*([^;]+)", style)
            if m:
                return m.group(1).strip()
        return ""

    @classmethod
    def _infer_entree(cls, td, entree_txt: str, etat_txt: str):
        code = cls._map_entree(entree_txt)
        if code != -1:
            return code, entree_txt

        color = cls._color_hint(td)
        if color:
            if "green" in color or "#008000" in color:
                return 0, entree_txt or "Fermée"
            if "red" in color or "#ff0000" in color:
                return 1, entree_txt or "Ouverte"
            if any(c in color for c in ("orange", "#ffa500", "#ff9900")):
                return 2, entree_txt or "Isolée"
            if any(c in color for c in ("blue", "#0000ff")):
                return 3, entree_txt or "Inhibée"

        etat_code = cls._map_zone_state(etat_txt)
        if etat_code == 2:
            return 2, entree_txt or "Isolée"
        if etat_code == 3:
            return 3, entree_txt or "Inhibée"
        if etat_code == 1:
            return 1, entree_txt or "Ouverte"
        if etat_code == 0:
            return 0, entree_txt or "Fermée"
        if etat_code == 4:
            return 1, entree_txt or "Trouble"

        return -1, entree_txt

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
        if "ouvr" in s or "open" in s: return 1
        if "ferm" in s or "clos" in s or "close" in s: return 0
        if "activ" in s or "alarm" in s or "alarme" in s or "alert" in s: return 1
        if "normal" in s or "repos" in s or "rest" in s: return 0
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
    def door_id_from_name(name: str) -> str:
        m = re.match(r"^\s*(\d+)\b", name or "")
        if m:
            return m.group(1)
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", name or "").strip("_").lower()
        return slug or "door"

    @staticmethod
    def _map_area_state(txt):
        s = (txt or "").lower()
        if "partiel b" in s or "partielle b" in s or "partial b" in s or "part b" in s:
            return 3
        if "partiel a" in s or "partielle a" in s or "partial a" in s or "part a" in s:
            return 2
        if "mes partiel" in s or "mes partielle" in s or "partiel" in s or "partielle" in s or "partial" in s:
            return 2
        if "mes totale" in s or "total" in s or "totale" in s or "tot" in s:
            return 1
        if "mhs" in s or "désarm" in s or "desarm" in s or "off" in s or "ready" in s:
            return 0
        if "alarme" in s or "alarm" in s or "alert" in s:
            return 4
        if "trouble" in s or "defaut" in s or "défaut" in s or "fault" in s:
            return 4
        return -1

    @staticmethod
    def _map_door_release_state(txt, color_hint: str = ""):
        s_raw = (txt or "").strip()
        s = s_raw.lower()
        if s:
            # Détection directe sur le texte (avec et sans accents).
            if any(k in s for k in ("ouvr", "open", "liber", "release", "moment")):
                return 1
            if any(k in s for k in ("appuy", "press", "active", "actif", "pulse", "impuls")):
                return 1
            if any(k in s for k in ("ferm", "close", "repos", "relach", "relâch", "normal", "rest")):
                return 0
            if any(k in s for k in ("libre", "relache", "relâché")):
                return 0
            if s.isdigit():
                try:
                    return 1 if int(s) != 0 else 0
                except ValueError:
                    pass

        # Réessayer avec une normalisation (supprime les accents) pour les
        # chaînes contenant uniquement des caractères accentués.
        norm = SPCClient._normalize_label(s_raw)
        if norm:
            if any(k in norm for k in ("ouvr", "open", "liber", "release", "moment")):
                return 1
            if any(k in norm for k in ("appuy", "press", "active", "actif", "pulse", "impuls")):
                return 1
            if any(k in norm for k in ("ferm", "close", "repos", "relach", "normal", "rest", "libre")):
                return 0

        color = (color_hint or "").strip().lower()
        if color:
            if any(c in color for c in ("green", "lime", "#0", "vert")):
                return 1
            if any(c in color for c in ("red", "rouge", "orange", "jaune", "yellow")):
                return 1
            if any(c in color for c in ("navy", "blue", "bleu", "black", "noir", "gray", "grey", "#00f", "#000")):
                return 0

        return -1

    @staticmethod
    def _map_door_state(txt):
        s = (txt or "").lower()
        if not s:
            return -1
        if any(k in s for k in ("déverrou", "deverrou", "accès libre", "acces libre", "unlock", "libre")):
            return 1
        if any(k in s for k in ("libération", "liberation", "release")):
            return 1
        if any(k in s for k in ("ouver", "open")):
            return 1
        if any(k in s for k in ("verrouill", "lock")):
            return 0
        if "normal" in s or "ferm" in s:
            return 0
        if any(k in s for k in ("alarm", "alarme", "trouble", "defaut", "défaut", "fault", "intrus", "force")):
            return 4
        return -1

    def parse_zones(self, html):
        soup = BeautifulSoup(html, "html.parser")
        grid = soup.find("table", {"class": "gridtable"})
        zones = []
        if not grid:
            return zones
        zone_idx, sect_idx, entree_idx, etat_idx = 0, 1, 4, 5
        header_labels = []
        for tr in grid.find_all("tr"):
            header_cells = tr.find_all("th")
            if header_cells:
                header_labels = [self._normalize_label(th.get_text(" ", strip=True)) for th in header_cells]
                zone_idx = self._find_column(header_labels, ("zone", "libelle", "nom"), zone_idx)
                sect_idx = self._find_column(header_labels, ("secteur", "partition", "area"), sect_idx)
                entree_idx = self._find_column(header_labels, ("entree", "entrée", "input"), entree_idx)
                etat_idx = self._find_column(header_labels, ("etat", "état", "state", "statut"), etat_idx)
                continue

            tds = tr.find_all("td")
            if len(tds) < 2:
                continue

            zone_td = tds[zone_idx] if zone_idx is not None and zone_idx < len(tds) else tds[0]
            sect_td = tds[sect_idx] if sect_idx is not None and sect_idx < len(tds) else (tds[1] if len(tds) > 1 else tds[0])

            entree_td = None
            etat_td = None
            if len(tds) >= 6:
                entree_td = tds[entree_idx] if entree_idx is not None and entree_idx < len(tds) else tds[-2]
                etat_td = tds[etat_idx] if etat_idx is not None and etat_idx < len(tds) else tds[-1]
            elif len(tds) >= 4:
                entree_td = tds[-2]
                etat_td = tds[-1]

            zname = zone_td.get_text(strip=True)
            sect = sect_td.get_text(strip=True)
            entree_txt = self._extract_state_text(entree_td) if entree_td else ""
            etat_txt = self._extract_state_text(etat_td) if etat_td else ""
            raw_entree, raw_etat = "", ""
            if self.debug and entree_td is not None and etat_td is not None:
                try:
                    raw_entree = entree_td.decode_contents().strip()
                except Exception:
                    raw_entree = str(entree_td)
                try:
                    raw_etat = etat_td.decode_contents().strip()
                except Exception:
                    raw_etat = str(etat_td)

            entree_code, entree_txt = self._infer_entree(entree_td, entree_txt, etat_txt)
            if not etat_txt:
                etat_txt = entree_txt
            etat_code = self._map_zone_state(etat_txt)
            if etat_code == -1 and entree_code in (0, 1, 2, 3):
                etat_code = entree_code

            if zname:
                zone_data = {
                    "zone": zname,
                    "secteur": sect,
                    "entree_txt": entree_txt,
                    "etat_txt": etat_txt,
                    "entree": entree_code,
                    "etat": etat_code,
                    "id": self.zone_id_from_name(zname),
                }
                if self.debug and (entree_code == -1 or zone_data["etat"] == -1):
                    logging.debug(
                        "Zone '%s' parsed with raw_entree=%r raw_etat=%r -> entree_txt=%r etat_txt=%r code=%s etat=%s",
                        zname,
                        raw_entree,
                        raw_etat,
                        entree_txt,
                        etat_txt,
                        entree_code,
                        zone_data["etat"],
                    )
                zones.append(zone_data)
        return zones

    def parse_areas(self, html):
        soup = BeautifulSoup(html, "html.parser")
        areas = []
        for tr in soup.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 3: continue
            label = tds[1].get_text(strip=True)
            state = self._extract_state_text(tds[2])
            if not state:
                state = self._guess_area_state_label(" ".join(self._attr_values(tds[2])))
            norm_label = self._normalize_label(label)
            if label.lower().startswith("secteur"):
                m = re.match(r"^Secteur\s+(\d+)\s*:\s*(.+)$", label, re.I)
                if m:
                    num, nom = m.groups()
                    area_state = self._map_area_state(state)
                    if self.debug and (not state or (area_state == 0 and state.strip() == "")):
                        try:
                            raw_state = tds[2].decode_contents().strip()
                        except Exception:
                            raw_state = str(tds[2])
                        logging.debug(
                            "Area '%s' parsed with raw_state=%r -> etat_txt=%r etat=%s",
                            nom,
                            raw_state,
                            state,
                            area_state,
                        )
                    areas.append({
                        "secteur": f"{num} {nom}",
                        "nom": nom,
                        "etat_txt": state,
                        "etat": area_state,
                        "sid": num,
                    })
            elif norm_label and (
                "tous secteurs" in norm_label
                or "all areas" in norm_label
                or "toutes les zones" in norm_label
                or "all sectors" in norm_label
            ):
                area_state = self._map_area_state(state)
                areas.append({
                    "secteur": label,
                    "nom": label,
                    "etat_txt": state,
                    "etat": area_state,
                    "sid": "0",
                })
        return areas

    def parse_doors(self, html):
        soup = BeautifulSoup(html, "html.parser")
        grid = soup.find("table", {"class": "gridtable"})
        doors = []
        if not grid:
            return doors

        door_idx, zone_idx, sect_idx = 0, 1, 2
        drs_idx, state_idx = 4, 5
        header_labels = []

        for tr in grid.find_all("tr"):
            header_cells = tr.find_all("th")
            if header_cells:
                header_labels = [self._normalize_label(th.get_text(" ", strip=True)) for th in header_cells]
                door_idx = self._find_column(header_labels, ("porte", "door"), door_idx)
                zone_idx = self._find_column(header_labels, ("zone",), zone_idx)
                sect_idx = self._find_column(header_labels, ("secteur", "partition", "area"), sect_idx)
                drs_idx = self._find_column(header_labels, ("drs", "liber", "release"), drs_idx)
                state_idx = self._find_column(header_labels, ("etat", "état", "state", "statut"), state_idx)
                continue

            tds = tr.find_all("td")
            if len(tds) < 2:
                continue

            door_td = tds[door_idx] if door_idx is not None and door_idx < len(tds) else tds[0]
            zone_td = tds[zone_idx] if zone_idx is not None and zone_idx < len(tds) else (tds[1] if len(tds) > 1 else tds[0])
            sect_td = tds[sect_idx] if sect_idx is not None and sect_idx < len(tds) else (tds[2] if len(tds) > 2 else tds[-1])
            drs_td = tds[drs_idx] if drs_idx is not None and drs_idx < len(tds) else None
            state_td = tds[state_idx] if state_idx is not None and state_idx < len(tds) else (tds[-2] if len(tds) >= 2 else None)

            door_lbl = door_td.get_text(" ", strip=True)
            zone_lbl = zone_td.get_text(" ", strip=True)
            sect_lbl = sect_td.get_text(" ", strip=True)
            drs_txt = self._extract_state_text(drs_td) if drs_td else ""
            drs_color = self._color_hint(drs_td) if drs_td else ""
            state_txt = self._extract_state_text(state_td) if state_td else ""

            door_data = {
                "door": door_lbl,
                "zone": zone_lbl,
                "secteur": sect_lbl,
                "drs_txt": drs_txt,
                "drs_color": drs_color,
                "etat_txt": state_txt,
                "drs": self._map_door_release_state(drs_txt, drs_color),
                "etat": self._map_door_state(state_txt),
                "id": self.door_id_from_name(door_lbl),
            }
            doors.append(door_data)
        return doors

    @staticmethod
    def _map_output_state(state_token: str, icon_src: str) -> int:
        norm = SPCClient._normalize_label(state_token)
        icon = (icon_src or "").strip().lower()
        if icon:
            if "_on" in icon or "-on" in icon or "output_on" in icon:
                return 1
            if "_off" in icon or "-off" in icon or "output_off" in icon:
                return 0
        if norm:
            if any(k in norm for k in ("on", "marche", "active", "actif", "ouvert")):
                return 1
            if any(k in norm for k in ("off", "arret", "arr", "stop", "inactive", "ferme")):
                return 0
        return -1

    def parse_outputs(self, html):
        soup = BeautifulSoup(html, "html.parser")
        outputs = []

        table = soup.find("table", {"class": "gridtable"})
        if not table:
            return outputs

        rows = table.find_all("tr")
        if not rows or len(rows) <= 1:
            return outputs

        for row in rows[1:]:
            cells = row.find_all("td")
            if len(cells) < 3:
                continue

            raw_id = cells[0].get_text(" ", strip=True)
            if not raw_id:
                continue
            m = re.search(r"\d+", raw_id)
            if m:
                oid = str(int(m.group(0)))
            else:
                oid = raw_id.strip()

            label_cell = cells[1]
            label_text = label_cell.get_text(" ", strip=True)
            state_token = ""
            name = label_text
            if ":" in label_text:
                parts = label_text.split(":", 1)
                state_token = parts[0].strip()
                name = parts[1].strip()
            icon = label_cell.find("img")
            icon_src = icon.get("src", "") if icon else ""
            state = self._map_output_state(state_token, icon_src)

            action_cell = cells[2]
            button_on = None
            button_off = None
            for button in action_cell.find_all("input"):
                btn_name = str(button.get("name", "")).strip()
                btn_value = str(button.get("value", "")).strip()
                if not btn_name:
                    continue
                btn_info = {"name": btn_name, "value": btn_value}
                low_name = btn_name.lower()
                if low_name.startswith("on") and button_on is None:
                    button_on = btn_info
                elif low_name.startswith("off") and button_off is None:
                    button_off = btn_info

            outputs.append({
                "id": oid,
                "interaction": raw_id,
                "name": name,
                "state": state,
                "state_txt": state_token or label_text,
                "icon": icon_src,
                "button_on": button_on or {},
                "button_off": button_off or {},
            })

        return outputs

    @staticmethod
    def _slug(text: str) -> str:
        norm = SPCClient._normalize_label(text)
        if not norm:
            return ""
        slug = re.sub(r"[^a-z0-9]+", "_", norm)
        slug = re.sub(r"_+", "_", slug).strip("_")
        return slug

    def parse_controller(self, html):
        soup = BeautifulSoup(html, "html.parser")
        sections = []

        for border in soup.select("td.section_border"):
            title = border.get_text(" ", strip=True)
            if not title:
                continue

            section_slug = self._slug(title)
            if not section_slug:
                continue

            data_table = None
            current_tr = border.find_parent("tr")
            for _ in range(6):
                if current_tr is None:
                    break
                current_tr = current_tr.find_next_sibling("tr")
                if current_tr is None:
                    break
                candidate = current_tr.find("table")
                if candidate:
                    data_table = candidate
                    break

            if data_table is None:
                continue

            values = {}
            labels = {}

            for row in data_table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) < 2:
                    continue
                key_raw = cells[0].get_text(" ", strip=True)
                key = key_raw.rstrip(":")
                if not key:
                    continue

                val_parts = []
                for cell in cells[1:]:
                    txt = cell.get_text(" ", strip=True)
                    if txt:
                        val_parts.append(txt)
                value = " ".join(val_parts).strip()
                if not value:
                    continue

                key_slug = self._slug(key)
                if not key_slug:
                    continue

                values[key_slug] = value
                labels[key_slug] = key_raw if key_raw else key

            if not values:
                continue

            sections.append({
                "slug": section_slug,
                "title": title,
                "values": values,
                "labels": labels,
            })

        return sections

    def fetch_status(self):
        sid = self.get_or_login()
        if not sid:
            logging.error("SPC: impossible d’obtenir une session après tentatives de relogin")
            return {"error": "Impossible d’obtenir une session"}

        def _fetch(page: str, referer_page: str = "spc_home"):
            url = f"{self.host}/secure.htm?session={sid}&page={page}"
            referer = f"{self.host}/secure.htm?session={sid}&page={referer_page}"
            r = self._get(url, referer=referer)
            return sid, r

        sid, r_z = _fetch("status_zones", referer_page="status_zones")
        logging.debug("Requesting zones from: %s (len=%d)", r_z.url, len(r_z.text))
        zones = self.parse_zones(r_z.text)
        if len(zones) == 0 and self._is_login_response(r_z.text, getattr(r_z, "url", ""), True):
            logging.debug("Zones parse empty + looks like login — re-login once")
            self._reset_session_state()
            new_sid = self._do_login()
            if new_sid:
                sid = new_sid
                r_z = self._get(f"{self.host}/secure.htm?session={sid}&page=status_zones",
                                 referer=f"{self.host}/secure.htm?session={sid}&page=status_zones")
                zones = self.parse_zones(r_z.text)
                logging.debug("zones retry length: %d — parsed: %d", len(r_z.text), len(zones))

        sid, r_a = _fetch("system_summary", referer_page="controller_status")
        logging.debug("Requesting areas from: %s (len=%d)", r_a.url, len(r_a.text))
        areas = self.parse_areas(r_a.text)
        if len(areas) == 0 and self._is_login_response(r_a.text, getattr(r_a, "url", ""), True):
            logging.debug("Areas parse empty + looks like login — re-login once")
            self._reset_session_state()
            new_sid = self._do_login()
            if new_sid:
                sid = new_sid
                r_a = self._get(f"{self.host}/secure.htm?session={sid}&page=system_summary",
                                 referer=f"{self.host}/secure.htm?session={sid}&page=controller_status")
                areas = self.parse_areas(r_a.text)
                logging.debug("areas retry length: %d — parsed: %d", len(r_a.text), len(areas))

        sid, r_c = _fetch("controller_status", referer_page="controller_status")
        logging.debug("Requesting controller status from: %s (len=%d)", r_c.url, len(r_c.text))
        controller = self.parse_controller(r_c.text)
        if len(controller) == 0 and self._is_login_response(r_c.text, getattr(r_c, "url", ""), True):
            logging.debug("Controller parse empty + looks like login — re-login once")
            self._reset_session_state()
            new_sid = self._do_login()
            if new_sid:
                sid = new_sid
                r_c = self._get(f"{self.host}/secure.htm?session={sid}&page=controller_status",
                                referer=f"{self.host}/secure.htm?session={sid}&page=controller_status")
                controller = self.parse_controller(r_c.text)
                logging.debug("controller retry length: %d — parsed: %d", len(r_c.text), len(controller))

        sid, r_d = _fetch("door_status", referer_page="controller_status")
        logging.debug("Requesting doors from: %s (len=%d)", r_d.url, len(r_d.text))
        doors = self.parse_doors(r_d.text)
        if len(doors) == 0 and self._is_login_response(r_d.text, getattr(r_d, "url", ""), True):
            logging.debug("Doors parse empty + looks like login — re-login once")
            self._reset_session_state()
            new_sid = self._do_login()
            if new_sid:
                sid = new_sid
                r_d = self._get(f"{self.host}/secure.htm?session={sid}&page=door_status",
                                referer=f"{self.host}/secure.htm?session={sid}&page=controller_status")
                doors = self.parse_doors(r_d.text)
                logging.debug("doors retry length: %d — parsed: %d", len(r_d.text), len(doors))

        sid, r_o = _fetch("status_mg", referer_page="status_outputs_menu")
        logging.debug("Requesting outputs from: %s (len=%d)", r_o.url, len(r_o.text))
        outputs = self.parse_outputs(r_o.text)
        if len(outputs) == 0 and self._is_login_response(r_o.text, getattr(r_o, "url", ""), True):
            logging.debug("Outputs parse empty + looks like login — re-login once")
            self._reset_session_state()
            new_sid = self._do_login()
            if new_sid:
                sid = new_sid
                r_o = self._get(f"{self.host}/secure.htm?session={sid}&page=status_mg",
                                 referer=f"{self.host}/secure.htm?session={sid}&page=status_outputs_menu")
                outputs = self.parse_outputs(r_o.text)
                logging.debug("outputs retry length: %d — parsed: %d", len(r_o.text), len(outputs))

        self._save_cookies()
        self._save_session_cache(sid)
        return {"zones": zones, "areas": areas, "doors": doors, "outputs": outputs, "controller": controller}

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
