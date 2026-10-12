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

1. Créer un bot avec @BotFather, récupérer le jeton.
2. Envoyer un message au bot, puis ouvrir `https://api.telegram.org/bot<jeton>/getUpdates` pour lire
   le `chat.id`.
3. Renseigner `TELEGRAM_BOT_TOKEN` et `TELEGRAM_CHAT_ID`, redémarrer, puis « Envoyer une alerte test ».

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
| `TELEGRAM_BOT_TOKEN` | vide | Jeton du bot |
| `TELEGRAM_CHAT_ID` | vide | Destinataire des alertes |
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
pointe alors sur `<utilisateur-docker-hub>/domain-watch:latest`, et le lien de Container Manager met
l'image à jour.

Sans Docker Hub : arrêter le projet, supprimer l'image dans Image, puis Construire le projet, qui
retélécharge `latest`.

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
