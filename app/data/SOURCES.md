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

Prénoms donnés plus de 500 fois en France entre 1900 et 2018 (hors Mayotte), filles et garçons
réunis. En minuscules, sans accents ; les prénoms composés gardent leur tiret (« jean-pierre »).

Source : Insee, fichier des prénoms, données publiques. Lu dans le même dépôt
github.com/cnovel/prenoms (fichiers dist.prenoms.f.all.txt et dist.prenoms.m.all.txt).

## mots_courts.txt

Toutes les formes de 2 à 4 lettres de Lexique 3.83, même rares (« oye », « ovni »), pour
reconnaître un vrai mot parmi les noms courts. Même normalisation, même source, même licence.

## trigrammes.json

Fréquences des suites de trois lettres (avec début et fin de mot) dans les formes de Lexique
3.83 et les listes de noms et prénoms, chaque mot compté une fois. Sert à juger si un nom court
se prononce en français. Dérivé de Lexique 3.83 (CC BY-SA 4.0) et des fichiers Insee.

## anglais.txt

Les 30 000 mots anglais les plus fréquents de wordfreq 3.1.1 (liste « large » en anglais),
en lettres seules, 2 lettres au moins. Marques connues retirées (scripts/lists/marques.txt,
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

Pour reconstruire les quatre fichiers : `python scripts/build_lists.py DOSSIER_WORDFREQ LEXIQUE383_TXT`.
