# domain-watch

Surveillance de noms de domaine déjà enregistrés. L'appli interroge le RDAP de chaque registre,
suit le cycle de vie du nom (expiration, rédemption, suppression) et envoie une alerte Telegram
à chaque changement d'état ou de registrar.

Un seul conteneur : FastAPI, SQLite, planificateur intégré. Les données vivent dans `/data`.

## Installation sur un NAS Synology

Prérequis : DSM 7.2 ou plus, paquet Container Manager installé. Pas besoin de SSH.

### 1. Accès à l'image

Container Manager ne sait pas s'authentifier de façon fiable sur GHCR. Sans SSH, l'image doit
être publique : sur GitHub, Packages, `domain-watch`, Package settings, Change visibility, Public.
Le dépôt reste privé. L'image ne contient aucun secret, seulement le code de l'appli.

Avec SSH, l'image peut rester privée : `sudo docker login ghcr.io` avec un jeton classic limité à
`read:packages`, puis `sudo docker pull ghcr.io/khasys/domain-watch:latest` avant l'étape 3.

### 2. Dossiers

Dans File Station, créer `docker/domain-watch/data`. Le conteneur adopte le propriétaire de ce
dossier : aucun uid à renseigner.

### 3. Premier lancement

Container Manager, Projet, Créer : chemin `/volume1/docker/domain-watch`, contenu de
`docker-compose.yml` collé dans l'éditeur. `AUTH_USERNAME` et `AUTH_PASSWORD_HASH` restent vides.

Ouvrir `http://IP-DU-NAS:8080`. Sans identifiants configurés, l'appli n'affiche que la page de
configuration : saisir le mot de passe voulu, elle renvoie son hash bcrypt avec chaque `$` doublé,
forme attendue par `docker compose` en ligne de commande. Container Manager ne dédouble pas les `$` :
l'appli accepte le hash sous les deux formes. Rien n'est enregistré côté appli.

### 4. Identifiants

Dans le projet, modifier le compose : `AUTH_USERNAME` et `AUTH_PASSWORD_HASH` (le hash entre
guillemets), puis relancer le projet. La page de connexion remplace la page de configuration.

La clé de signature des sessions est générée au premier démarrage dans `data/secret_key`. La
supprimer déconnecte toutes les sessions. `SECRET_KEY` permet de l'imposer.

`/health` renvoie l'état et la version sans authentification.

### Telegram

Tout se règle dans l'appli, menu Paramètres, sans redémarrage :

1. Dans Telegram, ouvrir @BotFather, envoyer `/newbot`, choisir un nom et un identifiant.
2. Coller le jeton donné par BotFather dans Paramètres, Telegram, puis Enregistrer.
3. Cliquer « Associer mon compte » : l'appli affiche un code. L'envoyer au bot sous la forme
   `/start 123456`. Seul ce chat recevra les alertes et pourra piloter le bot.

Commandes du bot : `/liste`, `/urgent`, `/ajouter nom.fr`, `/supprimer nom.fr`, `/verifier nom.fr`,
`/statut`, `/aide`. Le bot interroge Telegram lui-même : aucun port à ouvrir.

Le résumé hebdomadaire (changements de la semaine, expirations et libérations des 30 jours à venir,
domaines en erreur) se règle au même endroit : jour, heure, activation. L'heure suit la variable `TZ`.

### Apparence

Paramètres, Apparence : nom affiché en haut à gauche, logo (PNG, JPEG, WebP ou SVG, 300 Ko maximum,
rangé dans `data/`), taille réglable au curseur avec aperçu en direct (hauteur du logo de 20 à 120 px, ou taille du nom sans logo), et thème clair, sombre ou selon le système. Le réglage vaut pour tous les
appareils.

### Calendrier

Paramètres, Calendrier, « Créer l'adresse » : une URL secrète au format ICS, à ajouter comme
calendrier par URL dans l'agenda du téléphone. Elle contient les expirations et les libérations
estimées, avec un rappel la veille. Elle s'ouvre sans mot de passe : la changer si elle a fuité.

### Appli sur le téléphone

L'interface est une PWA : sur téléphone, navigation en bas, panneaux plein écran, boutons larges.
La session se prolonge à chaque ouverture : pas de mot de passe à retaper tant que l'appli est
ouverte au moins une fois par période `SESSION_MAX_AGE` (7 jours par défaut).

L'installation complète demande HTTPS. Sur `http://ip-du-nas:8111`, Android se contente d'un
raccourci vers le navigateur, et l'iPhone installe l'appli sans le mode hors ligne.

Pour passer en HTTPS sur le Synology, sans SSH :

1. Panneau de configuration, Accès externe, DDNS : créer une adresse (par exemple `xxx.synology.me`).
2. Panneau de configuration, Sécurité, Certificat : Ajouter, Let's Encrypt, pour
   `domaines.xxx.synology.me`.
3. Panneau de configuration, Portail de connexion, Avancé, Proxy inversé, Créer : source HTTPS,
   `domaines.xxx.synology.me`, port 443 ; destination HTTP, `localhost`, port `8111`.
4. Certificat, Paramètres : associer le certificat à `domaines.xxx.synology.me`.
5. Dans le compose, passer `COOKIE_SECURE=true`, puis reconstruire le projet. La connexion par
   `http://ip-du-nas:8111` ne marche alors plus : le navigateur ne renvoie pas un cookie sécurisé en HTTP.

Ouvrir ensuite `https://domaines.xxx.synology.me` sur le téléphone. Android (Chrome) : menu,
Installer l'application. iPhone (Safari) : Partager, Sur l'écran d'accueil.

Accessible depuis Internet, l'appli n'est protégée que par son mot de passe (verrouillage après
5 échecs). Pour la garder en réseau local, ne pas ouvrir le port 443 sur la box, ou passer par le
VPN du NAS.

## Variables d'environnement

Les durées acceptent `30s`, `5m`, `6h`, `7d`, ou un nombre de secondes.

| Variable | Défaut | Rôle |
|---|---|---|
| `AUTH_USERNAME` | vide | Identifiant de connexion. Vide : page de configuration |
| `AUTH_PASSWORD_HASH` | vide | Hash bcrypt du mot de passe. Vide : page de configuration |
| `SECRET_KEY` | générée | Clé de signature du cookie, sinon lue dans `data/secret_key` |
| `APP_URL` | vide | URL de l'appli, ajoutée aux alertes |
| `SESSION_MAX_AGE` | `7d` | Durée de validité de la session |
| `COOKIE_SECURE` | `false` | `true` si l'appli est servie en HTTPS |
| `LOGIN_MAX_FAILURES` | `5` | Échecs de connexion avant blocage |
| `LOGIN_LOCK_DURATION` | `15m` | Durée du blocage, par adresse IP |
| `INTERVAL_ACTIVE` | `24h` | Fréquence de check, état actif |
| `INTERVAL_EXPIRING` | `6h` | Expire bientôt |
| `INTERVAL_EXPIRED` | `6h` | Expiré |
| `INTERVAL_REDEMPTION` | `1h` | En rédemption |
| `INTERVAL_PENDING_DELETE` | `5m` | Suppression imminente |
| `INTERVAL_AVAILABLE` | `24h` | Disponible |
| `INTERVAL_UNKNOWN` | `1h` | Jamais vérifié avec succès |
| `EXPIRING_DAYS` | `30` | Seuil de l'état « expire bientôt » |
| `RDAP_MIN_DELAY` | `2` | Secondes minimum entre deux requêtes vers un même registre |
| `RDAP_RETRIES` | `3` | Nouvelles tentatives sur erreur réseau, 429 ou 5xx |
| `RDAP_BACKOFF` | `5` | Base du backoff exponentiel, en secondes |
| `RDAP_MAX_RETRY_AFTER` | `2m` | Au-delà, un `Retry-After` du registre met fin au check |
| `RDAP_TIMEOUT` | `20` | Timeout d'une requête RDAP ou WHOIS, en secondes |
| `WHOIS_MIN_DELAY` | `5` | Secondes minimum entre deux requêtes vers un même serveur WHOIS |
| `TELEGRAM_BOT_TOKEN` | vide | Ancienne configuration, utilisée tant que rien n'est réglé dans l'appli |
| `TELEGRAM_CHAT_ID` | vide | Idem |
| `UPDATE_REPOSITORY` | `khasys/domain-watch` | Dépôt Docker Hub consulté chaque jour pour signaler une nouvelle version. Vide : pas de vérification |
| `TZ` | UTC | Fuseau des journaux |

## Fonctionnement

- Seuls les noms enregistrables sont acceptés. Un sous-domaine (`blog.exemple.com`) est refusé avec
  le nom à surveiller à la place, d'après la section ICANN de la Public Suffix List.
- Le serveur RDAP de chaque TLD vient du bootstrap IANA, mis en cache 24 h dans `/data`, complété
  par les serveurs absents du bootstrap mais vérifiés à la main (`EXTRA_SERVERS` dans `app/rdap.py`,
  .so pour l'instant).
- Une réponse 404 du registre donne l'état « disponible ». Une erreur réseau, un 429 ou un 5xx ne
  le donnent jamais : le domaine passe en « erreur de check » et garde son dernier état connu.
- Sans RDAP (.co par exemple), l'appli interroge le WHOIS du registre, trouvé via `whois.iana.org`.
  Elle ne lit que des lignes « clé: valeur » connues. « Disponible » exige une formule explicite du
  registre (« not found », « no match »...) et aucune donnée d'enregistrement dans la réponse. Toute
  réponse non reconnue donne une erreur de check. Le détail d'un domaine indique la source utilisée.
- Signaux d'abandon, une fois par jour pour chaque domaine enregistré : le nom résout-il, le site
  répond-il, le certificat HTTPS est-il valide, y a-t-il des serveurs de mail, le domaine est-il en
  parking ou affiché à vendre. Le badge « Signes d'abandon » apparaît avec au moins deux indices
  (l'absence de mail seule ne compte pas) ou un parking.
- Pour .com et .net, la date de libération estimée porte aussi une heure : le début de la fenêtre
  quotidienne de suppression de Verisign, vers 12:00 UTC depuis le 1er octobre 2026 d'après la
  presse spécialisée. Pas d'annonce officielle trouvée, d'où l'affichage en estimation.
- Filtres (nom, état, tag, signes d'abandon) et tri sont gardés par le navigateur d'une visite à
  l'autre. Tags libres par domaine, ajoutés dans le détail.
- Certains registres n'ont ni RDAP ni WHOIS public, ou réservent leur WHOIS à des accès
  authentifiés : leurs domaines restent en erreur de check.
- Toutes les requêtes vers un même registre passent par une file unique, avec le délai minimum
  entre deux appels. Le check global et les fréquences rapides ne peuvent pas la contourner.
- La date de libération est une estimation. Elle repose sur `app/tld_delays.toml`, qui donne pour
  chaque TLD les délais publiés par le registre avec leur source. Si un délai manque, aucune
  estimation n'est affichée pour ce TLD.

État des délais au 2026-10-12 :

| TLD | Grâce après expiration | Rédemption | Suppression | Estimation |
|---|---|---|---|---|
| .com | 45 j | 30 j | 5 j | oui |
| .net | 45 j | 30 j | 5 j | oui |
| .fr | non fixé par l'Afnic | 30 j | non trouvé | non |

Pour ajouter un TLD, compléter `tld_delays.toml` avec la source et la date de consultation, puis
publier une nouvelle version.

## Mises à jour

Container Manager ne signale les nouvelles versions (« Mise à jour disponible » dans Image) que pour
les images Docker Hub au tag `latest`. Les images `ghcr.io` ne sont jamais vérifiées.

Le workflow `release` publie donc aussi sur Docker Hub si les secrets `DOCKERHUB_USERNAME` et
`DOCKERHUB_TOKEN` sont définis dans le dépôt (Settings, Secrets and variables, Actions). Le compose
pointe alors sur `khasys/domain-watch:latest`, et le lien de Container Manager met
l'image à jour.

Sans Docker Hub : arrêter le projet, supprimer l'image dans Image, puis Construire le projet, qui
retélécharge `latest`.

L'appli vérifie aussi Docker Hub une fois par jour. Paramètres, Version, affiche la version installée,
encadrée en vert si elle est à jour, en rouge avec la dernière version publiée sinon. Une pastille
rouge apparaît alors sur le bouton Paramètres. La date de la dernière vérification et celle de la
prochaine sont affichées. « Vérifier les mises à jour » relance la vérification et repousse la
suivante de 24 h.

Pour figer une version, remplacer `latest` par un tag précis (`v0.2.0`).

Au démarrage, les migrations SQLite en attente sont appliquées. Juste avant, la base est copiée dans
`/data/backups/domain-watch-AAAAMMJJ-HHMMSS.db`. Les 10 dernières copies sont conservées.

Pour revenir en arrière : arrêter le projet, remplacer `data/domain-watch.db` par la sauvegarde
voulue, mettre le tag de l'ancienne version à la place de `latest`, relancer.

## Publier une version

Depuis l'onglet Actions : workflow `release`, Run workflow, version `v0.2.0`. Le tag git est créé
à la fin. Un `git push` du tag `v0.2.0` donne le même résultat.

Chaque push sur `main` lance le workflow `ci` : tests, build de l'image avec démarrage du
conteneur, et `scripts/rdap_smoke.py` qui interroge les vrais registres de .com, .net, .org, .fr,
.so et .co. Lancé à la main avec des domaines en argument, il affiche la réponse brute de chacun.

Le workflow `release` lance les tests puis pousse `ghcr.io/<owner>/domain-watch:v0.2.0` et `:latest`
(amd64 et arm64). La version est affichée en pied de page et renvoyée par `/health`.

## Développement

```sh
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest

export DATA_DIR=./data AUTH_USERNAME=moi SECRET_KEY=dev
export AUTH_PASSWORD_HASH="$(python -c 'import bcrypt; print(bcrypt.hashpw(b"motdepasse", bcrypt.gensalt()).decode())')"
uvicorn app.main:app --reload --port 8080
```

Une migration s'ajoute sous `app/migrations/` avec un numéro supérieur au dernier
(`002_xxx.sql`). Le numéro est stocké dans `PRAGMA user_version`.
