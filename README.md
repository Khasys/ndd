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

Messages envoyés :

| Message | Quand |
|---|---|
| Changement d'un domaine surveillé (état, registrar, libération estimée), lien vers sa fiche | à chaque changement ; vérification toutes les 5 min en suppression |
| Résumé du matin : libérés, entrés en rédemption ou en suppression, .co / .ai / .io sortis du DNS, bons noms seulement, lien vers chaque fiche | chaque jour à l'heure choisie (8 h par défaut), rien s'il n'y a rien de bon |
| « À prendre » : nom de la Veille qui passe libre ou entre en rédemption avec un avis IA « À prendre » et une note de revente au seuil réglé (70 par défaut, Paramètres) ; verdict, notes, prix utilisateur final, raison, lien vers la fiche et lien Réserver | dans les 10 minutes, une fois par nom et par état ; les noms d'un même passage dans un seul message (10 détaillés, puis « et N autres ») |
| Vue enregistrée en alerte : « Vue « X » : N nouveaux noms », les noms devenus libres ou bientôt libres depuis le dernier passage qui répondent aux filtres de la vue, lien vers chaque fiche | une fois par heure au plus, après les lectures de la nuit comme en journée |
| Résumé hebdomadaire | jour et heure choisis |
| Analyse IA demandée prête, lien vers la fiche | dans l'heure qui suit la demande |
| Panne : zone en erreur ou pas lue depuis 36 h (ou aucune zone lue avec succès depuis 36 h), fichier .fr en erreur, pause .fr de plus d'une heure, notation IA muette depuis 36 h, passe DNS (.fr, .co, .ai, .io) en erreur ou finie sans aucun nom vérifié, plus de 3 exécutions manquées du planificateur en 24 h | à l'apparition, puis « Réglé » à la disparition |
| Passe DNS arrêtée (refus ou silence du registre) | à l'arrêt |

Les liens utilisent `APP_URL`, sinon l'adresse vue par le navigateur à la dernière ouverture de l'appli.

Rejeu : un message que Telegram refuse passagèrement (trop de requêtes, panne de leur côté) ou qui ne
part pas (réseau coupé) est mis en file dans la base au lieu d'être perdu. Toutes les 5 minutes, l'appli
renvoie ce qui est dû, plusieurs messages en attente regroupés en un seul (4 000 caractères au plus) ;
après un nouvel échec, l'essai suivant attend deux fois plus longtemps (6 h au plus) ; après 48 h, le
message est abandonné avec une ligne de journal. Le message du bouton « Tester » n'est jamais rejoué,
ni un message refusé pour de bon (jeton invalide, chat inconnu). `/api/diag` donne la taille de la file.

### Apparence

Paramètres, Apparence : nom affiché en haut à gauche, logo (PNG, JPEG, WebP ou SVG, 300 Ko maximum,
rangé dans `data/`), taille réglable au curseur avec aperçu en direct (hauteur du logo de 20 à 120 px, ou taille du nom sans logo), et thème clair, sombre ou selon le système. Le réglage vaut pour tous les
appareils.

### Paramètres

En tête, le bloc **Démarrer en 3 étapes** (le même que sur Aujourd'hui) tant qu'une étape manque. Puis,
dans l'ordre : les sources (Extension .fr, Zones ICANN, Sources et lectures), Telegram, Alertes, Avis IA.
Le reste est replié sous « Afficher les réglages avancés » : Résumé du matin, Résumé hebdomadaire, Écran
Veille, Premiums probables, Noms réservés, Prénoms récents (Insee), SEO, Score de l'Explorer, Lien « Réserver »,
Calendrier, Apparence. La carte Version est en bas. Sur téléphone, Paramètres s'ouvre par l'icône en haut à droite de
chaque écran.

### Calendrier

Paramètres, Calendrier, « Créer l'adresse » : une URL secrète au format ICS, à ajouter comme
calendrier par URL dans l'agenda du téléphone. Elle contient les expirations et les libérations
estimées, avec un rappel la veille. Elle s'ouvre sans mot de passe : la changer si elle a fuité.
Les lignes du fichier sont pliées à 75 octets, comme le demande la RFC 5545 : les agendas stricts
(Apple, Thunderbird) lisent les longs noms et descriptions sans rien tronquer.

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

L'écran d'accueil. En tête, les chiffres du jour : noms libérés depuis 24 h, libération estimée sous 48 h,
nouveaux avis « À prendre » reçus depuis 24 h sur un nom libre ou bientôt libre, regrets (noms ignorés
repris depuis par quelqu'un d'autre). Chaque chiffre ouvre exactement ses noms, même compte : les trois
premiers ouvrent la Veille sur cette liste seule (puce « Libérés depuis 24 h », « Libération sous 48 h » ou
« Nouveaux « À prendre » depuis 24 h », sans onglets ni filtres de la Veille, noms faibles compris ; la puce
efface le filtre et rend la Veille complète), les regrets ouvrent les Décisions du Portefeuille filtrées sur
« Repris seulement » (puce effaçable, aussi disponible à la main). Puis le bloc
**Démarrer en 3 étapes** (sources, Telegram, avis IA, avec « Configurer »), tant qu'une étape manque.
Enfin les 20 meilleurs noms libres ou libérés sous 48 h, non ignorés, sur la carte de la Veille, dans
l'ordre de la Veille pour l'usage choisi (Pour revendre / Pour exploiter, réglage commun) et avec ses
réglages par défaut (noms faibles et suites imprononçables masqués), et le bouton « Voir toute la Veille ».
Rien à prendre : « Rien à prendre aujourd'hui. Les prochaines libérations sont dans la Veille. ».

`GET /api/today?sort=resale|exploit` (défaut `resale`, calcul hors de la boucle) : `counts` (`freed_24h`,
`coming_48h`, `new_take`, `regrets`), `items` (format des éléments de `/api/drops`), `setup` (les trois
étapes : `key` `sources`, `telegram` ou `ai`, `title`, `text`, `done`). `/api/meta` donne les mêmes étapes
dans `steps`. Lisible avec le jeton en lecture seule, comme les autres écrans.

`GET /api/drops?focus=freed_24h|coming_48h|new_take` : la réponse de `/api/drops` réduite aux noms du chiffre
d'Aujourd'hui (même calcul que `counts`), avec `focus` rappelé ; une autre valeur : 422.

### Veille et Explorer : deux écrans, mêmes réglages

**Veille** : ce qui tombe et ce qui va tomber. Deux onglets : **Libérés** (réservables maintenant)
et **Bientôt libres** (en rédemption ou en suppression, avec la date estimée). **Explorer** : tous
les noms suivis, pour chercher. Grande barre de recherche, puis l'état : **Libres**, **Bientôt
libres** ou **Tous, même pris**, et le type de nom en mots simples (mots courants, anglais,
prénoms, tech et startups, médias et marques, secteurs, métiers, sport, animaux, cuisine, courts…).

Réglages communs, gardés d'un écran à l'autre :

- **Pour revendre / Pour exploiter** : d'abord les noms que l'IA conseille (« À prendre », « À
  surveiller »), par sa note de revente ou d'exploitation ; puis les noms sans avis, par score ; à la
  fin, ceux qu'elle déconseille (« À laisser »).
- **Masquer les noms faibles** (case cochée par défaut) : sans avis IA, score sous 30 ; avec un avis,
  « À laisser », même à bon score.
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
Portefeuille (nom du portefeuille). Arrêter le suivi se fait par le picto « Suivi », pas en double dans le menu. L'ajout au portefeuille se fait à la main, dans l'écran Portefeuille.

En .fr, un mot seulement anglais (should) ou une forme conjuguée (vais) perd une vingtaine de
points de forme et de sens par rapport au même mot bien placé : ils se lisent mal en .fr. La raison
est affichée sur la carte.

Menu Veille : les noms qui quittent les zones des extensions génériques (et le DNS de .co, .ai et
.io), suivis jusqu'à leur libération. Sont suivis tous les noms de 1 à 4 caractères (lettres, chiffres, tiret), les 20 000
mots français les plus courants (forme de base, sans accents), les noms de famille donnés plus
de 500 fois en France entre 1891 et 2000 (environ 21 700) et les prénoms donnés plus de 100 fois
entre 1900 et 2018 (environ 10 000). S'y ajoutent les 30 000 mots anglais les plus fréquents
(marques connues retirées), environ 580 mots des secteurs qui paient (assurance, crédit,
immobilier, droit, santé, travaux…), environ 200 mots en vogue et environ 35 000 combinaisons
(monassurance, plombier-lyon, insurancehub, dallasplumber…) et environ 4 000 noms de sites de
niche (forumdomotique, guidepotager, gardeningforum…), environ 1 000 expressions courantes
françaises et anglaises (bonplan, faitmaison, goodvibes…) et quatre listes thématiques (métiers,
sport, animaux, cuisine, voir « Listes de noms »). Une liste ajoutée n'est cherchée
dans une zone qu'à la lecture suivante : d'ici là, ses noms n'apparaissent pas pour cette
extension plutôt que d'y passer pour libres. Sources et licences des listes :
`app/data/SOURCES.md`. Un bouton Surveiller ajoute un nom à la liste principale.

#### Listes de noms

Chaque liste est un fichier de `app/data` (un nom par ligne, minuscules sans accents, du plus
courant au moins courant : le rang sert au score), déclaré dans `app/wordlists.py` (clé, libellé,
fichier). Un nom peut être dans plusieurs listes (avocat : mots, secteurs, métiers). Les listes
apparaissent telles quelles dans les catégories de l'Explorer et de la Veille, le Top, la carte et
le filtre de langue (langue relevée dans `app/data/langues.txt` pour les listes mêlées).

| Clé | Libellé | Contenu |
|---|---|---|
| word, form | Mots, Formes fléchies | 20 000 lemmes et 10 000 formes du Lexique |
| surname, firstname | Noms de famille, Prénoms | Insee |
| english | Mots anglais | 30 000 mots de wordfreq, marques retirées |
| sector, trend, startup | Secteurs porteurs, Tendances, Tech et startups | listes à la main |
| combo, niche, expr, media | Combinaisons, Sites de niche, Expressions, Médias et marques | construites par `scripts/build_lists.py` |
| job | Métiers | environ 500 métiers et professions, féminins et pluriels courants (plombier, notaire, infirmiere, avocats) |
| sport | Sport | environ 220 sports, disciplines, équipements et mots du sport (football, padel, raquette, marathon) |
| animal | Animaux | environ 220 animaux et mots des animaux de compagnie (chien, chaton, renard, croquettes) |
| food | Cuisine | environ 330 plats, ingrédients, boissons et mots de la table (recette, raclette, fraise, cafe) |

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
minutes de calcul sur le NAS. La recherche est répartie sur tous les cœurs sauf un (4 au plus) ;
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
   le plafond de rythme des nuits suivantes et envoient une alerte Telegram. Le plafond remonte
   de 25 % par nuit sans incident.
   Désactivable : case « Interroger le DNS du .fr chaque nuit ».
5. .co, .ai et .io : ces extensions de pays ne publient pas leur zone. Chaque jour, en même temps
   que le .fr, l'appli interroge leurs serveurs DNS pour les 50 000 meilleurs noms (plus de 2
   caractères), jamais interrogés d'abord : toutes les listes pour .co (vendu comme le .com) ; Mots
   anglais, Tendances et Tech et startups pour .ai et .io. Mêmes garde-fous, propres à chaque
   extension. Dès la première passe, chaque extension apparaît dans l'Explorer : délégué, « Enregistré » ;
   absent, « Absent du DNS, à vérifier » ; pas interrogé, hors des listes et des compteurs. Absent du DNS
   ne veut pas dire libre : le nom peut être enregistré sans serveurs DNS, réservé ou premium du registre,
   et Vérifier tranche ; seuls les 50 000 meilleurs noms par extension sont interrogés. Un nom délégué
   qui ne l'est plus (« sorti du DNS », souvent un nom qui expire) entre dans la Veille comme un nom
   sorti d'une zone : vérifié auprès du registre (RDAP, sinon WHOIS), puis Libérés ou Bientôt libres
   selon son état ; revenu dans le DNS, il en sort. Un nom absent dès sa première interrogation n'y
   entre pas, et une vérification à la main n'en fait pas un nom « sorti du DNS » (la date de sortie
   n'apparaît que pour un nom vu délégué puis absent). Les noms sortis avant cette version sont repris au démarrage et à chaque passe. Le résumé
   Telegram du matin les liste sous « Sortis du DNS », sauf ceux déjà listés avec leur état dans une
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
.fr est relu automatiquement : sans ça, les nouveaux noms passeraient pour jamais enregistrés.

#### Portefeuille

Les noms que tu possèdes : prix et date d'achat, renouvellement par an, échéance, prix demandé,
meilleure offre, statut (possédé, en exploitation, pas à vendre, en vente, vendu, abandonné), prix et date de vente, notes. Totaux :
investi, renouvellements par an, ventes, bénéfice. Un nom s'ajoute à la main, avec le formulaire
de l'écran. Un nom ajouté est aussi surveillé : la vérification RDAP part en arrière-plan, la
réponse n'attend pas (`checking`), et l'échéance réelle remplace la date saisie dès qu'elle est lue ;
rappel Telegram 30 puis 7 jours avant. Les ventes et les offres reçues sont ajoutées à
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

- **Top** : les 10, 50, 100, 200, 500 ou 1 000 premiers d'une liste (secteurs, métiers, sport, animaux,
  cuisine, tendances, sites de niche, mots, anglais, prénoms, noms…), pris ou non, avec l'état sur chaque extension en toutes lettres
  (probablement libre, pris, à venir, libre, réservé, premium probable). Un état ouvre la fiche. Les
  puces d'état de la liste sont masquées tant que le Top est ouvert : il montre tous les états.
  À côté de « Prénoms » (1900-2018), « Prénoms 2024 » (l'année la plus récente du fichier de l'Insee,
  voir « Listes de noms »), dès que le fichier a été lu : rang de l'Insee, nombre de naissances au
  survol, prénoms hors de l'univers sautés (`cat=firstname_recent` de `/api/explore/top`, avec `year`
  et `count` ; 409 tant qu'aucun fichier n'est lu ; `recent` de `/api/explore/rank` donne `key`, `year`
  et `label`, ou null).
- **Langue** : filtre Français / Anglais (Explorer et Veille). Listes mêlées : langue relevée à
  la construction (app/data/langues.txt) ; jargon des tendances absent des deux listes : les deux.
- **Fiche** : nuit prévue pour l'avis IA (place dans la file, 1 000 noms par nuit, horizon de 30
  nuits) et, pour un .fr, la prochaine interrogation DNS et la dernière réponse.
- **États** : Libres (libres et probablement libres), Bientôt libres (triés par libération la plus
  proche), Tous, même pris. Compteur sur chaque état avec le niveau « Tout » et sans score minimum ;
  avec un niveau, il est masqué : le compte exact demanderait de noter chaque nom à chaque clic.
- **Premiums probables** : noms de 3 caractères libres hors .fr, souvent vendus au prix premium
  par le registre (rien ne le confirme sans accès registrar). Masqués par défaut ; bouton « Premiums
  probables » pour les afficher. Paramètres : case pour diviser leur score par deux, et prix relevés
  à la main par extension pour 2 et 3 caractères, affichés sur ces noms.
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
  « alerte » par vue (`alert` de `PUT /api/explore/views`) : Telegram signale ses nouveaux noms, une fois
  par heure au plus.
- **Sélection multiple** : Vérifier, SEO, Surveiller ou Réservé en lot, bilan en un message.
- Recherche : le nom tapé sur toutes les extensions, puis les noms qui commencent pareil. Un nom
  absent de toutes les listes (monsuperprojet) s'affiche quand même, une ligne par extension suivie
  (ou celle tapée, monsuperprojet.fr) : « Vérifier » interroge le registre (`POST /api/explore/check`,
  qui renvoie alors l'état, le registrar et l'échéance sans score ni catégorie), puis Réserver ou
  Surveiller.
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
(`gandi_samples`). Première étape : la lecture des états et prix dans les listes viendra ensuite, calée sur
ces réponses.

Tri : score, fréquence (mot, nom ou prénom du plus courant au moins courant), prénoms de l'année la plus récente (Insee), longueur, SEO, avis IA. Un tri ne filtre pas : avec SEO ou avis IA, les noms analysés viennent d'abord, puis les autres par score. Chaque tri applique tous les filtres (recherche, SEO et avis IA compris pour la libération la plus proche). Les éléments de `/api/explore/rank`, `/api/drops`, `/api/today` et de la fiche portent `ranks` (rang du nom dans chaque liste, `{"firstname": 12, "word": 4881}`) ; avec `sort=freq`, `freq_list` dit la liste qui donne l'ordre (celle du filtre de catégorie, sinon celle où le nom est le mieux placé).

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
grille (`version` 4) n'en a pas et sera renoté. Validation de
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

Alerte « À prendre » : dès qu'un nom de la Veille passe libre ou entre en rédemption avec un avis « À
prendre » et une note de revente au seuil réglé (70 par défaut, désactivable ; Paramètres, carte
« Alertes » : case et seuil, `GET/PUT /api/settings/alerts`), un message Telegram
part avec le verdict, les notes, le prix utilisateur final, la raison, le lien vers la fiche et le lien
Réserver. Une alerte par nom et par état ; le résumé du matin reste, lui récapitule.

Le NAS publie la liste à noter (`GET /api/ai/pending`) : noms libres ou bientôt libres d'abord, puis
probablement libres, puis enregistrés, les plus prometteurs d'abord, avec la consigne et des exemples
de référence pour garder des notes cohérentes. Il réimporte les avis (`POST /api/ai/results`) après
contrôle du format, le même pour chaque avis : exactement quinze champs, sous-notes entières de 0
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

Le jeton se crée dans Paramètres > Avis IA ; il n'est montré qu'une fois. Il ouvre l'échange des
avis et, en lecture seule (GET), le diagnostic `/api/diag` (version, mémoire, zones, .fr, DNS, avis
IA, compteurs, tâches planifiées, exécutions manquées ou en erreur du planificateur, file Telegram,
dernières erreurs du journal), les écrans (Domaines, Veille,
Explorer, Top, fiches) et `/api/ai/rated` (avis enregistrés, filtres `verdict` et `tld`). Jamais
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
  confirmer »). Pas de « Libération constatée », pas compté dans Aujourd'hui, pas d'alerte « À prendre » ;
  le résumé du matin les liste à part, « à confirmer chez un registrar », et l'alerte d'un nom surveillé le
  dit. Réserver reste possible ; refusé par le registrar, « Plus », Réservé par registre, le sort des noms
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
