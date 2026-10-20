# Listes de noms surveillés

Chaque fichier est trié du plus fréquent au moins fréquent : le rang sert au score de l'Explorer.

## mots.txt

Les 20 000 lemmes les plus fréquents de Lexique 3.83 (somme des fréquences films et livres),
en minuscules, sans accents (« œ » devient « oe »). Les lemmes à espace ou apostrophe sont exclus.

Source : Lexique 3.83, B. New et C. Pallier, www.lexique.org, licence CC BY-SA 4.0.
Fichier lu dans le paquet PyPI pylexique 1.5.1, qui le redistribue. Ce fichier dérivé est
diffusé sous la même licence CC BY-SA 4.0.

## formes.txt

Les 10 000 formes fléchies les plus fréquentes de Lexique 3.83 (pluriels, féminins,
conjugaisons : « maisons », « belle », « avait »), absentes des trois autres listes. Même
normalisation que mots.txt. Même source et même licence CC BY-SA 4.0.

## natures.txt

Nature de chaque entrée de mots.txt et formes.txt, d'après la catégorie grammaticale du
Lexique : verb (verbes et auxiliaires), noun, adj, adv, other (mots outils). Plusieurs natures
possibles, séparées par des virgules. Même source et même licence CC BY-SA 4.0.

## noms.txt

Noms de famille donnés plus de 500 fois en France entre 1891 et 2000 (métropole, sauf personnes
nées avant 1946 et décédées avant 1972 ; outre-mer à partir de 1900), soit 21 817 noms avant
normalisation. En minuscules, sans accents, espaces et apostrophes retirés (« Le Gall » devient
« legall »).

Source : Insee, fichier des noms, données publiques. Lu dans le dépôt github.com/cnovel/prenoms
(fichier prenoms/data/dist.all.txt), qui le classe par fréquence avec ce seuil de 500.

## prenoms.txt

Prénoms donnés plus de 100 fois en France entre 1900 et 2018 (hors Mayotte), filles et garçons
réunis, environ 10 000. D'abord les 4 264 donnés plus de 500 fois, dans leur ordre d'origine,
puis les autres, filles et garçons en alternance du plus au moins donné. En minuscules, sans accents ; les prénoms composés gardent leur tiret (« jean-pierre »).

Source : Insee, fichier des prénoms, données publiques. Lu dans le même dépôt
github.com/cnovel/prenoms (fichiers dist.prenoms.f.all.txt et dist.prenoms.m.all.txt).

## prenoms_us.txt

Les 10 000 prénoms les plus donnés aux États-Unis entre 1880 et 2017, filles et garçons réunis
(naissances additionnées, prénom en minuscules), du plus au moins donné ; le dernier l'a été
1 088 fois. Réunis aux prénoms français dans la catégorie Prénoms : 3 001 sont dans les deux
listes (thomas, marie), 6 999 seulement ici (bernice, katelyn).

Source : Social Security Administration (cartes de sécurité sociale, prénoms donnés au moins
5 fois une année), données publiques du gouvernement américain. Lu dans le jeu babynames de
github.com/hadley/babynames (table babynames, total par prénom sur toutes les années).

## noms_us.txt

Les 21 689 noms de famille les plus portés aux États-Unis au recensement de 2010, autant que
noms.txt, du plus au moins porté (à égalité, l'ordre du fichier) ; le dernier l'est par
1 120 personnes. En minuscules, sans accents, espaces et apostrophes retirés ; une ligne qui n'est
pas un nom (« ALL OTHER NAMES » dans d'autres éditions) ou un libellé invalide pour l'appli serait
écartée. Réunis aux noms français dans la catégorie Noms de famille : 3 144 sont dans les deux
listes (martin, thomas), 18 545 seulement ici (smith, ford).

Source : U.S. Census Bureau, noms de famille du recensement de 2010 (noms portés par 100
personnes au moins), données publiques du gouvernement américain. Lu dans le dépôt
github.com/fivethirtyeight/data (most-common-name/surnames.csv).

Pour reconstruire les deux fichiers, sans réseau, depuis les fichiers téléchargés :
`python scripts/build_names_us.py PRENOMS_CSV SURNAMES_CSV` (PRENOMS_CSV : colonnes name,n, le
total des naissances par prénom tiré de babynames ; SURNAMES_CSV : le fichier du recensement).

## Prénoms récents (hors de ce dossier)

Pas de fichier ici : le NAS télécharge chaque mois le fichier national des prénoms de l'Insee
(« Fichier des prénoms », une édition par an, naissances jusqu'à l'année précédente ou celle
d'avant), trouvé par l'API de data.gouv.fr ou à l'adresse saisie dans Paramètres. Il en garde,
dans son dossier de données (`prenoms_recents.json`), les 2 000 prénoms les plus donnés des trois
années les plus récentes, normalisés comme prenoms.txt, filles et garçons réunis. Rien n'est
téléchargé à la construction de l'image ni pendant les tests.

Source : Insee, fichier des prénoms, Licence Ouverte (Etalab).

## mots_courts.txt

Toutes les formes de 2 à 4 lettres de Lexique 3.83, même rares (« oye », « ovni »), pour
reconnaître un vrai mot parmi les noms courts. Même normalisation, même source, même licence.

## trigrammes.json

Fréquences des suites de trois lettres (avec début et fin de mot) dans les formes de Lexique
3.83 et les listes de noms et prénoms, chaque mot compté une fois. Sert à juger si un nom court
se prononce en français. Dérivé de Lexique 3.83 (CC BY-SA 4.0) et des fichiers Insee.

## anglais.txt

Les 30 000 mots anglais les plus fréquents de wordfreq 3.1.1 (liste « large » en anglais),
en lettres seules, 2 lettres au moins. Marques connues retirées (app/data/marques.txt,
liste non exhaustive ; les marques qui sont aussi des mots courants restent). Contient aussi des
prénoms et des lieux anglophones, utiles en .com.

Source : wordfreq, R. Speer, github.com/rspeer/wordfreq, données sous licence CC BY-SA 4.0.
Ce fichier dérivé est diffusé sous la même licence.

## secteurs.txt

Mots des secteurs où les annonceurs paient cher (assurance, crédit, immobilier, droit, santé,
travaux, auto, voyage, emploi, formation, énergie, commerce), en français et en anglais en
alternance. Listes choisies et ordonnées à la main (scripts/lists/secteurs_fr.txt et
sectors_en.txt) : jugement, pas de données de coût par clic.

## tendances.txt

Mots en vogue en octobre 2026 (IA, énergie, mobilité, santé, consommation responsable…),
français et anglais, choisis et ordonnés à la main (scripts/lists/tendances.txt). À revoir
chaque année.

## combinaisons.txt

Noms composés construits par scripts/build_lists.py, les meilleurs d'abord :
- « mon / ma » et « le / la » + nom commun, selon le genre donné par Lexique 3.83 ;
- mot de secteur + préfixe ou suffixe (sos, allo, pascher, pro, direct, my, get, hq, hub…) ;
- métier + grande ville française, collé et avec tiret (plombierlyon, plombier-lyon) ;
- service + grande ville américaine, dans les deux sens (plumberdallas, dallasplumber).
Les combinaisons déjà présentes dans une autre liste sont retirées. Ordre : produit des rangs
du mot et de l'ajout, familles en alternance.

## niches.txt

Format de site + thème de passion ou de niche : forumdomotique, guidepotager, jardinagefacile,
gardeningforum. Environ 110 thèmes français avec 25 formats français (pour le .fr), environ 75
thèmes anglais avec 20 formats anglais (pour le .com), choisis et ordonnés à la main
(scripts/lists/themes_fr.txt, themes_en.txt, formats.txt). Version collée seulement.

## expressions.txt

Expressions courantes collées, environ 1 000, françaises (bonplan, faitmaison, coupdecoeur…) et
anglaises (goodvibes, homesweethome, dealoftheday…) en alternance, rédigées et ordonnées à la main
(scripts/lists/expressions_fr.txt et expressions_en.txt). Slogans de marques retirés. Les
expressions déjà présentes dans une autre liste sont retirées.


## medias.txt

Noms de média ou de marque, sur quatre motifs, du plus fréquent au moins fréquent, en alternance :

- article + nom : le, la, l', les + les 600 noms les plus fréquents de chaque genre et les 600 pluriels
  les plus fréquents (5 lettres au moins) de Lexique 3.83 (lequipe, lecole). Mots commençant par h
  écartés (élision incertaine), mots négatifs écartés (scripts/lists/negatifs.txt, rédigée à la main) ;
- « les » + thème ou secteur au pluriel, si ce pluriel est employé au moins une fois par million de
  mots dans le Lexique (lesnumeriques, lesrecettes) ;
- « french » + mot tech, et « fr » devant quelques mots qui commencent par une voyelle (frandroid,
  frenchweb), gardés seulement s'ils se prononcent (modèle de app/pronounce.py) ; mots tech choisis à
  la main (scripts/lists/tech_en.txt) ;
- journal, planete, monde, univers, gazette + du, de la, de l', des + thème (journaldelinformatique).

Les noms déjà présents dans une autre liste sont retirés. Pour la part tirée du Lexique, même source
et même licence CC BY-SA 4.0 que mots.txt.
## langues.txt

Langue (fr, en ou les deux) de chaque nom des listes mêlées : secteurs, tendances, combinaisons,
sites de niche, expressions, métiers, sport, animaux, cuisine, et listes fournies (termes stars,
mots-clés, métier + ville) quand elle se déduit des mots qui composent le nom. Relevée à la construction.

Pour reconstruire les fichiers : `python scripts/build_lists.py DOSSIER_WORDFREQ LEXIQUE383_TXT` ;
`python scripts/build_lists.py --themes` pour les seules listes thématiques et langues.txt ;
`python scripts/build_lists.py --provided` pour la langue des listes fournies (elles ne sont jamais réécrites).

## startup.txt

Mots courts pour la tech, l'IA et les startups, anglais et français, choisis à la main
(scripts/lists/startup.txt), du plus porteur au moins. Sert surtout au DNS de .ai et .io.

## metiers.txt, sport.txt, animaux.txt, cuisine.txt

Listes thématiques en français (l'équivalent anglais seulement s'il manque à anglais.txt), du plus
courant au moins courant, constituées à la main, 2026-10 (scripts/lists/metiers.txt, sport.txt,
animaux.txt, cuisine.txt) :

- metiers.txt : métiers et professions, féminins et pluriels courants, environ 500 ;
- sport.txt : sports, disciplines, équipements et mots du sport, environ 220 ;
- animaux.txt : animaux et mots des animaux de compagnie, environ 220 ;
- cuisine.txt : plats, ingrédients, boissons et mots de la table, environ 330.

## sigles.txt

Sigles et abréviations d'usage courant de 2 à 4 lettres (tv, cv, rh, gps, saas), constituée à la
main, 2026-10. Sert au score (mesure « sigle connu »), pas à la surveillance.

## marques.txt

Marques connues, liste non exhaustive à la main : retirées de anglais.txt à la construction, et
mesure « marque » du score (un nom identique ne se revend pas sans risque).

## premium.txt

Termes stars : mots et expressions génériques à forte valeur de revente, du plus au moins précieux, une
entrée par ligne, en lettres minuscules et chiffres. Liste rédigée par IA, relue, 2026-10. Fichier fourni :
scripts/build_lists.py ne l'écrit pas. 4 800 entrées, 26 secteurs, termes absents des autres listes.

## motscles.txt

Mots-clés à forte valeur (assurance, crédit, santé, énergie, droit…), réunion de plusieurs listes françaises
et anglaises, du plus au moins précieux, une entrée par ligne, mots collés. Liste rédigée par IA, relue,
2026-10. Fichier fourni : 24 271 entrées, six blocs (finance, maison et énergie, santé, consommation en
français ; finance et santé, tech et services en anglais) entremêlés par rang, sans doublon.

## local.txt

Métier + ville, collés (agenceimmobiliereparis, courtiercreditlyon), du plus au moins précieux : 191 métiers
et services locaux, 165 villes, départements et régions. Liste rédigée par IA, relue, 2026-10. Fichier fourni :
15 831 entrées.

## marques_inventees.txt

Noms de marque (7 567) : six styles relus un à un et mêlés par rang, du plus au moins réussi : lieux et
légendes hors de France (1 125 : zanzibar, cervin), mots étrangers et diminutifs (865 : bellina, hanabi), mots réels
peu courants détournés (1 289 : colibri, toucan), noms inventés par syllabes (3 000 : kinia, vilio), composés
rythmés (771 : boumboum, kirakira), mots-valises (517 : boulango, cloudio). Marques connues, communes françaises
(de mémoire, sans liste officielle) et sens négatifs écartés. Pas de langue. Liste rédigée par IA, relue, 2026-10.

## Mes ajouts (hors de ce dossier)

Pas de fichier ici : les noms ajoutés depuis l'Explorer (« Ajouter aux noms suivis ») sont rangés dans le
dossier de données du NAS, `mes_ajouts.txt`, dans l'ordre d'ajout. Rien n'est livré avec l'image.
