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
