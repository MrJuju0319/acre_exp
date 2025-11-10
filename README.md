# ACRE SPC42 → MQTT Watchdog

Ce dépôt propose un service `systemd` qui collecte l'état d'une centrale **ACRE SPC42** via son interface Web et le publie sur un broker **MQTT**. Il peut également piloter les secteurs, portes et zones exposés par la centrale.

## Sommaire

1. [Installation](#installation)
2. [Configuration](#configuration)
3. [Mise à jour et vérifications](#mise-à-jour-et-vérifications)
4. [Topics MQTT publiés](#topics-mqtt-publiés)
5. [Topics MQTT de commande](#topics-mqtt-de-commande)
6. [Service systemd](#service-systemd)
7. [Sécurité](#sécurité)
8. [Dépannage](#dépannage)
9. [Désinstallation](#désinstallation)

## Installation

```bash
cd /usr/local/src
git clone https://github.com/MrJuju0319/acre_exp.git
cd acre_exp
chmod +x install.sh
./install.sh --install
```

## Configuration

Le script d'installation place un fichier `/etc/acre_exp/config.yml`. Exemple de configuration :

```yaml
spc:
  host: "https://192.168.1.100"
  user: "Engineer"
  pin: "1111"
  language: 253            # 253 = Français, 0 = Anglais
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
  refresh_interval: 2.0        # secondes (float accepté, min 0.2s)
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

> ℹ️ L'adresse `spc.host` accepte indifféremment `http://` ou `https://` selon la configuration de la centrale.
> ℹ️ Les sections `watchdog.information` et `watchdog.controle` permettent de désactiver la publication ou les commandes pour une catégorie. Les valeurs acceptent `true`/`false`, `1`/`0`, `oui`/`non`, etc.
> ℹ️ Lorsqu'une catégorie est désactivée côté **information**, aucun topic MQTT `name`, `state`, etc. n'est publié pour celle-ci. Lorsqu'elle est désactivée côté **contrôle**, aucun abonnement `…/set` n'est ouvert et toute commande reçue renverra `error:control-disabled`.

## Mise à jour et vérifications

### Mettre à jour le service

```bash
cd /usr/local/src/acre_exp
chmod +x install.sh
./install.sh --update
```

### Vérifier le fonctionnement

```bash
systemctl status acre-exp-watchdog.service
journalctl -u acre-exp-watchdog.service -f -n 100
/usr/local/bin/acre_exp_status.py -c /etc/acre_exp/config.yml | jq .
mosquitto_sub -h 127.0.0.1 -t 'acre_XXX/#' -v
```

## Topics MQTT publiés

| Topic | Description |
| --- | --- |
| `acre_XXX/zones/<id>/state` | 0 = zone normale, 1 = zone activée |
| `acre_XXX/zones/<id>/entree` | 1 = entrée fermée, 0 = entrée ouverte/alarme |
| `acre_XXX/secteurs/<id>/state` | 0 = MHS, 1 = MES totale, 2 = MES partielle A, 3 = MES partielle B, 4 = alarme |
| `acre_XXX/doors/<id>/state` | 0 = porte normale/verrouillée, 1 = porte déverrouillée/accès libre, 4 = alarme |
| `acre_XXX/doors/<id>/drs` | 0 = bouton de sortie relâché (fermé), 1 = bouton appuyé (ouvert) |
| `acre_XXX/etat/<section>/<Libellé>` | Valeurs textuelles de l'onglet « État Centrale » |
| `acre_XXX/outputs/<id>/state` | 0 = sortie à l'arrêt, 1 = sortie activée |
| `acre_XXX/outputs/<id>/state_txt` | Texte brut (« On », « Off », …) affiché sur la page Intéraction Logique |

> ℹ️ Les topics `name`, `zone` et `secteur` sont également publiés pour chaque porte (`doors/<id>/…`).
> ℹ️ L’identifiant `0` dans `secteurs/0/state` représente le statut global « Tous Secteurs » lu sur la page *État du système*.

## Topics MQTT de commande

### Secteurs

Publier sur `acre_XXX/secteurs/<id>/set` (ou `0` pour *Tous Secteurs*). Charges utiles acceptées :

| Valeur | Action |
| --- | --- |
| `0`, `mhs` | Mise Hors Service |
| `1`, `mes` | Mise En Service totale |
| `2`, `part` | Mise En Service partielle A |
| `3`, `partb` | Mise En Service partielle B |

Un accusé est publié sur `acre_XXX/secteurs/<id>/command_result` (`ok:<code>` ou `error:…`). Les codes `ok` correspondent à `state` (0 à 3).

### Portes

Publier sur `acre_XXX/doors/<id>/set`. Charges utiles acceptées :

| Valeur | Action |
| --- | --- |
| `normal` | Bouton **Normal** |
| `lock` | Bouton **Verrouiller** |
| `unlock` | Bouton **Déverrouiller** |
| `pulse` | Bouton **Impulsion** |

Un accusé est publié sur `acre_XXX/doors/<id>/command_result` (`ok:<action>` ou `error:…`).

### Sorties

Publier sur `acre_XXX/outputs/<id>/set`. Charges utiles acceptées :

| Valeur | Action |
| --- | --- |
| `1`, `on` | Bouton **ON** |
| `0`, `off` | Bouton **Off** |

Un accusé est publié sur `acre_XXX/outputs/<id>/command_result` (`ok:<action>` ou `error:…`).

### Zones

Publier sur `acre_XXX/zones/<id>/set`. Charges utiles acceptées :

| Valeur | Action |
| --- | --- |
| `inhibit` | Bouton **Inhiber** |
| `uninhibit` | Bouton **Dé-Inhiber** |
| `isolate` | Bouton **Isoler** |
| `unisolate` | Bouton **Dé-Isoler** |
| `testjdb` | Bouton **TestJDB** |
| `restore` | Bouton **Restaurer** |

Un accusé est publié sur `acre_XXX/zones/<id>/command_result` (`ok:<action>` ou `error:…`).

## Service systemd

```ini
[Unit]
Description=ACRE SPC42 -> MQTT Watchdog (zones + secteurs)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/acre_exp_watchdog.py -c /etc/acre_exp/config.yml
Restart=always
RestartSec=3
User=root
Group=root
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ProtectKernelModules=true
ProtectKernelTunables=true
ProtectControlGroups=true
LockPersonality=true
MemoryDenyWriteExecute=true
CapabilityBoundingSet=
AmbientCapabilities=
ReadWritePaths=/var/lib/acre_exp /etc/acre_exp

[Install]
WantedBy=multi-user.target
```

## Sécurité

```bash
chmod 640 /etc/acre_exp/config.yml
```

## Dépannage

```bash
# Corriger les fichiers Windows CRLF
perl -0777 -i -pe 's/\x0D\x0A/\x0A/g; s/\A\xEF\xBB\xBF//' install.sh
bash ./install.sh --update

# Voir les logs systemd
journalctl -u acre-exp-watchdog.service -n 200 --no-pager

# Tester MQTT
mosquitto_sub -v -t 'acre_XXX/#'
```

## Désinstallation

```bash
systemctl stop acre-exp-watchdog.service
systemctl disable acre-exp-watchdog.service
rm -f /usr/local/bin/acre_exp_watchdog.py /usr/local/bin/acre_exp_status.py
rm -f /etc/systemd/system/acre-exp-watchdog.service
rm -rf /etc/acre_exp /var/lib/acre_exp /opt/spc-venv
systemctl daemon-reload
```
