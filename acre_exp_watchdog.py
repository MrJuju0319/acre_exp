#!/opt/spc-venv/bin/python3
# -*- coding: utf-8 -*-

import os, re, sys, time, argparse, signal, logging, warnings
import queue
import yaml
import requests
from bs4 import BeautifulSoup
from http.cookiejar import MozillaCookieJar
from typing import Dict, Set

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
        has_pass = ('name="password"' in low) or ('id="password"' in low) or ("id='password'" in low)
        if has_user and has_pass:
            return True
        return "utilisateur déconnecté" in low

    @staticmethod
    def _normalize_state_text(txt: str) -> str:
        return (txt or "").strip().lower()

    @classmethod
    def zone_bin(cls, zone) -> int:
        if isinstance(zone, dict):
            etat = zone.get("etat")
            if isinstance(etat, int):
                if etat == 1:
                    return 1
                if etat in (0, 2, 3):
                    return 0
                if etat >= 4:
                    return 1
            etat_txt = zone.get("etat_txt")
        else:
            etat_txt = zone

        s = cls._normalize_state_text(etat_txt)
        if any(x in s for x in ("activ", "alarm", "alarme", "trouble", "défaut", "defaut")):
            return 1
        if any(x in s for x in ("normal", "repos", "isol", "inhib")):
            return 0
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
        if not s:
            return -1
        if "partiel b" in s or "partielle b" in s or "partial b" in s or "part b" in s:
            return 3
        if "partiel a" in s or "partielle a" in s or "partial a" in s or "part a" in s:
            return 2
        if "mes partiel" in s or "mes partielle" in s or "partiel" in s or "partielle" in s or "partial" in s:
            return 2
        if "mes totale" in s or "total" in s or "totale" in s or "tot" in s:
            return 1
        if "alarme" in s:
            return 4
        if "mhs" in s or "désarm" in s or "desarm" in s or "desactiv" in s or "desactive" in s:
            return 0
        return -1

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
            if isinstance(entree, int) and entree in (0, 1, 2, 3):
                return entree
            entree_txt = zone.get("entree_txt")
            etat_val = zone.get("etat") if isinstance(zone.get("etat"), int) else None
        else:
            entree_txt = zone
            etat_val = None

        s = SPCClient._normalize_state_text(entree_txt)
        if "isol" in s:
            return 2
        if "inhib" in s:
            return 3
        if "ferm" in s:
            return 0
        if "ouvr" in s or "alarm" in s:
            return 1
        if etat_val is not None:
            if etat_val == 2:
                return 2
            if etat_val == 3:
                return 3
            if etat_val == 1:
                return 1
            if etat_val == 0:
                return 0
            if etat_val >= 4:
                return 1
        return -1

    @classmethod
    def door_id(cls, door) -> str:
        if isinstance(door, dict):
            did = door.get("id") or door.get("door")
            if did:
                return str(did).strip()
        return cls.door_id_from_name(door)

    @staticmethod
    def door_name(door) -> str:
        if isinstance(door, dict):
            return str(door.get("door") or door.get("name") or "").strip()
        return str(door or "")

    @staticmethod
    def door_zone(door) -> str:
        if isinstance(door, dict):
            return str(door.get("zone") or "").strip()
        return ""

    @staticmethod
    def door_sector(door) -> str:
        if isinstance(door, dict):
            return str(door.get("secteur") or door.get("sector") or "").strip()
        return ""

    @classmethod
    def door_drs(cls, door) -> int:
        if isinstance(door, dict):
            val = door.get("drs")
            if isinstance(val, int) and val >= 0:
                return val
            txt = door.get("drs_txt")
        else:
            txt = None
        if not txt:
            return -1
        return StatusSPCClient._map_door_release_state(txt)

    @classmethod
    def door_state(cls, door) -> int:
        if isinstance(door, dict):
            state = door.get("etat")
            if isinstance(state, int) and state >= 0:
                return state
            txt = door.get("etat_txt")
        else:
            txt = door
        if not txt:
            return -1
        return StatusSPCClient._map_door_state(txt)

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
            return {"zones": [], "areas": [], "doors": [], "controller": []}
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

        doors = data.get("doors", [])
        for d in doors:
            if isinstance(d, dict):
                if not d.get("id"):
                    d["id"] = self.door_id(d)

        controller = data.get("controller", [])

        return {"zones": zones, "areas": areas, "doors": doors, "controller": controller}

    @staticmethod
    def _normalize_command(cmd: str) -> str:
        return StatusSPCClient._normalize_label(cmd)

    def _resolve_area_suffix(self, area_id: str):
        if area_id is None:
            raise ValueError("identifiant de secteur manquant")

        raw = str(area_id).strip()
        if not raw:
            raise ValueError("identifiant de secteur vide")

        norm = self._normalize_command(raw)
        if raw == "0" or norm in ("0", "all", "tous", "all_secteurs", "tous_secteurs", "toussecteurs", "allareas"):
            return "0", "all_areas", "Tous Secteurs"

        if norm.startswith("area") and norm[4:].isdigit():
            raw = norm[4:]

        if raw.isdigit():
            num = str(int(raw))
            return num, f"area{num}", ""

        # Essayer de retrouver par nom de secteur connu
        try:
            data = self.fetch()
        except Exception:
            data = {"areas": []}
        for area in data.get("areas", []):
            sid = str(area.get("sid") or "").strip()
            label = str(area.get("nom") or area.get("secteur") or "").strip()
            if sid:
                if sid == raw:
                    return sid, f"area{sid}", label
                if sid.isdigit() and self._normalize_command(sid) == norm:
                    num = str(int(sid))
                    return num, f"area{num}", label
            if label and self._normalize_command(label) == norm:
                sid = str(area.get("sid") or self.area_id(area) or "").strip()
                if sid:
                    num = sid if not sid.isdigit() else str(int(sid))
                    return num, ("all_areas" if num == "0" else f"area{num}"), label

        raise ValueError(f"secteur '{raw}' introuvable")

    def _command_to_button(self, area_suffix: str, command: str):
        norm = self._normalize_command(command)
        if not norm:
            raise ValueError("commande vide")

        mapping = {
            "fullset": {
                "mode": 1,
                "tokens": {"1", "mes", "mes totale", "mes total", "total", "totale", "full", "fullset", "arm", "arme", "armer", "set", "tot"},
            },
            "partset_a": {
                "mode": 2,
                "tokens": {"2", "part", "partial", "parta", "part a", "partiel", "partiel a", "partset", "partset a", "partseta", "mes partielle", "mes partiel", "mes partielle a", "mes partiel a", "partielle a", "partial a"},
            },
            "partset_b": {
                "mode": 3,
                "tokens": {"3", "partb", "part b", "partiel b", "partset b", "partsetb", "mes partielle b", "mes partiel b", "partielle b", "partial b"},
            },
            "unset": {
                "mode": 0,
                "tokens": {"0", "mhs", "unset", "off", "stop", "arret", "arreter", "desarm", "desarme", "desarmer", "desactiv", "desactive", "desactivation", "disarm"},
            },
        }

        for action, info in mapping.items():
            if norm in info["tokens"]:
                button = f"{action}_{area_suffix}"
                return button, info["mode"]

        raise ValueError(f"commande '{command}' inconnue")

    def send_area_command(self, area_id: str, command: str):
        area_num, suffix, area_label = self._resolve_area_suffix(area_id)
        button, mode = self._command_to_button(suffix, command)

        sid = self.get_or_login()
        if not sid:
            raise RuntimeError("Impossible d’obtenir une session")

        def _post_action(current_sid):
            url = f"{self.host}/secure.htm?session={current_sid}&page=system_summary&action=update"
            referer = f"{self.host}/secure.htm?session={current_sid}&page=system_summary"
            data = {button: "1"}
            if suffix.startswith("area"):
                num = suffix[4:]
                if num:
                    data[f"area_{num}_expanded"] = "1"
            return self._post(url, data=data, referer=referer)

        try:
            r = _post_action(sid)
        except Exception as exc:
            logging.debug("POST commande secteur échoué, tentative relogin", exc_info=True)
            sid = self._do_login()
            if not sid:
                raise RuntimeError(f"Impossible d’envoyer la commande ({exc})")
            r = _post_action(sid)

        if self._is_login_response(getattr(r, "text", ""), getattr(r, "url", ""), True):
            sid = self._do_login()
            if not sid:
                raise RuntimeError("Session expirée, relogin impossible")
            r = _post_action(sid)
            if self._is_login_response(getattr(r, "text", ""), getattr(r, "url", ""), True):
                raise RuntimeError("Commande refusée (retour page login)")

        label = area_label or area_num or suffix
        return {"ok": True, "area_id": area_num or "0", "mode": mode, "button": button, "label": label}

    def _resolve_door_number(self, door_id: str):
        if door_id is None:
            raise ValueError("identifiant de porte manquant")

        raw = str(door_id).strip()
        if not raw:
            raise ValueError("identifiant de porte vide")

        norm = self._normalize_command(raw)
        if raw.isdigit():
            num = str(int(raw))
            return num, f"Porte {num}"

        try:
            data = self.fetch()
        except Exception:
            data = {"doors": []}

        for door in data.get("doors", []):
            did = str(door.get("id") or door.get("door") or "").strip()
            name = str(door.get("door") or door.get("name") or "").strip()
            zone_lbl = str(door.get("zone") or "").strip()
            secteur_lbl = str(door.get("secteur") or door.get("sector") or "").strip()
            label = zone_lbl or secteur_lbl or name or did

            candidates = [did, name, zone_lbl, secteur_lbl]
            for candidate in candidates:
                if not candidate:
                    continue
                cand_norm = self._normalize_command(candidate)
                if cand_norm == norm or candidate == raw:
                    if did:
                        num = did if not did.isdigit() else str(int(did))
                        return num, label or f"Porte {num}"
            if did and self._normalize_command(did) == norm:
                num = did if not did.isdigit() else str(int(did))
                return num, label or f"Porte {num}"

        raise ValueError(f"porte '{raw}' introuvable")

    def _door_command_to_button(self, door_num: str, command: str):
        norm = self._normalize_command(command)
        if not norm:
            raise ValueError("commande vide")

        mapping = {
            "normal": {
                "tokens": {"normal", "reset", "std", "standard"},
                "value": "Normal",
                "label": "Normal",
            },
            "lock": {
                "tokens": {"lock", "verrou", "verrouille", "verrouiller", "fermer", "ferme", "close"},
                "value": "Verrouiller",
                "label": "Verrouiller",
            },
            "unlock": {
                "tokens": {"unlock", "deverrou", "deverrouille", "deverrouiller", "ouvrir", "open", "liberer", "liberation", "acces libre", "access libre"},
                "value": "Déverrouiller",
                "label": "Déverrouiller",
            },
            "pulse": {
                "tokens": {"pulse", "impulsion", "impulse", "impultion", "moment", "toggle"},
                "value": "Impulsion",
                "label": "Impulsion",
            },
        }

        for action, info in mapping.items():
            if norm in info["tokens"]:
                button = f"{action}{door_num}"
                return button, info["value"], action, info["label"]

        raise ValueError(f"commande '{command}' inconnue")

    def send_door_command(self, door_id: str, command: str):
        door_num, door_label = self._resolve_door_number(door_id)
        button, value, action, action_label = self._door_command_to_button(door_num, command)

        sid = self.get_or_login()
        if not sid:
            raise RuntimeError("Impossible d’obtenir une session")

        def _post_action(current_sid):
            url = (
                f"{self.host}/secure.htm?session={current_sid}&page=door_status"
                f"&action=update&door={door_num}"
            )
            referer = f"{self.host}/secure.htm?session={current_sid}&page=door_status"
            data = {button: value}
            return self._post(url, data=data, referer=referer)

        try:
            r = _post_action(sid)
        except Exception as exc:
            logging.debug("POST commande porte échoué, tentative relogin", exc_info=True)
            sid = self._do_login()
            if not sid:
                raise RuntimeError(f"Impossible d’envoyer la commande porte ({exc})")
            r = _post_action(sid)

        if self._is_login_response(getattr(r, "text", ""), getattr(r, "url", ""), True):
            sid = self._do_login()
            if not sid:
                raise RuntimeError("Session expirée, relogin impossible")
            r = _post_action(sid)
            if self._is_login_response(getattr(r, "text", ""), getattr(r, "url", ""), True):
                raise RuntimeError("Commande porte refusée (retour page login)")

        label = door_label or door_num
        return {
            "ok": True,
            "door_id": door_num,
            "button": button,
            "action": action,
            "action_label": action_label,
            "label": label,
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
        proto = str(m.get("protocol", "v311")).lower()
        self.protocol = mqtt.MQTTv5 if proto in ("v5", "mqttv5", "5") else mqtt.MQTTv311
        self.base_parts = [p for p in self.base.split("/") if p]
        self.command_queue: "queue.Queue" = queue.Queue()
        self.command_topics = [
            self._topic("secteurs/+/set") or "secteurs/+/set",
            self._topic("doors/+/set") or "doors/+/set",
        ]

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
            if rc == 0:
                for topic_name in self.command_topics:
                    try:
                        client.subscribe(topic_name, qos=self.qos)
                        print(f"[MQTT] Souscription commandes: {topic_name}")
                    except Exception as exc:
                        print(f"[MQTT] Souscription impossible ({topic_name}): {exc}")

        def _on_disconnect(client, userdata, reason_code=0, *rest):
            rc = _normalize_reason_code(reason_code)
            self._unset_conn(rc)

        if self.user:
            self.client.username_pw_set(self.user, self.pwd)

        self.connected = False
        self.client.on_connect = _on_connect
        self.client.on_disconnect = _on_disconnect
        self.client.on_message = self._on_message

    def _topic(self, suffix: str) -> str:
        suffix = (suffix or "").strip("/")
        if not self.base:
            return suffix
        if not suffix:
            return self.base
        return f"{self.base}/{suffix}"

    def _on_message(self, client, userdata, msg):
        topic = msg.topic if isinstance(msg.topic, str) else msg.topic.decode("utf-8", "ignore")
        if not topic:
            return
        parts = topic.split("/")
        if len(parts) < len(self.base_parts) + 3:
            return
        if parts[: len(self.base_parts)] != self.base_parts:
            return
        sub = parts[len(self.base_parts):]
        if len(sub) != 3 or sub[2] != "set":
            return
        category = sub[0]
        if category == "secteurs":
            cmd_type = "area"
        elif category == "doors":
            cmd_type = "door"
        else:
            return
        try:
            payload = msg.payload.decode("utf-8", errors="ignore").strip()
        except Exception:
            payload = ""
        target = sub[1]
        if not payload:
            print(f"[MQTT] Commande ignorée (payload vide) pour {category[:-1]} {target}")
            self.pub(f"{category}/{target}/command_result", "error:payload-empty")
            return
        self.command_queue.put((cmd_type, target, payload, topic))
        print(f"[MQTT] Commande reçue: {topic} → '{payload}'")

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
                    if self.connected:
                        return
                    time.sleep(0.2)
            except Exception as e:
                print(f"[MQTT] Erreur: {e}")
            time.sleep(2)

    def pub(self, topic, payload):
        full = self._topic(topic)
        try:
            self.client.publish(full, payload=str(payload), qos=self.qos, retain=self.retain)
        except Exception as e:
            print(f"[MQTT] publish ERR {full}: {e}")

    def next_command(self):
        try:
            return self.command_queue.get_nowait()
        except queue.Empty:
            return None

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
    try:
        interval = int(wd.get("refresh_interval", 2))
    except Exception:
        interval = 2
    if interval < 1:
        interval = 1

    try:
        controller_interval = int(wd.get("controller_refresh_interval", 60))
    except Exception:
        controller_interval = 60
    if controller_interval < 1:
        controller_interval = 1

    log_changes = bool(wd.get("log_changes", True))

    spc = SPCClient(cfg, debug=args.debug)
    mq  = MQ(cfg)

    print(
        "[SPC→MQTT] Démarrage (refresh={zones}s, controller_refresh={ctrl}s) — Broker {host}:{port}".format(
            zones=interval,
            ctrl=controller_interval,
            host=mq.host,
            port=mq.port,
        )
    )
    mq.connect()

    last_z: Dict[str, int] = {}
    last_z_in: Dict[str, int] = {}
    last_a: Dict[str, int] = {}
    last_door_state: Dict[str, int] = {}
    last_door_drs: Dict[str, int] = {}
    door_names: Dict[str, str] = {}
    last_controller: Dict[str, str] = {}
    cleared_legacy_controller_topics: Set[str] = set()
    area_names: Dict[str, str] = {"0": "Tous Secteurs"}

    controller_topic_map = {
        "systeme": "système",
        "alimentation": "alimentation",
        "ethernet": "ethernet",
        "modem_1": "modem1",
        "modem_2": "modem2",
        "x_bus": "X-BUS",
    }

    def _controller_topic(slug: str) -> str:
        slug = (slug or "").strip()
        if not slug:
            return ""
        mapped = controller_topic_map.get(slug)
        if mapped:
            return mapped
        compact = slug.replace("_", "")
        return compact

    def _controller_label_topic(label: str, fallback: str) -> str:
        label = (label or "").strip()
        if label:
            trimmed = label.rstrip(":：").rstrip()
            if trimmed:
                label = trimmed
        if not label:
            return (fallback or "").strip()
        return label

    def publish_controller_sections(sections, tick_label=None, log_section=False):
        changed = False
        for section in sections or []:
            if not isinstance(section, dict):
                continue
            slug = section.get("slug", "")
            topic_suffix = _controller_topic(slug)
            if not topic_suffix:
                continue
            values = section.get("values")
            if not isinstance(values, dict) or not values:
                continue
            labels = section.get("labels")
            ordered_keys = sorted(values)
            title = section.get("title") or topic_suffix
            for key in ordered_keys:
                value = values.get(key)
                if value is None:
                    continue
                label = ""
                if isinstance(labels, dict):
                    label = labels.get(key, "")
                label = label or key
                topic_label = _controller_label_topic(label, key)
                topic = f"etat/{topic_suffix}/{topic_label}"
                legacy_topic = None
                if topic_label != label:
                    legacy_topic = f"etat/{topic_suffix}/{label}"
                payload = str(value)
                old_payload = last_controller.get(topic)
                if old_payload == payload:
                    continue
                last_controller[topic] = payload
                mq.pub(topic, payload)
                if legacy_topic and legacy_topic not in cleared_legacy_controller_topics:
                    mq.pub(legacy_topic, "")
                    last_controller.pop(legacy_topic, None)
                    cleared_legacy_controller_topics.add(legacy_topic)
                changed = True
                if log_section and tick_label:
                    print(f"[{tick_label}] 🧩 {title} · {label} = {payload}")
        return changed

    def record_area_names(areas):
        for area in areas:
            sid = SPCClient.area_id(area)
            if not sid:
                continue
            label = area.get("nom") or area.get("secteur") or sid
            area_names[str(sid)] = label

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
        if entree in (0, 1, 2, 3):
            last_z_in[zid] = entree
            mq.pub(f"zones/{zid}/entree", entree)

    record_area_names(snap.get("areas", []))
    for a in snap["areas"]:
        sid = SPCClient.area_id(a)
        if not sid:
            continue
        mq.pub(f"secteurs/{sid}/name", a.get("nom", ""))
        s = SPCClient.area_num(a)
        if s >= 0:
            last_a[sid] = s
            mq.pub(f"secteurs/{sid}/state", s)

    for d in snap.get("doors", []):
        did = SPCClient.door_id(d)
        dname = SPCClient.door_name(d)
        if not did or not dname:
            continue
        mq.pub(f"doors/{did}/name", dname)
        zone_lbl = SPCClient.door_zone(d)
        if zone_lbl:
            mq.pub(f"doors/{did}/zone", zone_lbl)
        secteur_lbl = SPCClient.door_sector(d)
        if secteur_lbl:
            mq.pub(f"doors/{did}/secteur", secteur_lbl)
        door_names[did] = zone_lbl or secteur_lbl or dname
        state = SPCClient.door_state(d)
        if state >= 0:
            last_door_state[did] = state
            mq.pub(f"doors/{did}/state", state)
        drs = SPCClient.door_drs(d)
        if drs >= 0:
            last_door_drs[did] = drs
            mq.pub(f"doors/{did}/drs", drs)

    publish_controller_sections(snap.get("controller", []))
    next_controller_publish = time.monotonic() + controller_interval

    print("[SPC→MQTT] État initial publié.")

    command_state_labels = {
        0: "MHS",
        1: "MES totale",
        2: "MES partielle A",
        3: "MES partielle B",
        4: "Alarme",
    }

    def _normalize_area_token(token: str) -> str:
        tok = (token or "").strip()
        if not tok:
            return ""
        low = tok.lower()
        if low in ("all", "tous", "all_areas", "toussecteurs", "tous_secteurs", "*"):
            return "0"
        if low.startswith("area") and low[4:].isdigit():
            return str(int(low[4:]))
        if tok.isdigit():
            return str(int(tok))
        return tok

    def process_commands() -> bool:
        handled = False
        while True:
            item = mq.next_command()
            if item is None:
                break
            handled = True
            cmd_type, target, payload, _topic = item
            tick_cmd = time.strftime("%H:%M:%S")

            if cmd_type == "area":
                area_token = target
                ack_id = _normalize_area_token(area_token)
                ack_id = ack_id or "unknown"
                label = area_names.get(ack_id, ack_id)
                try:
                    result = spc.send_area_command(area_token, payload)
                    ack_id = str(result.get("area_id") or ack_id or "0")
                    label = result.get("label") or area_names.get(ack_id, ack_id)
                    area_names[ack_id] = label
                    mode = int(result.get("mode", -1))
                    status_payload = f"ok:{mode}" if mode >= 0 else "ok"
                    mq.pub(f"secteurs/{ack_id}/command_result", status_payload)
                    if log_changes:
                        mode_label = command_state_labels.get(mode, str(mode))
                        print(f"[{tick_cmd}] ✅ Commande secteur '{label}' → {mode_label}")
                except Exception as err:
                    mq.pub(f"secteurs/{ack_id}/command_result", f"error:{err}")
                    if log_changes:
                        print(f"[{tick_cmd}] ❌ Commande secteur '{label}' échouée: {err}")
                continue

            if cmd_type == "door":
                door_token = target
                ack_id = str(door_token).strip()
                if not ack_id:
                    ack_id = "unknown"
                label = door_names.get(ack_id, ack_id)
                try:
                    result = spc.send_door_command(door_token, payload)
                    ack_id = str(result.get("door_id") or ack_id or "unknown")
                    label = result.get("label") or door_names.get(ack_id, ack_id)
                    if ack_id:
                        door_names[ack_id] = label
                    action_code = str(result.get("action") or "").strip()
                    action_label = result.get("action_label") or action_code or payload
                    status_payload = f"ok:{action_code}" if action_code else "ok"
                    ack_topic_id = ack_id or "unknown"
                    mq.pub(f"doors/{ack_topic_id}/command_result", status_payload)
                    if log_changes:
                        print(f"[{tick_cmd}] ✅ Commande porte '{label}' → {action_label}")
                except Exception as err:
                    ack_topic_id = ack_id or "unknown"
                    mq.pub(f"doors/{ack_topic_id}/command_result", f"error:{err}")
                    if log_changes:
                        print(f"[{tick_cmd}] ❌ Commande porte '{label}' échouée: {err}")
                continue

            if log_changes:
                print(f"[{tick_cmd}] ⚠️ Commande inconnue ignorée: {item}")
        return handled

    while running:
        commands_before = process_commands()
        tick = time.strftime("%H:%M:%S")
        try:
            data = spc.fetch()
        except Exception as e:
            print(f"[SPC] fetch ERR: {e}")
            time.sleep(interval)
            continue

        record_area_names(data.get("areas", []))

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
            if entree in (0, 1, 2, 3):
                old_in = last_z_in.get(zid)
                if old_in is None or entree != old_in:
                    mq.pub(f"zones/{zid}/entree", entree)
                    last_z_in[zid] = entree
                    if log_changes:
                        state_txt = {
                            0: "fermée",
                            1: "ouverte",
                            2: "isolée",
                            3: "inhibée",
                        }.get(entree, str(entree))
                        print(f"[{tick}] 🟢 Entrée zone '{zname}' → {state_txt}")

        for a in data["areas"]:
            sid = SPCClient.area_id(a)
            if not sid:
                continue
            s = SPCClient.area_num(a)
            if s < 0:
                continue
            old = last_a.get(sid)
            if old is None or s != old:
                mq.pub(f"secteurs/{sid}/state", s)
                last_a[sid] = s
                if log_changes:
                    state_txt = {
                        0: "MHS",
                        1: "MES",
                        2: "MES partiel A",
                        3: "MES partiel B",
                        4: "Alarme",
                    }.get(s, str(s))
                    print(f"[{tick}] 🔵 Secteur '{a.get('nom', sid)}' → {state_txt}")

        now_monotonic = time.monotonic()
        if now_monotonic >= next_controller_publish:
            publish_controller_sections(data.get("controller", []), tick, log_changes)
            next_controller_publish = now_monotonic + controller_interval

        commands_after = process_commands()

        for d in data.get("doors", []):
            did = SPCClient.door_id(d)
            dname = SPCClient.door_name(d)
            if not did or not dname:
                continue
            zone_lbl = SPCClient.door_zone(d)
            secteur_lbl = SPCClient.door_sector(d)
            door_names[did] = zone_lbl or secteur_lbl or dname

            state = SPCClient.door_state(d)
            if state >= 0:
                old_state = last_door_state.get(did)
                if old_state is None or state != old_state:
                    mq.pub(f"doors/{did}/state", state)
                    last_door_state[did] = state
                    if log_changes:
                        state_txt = {
                            0: "normale",
                            1: "déverrouillée",
                            4: "alarme",
                        }.get(state, str(state))
                        print(f"[{tick}] 🟠 Porte '{dname}' → {state_txt}")

            drs = SPCClient.door_drs(d)
            if drs >= 0:
                old_drs = last_door_drs.get(did)
                if old_drs is None or drs != old_drs:
                    mq.pub(f"doors/{did}/drs", drs)
                    last_door_drs[did] = drs
                    if log_changes:
                        drs_txt = {
                            0: "fermé",
                            1: "ouvert",
                        }.get(drs, str(drs))
                        print(f"[{tick}] 🟤 Libération porte '{dname}' → {drs_txt}")

        if not commands_before and not commands_after:
            time.sleep(interval)

    mq.client.loop_stop()
    try:
        mq.client.disconnect()
    except Exception:
        pass
    print("[SPC→MQTT] Arrêt propre.")

if __name__ == "__main__":
    main()
