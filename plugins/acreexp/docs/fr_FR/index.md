# Plugin ACRE SPC

Le plugin **ACRE SPC** permet d'intégrer une centrale ACRE/Siemens SPC dans Jeedom. Il interroge périodiquement la centrale via l'API SPC Web Gateway et expose automatiquement les secteurs et zones sous forme de commandes infos.

## Pré-requis
- Une centrale ACRE/Siemens SPC disposant de l'API Web (licence « 3rd Party »).
- Les identifiants d'un utilisateur autorisé à interroger l'API ainsi que son code PIN.
- Python 3 disponible sur votre système (le démon l'utilise pour dialoguer avec la centrale).

## Installation
1. Installez le plugin depuis le Market Jeedom ou copiez-le dans `plugins/acreexp`.
2. Activez le plugin puis ouvrez la page de configuration pour vérifier les paramètres globaux :
   - **Intervalle de rafraîchissement** : fréquence à laquelle le démon interroge les centrales.
   - **Binaire Python** : chemin vers le binaire Python 3 à utiliser (laisser vide pour autodétection).
3. Démarrez le démon depuis la page de configuration une fois au moins un équipement créé.

## Configuration d'un équipement
1. Dans la page du plugin, cliquez sur **Ajouter**.
2. Renseignez les informations générales de l'équipement (nom, objet parent, visibilité…).
3. Complétez ensuite la section **Connexion à la centrale** :
   - Adresse IP ou nom d'hôte de la centrale.
   - Port d'accès à l'API (443 par défaut en HTTPS).
   - Activation de HTTPS si nécessaire.
   - Identifiant et code/PIN de l'utilisateur SPC.
4. Sauvegardez l'équipement puis cliquez sur **Synchroniser maintenant** pour récupérer les secteurs et zones.

## Commandes générées
Pour chaque zone et chaque secteur détecté, le plugin crée automatiquement deux commandes info :
- `…::state` : état numérique (0, 1, 2…) renvoyé par la centrale.
- `…::label` : libellé textuel associé à l'état.

Ces commandes sont historisables et peuvent être utilisées dans vos scénarios ou dashboards.

## Dépannage
- Vérifiez le log `acreexp` pour les actions générales et `acreexp_daemon` pour le démon.
- Assurez-vous que l'utilisateur SPC utilisé possède les droits nécessaires sur l'API.
- En cas d'erreur de communication, ajustez le délai d'interrogation ou le chemin du binaire Python dans la configuration du plugin.
