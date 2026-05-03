# ACRE SPC42 → MQTT Watchdog

Ce projet fournit un service `systemd` qui :

- interroge une centrale **ACRE SPC42** via l'interface Web,
- publie les états sur **MQTT**,
- expose des topics de **commande** (secteurs, portes, zones, sorties).

---

## Sommaire

1. [Prérequis](#prérequis)
2. [Installation rapide](#installation-rapide)
3. [Configuration](#configuration)
4. [Démarrage et vérification](#démarrage-et-vérification)
5. [Mise à jour](#mise-à-jour)
6. [Topics MQTT publiés](#topics-mqtt-publiés)
7. [Topics MQTT de commande](#topics-mqtt-de-commande)
8. [Service systemd](#service-systemd)
9. [Sécurité](#sécurité)
10. [Dépannage](#dépannage)
11. [Désinstallation](#désinstallation)

---

## Prérequis

- Linux avec `systemd`
- Accès réseau à la centrale SPC
- Broker MQTT joignable (local ou distant)
- Droits `root` pour l’installation (scripts + service)

---

## Installation rapide

```bash
cd /usr/local/src
git clone https://github.com/MrJuju0319/acre_exp.git
cd acre_exp
chmod +x install.sh
./install.sh --install
```

### Installation non-interactive

```bash
ASSUME_YES=true ./install.sh --install
```

Tu peux aussi injecter les variables d’environnement (SPC/MQTT) avant installation.

Exemple :

```bash
SPC_HOST="https://192.168.1.100" \
SPC_USER="Engineer" \
SPC_PIN="1111" \
MQTT_HOST="127.0.0.1" \
MQTT_BASE_TOPIC="acre_maison" \
ASSUME_YES=true \
./install.sh --install
```

---

## Configuration

Le fichier principal est :

- `/etc/acre_exp/config.yml`

Exemple complet :

```yaml
spc:
  host: "https://192.168.1.100"
  user: "Engineer"
  pin: "1111"
  language: 253
  session_cache_dir: "/var/lib/acre_exp"
  min_login_interval_sec: 60

mqtt:
  host: "127.0.0.1"
  port: 1883
  user: ""
  pass: ""
  base_topic: "acre_XXX"
  client_id: "acre-exp"
  qos: 0
  retain: true

watchdog:
  refresh_interval: 2.0
  controller_refresh_interval: 60.0
  log_changes: true
  information:
    zones: true
    secteurs: true
    doors: true
    outputs: true
  controle:
    zones: true
    secteurs: true
    doors: true
    outputs: true
```

### Détails des paramètres

#### `spc`

- `host` : URL de la centrale (`http://` ou `https://`)
- `user` : utilisateur SPC
- `pin` : code PIN
- `language` : identifiant langue SPC (`253` = FR)
- `session_cache_dir` : dossier cache session/cookies
- `min_login_interval_sec` : intervalle mini entre tentatives login

#### `mqtt`

- `host` / `port` : broker
- `user` / `pass` : credentials (optionnel)
- `base_topic` : racine des topics (ex: `acre_maison`)
- `client_id` : identifiant client MQTT
- `qos` : QoS publication (0/1/2)
- `retain` : retain MQTT (`true`/`false`)

#### `watchdog`

- `refresh_interval` : période de lecture zones/secteurs/portes/sorties
- `controller_refresh_interval` : période de lecture état centrale
- `log_changes` : log seulement les changements significatifs
- `information.*` : active/désactive la publication MQTT par catégorie
- `controle.*` : active/désactive les abonnements `.../set` par catégorie

---

## Démarrage et vérification

### Vérifier le service

```bash
systemctl status acre-exp-watchdog.service
journalctl -u acre-exp-watchdog.service -f -n 100
```

### Test direct du client statut

```bash
/usr/local/bin/acre_exp_status.py -c /etc/acre_exp/config.yml | jq .
```

### Écoute MQTT

```bash
mosquitto_sub -h 127.0.0.1 -t 'acre_XXX/#' -v
```

---

## Mise à jour

```bash
cd /usr/local/src/acre_exp
git pull
chmod +x install.sh
./install.sh --update
```

> `--update` met à jour les scripts, les dépendances Python, puis redéploie le service.

---

## Topics MQTT publiés

| Topic | Description |
| --- | --- |
| `acre_XXX/zones/<id>/state` | 0 = normale, 1 = active/alarme |
| `acre_XXX/zones/<id>/entree` | 1 = fermée, 0 = ouverte/alarme |
| `acre_XXX/secteurs/<id>/state` | 0 = MHS, 1 = MES totale, 2 = Nuit/partielle A, 3 = partielle B, 4 = alarme |
| `acre_XXX/doors/<id>/state` | 0 = normal/verrouillée, 1 = déverrouillée, 4 = alarme |
| `acre_XXX/doors/<id>/drs` | 0 = bouton sortie relâché, 1 = bouton appuyé |
| `acre_XXX/outputs/<id>/state` | 0 = OFF, 1 = ON |
| `acre_XXX/outputs/<id>/state_txt` | Texte brut SPC (On/Off/...) |
| `acre_XXX/etat/<section>/<libellé>` | Valeurs textuelles "État Centrale" |

---

## Topics MQTT de commande

Toutes les commandes publient un accusé sur :

- `.../command_result`

Format :

- succès : `ok:<code>`
- erreur : `error:<raison>`

### Secteurs

Topic : `acre_XXX/secteurs/<id>/set`

Payloads acceptés :

- `0`, `mhs`
- `1`, `mes`
- `2`, `part`, `nuit`
- `3`, `partb`

### Portes

Topic : `acre_XXX/doors/<id>/set`

Payloads acceptés :

- `normal`
- `lock`
- `unlock`
- `pulse`

### Sorties

Topic : `acre_XXX/outputs/<id>/set`

Payloads acceptés :

- `1`, `on`
- `0`, `off`

### Zones

Topic : `acre_XXX/zones/<id>/set`

Payloads acceptés :

- `inhibit`
- `uninhibit`
- `isolate`
- `unisolate`
- `testjdb`
- `restore`

---

## Service systemd

Fichier : `/etc/systemd/system/acre-exp-watchdog.service`

Commandes utiles :

```bash
systemctl daemon-reload
systemctl enable acre-exp-watchdog.service
systemctl restart acre-exp-watchdog.service
systemctl status acre-exp-watchdog.service
```

---

## Sécurité

Limiter les permissions du fichier de config :

```bash
chmod 640 /etc/acre_exp/config.yml
```

Recommandations :

- utiliser un utilisateur SPC dédié,
- isoler le broker MQTT sur VLAN/ACL,
- éviter l’exposition WAN directe de la centrale.

---

## Dépannage

### Logs

```bash
journalctl -u acre-exp-watchdog.service -n 200 --no-pager
```

### Vérifier les dépendances venv

```bash
/opt/spc-venv/bin/pip list
```

### Forcer une réinstallation propre

```bash
cd /usr/local/src/acre_exp
git fetch --all
./install.sh --update
```

### Corriger un dépôt cloné avec fins de ligne Windows

```bash
perl -0777 -i -pe 's/\x0D\x0A/\x0A/g; s/\A\xEF\xBB\xBF//' install.sh
bash ./install.sh --update
```

---

## Désinstallation

```bash
systemctl stop acre-exp-watchdog.service
systemctl disable acre-exp-watchdog.service
rm -f /usr/local/bin/acre_exp_watchdog.py /usr/local/bin/acre_exp_status.py
rm -f /etc/systemd/system/acre-exp-watchdog.service
rm -rf /etc/acre_exp /var/lib/acre_exp /opt/spc-venv
systemctl daemon-reload
```
