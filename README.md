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
| Résumé hebdomadaire | jour et heure choisis |
| Analyse IA demandée prête, lien vers la fiche | dans l'heure qui suit la demande |
| Panne : zone en erreur ou pas lue depuis 36 h, fichier .fr en erreur, pause .fr de plus d'une heure, notation IA muette depuis 36 h | à l'apparition, puis « Réglé » à la disparition |
| Passe DNS arrêtée (refus ou silence du registre) | à l'arrêt |

Les liens utilisent `APP_URL`, sinon l'adresse vue par le navigateur à la dernière ouverture de l'appli.

### Apparence

Paramètres, Apparence : nom affiché en haut à gauche, logo (PNG, JPEG, WebP ou SVG, 300 Ko maximum,
rangé dans `data/`), taille réglable au curseur avec aperçu en direct (hauteur du logo de 20 à 120 px, ou taille du nom sans logo), et thème clair, sombre ou selon le système. Le réglage vaut pour tous les
appareils.

### Calendrier

Paramètres, Calendrier, « Créer l'adresse » : une URL secrète au format ICS, à ajouter comme
calendrier par URL dans l'agenda du téléphone. Elle contient les expirations et les libérations
estimées, avec un rappel la veille. Elle s'ouvre sans mot de passe : la changer si elle a fuité.

### Veille et Explorer : deux écrans, mêmes réglages

**Veille** : ce qui tombe et ce qui va tomber. Deux onglets : **Libérés** (réservables maintenant)
et **Bientôt libres** (en rédemption ou en suppression, avec la date estimée). **Explorer** : tous
les noms suivis, pour chercher. Grande barre de recherche, puis l'état : **Libres**, **Bientôt
libres** ou **Tous, même pris**, et le type de nom en mots simples (mots courants, anglais,
prénoms, tech et startups, médias et marques, courts…).

Réglages communs, gardés d'un écran à l'autre :

- **Pour revendre / Pour exploiter** : d'abord les noms que l'IA conseille (« À prendre », « À
  surveiller »), par sa note de revente ou d'exploitation ; puis les noms sans avis, par score ; à la
  fin, ceux qu'elle déconseille (« À laisser »).
- **Masquer les noms faibles** (case cochée par défaut) : sans avis IA, score sous 30 ; avec un avis,
  « À laisser », même à bon score.
- **Extensions** : une, plusieurs ou toutes.
- **Avancé** (replié) : les filtres fins (nature, longueur, fréquence, SEO, avis IA, score
  minimum…), et dans l'Explorer les classements, la carte, les premiums et les vues enregistrées.

Chaque nom est une carte : le nom, le score, et une phrase (verdict IA, puis note de revente et
prix utilisateur final, ou note d'exploitation ; sinon les raisons du score, par exemple « 5 lettres · 153e mot le plus courant »).
Dans la Veille, **Nouveaux cette nuit** ne garde que les noms libérés ou entrés en rédemption depuis
24 h ; Réserver et Surveiller sont sur chaque carte. Tri en masse : cases à cocher (« Tout
sélectionner » pour les noms affichés), puis Surveiller, Analyse IA, Revérifier ou Masquer ; un nom
masqué quitte la Veille mais reste dans l'Explorer (Paramètres : « Tout réafficher »). L'état des
lectures (zones, .fr, DNS, historique) est dans Paramètres, carte « Sources et lectures » ; une
ligne d'état reste en haut de la Veille. Sur téléphone, l'en-tête défile avec la liste.

Fiche d'un nom : ligne de décision en tête (libération estimée, premium probable, verdict IA, prix
utilisateur final) et action mise en avant : Réserver pour un nom libre, Surveiller pour un nom qui
va se libérer, Analyse IA pour un nom pas encore noté. Surveiller reste disponible dans tous les cas.

En .fr, un mot seulement anglais (should) ou une forme conjuguée (vais) perd une vingtaine de
points de forme et de sens par rapport au même mot bien placé : ils se lisent mal en .fr. La raison
est affichée sur la carte.

Menu Veille : les noms qui quittent les zones des extensions génériques, suivis jusqu'à leur
libération. Sont suivis tous les noms de 1 à 4 caractères (lettres, chiffres, tiret), les 20 000
mots français les plus courants (forme de base, sans accents), les noms de famille donnés plus
de 500 fois en France entre 1891 et 2000 (environ 21 700) et les prénoms donnés plus de 100 fois
entre 1900 et 2018 (environ 10 000). S'y ajoutent les 30 000 mots anglais les plus fréquents
(marques connues retirées), environ 580 mots des secteurs qui paient (assurance, crédit,
immobilier, droit, santé, travaux…), environ 200 mots en vogue et environ 35 000 combinaisons
(monassurance, plombier-lyon, insurancehub, dallasplumber…) et environ 4 000 noms de sites de
niche (forumdomotique, guidepotager, gardeningforum…), et environ 1 000 expressions courantes
françaises et anglaises (bonplan, faitmaison, goodvibes…). Une liste ajoutée n'est cherchée
dans une zone qu'à la lecture suivante : d'ici là, ses noms n'apparaissent pas pour cette
extension plutôt que d'y passer pour libres. Sources et licences des listes :
`app/data/SOURCES.md`. Un bouton Surveiller ajoute un nom à la liste principale.

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
3. Toutes les requêtes vers l'Afnic (dates anniversaire, suppressions, boutons « Vérifier »)
   passent par une seule file, sous un plafond par minute réglable (2 par défaut, soit 2 880 par
   jour au plus). L'Afnic ne publie pas de limite. Au premier signal de trop de requêtes, pause
   d'une heure, doublée à chaque nouveau signal (24 h au plus), et durée demandée par l'Afnic
   respectée. Paramètres, carte « Sources et lectures », montre en direct le nom en cours, le travail du jour et du
   lendemain, et les dernières requêtes.
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
   extension. Un nom délégué qui ne l'est plus (« sorti du DNS ») part dans le résumé Telegram du
   matin : souvent un nom qui expire, à vérifier
   en RDAP. Pas de date de libération estimée ; un nom absent du DNS peut être enregistré sans
   serveurs, réservé ou premium du registre.

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
l'Explorer, gardés d'une visite à l'autre. Tri : les meilleurs d'abord (selon revendre ou
exploiter), les plus récents, ou la libération la plus proche. Chaque nom ouvre la même fiche.
Domaines a aussi une colonne Valeur (score et avis IA), triable, et des filtres par extension et
par verdict IA.

Quand les listes suivies changent (formes fléchies ajoutées, par exemple), le dernier fichier
.fr est relu automatiquement : sans ça, les nouveaux noms passeraient pour jamais enregistrés.

#### Portefeuille

Les noms que tu possèdes : prix et date d'achat, renouvellement par an, échéance, prix demandé,
meilleure offre, statut (possédé, en exploitation, pas à vendre, en vente, vendu, abandonné), prix et date de vente, notes. Totaux :
investi, renouvellements par an, ventes, bénéfice. « J'ai acheté » dans la fiche d'un nom l'ajoute
avec la date du jour. Un nom ajouté est aussi surveillé : l'échéance réelle vient du RDAP (sinon la
date saisie), rappel Telegram 30 puis 7 jours avant. Les ventes et les offres reçues sont ajoutées à
la consigne de notation IA comme repères de prix réels, et l'estimation IA s'affiche à côté du prix
obtenu.

#### Explorer

Tous les noms suivis, extension par extension : les 1 926 220 noms possibles de 1 à 4 caractères,
puis les mots, noms de famille et prénoms. L'état vient des données déjà sur le NAS, sans aucune
requête : zones ICANN lues chaque jour, open data de l'Afnic, données de la Veille par-dessus.

- **Top** : les 10, 50, 100, 200, 500 ou 1 000 premiers d'une liste (secteurs, tendances, sites de niche,
  mots, anglais, prénoms, noms…), pris ou non, avec l'état sur chaque extension. Un état ouvre la fiche.
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
  prénoms), nombre de noms à saisir par case. Une case ouvre la liste filtrée.
- **Liste** : les meilleurs d'abord (selon revendre ou exploiter), libération la plus proche, les plus
  courts, les plus courants, ou score seul. Une carte ouvre la fiche du nom.
- **Fiche** : la même dans Domaines, Veille et Explorer. État, score détaillé, raisons,
  catégories, nature, fréquence, historique .fr, SEO, dernière réponse RDAP ou WHOIS (registrar,
  dates, statuts, DNS), autres extensions, et les actions (Vérifier, Réserver chez Gandi,
  Surveiller ou Voir dans Domaines, SEO, Réservé).
- **Avancé** (panneau du bas sur téléphone) : catégorie, nature des mots (verbes, noms communs,
  adjectifs, adverbes, mots outils), longueur, fréquence (top 100 à top 5 000), pris sur une
  autre extension, SEO (analysé, PageRank 1, 3 ou 5 et plus, archivé 5 ans et plus), score
  minimum. Les compteurs suivent les filtres ; les filtres sont mémorisés. Le filtre SEO ne
  porte que sur les noms déjà analysés.
- **Extensions** : une ou plusieurs à la fois (« Toutes » au-delà de deux). Les listes sont
  fusionnées dans l'ordre du tri choisi, les compteurs additionnés.
- **Vues enregistrées** : un jeu de filtres nommé, rappelé d'un geste. La vue en cours est
  surlignée ; sa suppression demande confirmation et laisse les filtres affichés en place.
- **Sélection multiple** : Vérifier, SEO, Surveiller ou Réservé en lot.
- Recherche : le nom tapé sur toutes les extensions, puis les noms qui commencent pareil.
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

Forme et sens : poids calés sur des avis IA détaillés (grille v5) d'un échantillon de noms de toutes
les catégories, une famille de poids par sorte de nom (mots, noms de personnes, suites courtes),
dans `app/data/score_model.json`. Concordance de rang avec les avis IA, sur des noms gardés hors du
calage : 0,25 pour l'ancien barème, 0,85 pour celui-ci. Point faible connu : entre deux mots
courants, la valeur tient au sens et au marché, que seule la notation IA juge bien. Demande et
historique : taux de base par extension et par forme (1 à 4 lettres prononçables ou non, chiffres,
mélanges, mots), mesurés sur les zones lues ; la raison dit « courant pour cette forme » quand le
signal est banal. Le score de base de tous les noms est précalculé à la construction de l'image
(`app/precompute.py`, `scores.bin`) ; le classement écarte d'abord les noms sur cette borne et ne
fait le calcul complet que pour ceux qui peuvent entrer dans la page.

SEO à la demande (bouton « SEO » d'un nom, ou « SEO des 50 premiers ») : Open PageRank donne une
note de 0 à 10 tirée des liens (Common Crawl), avec une clé gratuite à saisir dans Paramètres ;
la Wayback Machine donne les années où le site a été archivé, sans clé. Le nombre exact de
backlinks n'existe que chez des services payants (Ahrefs, Majestic, Moz).

Tri : score, fréquence (mot, nom ou prénom du plus courant au moins courant), longueur, SEO, avis IA. Un tri ne filtre pas : avec SEO ou avis IA, les noms analysés viennent d'abord, puis les autres par score.

#### Avis IA

Pour chaque nom complet en .fr, .com ou .org (velo.fr, ciel.org ; les autres extensions valent trop peu pour être notées) : une grille de neuf sous-notes sur 10
(prononciation, test radio, mémorisation, sens, marque, marché, adéquation à l'extension,
acheteurs possibles, risque), deux fourchettes de prix (revente rapide à un investisseur ;
utilisateur final, avec de la patience) et une raison avec le type d'acheteur. Les notes
d'exploitation et de revente sur 100 et le verdict sont calculés par le NAS depuis la grille
(`app/ai_format.py`, `derive`) : ils ne peuvent plus se contredire. Le prix utilisateur final doit
tomber dans le palier de la note pour son extension (repères indicatifs, un palier de tolérance ;
pas de plafond pour les noms de 1 ou 2 caractères ni pour une note de revente de 90 et plus). Validation de
la grille : 50 noms témoins notés par plusieurs agents indépendants, écart-type moyen de 3 points,
même verdict dans 94 % des cas.

Second passage : chaque nuit, les 50 avis les plus éloignés de l'estimation du score (25 points
d'écart au moins) reviennent en tête de lot, avec l'ancienne note et l'estimation ; l'IA les renote à
froid, une seule fois.

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
IA, compteurs, tâches planifiées, dernières erreurs du journal), les écrans (Domaines, Veille,
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
enregistré d'après l'open data ; pour une zone ICANN, absent de la zone. Un nom enregistré sans
serveurs DNS est absent de la zone lui aussi. Toujours vérifier avant de conclure.

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
