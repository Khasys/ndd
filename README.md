# domain-watch

Surveillance de noms de domaine déjà enregistrés. L'appli interroge le RDAP de chaque registre,
suit le cycle de vie du nom (expiration, rédemption, suppression) et envoie une alerte Telegram
à chaque changement d'état ou de registrar.

Un seul conteneur : FastAPI, SQLite, planificateur intégré. Les données vivent dans `/data`.

## Installation sur un NAS Synology

Prérequis : DSM 7.2 ou plus, paquet Container Manager installé, accès SSH pour deux commandes.

### 1. Accès à l'image

Le dépôt est privé, l'image sur GHCR l'est aussi. Créer sur GitHub un jeton *classic* avec le seul
droit `read:packages`, puis sur le NAS, en SSH :

```sh
sudo docker login ghcr.io -u <utilisateur-github>
```

### 2. Dossier du projet

```sh
mkdir -p /volume1/docker/domain-watch/data
id   # noter uid et gid de l'utilisateur DSM
```

Dans `docker-compose.yml`, la ligne `user: "1026:100"` doit correspondre à cet uid:gid, sinon le
conteneur ne pourra pas écrire dans `data/`. 1026:100 est la valeur du premier compte créé sur DSM.

### 3. Mot de passe et clé de session

```sh
sudo docker run --rm -it ghcr.io/khasys/domain-watch:v0.1.0 python -m app.hashpw
```

La commande affiche le hash bcrypt avec chaque `$` doublé, prêt à coller dans
`docker-compose.yml` (Compose interprète `$` comme une variable).

Pour `SECRET_KEY`, une chaîne aléatoire longue :

```sh
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Changer cette clé déconnecte toutes les sessions.

### 4. Lancement

Copier `docker-compose.yml` dans `/volume1/docker/domain-watch/` et remplir les variables. Dans
Container Manager, créer un projet sur ce dossier en reprenant le `docker-compose.yml` existant.

L'appli répond sur le port 8080. `/health` renvoie l'état et la version sans authentification.

### Telegram

1. Créer un bot avec @BotFather, récupérer le jeton.
2. Envoyer un message au bot, puis ouvrir `https://api.telegram.org/bot<jeton>/getUpdates` pour lire
   le `chat.id`.
3. Renseigner `TELEGRAM_BOT_TOKEN` et `TELEGRAM_CHAT_ID`, redémarrer, puis « Envoyer une alerte test ».

## Variables d'environnement

Les durées acceptent `30s`, `5m`, `6h`, `7d`, ou un nombre de secondes.

| Variable | Défaut | Rôle |
|---|---|---|
| `AUTH_USERNAME` | obligatoire | Identifiant de connexion |
| `AUTH_PASSWORD_HASH` | obligatoire | Hash bcrypt du mot de passe |
| `SECRET_KEY` | obligatoire | Clé de signature du cookie de session |
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
| `RDAP_TIMEOUT` | `20` | Timeout d'une requête RDAP, en secondes |
| `TELEGRAM_BOT_TOKEN` | vide | Jeton du bot |
| `TELEGRAM_CHAT_ID` | vide | Destinataire des alertes |
| `TZ` | UTC | Fuseau des journaux |

## Fonctionnement

- Seuls les noms enregistrables sont acceptés. Un sous-domaine (`blog.exemple.com`) est refusé avec
  le nom à surveiller à la place, d'après la section ICANN de la Public Suffix List.
- Le serveur RDAP de chaque TLD vient du bootstrap IANA, mis en cache 24 h dans `/data`.
- Une réponse 404 du registre donne l'état « disponible ». Une erreur réseau, un 429 ou un 5xx ne
  le donnent jamais : le domaine passe en « erreur de check » et garde son dernier état connu.
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

Changer le tag dans `docker-compose.yml` (`v0.1.0` vers la nouvelle version), puis arrêter et
reconstruire le projet dans Container Manager pour tirer la nouvelle image. Le compose ne référence jamais `latest`, une mise à jour est
toujours volontaire.

Au démarrage, les migrations SQLite en attente sont appliquées. Juste avant, la base est copiée dans
`/data/backups/domain-watch-AAAAMMJJ-HHMMSS.db`. Les 10 dernières copies sont conservées.

Pour revenir en arrière : arrêter le projet, remplacer `data/domain-watch.db` par la sauvegarde
voulue, remettre l'ancien tag, relancer.

## Publier une version

```sh
git tag v0.2.0
git push origin v0.2.0
```

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
