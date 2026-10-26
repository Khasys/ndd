# domain-watch

Surveillance de noms de domaine déjà enregistrés. L'appli interroge le RDAP de chaque registre,
suit le cycle de vie du nom (expiration, rédemption, suppression) et envoie une alerte Telegram
à chaque changement d'état ou de registrar.

Un seul conteneur : FastAPI, SQLite, planificateur intégré. Les données vivent dans `/data`.

États d'un nom surveillé : Actif, Expire bientôt, Expiré, **Renouvelé**, En rédemption, Suppression imminente,
Disponible. **Renouvelé** (.fr seulement) : statut « auto renew period » et échéance repoussée, l'Afnic a renouvelé
le nom à l'échéance pour le même titulaire ; il redevient Actif quand le statut disparaît, ou passe en rédemption
si le registrar le supprime. Pour les autres extensions, ce statut s'affiche même sans paiement (période de
grâce) : le nom reste Expiré, avec sa libération estimée. Dans Dates clés, « Repris » est réservé à un nouvel
enregistrement ; un nom gardé par son titulaire (date de création antérieure) se lit « Renouvelé par le titulaire ».

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

### Alertes

Paramètres, carte **Alertes** : la seule où se règlent les messages. Une ligne par type, avec son rythme et sa case ;
le seuil des pépites (note de revente, 70 par défaut) ; l'heure du message du jour (8 h par défaut, heure `TZ`) et le
jour du résumé de la semaine ; le score minimum des bons noms du jour (40) ; « Envoyer un essai ». Les réglages
d'avant (alerte « À prendre », résumé du matin, résumé hebdomadaire) sont repris à la mise à jour (migration 034).

Deux rythmes. **Immédiat** : ce qui demande d'agir tout de suite. **Chaque jour** : un seul message à l'heure choisie
(en plusieurs parts au-delà de 4 000 caractères, sans couper une rubrique), rien s'il n'y a rien à dire.

| Type | Rythme | Contenu | Une seule fois |
|---|---|---|---|
| Pépite libre | immédiat, vu chaque minute | nom, état (libre vérifié au registre ou chez Gandi, libre selon le WHOIS « à confirmer chez un registrar », premium), prix d'achat Gandi s'il est connu, avis IA (revente, exploitation, prix final), raison, lien vers la fiche, lien Réserver ; les pépites d'un même passage dans un seul message, 10 détaillées puis « et N autres » | par nom et par libération (table `pepites`) |
| Domaine suivi | immédiat | nom de l'écran Domaines : état avant → après, registrar, libération estimée, lien vers la fiche ; vérification toutes les 5 min en suppression | à chaque changement |
| Analyse IA prête | immédiat, dans l'heure qui suit la demande | avis IA, raison, lien vers la fiche | par demande |
| Pépites bientôt libres | chaque jour | même gabarit, avec la libération estimée et la raison du rappel : nouvelle, libération sous 48 h, date revue | jamais la même liste deux jours de suite |
| Bons noms du jour | chaque jour | libérés, entrés en rédemption ou en suppression, libres selon le WHOIS à confirmer, .co / .ai / .io sortis du DNS, en 24 h ; score minimum ou avis « À prendre » / « À surveiller » ; sans les pépites | fenêtre de 24 h |
| Vues enregistrées | chaque jour | « Vue « X » : N nouveaux noms », les noms devenus libres ou bientôt libres depuis le message précédent qui répondent aux filtres de la vue (case « alerte » de la vue, Explorer) ; sans les pépites | depuis le passage précédent de la vue |
| Renouvellements | chaque jour | noms du portefeuille 30 puis 7 jours avant l'échéance | par échéance et par rappel |
| Panne bloquante | immédiat, au contrôle de santé (toutes les 10 minutes) | « Panne bloquante » puis « Réglé » : jeton Gandi refusé, vérifications .fr en pause plus d'une heure (limite Afnic), aucune zone ICANN lue avec succès depuis 36 h, fichier .fr de l'Afnic en erreur | à l'apparition et à la fin ; le quotidien ne la redit qu'en « Toujours en cours » |
| Santé | chaque jour | apparu, réglé, signalé : zone en erreur ou pas lue depuis 36 h (ou aucune zone lue avec succès depuis 36 h), fichier .fr en erreur, pause .fr de plus d'une heure, notation IA muette depuis 36 h, passe DNS (.fr, .co, .ai, .io) en erreur, à vide ou arrêtée, Gandi en erreur ou passe arrêtée, plus de 3 exécutions manquées du planificateur en 24 h ; puis « Toujours en cours » pour ce qui dure | chaque événement une fois |
| Résumé de la semaine | le jour choisi, dans le message du jour | domaines suivis : changements de la semaine, expirations et libérations des 30 jours à venir, domaines en erreur (rien sans domaine suivi) | une fois par semaine |

**Pépite** : un avis IA « À prendre » avec une note de revente au moins égale au seuil, sur un nom libre (vérifié au
registre ou chez Gandi ; libre selon le WHOIS, compris mais signalé ; premium vu chez Gandi, à son prix) ou bientôt
libre (expiré en délai de grâce, rédemption, suppression imminente). Toutes extensions, qu'il soit sorti d'une zone
(Veille) ou n'ait jamais été enregistré (Explorer : libre au registre après Vérifier, ou libre chez Gandi après sa
passe de nuit). L'état est celui de l'Explorer (`explore.gandi_effect`, règle unique) : un nom indisponible à l'achat,
réservé, premium probable non vérifié ou seulement « probablement libre » n'en est pas une. Un nom ignoré (Veille,
Ignorer) non plus.

Règles de l'immédiat : un nom suivi dans Domaines n'a que son alerte de changement d'état ; un nom que Gandi vient de
dire indisponible attend la revérification au registre ; un nom tout juste libre attend son prix Gandi cinq minutes
au plus (jeton Gandi configuré) ; un événement vu plus de 48 h après coup (seuil baissé, par exemple) n'est pas
annoncé, le nom reste dans Aujourd'hui. Au premier passage après la mise à jour, les pépites déjà libres servent de
référence, sans message. Type décoché ou Telegram pas configuré : les nouvelles pépites sont notées sans message, rien
ne part en rafale ensuite. Bientôt libre puis libre : deux événements (le quotidien, puis l'immédiat). Repris puis
libéré de nouveau après plus de 7 jours : nouvel événement.

Jamais en double : chaque envoi est noté en base une fois parti (ou mis en file de rejeu) ; un redémarrage, une tâche
planifiée rattrapée, « Envoyer un essai » ne renvoient rien. Le message du jour est noté fait pour la date ; s'il n'a
pas pu partir à l'heure (appli arrêtée), il part au premier passage suivant, une fois. Un nom annoncé comme pépite ne
revient ni dans les bons noms du jour ni dans une vue.

« Envoyer un essai » (`POST /api/alerts/test`) : les types activés avec leur rythme et, en exemple, la meilleure
pépite du moment, au gabarit des alertes. Ni rejoué ni noté.

`GET /api/settings/alerts` (aussi `alerts` de `GET /api/settings`) : `types` (`key`, `label`, `rhythm` `immediate`,
`daily` ou `weekly`, `rhythm_label`, `text`, `enabled`), `pepite_min`, `daily_hour`, `weekly_day` (0 lundi),
`morning_min`, `daily_last`. `PUT /api/settings/alerts` : `{"enabled": {"views": false}, "pepite_min": 80,
"daily_hour": 7}`, champs facultatifs, hors bornes ou type inconnu : 422. Clés des types : `pepite_free`,
`domain_change`, `ai_ready`, `pepite_coming`, `morning`, `views`, `renewals`, `health`, `weekly`. Session seulement.

Autres messages : la confirmation de l'association (« Associé. Ce chat recevra les alertes ») et les réponses du bot
à ses commandes.

Les liens utilisent `APP_URL`, sinon l'adresse vue par le navigateur à la dernière ouverture de l'appli.

Rejeu : un message que Telegram refuse passagèrement (trop de requêtes, panne de leur côté) ou qui ne
part pas (réseau coupé) est mis en file dans la base au lieu d'être perdu. Toutes les 5 minutes, l'appli
renvoie ce qui est dû, plusieurs messages en attente regroupés en un seul (4 000 caractères au plus) ;
après un nouvel échec, l'essai suivant attend deux fois plus longtemps (6 h au plus) ; après 48 h, le
message est abandonné avec une ligne de journal. Le message d'« Envoyer un essai » n'est jamais rejoué,
ni un message refusé pour de bon (jeton invalide, chat inconnu) : une pépite dont l'alerte est refusée ainsi est
reprise au passage suivant, comme le message du jour dont la première part est refusée. `/api/diag` donne la taille de la file.

### Apparence

Paramètres, Apparence : nom affiché en haut à gauche, logo (PNG, JPEG, WebP ou SVG, 300 Ko maximum,
rangé dans `data/`), taille réglable au curseur avec aperçu en direct (hauteur du logo de 20 à 120 px, ou taille du nom sans logo), et thème clair, sombre ou selon le système. Le réglage vaut pour tous les
appareils.

Interface : police Plus Jakarta Sans (noms de domaine en JetBrains Mono), deux accents (violet pour l'action, vert
émeraude pour ce qui est libre ou rentable), thème sombre bleu nuit. Pictos Lucide (licence ISC) rangés dans un
sprite SVG en tête de page. Sur ordinateur, barre latérale large dès 1 200 px (marque, rubriques Pilotage et
Analyse, Paramètres et Déconnexion en pied), rail d'icônes en dessous ; barre du bas sur téléphone.

### Paramètres

En tête, le bloc **Démarrer en 3 étapes** (le même que sur Aujourd'hui) tant qu'une étape manque. Puis,
dans l'ordre : les sources (Extension .fr, Zones ICANN, Sources et lectures), Telegram, Alertes, Avis IA.
Le reste est replié sous « Afficher les réglages avancés » : Écran
Veille, Noms réservés, Prénoms récents (Insee), SEO, Score de l'Explorer, Lien « Réserver »,
Calendrier, Apparence. La carte Version est en bas. Sur téléphone, Paramètres s'ouvre par l'icône en haut à droite de
chaque écran.

### Calendrier

Paramètres, Calendrier, « Créer l'adresse » : une URL secrète au format ICS, à ajouter comme
calendrier par URL dans l'agenda du téléphone. Elle contient les expirations et les libérations
estimées, avec un rappel la veille. Elle s'ouvre sans mot de passe : la changer si elle a fuité.
Les lignes du fichier sont pliées à 75 octets, comme le demande la RFC 5545 : les agendas stricts
(Apple, Thunderbird) lisent les longs noms et descriptions sans rien tronquer.

### Jetons d'accès

Paramètres, Jetons d'accès : tout ce qui entre dans l'appli sans mot de passe, un jeton par ligne avec
son libellé (modifiable), son type, sa date de création et sa dernière utilisation (notée au plus toutes
les 10 minutes). « Révoquer » coupe l'accès tout de suite. Plusieurs jetons par type (30 au plus), seule
leur empreinte est gardée, le jeton n'est montré qu'à sa création (`GET/POST /api/tokens`,
`PUT/DELETE /api/tokens/{id}`).

| Type | Préfixe | Ouvre |
|---|---|---|
| Notation IA | `dw_` | liste à noter, envoi des avis, et toutes les lectures |
| Lecture seule | `dwr_` | lectures de l'API (écrans, fiches, diagnostic), rien d'autre |
| Connecteur MCP | `dwm_` | le connecteur ChatGPT / Claude ci-dessous, en lecture seule |
| Calendrier | (lien) | le lien ICS du calendrier, listé ici pour être révoqué au même endroit |

Les jetons uniques d'avant (notation IA, connecteur MCP) sont repris tels quels par la migration 041 :
la tâche de notation et un connecteur déjà branchés continuent de marcher.

### Connecteur ChatGPT / Claude (MCP)

Paramètres, Jetons d'accès, type « Connecteur MCP », « Créer » : une adresse MCP secrète,
`https://<appli>/mcp/<jeton>`, montrée une seule fois. Un jeton par assistant (ChatGPT, Claude) permet
de couper l'un sans l'autre. Un assistant compatible MCP (ChatGPT en mode
développeur, Claude avec un connecteur personnalisé) lit alors l'appli avec huit outils :
`search` (Explorer), `list_names` (noms par longueur, catégorie et état, sans mot à chercher), `fetch` (fiche d'un nom), `today` (Aujourd'hui), `drops` (Veille), `top_list`
(top d'une liste), `ai_rated` (avis IA) et `watched` (noms surveillés).

- **Lecture seule** : chaque outil lit un écran déjà ouvert à l'API en lecture ; rien n'écrit, aucun
  réglage ni secret ne sort.
- **Jeton** : de type « Connecteur MCP » seulement ; un jeton de notation ou de lecture ne l'ouvre pas.
- **Journal** : le jeton de l'adresse est masqué dans le journal d'accès et dans le diagnostic.
- **Sans en-tête possible** (ChatGPT) : l'adresse secrète suffit, en « sans authentification ». Un
  client qui envoie des en-têtes peut aussi appeler `/mcp` avec `Authorization: Bearer <jeton>`.
- **Protocole** : JSON-RPC sur HTTP, réponses JSON simples, sans flux ni session.

### Écrans

Navigation, dans l'ordre : **Aujourd'hui** (l'appli s'ouvre toujours dessus), **Domaines**, **Veille**,
**Explorer**, **Portefeuille**, puis Aide et Paramètres (barre latérale sur ordinateur). Chaque écran a un
sous-titre d'une ligne sous son titre : Aujourd'hui, « Les meilleurs noms à prendre maintenant » ; Domaines,
« Les noms que je surveille » ; Veille, « Ce qui tombe et ce qui va tomber » ; Explorer, « Tous les noms
suivis, pour chercher » ; Portefeuille, « Les noms que je possède » ; Paramètres, « Sources, alertes,
réglages ». Le retour arrière ferme la fiche ouverte, sinon ramène à Aujourd'hui.

Domaines, sur ordinateur, est un tableau d'une ligne par cellule : dates, badges, nombres et en-têtes ne
passent jamais à la ligne, un nom trop long finit en « … » (nom entier au survol). Plutôt que d'écraser les
colonnes, l'écran les retire selon la largeur : Registrar et Dernier check à partir de 1 700 px seulement,
Prochain à partir de 1 280 px, le bouton Vérifier à partir de 1 100 px, Jours restants à partir de 1 024 px ;
tout reste dans la fiche. Sur téléphone, chaque domaine est une carte.

#### Aujourd'hui

L'écran d'accueil. En tête, la **période** : 24 h, 7 j ou 30 j en puces (24 h par défaut, choix gardé par le
navigateur). Puis les chiffres de la période : pépites (voir Alertes), noms libérés depuis la période, libération estimée dans la période
à venir (sans les noms dont la libération estimée est passée depuis plus de 3 jours : leur date est fausse, ils restent
dans la Veille et sont revus au registre), nouveaux avis « À prendre » reçus depuis la période sur un nom libre ou bientôt libre, regrets (noms
ignorés repris depuis par quelqu'un d'autre) ; les libellés disent la période (« libérés depuis 7 jours »,
« libérations sous 7 jours »). Avec 24 h, la libération à venir est sous 24 h (48 h auparavant) : même fenêtre
dans les deux sens. Chaque chiffre ouvre exactement ses noms, même compte : les pépites descendent à leur rubrique, dépliée ; les trois
premiers ouvrent la Veille sur cette liste seule, pour la même période (puce « Libérés depuis 7 jours »,
« Libération sous 7 jours » ou « Nouveaux « À prendre » depuis 7 jours », sans onglets ni filtres de la Veille,
noms faibles compris ; la puce efface le filtre et rend la Veille complète), les regrets ouvrent les Décisions du
Portefeuille filtrées sur « Repris seulement » (puce effaçable, aussi disponible à la main). Puis le bloc
**Démarrer en 3 étapes** (sources, Telegram, avis IA, avec « Configurer »), tant qu'une étape manque.
Puis la rubrique **Pépites** : celles devenues libres depuis la période (libération constatée, première
confirmation de libre, ou avis « À prendre » reçu sur un nom libre ; date gardée dans la table `pepites` une fois le
nom vu), puis celles dont la libération estimée tombe dans la période ; triées par note de revente puis prix
utilisateur final ; 10 par groupe, « Tout afficher » pour le reste ; cartes de nom de l'Explorer (état, prix Gandi,
Réserver, Ignorer) ; « Masquer les premiums » vaut aussi pour elles. Rien : « Aucune pépite sur 24 h ».
Enfin les 20 meilleurs noms libérés depuis la période ou libérés dans la période, non ignorés, sur la carte de la Veille, dans
l'ordre de la Veille pour l'usage choisi (Pour revendre / Pour exploiter, réglage commun) et avec ses
réglages par défaut (noms faibles et suites imprononçables masqués), et le bouton « Voir toute la Veille ».
Rien à prendre : « Rien à prendre aujourd'hui. Les prochaines libérations sont dans la Veille. » (sur 7 ou
30 jours : « Rien à prendre sur 7 jours. Les autres libérations sont dans la Veille. »).

`GET /api/today?sort=resale|exploit&period=24h|7d|30d` (défauts `resale` et `24h`, autre période : 422, calcul
hors de la boucle) : `period`, `counts` (`pepites`, `freed`, `coming`, `new_take`, `regrets`), `pepites` (`min` : le seuil ; `free` et
`coming` : éléments au format de l'Explorer, avec `pepite` : `state` `free`, `unconfirmed`, `premium` ou `coming`,
`since`, `release_at`, `price`), `items` (format des éléments de
`/api/drops`), `setup` (les trois
étapes : `key` `sources`, `telegram` ou `ai`, `title`, `text`, `done`). `/api/meta` donne les mêmes étapes
dans `steps`. Lisible avec le jeton en lecture seule, comme les autres écrans.

`GET /api/drops?focus=freed|coming|new_take&period=24h|7d|30d` : la réponse de `/api/drops` réduite aux noms du
chiffre d'Aujourd'hui (même calcul que `counts`, même période, `24h` par défaut), avec `focus` et `period` rappelés ;
une autre valeur : 422. Les anciens noms `freed_24h` et `coming_48h` restent acceptés.

#### Entreprises

« Les plus grandes entreprises françaises et leurs noms en .fr et .com : ce qui est libre, ce qui expire
bientôt. » Pour un audit de présence en ligne (repérer les trous d'une entreprise et les lui signaler), pas pour
revendre : ces noms restent à part, ni listes, ni notation IA, ni pépites, ni Veille, ni alertes, et rien n'est
acheté. Entrée dans la barre latérale (Analyse) et, sur téléphone, en haut de Paramètres.

- **Liste** : les 5 000 plus grandes entreprises actives, lues tous les mois par le NAS dans l'API de recherche
  d'entreprises de l'État (Annuaire des Entreprises : Insee Sirene, INPI ; 4 requêtes par seconde, la limite est
  de 7, environ 2 minutes). Unités légales des GE et des ETI, rangées par leur propre tranche d'effectif, la plus
  grande d'abord, puis par dernier chiffre d'affaires publié. La catégorie Insee est celle du groupe entier
  (toute filiale d'un grand groupe est « GE », plus de 10 000 unités légales) : elle filtre, elle ne range pas.
  Administrations, associations et organismes (catégories juridiques 1, 2, 7, 8, 9) écartés. L'effectif n'est
  publié que par tranches (« 5 000 à 9 999 salariés »). Au 27/10/2026 : 59 unités de 10 000 salariés et plus,
  80 de 5 000 à 9 999, 419, 721, 1 664 puis 2 057 de 250 à 499.
- **Noms vérifiés** : raison sociale (mots collés, et avec tirets s'il y en a plusieurs ; formes juridiques
  ôtées : L'Oréal donne loreal et l-oreal), son premier mot quand c'est une marque (STELLANTIS AUTO : stellantis ;
  pas un mot courant comme Société ou Électricité, qui ferait croire à un trou), sigle, nom commercial et
  enseignes du siège, et les noms ajoutés à la main dans la fiche (marque, ancien nom). En .fr et en .com.
  Environ 24 000 noms pour le top.
- **.com** : RDAP de Verisign (404 libre, 200 enregistré, avec création, échéance et registrar), 4 requêtes par
  seconde, 2 000 noms par passage de la tâche (toutes les 10 minutes) ; revu tous les 30 jours, chaque semaine à
  moins de 60 jours de l'échéance. Sans réponse nette (réseau, refus), rien n'est écrit : redemandé au passage
  suivant. **.fr** : fichier mensuel de l'Afnic (le RDAP .fr de l'appli est plafonné à quelques requêtes par
  minute), relu à chaque nouveau fichier ou quand de nouveaux noms .fr attendent (six heures au moins entre deux
  lectures, jamais pendant l'import .fr de l'appli) ; absent du fichier ou retiré : libre d'après l'Afnic, à
  confirmer avant de le signaler (noms réservés ou interdits). Un .fr enregistré se renouvelle chaque année.
- **Écran** : recherche par SIREN, SIRET, raison sociale ou nom commercial, avec auto-complétion (le top d'abord ;
  s'il rend peu, l'Annuaire des Entreprises : une entreprise hors du top s'ajoute d'un clic et ses noms sont
  vérifiés au passage suivant). Filtres « Nom libre » et « Expire bientôt » (moins de 60 jours), catégorie.
  Chaque carte : rang, raison sociale, sigle et noms commerciaux, catégorie, tranche d'effectif, chiffre
  d'affaires, ville, et ses noms (libre en vert, échéance proche en jaune). Dépliée : chaque nom avec son origine,
  son état, sa création, son échéance et son registrar ; SIREN, SIRET du siège, activité, effectif, chiffre
  d'affaires et résultat net, siège, création ; noms ajoutés ; lien vers l'Annuaire des Entreprises. « Pris » ne
  dit pas qui : le titulaire est masqué, peut-être un tiers.

`GET /api/companies?q=&gaps=free|expiring&category=GE|ETI|PME&limit=&offset=` (lisible avec le jeton en lecture
seule), `GET /api/companies/{siren}`, `GET /api/companies/suggest?q=`, `POST /api/companies/{siren}` (ajout
depuis l'Annuaire), `PUT /api/companies/{siren}/names` (`{"names": [...]}`), `DELETE /api/companies/{siren}`
(entreprise ajoutée à la main), `POST /api/companies/refresh` (relire la liste tout de suite). État dans
`companies` de `/api/diag`. Tables `companies` et `company_domains`.

### Veille et Explorer : deux écrans, mêmes réglages

Début de rédemption : la dernière modification RDAP, sauf si elle précède de plus d'une semaine la sortie de la
zone (ou du DNS) ; c'est alors le jour de sortie qui compte. Au .biz, cette modification est le renouvellement
automatique, 40 jours avant la suppression (45 jours de grâce) : la libération estimée était passée depuis deux
semaines et 67 noms restaient « libération sous 24 h » (27/10/2026). Les noms déjà en base sont redatés au démarrage.

**Veille** : ce qui tombe et ce qui va tomber. Deux onglets : **Libérés** (réservables maintenant)
et **Bientôt libres** (en rédemption ou en suppression, avec la date estimée). **Explorer** : tous
les noms suivis, pour chercher. Grande barre de recherche, puis l'état : **Libres**, **Bientôt
libres** ou **Tous, même pris**, et le type de nom en mots simples (mots courants, anglais,
prénoms, tech et startups, noms de médias, termes stars, métiers, sport, animaux, cuisine, courts…).

Réglages communs, gardés d'un écran à l'autre :

- **Pour revendre / Pour exploiter** : d'abord les noms que l'IA conseille (« À prendre », « À
  surveiller »), par sa note de revente ou d'exploitation ; puis les noms sans avis, par score ; à la
  fin, ceux qu'elle déconseille (« À laisser »). Pour revendre, les conseillés vont par note de A à E,
  puis par rendement attendu, puis par note de revente.
- **Masquer les noms faibles** (case cochée par défaut) : sans avis IA, score sous 30 ; avec un avis,
  « À laisser », même à bon score.
- **Types et catégories** : un, plusieurs ou tous, en puces à cocher (« Toutes » les efface). Plusieurs : union, un
  nom de l'une ou de l'autre liste (Prénoms et Noms de famille). Dans l'Explorer, `cat=firstname,surname` (une seule
  valeur reste acceptée, formes `letters`, `digits`, `mixed`, `hyphen` comprises) suit compteurs, tri, export CSV,
  vues enregistrées et mémoire du navigateur ; la longueur reste un filtre à part, la carte garde une case par
  catégorie. Les plus fréquents avec plusieurs listes : par meilleur rang relatif, `freq_list` dit la liste du rang.
  Noms de marque ou Mes ajouts cochés avec une autre liste : la langue ne filtre plus (ces listes n'en ont pas).
  Une catégorie compte aussi ses noms dérivés (voir « Listes de noms ») ; `cat=sector` (vues enregistrées, adresses
  anciennes) se lit comme `cat=premium`.
- **Période** (Veille) : « Tout » par défaut (tout ce que la Veille garde), ou 24 h, 7 j, 30 j : Libérés depuis la
  période, Bientôt libres dont la libération estimée tombe dans la période (sans date estimée : masqués). Filtre
  du navigateur, puce effaçable (« Libérés depuis 7 jours »), gardé avec les autres filtres.
- **Extensions** : une, plusieurs ou toutes, en puces à cocher. Depuis « Toutes », une puce choisit cette
  extension seule ; les suivantes s'ajoutent ou se retirent. Le choix est mémorisé (Veille comme Explorer) et
  suit dans l'Explorer les vues enregistrées et l'export CSV (`tld=fr,com`).
- **Avancé** (replié) : les filtres fins (nature, longueur, fréquence, SEO, avis IA, score
  minimum…), et dans l'Explorer les classements, la carte, les premiums et les vues enregistrées.

Chaque nom est une carte, la même dans Aujourd'hui, la Veille, l'Explorer et la recherche (gabarit `#name-card`,
fonction `nameCard` dans `app.js`) : le nom ; en pastille principale la valeur qui trie pour l'usage
en cours (note IA de revente ou d'exploitation, étiquetée « IA 75 », sinon le score, étiqueté
« score 53 »), l'autre valeur en retrait ; une phrase (verdict IA en pastille, « À prendre » vert, « À surveiller »
ambre, « À laisser » gris, puis le prix utilisateur final en texte normal ; sans avis, les raisons du score, par
exemple « 5 lettres · 153e mot le plus courant ») ; avec le tri « Les plus fréquents », le rang du nom dans sa
liste (« 12e prénom le plus donné ») ; l'état en toutes lettres
avec sa date (« probablement libre », « libre, vérifié il y a 3 h », « En rédemption, libération
~09/11 », « pris », « réservé », « premium probable ») ; les actions Réserver (nom libre ou
probablement libre), Vérifier (nom probablement libre), Surveiller ; une case de sélection ; les badges
« Surveillé » et « Portefeuille » (`watched` et `in_portfolio` des éléments de `/api/drops`, `/api/today`,
`/api/explore/rank` et de la fiche, calculés par le serveur). Un bouton
« ? » à côté de l'usage ouvre l'Aide sur les deux échelles (score et note IA).
Dans la Veille, **Nouveaux depuis ma dernière visite** (bouton et étiquette « Nouveau », même règle)
ne garde que les noms libérés ou entrés en rédemption depuis l'ouverture précédente de l'écran
(heure gardée par le navigateur ; à la première visite, depuis 24 h). Tri en masse : cases à cocher
(« Tout sélectionner » pour les noms affichés), puis Surveiller, Analyse IA, Revérifier ou Ignorer, avec
un seul bilan à la fin (« 3 vérifiés, 1 échec ») ; chaque carte a aussi son bouton « Ignorer ». Un nom
ignoré quitte la Veille mais reste dans l'Explorer ; le message propose « Annuler » (`POST /api/drops/unhide`
avec les noms), et Paramètres, « Tout réafficher », les remet tous. La décision est gardée (`decisions`),
comme l'ajout d'un nom au portefeuille : si un nom ignoré est ensuite enregistré par quelqu'un d'autre, la liste des
décisions le dit (« repris », avec la date quand elle est connue) et compte ces regrets. Cette liste est
en bas du Portefeuille, section « Décisions » (nom, décision, date, état du jour, bouton « Retirer »).
L'état des lectures (zones, .fr, DNS, historique) est dans Paramètres, carte « Sources et lectures » ; une
ligne d'état reste en haut de la Veille, qui distingue une erreur temporaire (réseau injoignable, registre
muet : nouvel essai à la lecture suivante) d'une source à configurer. Sur téléphone, seuls les onglets et le tri restent collants
en haut ; l'usage, la case des noms faibles, « Nouveaux », les extensions et les types sont dans le
panneau Avancé.

Fiche d'un nom, de haut en bas, la même sur téléphone et sur ordinateur (panneau d'environ 520 px à droite,
42 % de la largeur au plus) :

1. **En-tête** collant : le nom en grand, le bouton fermer « × » ; dessous, une seule ligne d'état : badge,
   la date qui compte (« libération ~17/11 », « libre depuis le 06/10 », « expire le 12/03/2027 ») et les puces
   « Suivi » et « Portefeuille ».
2. **Carte décision**, qui porte tout l'avis IA : les deux notes, revente et exploitation, celle de l'usage
   choisi (Pour revendre / Pour exploiter, réglage commun) en premier et en grand ; le verdict en pastille à
   droite ; les prix de revente rapide et utilisateur final ; le « pourquoi » de l'avis ; le commentaire. Une
   ligne « Détail IA » (grille sur 10, date de l'avis) déroule les neuf sous-notes en mini-barres ; elle reste
   ouverte ou fermée d'une fiche à l'autre. Sans avis : le score, « Pas encore d'avis IA », la date de notation
   prévue (ou la demande en cours) et les deux premières raisons du score. Avis de version 4 : « Avis à
   l'ancienne grille, renoté bientôt » ; premium probable : l'avertissement.
3. **Actions** en pictos (voir plus bas).
4. **Dates clés** (voir l'Explorer).
5. Le détail en **sections repliables**, chacune avec un résumé à droite du titre : Score (ouverte
   sur ordinateur ; barre, détail, raisons), Suivi (nom surveillé : état et date d'entrée, erreur de check avec
   le dernier état conservé, registrar, serveurs DNS et statuts EPP expliqués au survol quand il y en a, tags à
   ajouter ou retirer, signaux d'abandon avec « Analyser », source, dernier et prochain check, puis
   l'**Historique des changements**, table `history` ; l'expiration et la libération n'y sont répétées que si
   Dates clés ne les a pas), Le nom (catégories, nature, fréquence), Comparables et repères, Autres extensions
   (« 3 libres sur 8 » : nom, état, résumé de l'avis), Registre et SEO (dernière lecture RDAP ou WHOIS sauf
   pour un nom surveillé, prochaine lecture et sa raison, open data .fr, DNS, SEO). Les champs vides ou
   inconnus ne s'affichent pas. Libellés en casse normale sur une colonne étroite, au-dessus de la valeur
   sous 360 px.

Action mise en avant : Réserver pour un nom libre, Suivre pour un nom qui va se libérer, Analyser pour un nom
pas encore noté. C'est la seule vue détaillée d'un nom : dans Domaines aussi, un clic sur un nom ouvre la
fiche ; Vérifier y lance le check de Domaines. Le retour arrière ou Échap la ferme (Échap ferme d'abord le
menu « Plus » s'il est ouvert). Sur ordinateur, un clic hors de la fiche la ferme aussi ; un clic sur un autre
nom ouvre ce nom, un clic dans le message du bas ou une sélection de texte qui finit dehors ne la ferment pas,
et le premier clic dehors menu « Plus » ouvert ne ferme que le menu. Sur téléphone, la fiche est plein écran.

Actions de la fiche : une rangée de pictos sur une seule ligne, colonnes égales, comme la barre du bas, dont
l'allure suit l'état du nom. Réserver (en couleur pleine si le nom est libre ou probablement libre) ; Suivre,
ou « Suivi » (teinte verte) pour un nom surveillé, qu'un appui propose d'arrêter après confirmation ; Analyser,
picto « IA » en gras dans un cadre : en pointillés et « En cours » quand une analyse IA est demandée, plein et
« Analysé » quand un avis existe (un appui relance après confirmation) ; Vérifier, qui tourne pendant la
vérification ; « Plus » (trois points) ouvre un petit menu sous la rangée, au clavier comme à la souris (flèches,
Échap, clic dehors) : Analyse SEO, « Réservé par registre » (à cocher quand le registre refuse un nom que
l'appli croyait libre : il sort des noms à prendre ; « Pas réservé par registre » l'y remet), Voir dans
Portefeuille (nom du portefeuille), « Retirer de mes ajouts » pour un nom de Mes ajouts. Un nom hors des listes
suivies a, sous la rangée, une ligne « Hors des listes suivies » et le bouton « Ajouter aux noms suivis ». Arrêter le suivi se fait par le picto « Suivi », pas en double dans le menu. L'ajout au portefeuille se fait à la main, dans l'écran Portefeuille.

En .fr, un mot seulement anglais (should) ou une forme conjuguée (vais) perd une vingtaine de
points de forme et de sens par rapport au même mot bien placé : ils se lisent mal en .fr. La raison
est affichée sur la carte.

Menu Veille : les noms qui quittent les zones des extensions génériques (et le DNS de .co, .ai et
.io), suivis jusqu'à leur libération. Sont suivis tous les noms de 1 à 4 caractères (lettres, chiffres, tiret), les 20 000
mots français les plus courants (forme de base, sans accents), les noms de famille donnés plus
de 500 fois en France entre 1891 et 2000 (environ 21 700) et les prénoms donnés plus de 100 fois
entre 1900 et 2018 (environ 10 000), avec autant de noms de famille (recensement de 2010) et de prénoms
(naissances de 1880 à 2017) des États-Unis dans les mêmes listes. S'y ajoutent les 30 000 mots anglais les plus fréquents
(marques connues retirées), environ 580 mots des secteurs qui paient (assurance, crédit,
immobilier, droit, santé, travaux…), environ 200 mots en vogue et environ 35 000 noms composés
(monassurance, plombier-lyon, insurancehub, dallasplumber…), environ 5 500 noms de sites de
niche (forumdomotique, guidepotager, et autant en anglais : gardeningforum, dogtraining…), environ 2 800 noms
de médias (lequipe, mondedujardinage, et autant en anglais : golfdaily, thegardener…), environ 1 000 expressions
courantes françaises et anglaises (bonplan, faitmaison, goodvibes…), quatre listes thématiques (métiers,
sport, animaux, cuisine), cinq listes fournies (termes stars, mots-clés à forte valeur, métier + ville de France
et des États-Unis, marques inventées) et « Mes ajouts », les noms ajoutés depuis l'Explorer (voir « Listes de noms »). Une liste
ajoutée n'est cherchée dans une zone qu'à la lecture suivante, dans le fichier .fr qu'à l'import suivant,
dans le DNS de .co, .ai et .io qu'à la passe suivante : d'ici là, ses noms n'apparaissent pas pour cette
extension plutôt que d'y passer pour libres. Sources et licences des listes :
`app/data/SOURCES.md`. Un bouton Surveiller ajoute un nom à la liste principale.

#### Listes de noms

Chaque liste est un fichier de `app/data` (un nom par ligne, minuscules sans accents, du plus
courant au moins courant : le rang sert au score), déclaré dans `app/wordlists.py` (clé, libellé,
fichier). Un nom peut être dans plusieurs listes (avocat : mots, secteurs, métiers). Les catégories de l'Explorer
et de la Veille (puces, compteurs, carte, Top, vues enregistrées, export CSV, fiche) sont les listes et leurs noms
dérivés (ci-dessous) ; le filtre de langue suit la langue relevée dans `app/data/langues.txt` pour les listes mêlées.

| Clé | Libellé | Contenu |
|---|---|---|
| word, form | Mots, Formes fléchies | 20 000 lemmes et 10 000 formes du Lexique |
| surname, firstname | Noms de famille, Prénoms | Insee (France), et les listes des États-Unis qui y sont réunies (ci-dessous) |
| english | Mots anglais | 30 000 mots de wordfreq, marques retirées |
| sector, trend, startup | Secteurs porteurs, Tendances, Tech et startups | listes à la main ; Secteurs porteurs sans puce à l'écran (ses noms sont dans Termes stars), la liste reste pour le score et le calage IA |
| combo, niche, expr, media | Noms composés, Sites de niche, Expressions, Noms de médias | construites par `scripts/build_lists.py` (noms de médias : lesvoeux, mondedujardinage, frandroid) |
| job | Métiers | environ 500 métiers et professions, féminins et pluriels courants (plombier, notaire, infirmiere, avocats) |
| sport | Sport | environ 220 sports, disciplines, équipements et mots du sport (football, padel, raquette, marathon) |
| animal | Animaux | environ 220 animaux et mots des animaux de compagnie (chien, chaton, renard, croquettes) |
| food | Cuisine | environ 330 plats, ingrédients, boissons et mots de la table (recette, raclette, fraise, cafe) |
| premium | Termes stars | `premium.txt`, fourni |
| keyword | Mots-clés à forte valeur | `motscles.txt`, fourni (réunion de listes françaises et anglaises) |
| local | Métier + ville | `local.txt`, fourni (plombierparis) |
| brand | Noms de marque | `marques_inventees.txt`, fourni : lieux et légendes, mots étrangers, mots réels détournés, noms inventés, composés rythmés, mots-valises ; la sonorité compte, pas le sens |
| compound_en, compound_fr | réunies à Noms composés | `composes_en.txt` (100 000, appris des ventes publiques de noms de deux mots) et `composes_fr.txt` (45 705, têtes et queues propres à chaque groupe de mots, accordées au genre), écrits par `scripts/build_compounds.py` |
| invented | réunie à Noms de marque | `inventes.txt` : 7 642 noms « mot + twist » (shoply, techify, uptech), écrit par `scripts/build_compounds.py` |
| mine | Mes ajouts | `data/mes_ajouts.txt`, dans le dossier de données : les noms ajoutés depuis l'Explorer |
| firstname_us, surname_us | Prénoms et Noms de famille des États-Unis, sans puce | `prenoms_us.txt` (10 000 prénoms, Social Security Administration, 1880-2017) et `noms_us.txt` (21 689 noms, recensement de 2010), construits par `scripts/build_names_us.py` ; réunis à Prénoms et Noms de famille |
| niche_us, media_us | Sites de niche et Noms de médias des États-Unis, sans puce | `niches_us.txt` (2 775 : gardeningforum, dogtraining, allaboutdogs) et `medias_us.txt` (1 381 : golfdaily, healthinsider, thegardener), en anglais, construits par `scripts/build_lists.py --us` ; réunis à Sites de niche et Noms de médias |
| local_us | Métier + ville des États-Unis, sans puce | `local_us.txt`, fourni (7 500 : scottsdaledentist, dentistscottsdale), écrit par `scripts/build_local_us.py` ; réuni à Métier + ville |

**Prénoms et noms des États-Unis** : autant que de prénoms et de noms de l'Insee, dans les mêmes catégories
(`wordlists.MERGED`) : puces, compteurs, filtres, Top, export et fiche ne connaissent que Prénoms et Noms de famille,
un nom des deux pays (thomas, martin) y est un seul nom ; `cat=firstname_us` se lit comme `cat=firstname`. Chaque
source garde ses rangs (`ranks`, `freq_list`) :
- score : le rang de chaque source sur la taille de sa liste, le meilleur des deux (le 50e prénom des États-Unis
  pèse comme le 50e prénom français ; à égalité, le français), avec le poids des prénoms et des noms français
  (`wordlists.SCORE_AS`). Un nom seulement français garde exactement son rang et son score. Raison : « 396e prénom
  le plus donné aux États-Unis », « 1er nom de famille le plus porté aux États-Unis » ; « 2e prénom le plus donné »
  pour un prénom français ;
- ordre « les plus fréquents », Top, filtre de fréquence : le meilleur rang relatif des deux sources, le Ne des
  États-Unis juste après le Ne de France (`wordlists.relative`) ; la carte dit le rang de la source retenue ;
- langue : français pour l'Insee, anglais pour les États-Unis, les deux pour un nom des deux listes. Les prénoms
  récents (Insee, ci-dessous) restent ceux de la France.

**Sites de niche, noms de médias et métier + ville des États-Unis** : autant de noms en anglais qu'en français dans
ces trois catégories, chacun ayant un sens pour un acheteur américain, réunis de la même façon (`wordlists.MERGED`) ;
les listes françaises n'y perdent ni ne changent aucun nom (les sites de niche anglais ont quitté `niches.txt` pour
`niches_us.txt` : les noms français y gardent leur ordre). Score : le poids des sites de niche, des médias et, pour métier + ville, des noms composés ;
raison « 12e site de niche des États-Unis ». Rang relatif : celui de la liste (le 100e de 7 500 métier + ville des
États-Unis près du 200e de 15 000 de France). Langue : anglais (`langues.txt`). Motifs et règles :
`app/data/SOURCES.md`.
- Sites de niche (`niches_us.txt`, 2 775) : thème + format qui lui va, la forme du thème devant un nom au singulier
  (dogforum, chickenforum ; plus de dogsforum ni de chickenszone), formats placés devant avec le thème nommé
  (allaboutdogs, easybaking, learngolf) ; un format ne va qu'aux thèmes où il a un sens (dogtraining, sailinglessons,
  coffeereviews ; pas de dogreviews).
- Noms de médias (`medias_us.txt`, 1 381) : thème + tête de média (golfdaily, healthinsider, techweekly,
  worldofgolf) et the + amateur ou lieu (thegardener, theworkshop, thefairway).
- Métier + ville (`local_us.txt`, 7 500, fourni) : 151 villes et métropoles, 148 services locaux, la ville devant
  puis derrière (scottsdaledentist, dentistscottsdale), en plus des 4 368 noms service + ville des combinaisons
  (dallasplumber), qui y restent : environ 11 900 noms en anglais, autant qu'en français (11 855).
- Marques, titres et sites connus écartés (`scripts/lists/connus_en.txt` : golfdigest, businessinsider, boardgamegeek,
  cartalk ; marques de `marques.txt`) ; un nom déjà dans une autre liste n'y entre pas.

**Catégories dérivées** : un nom garde ses listes d'origine et reçoit en plus les catégories que lui donnent des
règles, calculées par `scripts/build_lists.py` (`--provided`, `--themes`, `--us` et la construction complète finissent
par là) dans `app/data/categories.txt` (nom, catégories gagnées), lu par `app/wordlists.py` (`classes`, `categories`).
Elles servent à classer et filtrer seulement : rangs, traits et score restent ceux des listes d'origine (`lists`,
`ranks`), et l'empreinte de `scores.bin` ne couvre pas ce fichier (table identique octet par octet avant et après,
test `test_derived_categories_do_not_change_the_score`).

- Secteurs porteurs et tendances sont aussi des termes stars (assurance, credit, mortgage, padel).
- Un nom composé est aussi un mot-clé à forte valeur quand elle associe une racine commerciale (secteur, terme
  star) à une intention de recherche (pas cher, discount, en ligne, comparateur, best, finder, online…) ou à
  une autre racine : assurancepascher, bestinsurance ; devis et quotes seulement pour un métier ou un service
  (assurancedevis, moversquotes ; pas matelasdevis) ; pas les possessifs ni les ajouts de média ou de marque
  (monassurance, mytravel, insurancehub, hairexpress, assurancepro, buyteeth).
- Un nom composé ou un mot-clé de forme métier + ville est aussi Métier + ville : plombierlyon, plombier-lyon,
  arlingtonlawyer, fortworthdentist ; ville d'abord seulement pour une ville américaine et pour immo, immobilier,
  auto, garage (lyonimmobilier). Villes de France et régions de `scripts/lists/metier_ville.txt` (celles de
  `local.txt`), villes américaines de `scripts/lists/cities_us.txt` et `metier_ville_us.txt` ; pas de numéro de
  département (24 : 24 h).
- Un nom composé (noms composés, mots-clés, métier + ville, termes stars, sites de niche, expressions, noms de
  médias) est découpé en vrais mots (Lexique, formes, anglais, thèmes, villes, affixes ; la découpe la plus économe,
  toutes ses variantes d'accord) : un mot de Métiers, Sport, Animaux, Cuisine, Tech et startups ou Tendances lui
  donne ce thème (coachsportif : Sport et Métiers, assuranceanimaux : Animaux, aiagents : Tech et startups et
  Tendances). Jamais par sous-chaîne (marathon n'est pas de la cuisine) ; un mot de 3 lettres ne compte qu'entre de
  vrais mots (petsitting oui, colo+cat+ion non) ; mots trop généraux ou ambigus écartés (`WEAK` : bear, terrain,
  the, chat, expert…) ; Tech et startups par ses seuls mots techniques (`STRONG` : agents, cloud, api, logiciel…) ;
  cuisine et kitchen dans une liste commerciale sont le cuisiniste. Faux positifs mesurés sur 50 noms tirés au
  hasard par catégorie : 0 à 2 par thème (devisrobotpiscine en Tech et startups), 4 pour les mots-clés
  (terraindiscount), 0 pour métier + ville.
- Jamais vers les mots, formes, anglais, noms de famille, prénoms, marques inventées ni Mes ajouts.

Ordre « les plus fréquents » et Top : un nom dérivé prend son rang relatif dans les listes d'où vient la catégorie
(assurance, 1er terme star, parmi les dix premiers termes stars) ; dans un thème, les noms composés gagnés
viennent après les mots du thème. Le filtre de fréquence (top N) leur applique ce même rang d'origine. Comparables
de la fiche : la liste principale reste la première liste d'origine.

**Listes fournies** (premium, keyword, local, local_us, brand) : rédigées ailleurs, une entrée par ligne, en `[a-z0-9]`, de
la plus à la moins précieuse, jusqu'à environ 100 000 entrées en tout. `scripts/build_lists.py` ne les écrit
jamais : `python scripts/build_lists.py --provided` en vérifie le format (une entrée hors format ou en double est
signalée, et écartée par l'appli) et relève leur langue dans `langues.txt` : celle d'un mot déjà connu, sinon
celle des mots qui composent le nom (plombierparis : français ; carinsurance : anglais), sinon aucune. Les marques inventées n'ont pas de langue.
Un fichier absent vaut une liste vide. Dans le
score, leur rang compte comme celui des autres listes, avec le poids appris pour la liste la plus proche
(termes stars et mots-clés : secteurs porteurs ; métier + ville : noms composés ; marques inventées : tech et
startups). Une marque inventée n'est pas un « vrai mot » : sa forme se juge à la sonorité et elle ne gagne
aucun point de sens, mais son rang lui en donne : pas de classement « faible » d'office (sur l'échantillon,
de 30 à 41 selon le rang et l'extension, 30 étant le seuil des noms faibles).

Pour brancher les vrais fichiers : les copier dans `app/data` (mêmes noms), puis
`python scripts/build_lists.py --provided && python -m app.precompute` (catégories dérivées recalculées au
passage ; scores.bin, environ 70 s pour 250 000 noms ; sans lui, chaque démarrage recalcule). Au premier démarrage, le fichier .fr est relu (les listes ont
changé) et chaque zone cherche ces noms à sa lecture suivante.

**Mes ajouts** : dans l'Explorer, un nom cherché absent des listes (« hors des listes suivies ») porte le bouton
« Ajouter aux noms suivis », sur la carte de recherche comme dans sa fiche. Son libellé, sans l'extension, est
normalisé (minuscules, sans accents, lettres, chiffres et tirets, 1 à 63 caractères ; sinon refus, 422) et
rangé dans `data/mes_ajouts.txt`. L'univers de l'Explorer est rechargé sans redémarrage (environ 1 à 2 s) :
le nom a un score (sans point pour son rang, l'ordre d'ajout ne classe rien), il entre dans les zones à leur
prochaine lecture, le fichier .fr au prochain import (le DNS du .fr l'interroge dès la nuit suivante), le DNS
de .co, .ai et .io en tête de la passe suivante, la passe Gandi et la file IA comme les autres noms. D'ici là,
le nom demandé (avec son extension) est vérifié au registre au moment de l'ajout ; « Vérifier » fait de même
pour les autres extensions. Retirer : menu « Plus » de la fiche, « Retirer de mes ajouts ».

`POST /api/explore/mine` (`{"name": "monsuperprojet.fr"}`, l'extension est facultative) : `label`, `added`
(false s'il y était déjà), `count`, `check` (l'élément de Vérifier pour le nom avec extension, sinon null),
`check_error` (la vérification a échoué, l'ajout est fait), `seconds` (rechargement). `DELETE
/api/explore/mine/{label}` : `label`, `removed`, `count`, `seconds` ; 404 s'il n'y est pas. Session seulement :
le jeton en lecture seule n'y a pas accès. `/api/explore/name` et la fiche portent `mine` (dans Mes ajouts).

Les listes thématiques sont en français ; l'équivalent anglais n'y est ajouté que s'il manque à la
liste anglaise (roofer, petshop, dumbbell), sinon il garde son rang dans celle-ci. Sources :
`scripts/lists/metiers.txt`, `sport.txt`, `animaux.txt`, `cuisine.txt`, copiées par
`python scripts/build_lists.py --themes` (sans Lexique ni wordfreq), qui complète aussi
`langues.txt`. Les listes à la main `app/data/sigles.txt` (sigles courants : tv, cv, rh, gps) et
`app/data/marques.txt` (marques connues, aussi retirées de la liste anglaise) servent au score,
pas à la surveillance.

**Prénoms récents** : `prenoms.txt` classe les prénoms sur 1900-2018. Les années récentes viennent du
fichier des prénoms de l'Insee, publié une fois par an avec les naissances jusqu'à l'année précédente
(parfois deux ans avant) : l'année en cours n'existe jamais. Le NAS le télécharge une fois par mois
(`app/prenoms.py`, nouvel essai le lendemain après une erreur). Découverte : l'adresse collée dans
Paramètres, carte « Prénoms récents (Insee) », « Adresse du fichier », si elle existe ; sinon un fichier
national direct sur data.gouv.fr ; sinon les pages de l'Insee : l'outil interactif sur les prénoms (qui
renvoie vers la dernière édition) et la page citée par data.gouv.fr, où l'appli prend le fichier national
CSV dont le nom porte l'année la plus récente (`prenoms-2025-nat_csv.zip`, édition parue le 18 juin 2026,
naissances jusqu'à 2025). Vérifié sur les vrais sites en octobre 2026 : data.gouv.fr ne pointe que vers
une page d'une ancienne édition (2022), d'où ce passage par l'Insee. Le fichier est lu en flux puis effacé ; colonnes repérées par
leur intitulé (ancien format `sexe;preusuel;annais;nombre`, récent `sexe;prenom;periode;valeur`) ; prénoms
rares (« XXXX », « _PRENOMS_RARES ») écartés ; même normalisation que `prenoms.txt`, filles et garçons
additionnés. Reste `data/prenoms_recents.json` : les 2 000 prénoms les plus donnés de chacune des trois
années les plus récentes. Il ne change ni les listes suivies ni le score : un prénom récent absent de
l'univers de l'Explorer (ni nom court, ni nom d'une liste) n'est pas suivi, la carte le compte. Si
l'Insee renomme ses colonnes, la lecture s'arrête et la carte affiche les colonnes lues ; le classement
précédent reste en place. La carte donne l'année couverte, le nombre de prénoms, l'heure de la dernière
lecture et l'erreur éventuelle ; « Mettre à jour » relance la lecture (`POST /api/prenoms/refresh`,
état dans `prenoms` de `GET /api/settings` et de `/api/diag`).

Source : les fichiers de zone publiés chaque jour par les registres via CZDS d'ICANN, gratuits sur
inscription. Le .fr et les autres extensions nationales n'y sont pas.

1. Créer un compte sur czds.icann.org et demander l'accès aux zones voulues. Chaque registre valide
   la demande, en général en quelques jours. Un accès accordé expire au bout d'un certain temps et
   se renouvelle sur le même site.
2. Paramètres, carte « Zones ICANN » : identifiant et mot de passe du compte, puis Enregistrer.
   L'appli vérifie le compte et affiche les zones autorisées. Un clic sur une zone l'active ou non.
3. La lecture se fait une fois par jour, à l'heure choisie (6 h par défaut), ou tout de suite avec
   « Lire les zones maintenant ». La première lecture pose la référence : les premiers noms sortis
   apparaissent le lendemain.

Fonctionnement : chaque zone est lue en flux, sans être stockée. Seuls les noms suivis sont
retenus : une carte de 240 Ko par extension pour les noms courts, un fichier texte pour les noms
des listes, rangés dans `data/zones/`. Un nom présent la
veille et absent ce jour est vérifié en RDAP : en rédemption ou en suppression, il est affiché avec
sa date de libération estimée ; suspendu pour une autre raison, il est masqué. Un nom qui revient
dans la zone (renouvelé ou repris) sort de la liste.

Écart de zone : une lecture où plus de 5 % des noms connus disparaissent d'un coup est refusée (zone
tronquée ou incomplète, le plus souvent) ; la carte précédente est gardée et rien n'est marqué supprimé.
La carte « Sources et lectures » signale l'extension refusée, avec le nombre attendu et le nombre lu.
« Accepter l'écart » fait passer la lecture suivante de cette extension malgré l'écart, une seule fois :
le garde-fou revient ensuite.

La zone .com pèse plusieurs Go compressés : compter un gros téléchargement par jour et plusieurs
minutes de calcul sur le NAS. Elle est lue comme les autres zones ICANN, une fois par jour ; le .com entre dans
l'Explorer à sa première lecture (la migration 042 efface les traces du mécanisme d'attente d'avant l'accès CZDS :
noms .com interrogés dans le DNS et réglages devenus inutiles). La recherche est répartie sur tous les cœurs sauf un (4 au plus) ;
la décompression reste sur un seul. Le mot de passe ICANN est stocké dans la base sur le NAS, puisque
l'appli en a besoin chaque jour.

#### Le .fr

Le .fr n'est pas dans CZDS. Paramètres, carte « Extension .fr (Afnic) », case « Suivre le .fr ».
L'appli fonctionne alors en trois temps :

1. Une fois par mois, elle importe la liste ouverte des noms en .fr publiée par l'Afnic (Licence
   Ouverte, environ 120 Mo). Elle prend le fichier du mois le plus récent sur afnic.fr
   (`AAAAMM_OPENDATA_A-NomsDeDomaineEnPointFr.zip`), sinon le cherche sur data.gouv.fr.
   Une adresse collée dans « Adresse du fichier » qui contient un mois est suivie de mois en
   mois. Elle ne garde que les noms suivis. Chaque nom apparu comme supprimé depuis le fichier
   précédent est vérifié, quelle que soit sa date de suppression : le fichier arrive avec
   plusieurs semaines de retard. Au premier import, faute de fichier précédent, les suppressions
   des 90 derniers jours.
2. Un nom .fr expire à la date anniversaire de sa création. L'appli le vérifie en RDAP le
   lendemain de cet anniversaire : renouvelé, la vraie échéance lue en RDAP fixe la vérification
   suivante, un an plus tard ; en rédemption ou libre, il passe dans l'écran Veille.
3. Toutes les requêtes vers l'Afnic (dates anniversaire, suppressions, boutons « Vérifier », et les
   .fr de l'écran Domaines) passent par une seule file, sous un plafond par minute réglable (2 par défaut, soit 2 880 par
   jour au plus). L'Afnic ne publie pas de limite. Au premier signal de trop de requêtes, pause
   d'une heure, doublée à chaque nouveau signal (24 h au plus), et durée demandée par l'Afnic
   respectée. Paramètres, carte « Sources et lectures », montre en direct le nom en cours, le travail du jour et du
   lendemain, et les dernières requêtes. Pendant la pause, un domaine .fr de l'écran Domaines garde son
   dernier état et sa vérification est reportée à la reprise ; le plafond de la minute atteint, elle attend
   la minute suivante.
4. Chaque jour à partir de minuit, l'appli demande directement aux serveurs DNS du .fr si les
   noms « probablement libres » y sont délégués (150 000 par jour au plus, de 2 à 4 requêtes par
   seconde selon les réponses, ralenti au premier silence, arrêt à 23 h). La passe est étalée
   sur la journée : rythme plus doux pour le registre, moins de risque de blocage. Ordre : meilleur score de base d'abord ;
   score de base 40 et plus revu chaque nuit, les autres chaque semaine. Un nom délégué passe en
   « Enregistré » dans l'Explorer sans attendre le fichier mensuel. Pas de résolveur
   intermédiaire : seule l'Afnic voit les requêtes. Aucun quota RDAP consommé. Limite : un nom
   enregistré sans serveurs DNS passe pour libre ; le bouton « Vérifier » (RDAP) tranche.
   Chaque échec est journalisé avec sa cause (délai dépassé, réponse tronquée, refusé, code
   d'erreur, erreur réseau) et le serveur interrogé ; les 30 derniers sont visibles dans
   Paramètres, carte « Sources et lectures ». Un refus du serveur ou 20 échecs d'affilée arrêtent la passe, divisent par deux
   le plafond de rythme des nuits suivantes et sont signalés dans la rubrique Santé du message du jour. Le plafond remonte
   de 25 % par nuit sans incident.
   Désactivable : case « Interroger le DNS du .fr chaque nuit ».
5. .co, .ai et .io : ces extensions de pays ne publient pas leur zone. Chaque jour, en même temps
   que le .fr, l'appli interroge leurs serveurs DNS pour les 50 000 meilleurs noms (plus de 2
   caractères), jamais interrogés d'abord : toutes les listes pour .co (vendu comme le .com) ; Mots
   anglais, Tendances, Tech et startups et Marques inventées pour .ai et .io. Mes ajouts sont toujours
   interrogés, en tête, quel que soit leur score. Mêmes garde-fous, propres à chaque
   extension. Dès la première passe, chaque extension apparaît dans l'Explorer : délégué, « Enregistré » ;
   absent, « Absent du DNS, à vérifier » ; pas interrogé, hors des listes et des compteurs. Absent du DNS
   ne veut pas dire libre : le nom peut être enregistré sans serveurs DNS, réservé ou premium du registre,
   et Vérifier tranche ; seuls les 50 000 meilleurs noms par extension sont interrogés. Un nom délégué
   qui ne l'est plus (« sorti du DNS », souvent un nom qui expire) entre dans la Veille comme un nom
   sorti d'une zone : vérifié auprès du registre (RDAP, sinon WHOIS), puis Libérés ou Bientôt libres
   selon son état ; revenu dans le DNS, il en sort. Un nom absent dès sa première interrogation n'y
   entre pas, et une vérification à la main n'en fait pas un nom « sorti du DNS » (la date de sortie
   n'apparaît que pour un nom vu délégué puis absent). Les noms sortis avant cette version sont repris au démarrage et à chaque passe. Le message
   du jour les liste sous « Sortis du DNS », sauf ceux déjà listés avec leur état dans une
   autre rubrique. Pas de date de libération estimée (délais de ces registres non relevés). Paramètres,
   « Sources et lectures » : une ligne par extension, noms interrogés, présents et absents, date de la
   dernière passe.

Le fichier de l'Afnic ne donne pas de date d'expiration, d'où le calcul par la date de création.
Un nom sans date de création dans le fichier n'est pas suivi. Ses colonnes
sont repérées par leur intitulé : si l'Afnic les renomme, l'import s'arrête et affiche les colonnes
lues. Pour le .fr, la libération est estimée à 30 jours après l'entrée en rédemption, sans période
de suppression distincte (guide des procédures Afnic, voir `app/tld_delays.toml`).

#### Revérifications

Le moins de requêtes possible : un nom n'est revérifié que quand quelque chose doit changer.

| État | Vérification suivante |
|---|---|
| Rédemption, suppression en cours | à la libération estimée, puis toutes les 6 h tant qu'il n'est pas libre |
| Expiré (période de grâce) | une fois par semaine, ou à la date estimée si elle est plus proche |
| Suspendu (hold) | une fois par semaine |
| Libre, repris | jamais automatiquement : boutons « Revérifier » et « Surveiller » |

Un nom libre reste dans la Veille 30 jours (réglable dans Paramètres). Rien n'est effacé de la
base : passé ce délai, il reste visible dans l'Explorer. La Veille a les mêmes filtres avancés que
l'Explorer, gardés d'une visite à l'autre avec le tri et la case des noms faibles. Tri : les meilleurs
d'abord (selon revendre ou exploiter), les plus récents, la libération la plus proche (Bientôt libres
seulement : grisée dans Libérés, où la liste suit les meilleurs, et de retour sur Bientôt libres), ou les plus
fréquents (rang Insee pour prénoms et noms de famille, rang de fréquence pour les mots ; avec une seule
liste cochée dans Avancé, son rang ; sinon le meilleur rang toutes listes confondues). Un tri ne retire
aucun filtre, un filtre ne change pas le tri. Chaque nom ouvre la même fiche.
Domaines (un clic sur un nom ouvre sa fiche) a aussi une colonne Valeur (score, verdict IA en pastille et prix
utilisateur final), triable, des filtres par extension et
par verdict IA, et une sélection multiple (case devant chaque nom, « Sélectionner » pour les lignes
affichées) avec Vérifier et Analyse IA groupés, bilan en un message (« 3 vérifiés, 1 échec »).

Quand les listes suivies changent (formes fléchies ajoutées, par exemple), le dernier fichier
.fr est relu automatiquement : sans ça, les nouveaux noms passeraient pour jamais enregistrés. D'ici là, et
pour Mes ajouts (qui ne font pas retélécharger le fichier : ils attendent l'import suivant), un nom pas encore
cherché dans le fichier est masqué dans l'Explorer (noms cherchés à chaque import : `data/zones/fr.searched`) ;
la passe DNS du .fr l'interroge quand même, et un nom délégué passe « Enregistré ».

#### Portefeuille

Les noms que tu possèdes : prix et date d'achat, renouvellement par an, échéance, prix demandé,
meilleure offre, statut (possédé, en exploitation, pas à vendre, en vente, vendu, abandonné), prix et date de vente, notes. Totaux :
investi, renouvellements par an, ventes, bénéfice. Un nom s'ajoute à la main, avec le formulaire
de l'écran. Un nom ajouté est aussi surveillé : la vérification RDAP part en arrière-plan, la
réponse n'attend pas (`checking`), et l'échéance réelle remplace la date saisie dès qu'elle est lue ;
rappel dans le message du jour 30 puis 7 jours avant. Les ventes et les offres reçues sont ajoutées à
la consigne de notation IA comme repères de prix réels, et l'estimation IA s'affiche à côté du prix
obtenu. Bilan par année (`years` de `GET /api/portfolio`) : investi (achats), renouvellements (un par an
après l'année d'achat, jusqu'à la vente ou l'année en cours ; rien pour un nom abandonné, sa date
d'abandon étant inconnue), ventes, bénéfice. Chaque nom porte des liens de mise en vente sans API
(`sale_links`) : recherche Sedo, page du nom chez Dan et chez Afternic, le nom dans l'adresse (formats
relevés sur les sites ; Dan.com rejoint Afternic, son lien peut y renvoyer). `GET /api/portfolio/export` :
le portefeuille en CSV (point-virgule, BOM pour Excel), colonnes nom, statut, dates et prix, échéance et
sa source, bénéfice, avis IA (verdict, note de revente, prix final), notes. Réservé à la session : le
jeton en lecture seule n'y accède pas. Dans l'écran : tableau « Par année », bouton « Export CSV »,
liens « Mettre en vente » (Sedo, Dan, Afternic, dans un nouvel onglet : des liens de recherche, pas une
mise en vente automatique), message de confirmation à chaque changement de statut ou de prix ; « J'ai
acheté » depuis la fiche affiche un message avec « Voir dans Portefeuille » ; à l'ajout, « vérification du
registre en cours » tant que l'écran n'a pas été rechargé.

#### Explorer

Tous les noms suivis, extension par extension : les 1 926 220 noms possibles de 1 à 4 caractères,
puis les mots, noms de famille et prénoms. L'état vient des données déjà sur le NAS, sans aucune
requête : zones ICANN lues chaque jour, open data de l'Afnic, DNS de .co, .ai et .io (les 50 000
meilleurs noms de chacune, voir « Le .fr », point 5), données de la Veille par-dessus. Une extension
DNS change de version à la fin de chaque passe : l'Explorer se recalcule alors.

- **Top** : les 10, 50, 100, 200, 500 ou 1 000 premiers d'une catégorie (termes stars avec les secteurs
  porteurs et les tendances, métiers, sport, animaux, cuisine, tendances, sites de niche, mots-clés, métier + ville,
  marques inventées, Mes ajouts
  dans l'ordre d'ajout, mots, anglais, prénoms, noms…), pris ou non, avec l'état sur chaque extension en toutes lettres
  (probablement libre, pris, à venir, libre, réservé, premium probable). Un état ouvre la fiche. Les
  puces d'état de la liste sont masquées tant que le Top est ouvert : il montre tous les états.
  À côté de « Prénoms » (France 1900-2018, États-Unis 1880-2017), « Prénoms 2024 » (l'année la plus récente du fichier de l'Insee,
  voir « Listes de noms »), dès que le fichier a été lu : rang de l'Insee, nombre de naissances au
  survol, prénoms hors de l'univers sautés (`cat=firstname_recent` de `/api/explore/top`, avec `year`
  et `count` ; 409 tant qu'aucun fichier n'est lu ; `recent` de `/api/explore/rank` donne `key`, `year`
  et `label`, ou null).
- **Langue** : filtre Français / Anglais (Explorer et Veille). Listes mêlées : langue relevée à
  la construction (app/data/langues.txt) ; jargon des tendances absent des deux listes : les deux.
  Prénoms et noms de famille : français pour ceux de l'Insee, anglais pour ceux des États-Unis, les deux pour un
  nom des deux listes (`lang=en` avec Prénoms : bernice, james, thomas ; pas jean-pierre). Sans langue : marques
  inventées ; Mes ajouts gardent celle de leurs autres listes ; le Top « Prénoms 2024 » (Insee) ignore la langue.
- **Fiche** : nuit prévue pour l'avis IA (place dans la file, 1 000 noms par nuit, horizon de 30
  nuits) et, pour un .fr, la prochaine interrogation DNS et la dernière réponse.
- **États** : Libres (libres et probablement libres), Bientôt libres (triés par libération la plus
  proche), Tous, même pris. Compteur sur chaque état avec le niveau « Tout » et sans score minimum ;
  avec un niveau, il est masqué : le compte exact demanderait de noter chaque nom à chaque clic.
- **Premiums probables** : noms de 3 caractères libres hors .fr, souvent vendus au prix premium
  par le registre ; Gandi confirme ou dément (voir Gandi plus bas). Visibles par défaut, comme les premiums vus
  chez Gandi ; case « Masquer les premiums » pour les retirer (la carte compte les premiums, comme la puce
  « Libres »). Pas de prix tant que Gandi n'a pas répondu : le prix affiché est toujours celui de Gandi.
  .com et .net n'ont jamais de premium chez le registre (prix unique imposé à Verisign) : la règle ne s'y applique pas.
- **Carte** : extension × catégorie (1 à 3 caractères, 4 lettres, mots, formes fléchies, noms,
  prénoms), nombre de noms réservables par case, le même compte que la puce « Libres » de la liste
  (libération à venir exclue). Une case ouvre la liste filtrée et referme la carte.
- **Liste** : les meilleurs d'abord (selon revendre ou exploiter), libération la plus proche (état Bientôt
  libres seulement), les plus courts, les plus fréquents, ou score seul. Les plus fréquents : avec le type
  Prénoms ou Noms de famille, du plus donné au moins donné (Insee), « Masquer les noms faibles » coché ou non ;
  chaque carte dit le rang (« 12e prénom le plus donné »). Un type ne remet pas la longueur ni la nature à
  zéro. Avec le type Prénoms et le fichier de l'Insee lu,
  « Les plus donnés en 2024 » (`sort=recent`) : prénoms de l'année la plus récente dans l'ordre de l'Insee,
  tous les filtres appliqués, puis les autres par fréquence ; un prénom classé porte la raison « 12e prénom
  donné en 2024 », sans point de score. Une carte ouvre la fiche du nom.
- **Fiche** : la même dans Domaines, Veille et Explorer. État, score détaillé, raisons,
  catégories et nature en français, fréquence (rang dans chaque liste, « 442e mot anglais le plus courant »), historique .fr, SEO, dernière réponse RDAP ou WHOIS (registrar,
  dates, statuts, DNS ; pour un nom surveillé, la section Suivi la remplace), autres extensions (avec le
  résumé de leur avis IA : verdict, note de revente, prix final), et les actions (Vérifier, Réserver chez
  Gandi, Suivre ou arrêter le suivi, Analyser ; SEO et Réservé dans le menu « Plus »). **Comparables** : jusqu'à trois noms déjà notés avec la grille sur la
  même extension, de la même liste principale (pour un nom court hors des listes : même forme et
  même longueur) et de longueur proche (un caractère d'écart au plus), les plus proches par note
  de revente (sans avis sur le nom, par l'estimation du score) ; avec, pour chacun, verdict, notes,
  prix final et état. **Repères** : trois ventes ou offres reçues du portefeuille, les plus proches
  en longueur. **Dates clés** (ligne de temps, juste sous les pictos, ouverte partout ; au-delà de six dates, les trois
  dernières passées et toutes celles à venir, « Tout afficher » pour le reste) : tout ce que la
  base sait du nom, daté et trié : création (Afnic ou registre) et suppression, expiration, sortie de
  zone, entrée en rédemption ou en suppression, libération constatée, reprise, avis IA (noté, renoté),
  surveillance et changements d'état ou de registrar, achat et vente ; puis, après un trait
  « aujourd'hui », ce qui est à venir : expiration, libération estimée, prochaine vérification. Dans l'avis IA, « Pourquoi » (`why`) quand l'avis
  est réservé, et dans la carte décision la mention « Avis à l'ancienne grille, renoté bientôt » pour un avis de version 4 ; les
  cartes de la Veille et de l'Explorer ajoutent ce « pourquoi » en fin de phrase.
- **Avancé** (panneau du bas sur téléphone) : catégorie, nature des mots (verbes, noms communs,
  adjectifs, adverbes, mots outils), longueur, fréquence (top 100 à top 5 000), pris sur une
  autre extension, SEO (analysé, PageRank 1, 3 ou 5 et plus, archivé 5 ans et plus), score
  minimum. Les compteurs suivent les filtres ; les filtres sont mémorisés. Le filtre SEO ne
  porte que sur les noms déjà analysés.
- **Extensions** : une ou plusieurs à la fois (« Toutes » dès qu'il y en a deux). Les listes sont
  fusionnées dans l'ordre du tri choisi, les compteurs additionnés.
- **Vues enregistrées** : un jeu de filtres nommé, rappelé d'un geste. La vue en cours est
  surlignée ; sa suppression demande confirmation et laisse les filtres affichés en place. Case
  « alerte » par vue (`alert` de `PUT /api/explore/views`) : ses nouveaux noms, dans le message du jour (voir
  Alertes).
- **Sélection multiple** : Vérifier, SEO, Surveiller ou Réservé en lot, bilan en un message.
- Recherche : le nom tapé sur toutes les extensions, puis les noms qui commencent pareil. Un nom
  absent de toutes les listes (monsuperprojet) s'affiche quand même, une ligne par extension suivie
  (ou celle tapée, monsuperprojet.fr) : « Vérifier » interroge le registre (`POST /api/explore/check`,
  qui renvoie alors l'état, le registrar et l'échéance sans score ni catégorie), puis Réserver ou
  Surveiller. « Ajouter aux noms suivis » le range dans Mes ajouts (voir « Listes de noms ») ; le nom vérifié
  à l'ajout est celui tapé avec son extension, sinon sur la première extension choisie dans l'Explorer.
- Par défaut, seuls les noms qui ont un sens sont listés et comptés : noms des listes, vrais mots
  courts (toutes les formes de 2 à 4 lettres du Lexique, même rares), noms prononçables, chiffres
  seuls. Les suites imprononçables (qsd, xkcd), les mélanges lettres-chiffres et les tirets
  restent accessibles par une case du panneau des filtres.

Prononçable : modèle de trigrammes de lettres appris sur le Lexique et les listes de noms
(`app/pronounce.py`). Seuil choisi pour garder 92 % des vrais mots de 3 et 4 lettres et
seulement 8 % des suites de lettres tirées au hasard. La forme des 1,9 million de noms courts
est précalculée à la construction de l'image (`app/precompute.py`). L'Explorer garde ses calculs
en mémoire et les prépare au démarrage, après chaque import .fr et chaque lecture de zone. Un
nom vérifié, marqué ou analysé (bouton ou vérification automatique) est corrigé en place : son
octet d'état, les compteurs de ±1, la carte. Pas de recalcul d'ensemble.

Score (`app/value.py`, `app/quality.py`), chaque critère pondérable de 0 à 2 dans Paramètres :

| Critère | Points |
|---|---|
| Forme et sens : longueur, sonorité en français et en anglais (ressemblance aux vrais mots, alternance consonnes-voyelles, test radio, lettres ambiguës), sens et rang dans sa liste (Lexique, Insee, anglais, secteurs…), nature du mot, image négative, langue adaptée à l'extension | 70 au plus |
| Demande : pris sur d'autres extensions, .com surtout, seulement si c'est rare pour un nom de sa forme | 15 au plus |
| Historique .fr : années de détention, suppression récente, moins si la plupart des noms de sa forme ont déjà été tenus | 15 au plus |
| SEO, si analysé : Open PageRank, années archivées par la Wayback Machine | 20 au plus |

Forme et sens : poids calés sur des avis IA détaillés (grille v5) d'un échantillon de 5 240 noms de
toutes les catégories, une famille de poids par sorte de nom (mots, noms de personnes, suites courtes
de lettres, chiffres), dans `app/data/score_model.json`. Les noms de 1 ou 2 caractères sont toujours
dans la famille des suites courtes : presque toute paire de lettres figure dans la liste anglaise, et
ce « sens » ne vaut rien. Mesures propres aux formes courtes : sigle courant (`app/data/sigles.txt`),
marque connue (`app/data/marques.txt`), et pour les chiffres les motifs qui se retiennent (rond,
répété, suite, palindrome, dizaine, année, zéro devant, département en .fr, .com jusqu'à 3 chiffres).
Concordance de rang avec les avis IA, sur des noms gardés hors du calage : 0,25 pour l'ancien barème,
0,85 pour celui-ci ; par catégorie, 0,79 pour les 2 lettres et 0,77 pour les 3 chiffres (validation
croisée) après l'ajout de 240 noms de ces deux formes à l'échantillon. Point faible connu : entre deux
mots courants, la valeur tient au sens et au marché, que seule la notation IA juge bien. Demande et
historique : taux de base par extension et par forme (1 à 4 lettres prononçables ou non, chiffres,
mélanges, mots), mesurés sur les zones lues (pour .co, .ai et .io, parmi les seuls noms interrogés
dans le DNS) ; la raison dit « courant pour cette forme » quand le signal est banal. Un nom délégué en
.co, .ai ou .io compte comme pris ailleurs, comme un nom d'une zone. Le score de base de tous les noms est précalculé à la construction de l'image
(`app/precompute.py`, `scores.bin`) ; le classement écarte d'abord les noms sur cette borne et ne
fait le calcul complet que pour ceux qui peuvent entrer dans la page.

SEO à la demande (bouton « SEO » d'un nom, ou « SEO des 50 premiers ») : Open PageRank donne une
note de 0 à 10 tirée des liens (Common Crawl), avec une clé gratuite à saisir dans Paramètres ;
la Wayback Machine donne les années où le site a été archivé, sans clé. Le nombre exact de
backlinks n'existe que chez des services payants (Ahrefs, Majestic, Moz).

Gandi (Paramètres, « Gandi (prix et premiums) ») : source d'information seulement, rien n'est acheté. Jeton
d'accès personnel créé dans le compte Gandi (Paramètres du compte, « Create a token »), permission de lecture
des domaines ; gardé sur le NAS, jamais renvoyé (`PUT /api/settings/gandi`). « Tester »
(`POST /api/settings/gandi/test`) interroge quelques noms témoins (`app/gandi.py`, `SAMPLES`) et affiche leur
état et leur prix de création ; les réponses brutes, sans aucun secret, sont lisibles dans `/api/diag`
(`gandi_samples`).

Ce que Gandi apporte : pour un nom non pris, premium ou non, son prix, ou son indisponibilité. Prix affiché
« prix Gandi, indicatif » (création pour un an, TTC, en euros) : l'achat se fait où l'on veut, le prix d'un premium
vaut partout en ordre de grandeur. Lecture d'une réponse (`gandi.read`, calée sur les réponses réelles et la
documentation) : premium si une ligne de prix est de type « premium » ou si le statut est `unavailable_premium` ;
réservé pour `available_reserved` ou `reserved_corporate` ; restreint pour `unavailable_restricted` ; indisponible
pour `unavailable` ; libre pour `available` (et `available_preorder`) ; erreur pour `pending`, `error_*` ou un statut
inconnu. Prix : la première ligne d'une durée minimale d'un an (la TVA n'apparaît que pour un nom disponible).
Résultats dans la table `gandi_checks` (migration 030 : statut brut, sorte, prix TTC et HT, date, erreur). Une réponse
en erreur ne remplace pas un résultat connu : elle est notée à côté.

- **Passe de nuit** (`gandi.NightPass`), de 3 h 30 à 4 h 45 (heure `TZ`), après les lectures et les passes DNS,
  avant la notation IA de 5 h. File : les noms non pris de toutes les extensions lues (.fr, zones ICANN, .co, .ai,
  .io), dans l'ordre de la file IA : libres d'abord (score complet), puis probablement libres,
  libres selon le WHOIS, premiums et noms déjà dits indisponibles par Gandi, par score de base décroissant ; noms qui
  ont un sens seulement. Bientôt libres (rédemption, suppression) : pas demandés, Gandi les dit toujours
  indisponibles. Un nom déjà demandé n'est revu que selon `gandi.revisit_due` (voir « Économie des appels »). 3 requêtes par seconde au plus et 8 000 noms par nuit au plus (réglables) ;
  jusqu'à 8 requêtes en vol (`gandi.Inflight`) : une à une, la passe tombait à 1 nom par
  seconde (4 384 noms la nuit du 19/10) et n'atteignait pas son plafond dans la fenêtre. Jeton
  refusé (401, 403) : arrêt, erreur dans Paramètres et dans la rubrique Santé du message du jour. Trop de requêtes
  (429) : attente demandée par Gandi (60 s par défaut), puis reprise au même nom ; au troisième refus d'affilée,
  arrêt. 20 échecs réseau d'affilée : arrêt. Réponse vide (Gandi limite) : nom pas noté, attente d'une minute
  doublée à chaque réponse vide d'affilée (10 min au plus), reprise au même nom ; 6 d'affilée : arrêt. Une extension qui répond 5 fois de suite « invalide »
  (error_invalid, HTTP 400 ou 422 ; jamais une réponse vide ni une erreur 5xx) est écartée 30 jours (Gandi ne la
  vend pas). Sans jeton ou case décochée : rien. « Lancer maintenant » (`POST /api/settings/gandi/run`,
  202, 409 sans jeton) lance la passe tout de suite, hors fenêtre, jusqu'au plafond. Réglages :
  `PUT /api/settings/gandi/pass` (`enabled`, `rate` de 0,2 à 10, `nightly` de 1 à 50 000, `days` de 1 à 90). État :
  `gandi.pass` de `GET /api/settings`, `gandi_pass` de `/api/diag` (sans le jeton ni sa présence), `gandi` de
  `/api/drops` (ligne des sources de la Veille et carte « Sources et lectures »).
- **Économie des appels** : chaque appel extérieur ne part que s'il peut apprendre quelque chose.
  - Gandi, nom déjà classé (`revisit_due`) : sur un événement (état changé
    depuis la réponse : sortie de zone ou du DNS, libération), ou une réponse en erreur, le lendemain ; libre au tarif
    normal, jamais d'office (zone, DNS ou registre disent s'il est repris) ; premium, 30 jours (7 avec un avis IA
    « À prendre ») ; pris, réservé ou restreint, 30 jours (réglable, `days`). Avant : libres et premiums chaque jour
    (6 100 des 8 000 requêtes de la nuit du 19/10).
  - Prix normal par extension (`Gandi.refresh_grid`, tâche `gandi_grid`) : un appel par extension et par jour, sur un
    nom de 16 lettres tiré au hasard ; un prix qui change est reporté sur tous les libres au tarif normal de
    l'extension (`gandi_grid` dans les réglages), sans les redemander.
  - DNS .co, .ai, .io (`ccdns.pending`) : comme le .fr, fort score de base (40 et plus) et Mes ajouts
    chaque jour, les autres chaque semaine ; jamais interrogés d'abord. Avant : la même liste entière chaque jour.
  - Nom libre ou premium d'après Gandi, enregistré depuis : « Enregistré » (règle d'état), hors du filtre « prix
    d'achat au plus », et sa ligne Gandi effacée (`Explorer.forget_taken_prices`) : tout de suite quand une passe DNS
    le voit délégué, chaque heure pour la zone et le registre. Libéré de nouveau, Gandi est redemandé (`on_freed`).
  - Registre (RDAP, WHOIS) : déjà sur échéance (dates anniversaire et suppressions .fr, Veille, domaines suivis) ou à
    la demande ; inchangé.
- **À la demande** : Vérifier d'une fiche (après le RDAP, pour un nom libre, à confirmer ou en rédemption ; une panne
  de Gandi est dite dans `gandi_note` sans faire échouer la vérification) et la vérification d'un domaine suivi
  libre, en rédemption ou en suppression (une fois par jour au plus, toujours à la main ou à un changement d'état).
  Nom tout juste vu libre au registre (Veille, domaine suivi) : son prix est demandé une minute après, sans attendre
  la nuit, sauf réponse de Gandi de moins de dix minutes (`drops.on_freed`).
- **Plafond commun** : toutes les requêtes vers Gandi (passe de nuit, Vérifier, domaines suivis) passent par le même
  client, 900 par minute glissante au plus (`GLOBAL_PER_MINUTE`), sous la limite de Gandi (1 000 par minute et par
  adresse IP). Réponses rangées avec l'extension (`gandi_checks.tld`, colonne calculée, migration 033) : chaque
  extension lit les siennes par l'index.
- **Effet sur l'état** : une seule règle pour tous les écrans (Explorer et fiche, Veille, Aujourd'hui, Domaines),
  `explore.gandi_effect`, détaillée et testée combinaison par combinaison dans `tests/test_state_rules.py`. Premium
  (statut available avec un prix premium) : « Premium » et son prix ; libre au prix standard : « Libre (vérifié) », le
  prix en petit (« achat 10 € ») ; indisponible, réservé ou restreint (`unavailable_premium` compris : un premium déjà
  enregistré ne s'achète pas) : d'abord une revérification au registre, tout de suite (`Explorer.recheck`) ; pris, le
  nom passe « Enregistré » ; toujours libre au registre, « Indisponible à l'achat » (premium ou vente par un tiers, prix
  à voir chez un registrar). D'ici là (`pending`), l'état d'avant tient. Jamais d'effet sur un nom enregistré, bientôt
  libre ou suspendu, ni sur un terme d'une liste de réservés ; une réponse d'avant le dernier changement d'état ne
  compte plus (`stale`) ; une erreur ne change rien. `gandi_state` (Explorer, fiche, Veille, Aujourd'hui, Domaines) :
  la sorte qui fait l'état affiché ; l'interface ne refait pas le calcul.
- **La dernière lecture l'emporte** : un .fr libre au registre puis vu délégué dans le DNS est enregistré depuis
  (`explore.drop_open_code`), et le registre est revu ; la vérification d'un domaine suivi (écran Domaines) vaut aussi
  pour sa ligne de Veille et l'Explorer (`Explorer.sync_registry`) ; un nom revenu dans sa zone ou dans le DNS est
  repris. Une ligne de Veille créée sans sortie constatée (`origin` : `manual` pour Vérifier ou un domaine suivi,
  `gandi` pour une revérification après un refus d'achat) dit « Vu libre au registre » ou « Vu enregistré au
  registre », jamais « Libération constatée » ni « Repris » ; celles d'un refus d'achat restent hors de la Veille,
  d'Aujourd'hui et des alertes. Contrôle sur des données réelles : `tests/fixtures/state_cases.json`
  (combinaisons relevées sur un NAS, sans les noms), rejouées dans tous les écrans.
- **Données** : `gandi` dans les éléments de l'Explorer, de la fiche (et `fiche.gandi`), de `/api/drops`,
  `/api/today` et `/api/domains` (`kind`, `status`, `label`, `price`, `price_ht`, `currency`, `checked_at`, `error`
  en clair, `stale`) ; `gandi_state` dans la Veille (la sorte qui vaut pour l'état affiché). File IA
  (`/api/ai/pending`) : `"gandi": {"kind": "premium", "price": 878}` pour un nom connu de Gandi, et une phrase de la
  consigne (un premium doit être couvert par la revente, un nom indisponible ne vaut rien à l'achat) ; un premium vu
  chez Gandi entre dans la file avec les libres (un premium probable non vérifié n'y entre pas).
- **Filtres** : « Masquer les premiums » (case décochée par défaut : les premiums, vus chez Gandi ou probables, restent
  visibles) dans l'Explorer (`nopremium=1`, vues enregistrées, mémoire du navigateur, export CSV, compteurs), la
  Veille et Aujourd'hui (`/api/today?nopremium=1`, chiffres compris ; `/api/drops?focus=…&nopremium=1`). « Prix
  d'achat au plus » dans le panneau Avancé de l'Explorer (`gprice=20`, seulement les noms vérifiés chez Gandi,
  compteurs compris) et de la Veille. Export CSV : colonnes `gandi_kind` et `gandi_price`.
- **Affichage** : sur la carte de nom, pastille discrète « 13 € » ou ambre « Premium 878 € » (dans l'Explorer, l'état
  d'un premium vu chez Gandi est lui-même « Premium 878 € ») ; Domaines, sous la valeur, « 8 € chez Gandi »,
  « Premium 878 € » ou « Indisponible chez Gandi » ; fiche : badge et prix dans la ligne d'état, « achat Gandi » à
  côté des prix de revente dans la carte décision, ligne « Gandi » de Registre et SEO (statut, prix TTC et HT, date,
  erreur), « Vérifié chez Gandi » dans Dates clés.

Tri : score, fréquence (mot, nom ou prénom du plus courant au moins courant), prénoms de l'année la plus récente (Insee), longueur, SEO, avis IA. Un tri ne filtre pas : avec SEO ou avis IA, les noms analysés viennent d'abord, puis les autres par score. Chaque tri applique tous les filtres (recherche, SEO et avis IA compris pour la libération la plus proche). Les éléments de `/api/explore/rank`, `/api/drops`, `/api/today` et de la fiche portent `ranks` (rang du nom dans chaque liste, `{"firstname": 12, "word": 4881}` ; prénoms et noms des États-Unis sous `firstname_us` et `surname_us`) ; avec `sort=freq`, `freq_list` dit la liste qui donne l'ordre (celle du filtre de catégorie ou l'une de ses sources, sinon celle où le nom est le mieux placé).

#### Avis IA

Pour chaque nom complet en .fr, .com ou .org (velo.fr, ciel.org ; les autres extensions valent trop peu pour être notées) : une grille de neuf sous-notes sur 10
(prononciation, test radio, mémorisation, sens, marque, marché, adéquation à l'extension,
acheteurs possibles, risque), deux fourchettes de prix (revente rapide à un investisseur ;
utilisateur final, avec de la patience) et une raison avec le type d'acheteur. Les notes
d'exploitation et de revente sur 100 et le verdict sont calculés par le NAS depuis la grille
(`app/ai_format.py`, `derive`) : ils ne peuvent plus se contredire. Le prix utilisateur final doit
tomber dans le palier de la note de revente pour son extension (repères indicatifs, un palier de
tolérance de chaque côté). Paliers pour un .fr : 95 et plus, 20 000 à 300 000 ; 90, 10 000 à
150 000 ; 85, 8 000 à 80 000 ; 80, 5 000 à 30 000 ; 70, 3 000 à 15 000 ; 60, 1 500 à 8 000 ; 50,
1 000 à 5 000 ; 40, 500 à 2 000 ; 30, 200 à 1 000 ; 20, 100 à 800 ; 10, 50 à 300 ; en dessous,
0 à 100 ; les autres extensions multiplient ces paliers (.com 1,5 ; .io et .ai 0,8 ; .co 0,6 ;
.org, .app et .dev 0,5 ; .net 0,4 ; .info, .biz et .xyz 0,25 ; autres 0,4). Pas de plafond pour
les noms de 1 ou 2 caractères (3 en .com). Pour un verdict « À laisser » ou « À surveiller »,
l'avis dit pourquoi en mots simples (`why`) : risque fort (marque connue, sens négatif ou gênant),
sinon la sous-note la plus faible parmi sens, marque, marché et acheteurs ; un avis à l'ancienne
grille (`version` 4) n'en a pas et sera renoté. Un mot ou une expression française en .com n'est pas un
défaut : la prononciation se juge dans la langue du mot et l'adéquation à l'extension vaut 10 en
français comme en anglais (les avis .com donnés avant cette règle repassent dans la file). La raison
doit apprendre ce que les sous-notes ne disent pas (usage concret, ce qui fait payer le secteur,
marque précise qui gêne) plutôt que « long » ou « descriptif ». La mémorisation est calculée par l'appli (10
jusqu'à 4 lettres ou mot très courant, 7 de 5 à 7 lettres ou mot courant, 4 de 8 à 12, 2 au-delà) ; au-delà de
12 lettres la note perd 3 % par lettre (30 % au plus). « À prendre » exige une note de revente d'au moins 60.
Rendement (version 7) : la note de revente est le potentiel du nom ; le verdict vient du rendement attendu, en
euros par an : chance de vente dans l'année × prix utilisateur final bas × (1 − 20 % de commission), moins le
renouvellement de l'extension chez Gandi. « À prendre » rapporte au moins deux fois son renouvellement, « À
laisser » coûte plus qu'il ne rapporte (ou risque de 8 et plus), « À surveiller » entre les deux.
Note de A à F (`ai_format.grade`, même règle en SQL avec `grade_sql`) : chaque verdict coupé selon ce rendement
(bénéfice net par an, renouvellement déjà payé) rapporté au renouvellement. A « Excellent » : « À prendre » d'au
moins 4 fois le renouvellement ; B : les autres « À prendre » ; C « Bon pari » : « À surveiller » d'au moins la
moitié ; D : « À surveiller » d'au moins un quart ; E « Peu rentable » : les autres « À surveiller » ; F : « À
laisser ». Exemple .fr à 7,20 € : un C rapporte au moins 3,60 € de bénéfice net par an (10,80 € de gain attendu avant
renouvellement), en espérance. Sans rendement calculé (avis anciens), la note de revente départage (70 pour un A, 55
pour un C, 50 pour un D). Au NAS le 25/10/2026 : 19 « À prendre » et 1 110 « À surveiller », dont le rendement allait
de 0,1 à plus de 2 000 € par an ; la note isole une trentaine de A et B, quelque 330 C, puis coupe le reste au quart du
renouvellement (254 D, 427 E). Filtre « Note IA » (Explorer `ai_g=A,B`, Veille, Domaines ; `/api/ai/rated?grade=`),
couleurs du vert au rouge, échelle ronde façon Nutri-Score dans la fiche, comptes dans Statistiques. La chance de
vente n'est pas estimée par l'IA : elle part des taux publiés (un portefeuille passif vend 1 à 2 % de ses noms par
an, Allemann/DomainNameWire 2022-2024 ; Afternic juge 1 % « ambitieux » pour un débutant, 2026), monte avec la
demande mesurée (nombre d'autres extensions où le nom est pris ; courbe Squadhelp 2021 : 2,84 % à 14,66 % selon 1 à
5 extensions prises, divisée par deux pour des noms non triés ; pour un nom hors .com, un .com pris ne compte pas)
et avec le potentiel (×2 tous les 15 points de note de revente au-dessus de 50), entre 0,5 % et 15 % par an. La
fiche montre « rapporte ~X €/an (vente ~Y %/an, 1 chance sur 2 en ~Z ans) ».

Ventes comparables : le NAS lit les rapports de ventes de DNJournal (toutes les deux semaines, archive de
l'année et des précédentes depuis 2025, tout l'historique au premier passage, 3 s entre deux rapports ; `app/market.py`, table `market_sales`, données gardées sur le NAS seulement).
Pour chaque nom, les 4 ventes réelles les plus proches (un mot courant en commun, puis même extension, même
nombre de mots, même langue, longueur voisine, ventes récentes d'abord) sont jointes au lot de notation
(`ventes_comparables`) : l'IA ancre son prix utilisateur final dessus, plus bas pour un nom plus faible, car
DNJournal ne publie que des ventes d'au moins 1 000 à 2 000 $ (médiane de toutes les ventes Sedo : 600 à 800 $).
La fiche les montre (section « Comparables et repères »). Source cassée (archive ou rapport injoignable, rapport
sans vente lisible, plus de nouveau rapport depuis 5 semaines) : panne bloquante, alerte Telegram immédiate.
Les avis v5 et v6 « À prendre » ou « À surveiller » sont renotés (v7) ; les « À laisser » restent.
Contre-notation : chaque « À prendre » repasse à l'aveugle chez un second correcteur, en tête de lot (200 au
plus), et la note la plus basse reste (`confirmed_at`). Validation de
la grille : 50 noms témoins notés par plusieurs agents indépendants, écart-type moyen de 3 points,
même verdict dans 94 % des cas.

Second passage : chaque nuit, les 50 avis les plus éloignés de l'estimation du score (25 points
d'écart au moins) reviennent en tête de lot, avec l'ancienne note et l'estimation ; l'IA les renote à
froid, une seule fois.

Mesure a posteriori : chaque avis garde l'état du nom au moment où il est donné (`status_at`). Chaque
nuit, les avis vieux de 30 jours au moins sur des noms alors libres ou probablement libres sont
confrontés à l'état du jour : enregistré par quelqu'un d'autre (ni surveillé ni au portefeuille), le
nom compte comme repris, daté du premier passage qui l'a vu (`ai_outcomes`). `GET /api/ai/outcomes`
résume, par verdict et par tranche de note de revente (0-39, 40-59, 60-79, 80-100), le nombre de noms
et la part reprise : si les « À prendre » partent vraiment, la grille tient. Paramètres, carte Avis IA,
bloc « Fiabilité des verdicts » : le tableau par verdict et par tranche, avec le nombre de noms mesurés ;
tant qu'aucun avis n'a 30 jours, une phrase l'explique.

Pépites : un avis « À prendre » au seuil des alertes sur un nom libre ou bientôt libre, de la Veille comme de
l'Explorer ; rubrique d'Aujourd'hui et alertes Telegram, voir « Alertes ».

Le NAS publie la liste à noter (`GET /api/ai/pending`) : noms libres ou bientôt libres d'abord (score complet ; la
borne du score range d'abord, un nom n'est noté en entier que s'il peut sortir), puis les noms probablement libres ou
libres des listes fournies (termes stars, mots-clés et marques mêlés par rang, puis métier + ville ; .fr et .com,
puis .org), puis les autres probablement libres, puis enregistrés, les plus prometteurs d'abord ; mêmes filtres partout (mot
seulement anglais écarté en .fr, 1 ou 2 caractères jamais vus écartés, réservés et premiums probables exclus), avec la consigne et des exemples
de référence pour garder des notes cohérentes. Il réimporte les avis (`POST /api/ai/results`) après
contrôle du format, le même pour chaque avis : exactement dix-sept champs, sous-notes entières de 0
à 10, prix sur une échelle fixe (0, 50, 100, 150, 200, 300, 500, 800, 1 000…), revente rapide à 40 %
du prix final au plus, raison sur le gabarit « Raison. Acheteur type : … » (180 caractères, sans
emoji ni tiret long, sans verdict). Le verdict calculé est « À prendre » (meilleure note d'au moins
60, risque de 4 au plus, acheteurs à 4 au moins), « À surveiller » ou « À laisser » (40 au plus, ou
risque de 8 et plus) ; il
apparaît en tête de l'étiquette dans les listes. Les avis à l'ancien format (version 4) restent
acceptés pendant la transition. Le script applique le même contrôle avant l'envoi (`check`). Une tâche planifiée fait l'échange
chaque nuit avec `scripts/ai_exchange.py` (variables `DW_URL` et `DW_AI_TOKEN`).

Analyse à la demande : bouton « Analyse IA » dans la fiche de n'importe quel nom, même hors des
listes ou des extensions notées la nuit. Une tâche planifiée passe toutes les heures
(`scripts/ai_exchange.py requested`) et s'arrête aussitôt s'il n'y a aucune demande. Une fois
l'avis reçu : message Telegram avec le lien direct vers la fiche (adresse `APP_URL`, sinon celle vue
par le navigateur au moment de la demande), et bandeau « Analyse IA prête » dans l'appli. Le
bandeau disparaît quand on l'ouvre ou quand on suit le lien Telegram ; l'avis est alors encadré
dans la fiche, puis affiché normalement aux visites suivantes. Lien ouvert sans session :
connexion, puis retour sur la fiche.

Le jeton se crée dans Paramètres > Jetons d'accès (type « Notation IA ») ; il n'est montré qu'une fois. Il ouvre l'échange des
avis et, en lecture seule (GET), le diagnostic `/api/diag` (version, mémoire, zones, .fr, DNS, avis
IA, compteurs, tâches planifiées, exécutions manquées ou en erreur du planificateur, file Telegram,
dernières erreurs du journal), les écrans (Domaines, Veille,
Explorer, Top, fiches) et `/api/ai/rated` (avis enregistrés, filtres `verdict` et `tld`). Le journal garde 7 jours
d'avertissements et d'erreurs en base, écrits chaque minute : `/api/diag/journal` (les plus récents d'abord ; `since`,
`level=ERROR`, `q=lente` ou `q=bloquée`), taille dans `journal` de `/api/diag` (environ 350 Ko pour une semaine
ordinaire, 50 000 lignes et 8 Mo au plus). On y lit les requêtes de plus d'une seconde, avec leurs paramètres et leur
code de retour, et les blocages de la boucle de l'appli de plus de 2 s : pendant un blocage rien n'est servi, et
une réponse qui dépasse le délai du proxy devient une 502. Un fil à part relève pendant le blocage la ligne de code
qui tournait, les tâches planifiées et les requêtes en cours ; s'il est lui-même retenu, le verrou de Python était
pris (nettoyage mémoire ou calcul en C). Les nettoyages mémoire de plus d'une seconde sont notés avec leur
génération, et `gc` de `/api/diag` donne leurs comptes. Jamais
les réglages, les secrets ni une écriture. Lecture depuis une session Claude :
`python3 scripts/read_api.py /api/diag`. L'avis s'affiche dans la fiche et les listes, avec un tri « Par avis IA » (revente
rapide) et un filtre dans le panneau de l'Explorer : verdicts, et bornes min et max de chaque note
(exploitation, revente) et de chaque prix (revente rapide bas et haut, prix final bas et haut). Les prix sont des avis, pas des cotes du marché.

#### Noms réservés (.fr)

Certains noms libres ne se réservent pas simplement. L'appli télécharge chaque semaine :

- la liste Afnic des termes soumis à examen préalable (PDF public, version 2021 :
  professions réglementées, institutions, termes sensibles), lue ligne par ligne ;
- les noms de communes (geo.api.gouv.fr), sous la forme protégée par l'Afnic : sans espaces,
  tirets, accents ni apostrophes, avec et sans l'article.

Ces noms ne sont enregistrables qu'en justifiant d'un intérêt légitime. Dans l'Explorer ils
prennent l'état « Réservé » et sont retirés des noms à saisir, de la carte et du classement
(réglable dans Paramètres). Bouton « Réservé » pour retirer un nom à la main, « Pas réservé »
pour corriger une liste. La liste Afnic publiée n'est pas exhaustive : l'Afnic indique ne pas
publier les termes dont la seule mention heurterait le public.

« Probablement libre » veut dire : pour le .fr, supprimé sans réenregistrement connu ou jamais
enregistré d'après l'open data ; pour une zone ICANN, absent de la zone ; pour .co, .ai et .io, absent
du DNS du registre (carte « probablement libre (absent du DNS) »). Un nom enregistré sans serveurs DNS
est absent de la zone et du DNS lui aussi. Toujours vérifier avant de conclure.

Zones ICANN : l'appli demande elle-même au registre (RDAP) l'état de tous les noms absents de la zone, les
meilleurs scores d'abord, extension par extension (10 000 noms par extension à chaque file, le .com en tête chez
Verisign : mêlées, les autres extensions passaient devant lui). La limite des registres n'est pas publiée : chaque registre (serveur RDAP) a son
rythme. Départ à 1 requête par seconde (16 en vol au plus, tous registres confondus), +25 % tous les 500 succès
jusqu'à 25 par seconde ; au premier refus, rythme du registre divisé par deux et pause (Retry-After, sinon
2 minutes, 30 minutes sur un 403), sans arrêter les autres. Une coupure réseau isolée : 30 secondes de pause, rythme
gardé. Un registre qui refuse tout depuis le matin (3 refus, aucune réponse) est écarté jusqu'au lendemain. Vu le
24/10/2026 : 429 de Google (.app, .dev), 403 du registre du .biz dès la première requête. Suivi par registre dans `/api/diag`, `absent_pass.registries`. Le rythme appris est gardé ; client à part, la Veille et Vérifier gardent leur cadence (`RDAP_MIN_DELAY`). Gandi ne passe ensuite que
sur les libres des extensions à premiums : un .com ou un .net libre au registre est achetable au prix de la grille
(filtre de prix compris). Un nom absent de la zone est souvent en rédemption, en suppression ou suspendu : il passe
« Libération à venir », « Suspendu » ou « Enregistré » ; sinon « Libre (vérifié) ». Ces réponses restent hors de
la Veille, des alertes et du quotidien (ce ne sont pas des libérations constatées) ; un nom vu enregistré est
redemandé après 30 jours, une vérification à la main le fait entrer dans la Veille. Suivi : `/api/diag`,
`absent_pass`.

### Appli sur le téléphone

L'interface est une PWA : sur téléphone, navigation en bas, panneaux plein écran, boutons larges. La barre
du bas garde cinq entrées aux libellés entiers à 390 px (Aujourd'hui, Domaines, Veille, Explorer,
Portefeuille) ; Paramètres est l'icône en haut à droite de chaque écran (six entrées tronquaient trois
libellés). Sur Aujourd'hui, les compteurs passent en tuiles, deux par ligne.
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
5 échecs, adresse réelle du visiteur lue derrière Cloudflare ou le proxy du NAS). Pour la garder en
réseau local, ne pas ouvrir le port 443 sur la box, ou passer par le VPN du NAS. Chaque réponse porte
les en-têtes `Content-Security-Policy: frame-ancestors 'none'`, `X-Frame-Options: DENY`,
`X-Content-Type-Options: nosniff` et `Referrer-Policy: same-origin` : l'appli ne s'affiche pas dans le
cadre d'un autre site. `/login` ouvert avec une session valide renvoie directement à l'appli.

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
- Sans RDAP (.co par exemple), l'appli interroge le WHOIS du registre, trouvé via `whois.iana.org`
  et gardé en base 7 jours (réglage `whois_servers`) : pas de nouvelle question à l'IANA à chaque redémarrage.
  Elle ne lit que des lignes « clé: valeur » connues. « Disponible » exige une formule explicite du
  registre (« not found », « no match »...) et aucune donnée d'enregistrement dans la réponse. Toute
  réponse non reconnue donne une erreur de check. La fiche d'un nom surveillé indique la source utilisée.
- Un WHOIS « inconnu » ne vaut pas « Libre (vérifié) » : il ignore souvent les noms réservés ou premium du
  registre (.co, .io, .ai). Un nom libre d'après le seul WHOIS est « Libre selon le WHOIS, à confirmer »
  (Explorer, code d'état 9, parmi « À réserver » et « Probablement libres », pas parmi « Libres vérifiés » ;
  Veille, état `unconfirmed`, onglet Libérés avec ce libellé ; Domaines, « Disponible selon le WHOIS, à
  confirmer »). Pas de « Libération constatée », pas compté dans les libérés d'Aujourd'hui ; une pépite à confirmer
  est annoncée avec la mention « à confirmer chez un registrar », les bons noms du jour les listent à part, et
  l'alerte d'un nom surveillé le dit. Réserver reste possible ; refusé par le registrar, « Plus », Réservé par registre, le sort des noms
  à prendre. Au démarrage, les noms marqués libres par une version précédente sur cette seule base sont
  corrigés (sans migration).
- RDAP d'une extension en panne (erreur réseau ou TLS, délai, 5xx à chaque essai) : repli sur son WHOIS,
  noté « Repli WHOIS : … » dans la réponse de Vérifier ; bootstrap IANA injoignable sans cache : même repli
  si le serveur WHOIS est déjà connu. Serveurs WHOIS en dur pour .co, .io et .ai (`KNOWN_WHOIS` dans
  `app/rdap.py`, non vérifiés d'ici), utilisés seulement quand l'IANA ne donne pas de referral.
- Signaux d'abandon, une fois par jour pour chaque domaine enregistré : le nom résout-il, le site
  répond-il, le certificat HTTPS est-il valide, y a-t-il des serveurs de mail, le domaine est-il en
  parking ou affiché à vendre. Le badge « Signes d'abandon » apparaît avec au moins deux indices
  (l'absence de mail seule ne compte pas) ou un parking.
- Pour .com et .net, la date de libération estimée porte aussi une heure : le début de la fenêtre
  quotidienne de suppression de Verisign, vers 12:00 UTC depuis le 1er octobre 2026 d'après la
  presse spécialisée. Pas d'annonce officielle trouvée, d'où l'affichage en estimation.
- Filtres (nom, état, tag, signes d'abandon, extension, verdict IA) et tri sont gardés par le navigateur d'une
  visite à l'autre ; revenir sur Domaines par la navigation ne remet pas le filtre d'état à zéro. Sur téléphone,
  où les en-têtes du tableau sont masqués, un menu donne les mêmes tris (état, nom, valeur, jours restants,
  libération, prochain check) et un bouton inverse le sens. Tags libres par domaine, ajoutés dans la fiche du nom.
- Certains registres n'ont ni RDAP ni WHOIS public, ou réservent leur WHOIS à des accès
  authentifiés : leurs domaines restent en erreur de check.
- Erreurs réseau dans l'interface : un message technique courant (ProxyError, ConnectError,
  « Bootstrap IANA indisponible », navigateur hors ligne) s'affiche « Réseau injoignable », un délai dépassé
  « Pas de réponse du registre », avec un bouton « Réessayer » dans le message quand l'action est
  rejouable. Le détail brut (serveur, type d'erreur) suit en ligne « Détail : … » dans le message et dans
  la section Suivi de la fiche, et au survol. Les réponses attendues (409 d'une vérification hors réseau, 422 d'une saisie) s'affichent
  telles quelles ; l'appli n'écrit rien dans la console du navigateur, qui journalise déjà la requête.
- Sondages de l'interface : l'association Telegram et la mise à jour des noms réservés lisent
  `GET /api/settings/status` (pas tous les réglages) ; une lecture de zones ou un import .fr lancés à la
  main lisent `GET /api/drops?items=0` (état sans la liste), puis la liste une fois à la fin.
- Toutes les requêtes vers un même registre passent par une file unique, avec le délai minimum
  entre deux appels. Le check global et les fréquences rapides ne peuvent pas la contourner.
- La date de libération est une estimation. Elle repose sur `app/tld_delays.toml`, qui donne pour
  chaque TLD les délais publiés par le registre avec leur source. Si un délai manque, aucune
  estimation n'est affichée pour ce TLD.

État des délais au 2026-10-15 :

| TLD | Grâce après expiration | Rédemption | Suppression | Estimation |
|---|---|---|---|---|
| .com | 45 j | 30 j | 5 j | oui |
| .net | 45 j | 30 j | 5 j | oui |
| .org | 45 j | 30 j | 5 j | oui |
| .info | 45 j | 30 j | 5 j | oui |
| .biz | 45 j | 30 j | 5 j | oui |
| .fr | non fixé par l'Afnic | 30 j | aucune (libre à la fin de la rédemption) | oui |

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
