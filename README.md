# 🛰️ ACRE SPC42 → MQTT

## 🚀 Installation

```
cd /usr/local/src
git clone https://github.com/MrJuju0319/acre_exp.git
cd acre_exp
chmod +x install.sh
./install.sh --install
```

## ⚙️ Configuration

```yaml
spc:
  host: "https://192.168.1.100"
  user: "Engineer"
  pin: "1111"
  language: 253  # 253 = Français, 0 = Anglais
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
  refresh_interval: 2
  log_changes: true
  ```

> ℹ️ **Astuce :** l'adresse `spc.host` peut indifféremment utiliser `http://` ou `https://` selon la configuration de votre centrale.

## 🔄 Mise à jour

```bash
cd /usr/local/src/acre_exp
./install.sh --update
```
🔍 Vérifications

```bash
systemctl status acre-exp-watchdog.service
journalctl -u acre-exp-watchdog.service -f -n 100
/usr/local/bin/acre_exp_status.py -c /etc/acre_exp/config.yml | jq .
mosquitto_sub -h 127.0.0.1 -t 'acre_XXX/#' -v
```

### Topics MQTT publiés

* `acre_XXX/zones/<id>/state` — 0 = zone normale, 1 = zone activée.
* `acre_XXX/zones/<id>/entree` — 1 = entrée fermée, 0 = entrée ouverte/alarme.
* `acre_XXX/secteurs/<id>/state` — 0 = MHS (désarmé), 1 = MES (totale), 2 = MES partielle A, 3 = MES partielle B, 4 = alarme.
* `acre_XXX/doors/<id>/state` — 0 = porte normale/verrouillée, 1 = porte déverrouillée/accès libre, 4 = alarme.
* `acre_XXX/doors/<id>/dps` — 0 = contact fermé, 1 = contact ouvert, 2 = isolé, 3 = inhibé, 4 = trouble.
* `acre_XXX/doors/<id>/drs` — mêmes valeurs que DPS pour le bouton de libération.

> ℹ️ Les topics `name`, `zone` et `secteur` sont également publiés pour chaque porte (`doors/<id>/…`).
> ℹ️ L’identifiant `0` dans `secteurs/0/state` représente le statut global « Tous Secteurs » lu sur la page *Etat du système*.

## 🧹 Désinstallation

```bash
systemctl stop acre-exp-watchdog.service
systemctl disable acre-exp-watchdog.service
rm -f /usr/local/bin/acre_exp_watchdog.py /usr/local/bin/acre_exp_status.py
rm -f /etc/systemd/system/acre-exp-watchdog.service
rm -rf /etc/acre_exp /var/lib/acre_exp /opt/spc-venv
systemctl daemon-reload
```

## 🧰 Dépannage

```
# Corriger les fichiers Windows CRLF
perl -0777 -i -pe 's/\x0D\x0A/\x0A/g; s/\A\xEF\xBB\xBF//' install.sh
bash ./install.sh --update
```

```bash
# Voir les logs systemd
journalctl -u acre-exp-watchdog.service -n 200 --no-pager
```

```bash
# Tester MQTT
mosquitto_sub -v -t 'acre_XXX/#'
```

## 🔒 Sécurité

```bash
chmod 640 /etc/acre_exp/config.yml
```

## 📦 Service systemd

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
