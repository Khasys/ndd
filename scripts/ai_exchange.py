"""Échange avec le NAS pour la notation IA (utilisé par la tâche planifiée).

  python scripts/ai_exchange.py pull 1000 lot.json      # liste à noter + consigne + format
  python scripts/ai_exchange.py pull 1000 lot.json 2000 # tranche suivante (noms 2000 à 2999), sessions en parallèle
  python scripts/ai_exchange.py requested lot.json      # analyses demandées depuis l'appli (tâche horaire)
  python scripts/ai_exchange.py guide lot.json          # la consigne seule, à lire une fois
  python scripts/ai_exchange.py show lot.json 0 250     # noms 0 à 249, une ligne par nom (format compact)
  python scripts/ai_exchange.py pack lot.json avis.json avis-1.txt avis-2.txt...  # lignes compactes vers JSON
  python scripts/ai_exchange.py check lot.json avis.json # contrôle le format, sans rien envoyer
  python scripts/ai_exchange.py push lot.json avis.json  # contrôle puis envoie les avis conformes
  python scripts/ai_exchange.py escalation /tmp/esc       # repêchage et panel : /tmp/esc/rescue.json et panel.json
  python scripts/ai_exchange.py escalation-push /tmp/esc/panel.json avis-1.json ... avis-10.json  # un fichier par correcteur
  python scripts/ai_exchange.py exploit rent.json        # analyses de rentabilité demandées (tâche du quart d'heure)
  python scripts/ai_exchange.py exploit-check rent.json rapports.json  # contrôle des rapports, sans rien envoyer
  python scripts/ai_exchange.py exploit-push rent.json rapports.json   # contrôle puis envoie les rapports conformes

Format compact (moins de texte à lire et à écrire, mêmes informations, même grille) : une ligne par nom,
  nom|oral,radio,memo,sens,marque,marche,ext,acheteurs,risque|prix|raison|acheteur type
prix : « - » pour le palier standard de la note de revente (calculé comme au NAS), ou
« revente rapide basse,haute,utilisateur final bas,haut » quand le nom s'écarte du palier.
Le verdict et les notes sur 100 sont calculés depuis les sous-notes.

Variables d'environnement : DW_URL (ex. https://domaines.exemple.fr) et DW_AI_TOKEN (jeton créé
dans Paramètres > Avis IA). Le jeton n'est jamais affiché ni écrit dans un fichier.
À lancer depuis la racine du dépôt : le contrôle est celui du NAS (app/ai.py).
"""

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.ai_format import CRITERIA_V5, check_any, default_prices, derive, is_v5, summary  # noqa: E402  même règle que le NAS, sans dépendance
from app.exploit_format import clean as clean_report  # noqa: E402
from app.wordlists import LABELS, NATURES  # noqa: E402


def call(method: str, path: str, body: dict | None = None) -> dict:
    url = os.environ.get("DW_URL", "https://domaines.skky.fr").rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    # Agent explicite : Cloudflare refuse la signature par défaut de Python (erreur 1010).
    headers = {"Content-Type": "application/json", "User-Agent": "domain-watch-ai/1.0"}
    # Jeton en variable d'environnement ; s'il est rangé en secret réseau, le proxy l'ajoute lui-même.
    if os.environ.get("DW_AI_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["DW_AI_TOKEN"]
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read())


def verify(lot_path: str, results_path: str) -> tuple[list, list]:
    """Avis conformes, et problèmes : format, nom absent du lot, nom du lot sans avis, doublon."""
    with open(lot_path, encoding="utf-8") as f:
        asked = [n["name"] for n in json.load(f)["names"]]
    with open(results_path, encoding="utf-8") as f:
        results = json.load(f)
    if not isinstance(results, list):
        sys.exit("Le fichier d'avis doit être une liste JSON.")
    wanted = set(asked)
    good, problems, seen = [], [], set()
    for item in results:
        reason = check_any(item, lambda name: name in wanted)
        name = item.get("name") if isinstance(item, dict) else None
        if not reason and name in seen:
            reason = "doublon"
        if reason:
            problems.append((name, reason))
        else:
            seen.add(name)
            good.append(item)
    problems += [(name, "pas d'avis") for name in asked if name not in seen and name not in {p[0] for p in problems}]
    return good, problems


# Libellés courts du format compact : catégories de forme, puis celles des listes (wordlists.LABELS).
SHAPES = {"letters": "lettres", "digits": "chiffres", "mixed": "lettres et chiffres", "hyphen": "tiret", "long": "long"}


def line(n: dict) -> str:
    """Un nom du lot sur une ligne : les mêmes informations que le JSON, sans clés ni indentation."""
    ranks = n.get("ranks") or {}
    cats = []
    for c in n.get("categories") or []:
        label = SHAPES.get(c) or LABELS.get(c, c).lower()
        cats.append(f"{label} {ranks[c]}e" if c in ranks else label)
    cats += [f"{LABELS.get(c, c).lower()} {r}e" for c, r in ranks.items() if c not in (n.get("categories") or [])]
    parts = [n["name"], n.get("status") or "", ", ".join(cats) or "hors listes"]
    if n.get("natures"):
        parts.append("nature " + ", ".join(NATURES.get(x, x).lower() for x in n["natures"]))
    parts.append("pris en " + ", ".join("." + t for t in n["taken_elsewhere"]) if n.get("taken_elsewhere") else "pris ailleurs : non")
    if n.get("base") is not None:
        parts.append(f"base {n['base']}")
    h = n.get("fr_history")
    if h:
        parts.append(f".fr créé {h.get('created_on') or '?'}" + (f", supprimé {h['removed_on']}" if h.get("removed_on") else ""))
    if n.get("requested"):
        parts.append("analyse demandée")
    if n.get("ventes_comparables"):
        parts.append("ventes comparables " + ", ".join(n["ventes_comparables"]))
    r = n.get("review")
    if r:
        parts.append(f"second passage : ancienne note {r.get('previous_note')}, estimation du score {r.get('score_estimate')}")
    known = {"name", "status", "categories", "ranks", "natures", "taken_elsewhere", "base", "fr_history", "review", "requested",
             "ventes_comparables"}
    parts += [f"{k} {json.dumps(v, ensure_ascii=False)}" for k, v in n.items() if k not in known]  # rien de perdu
    return " | ".join(parts)


def pack(lot_path: str, out_path: str, sources: list[str]) -> list[tuple[str, str]]:
    """Lignes compactes vers la liste JSON du format v5 ; problèmes de lecture, nom par nom."""
    with open(lot_path, encoding="utf-8") as f:
        asked = {n["name"] for n in json.load(f)["names"]}
    items, problems = [], []
    for path in sources:
        with open(path, encoding="utf-8") as f:
            rows = [r.strip() for r in f if r.strip() and not r.lstrip().startswith("#")]
        for row in rows:
            cells = [c.strip() for c in row.split("|")]
            name = cells[0]
            if len(cells) != 5:
                problems.append((name, "5 champs séparés par | attendus : nom|9 sous-notes|prix|raison|acheteur type"))
                continue
            name, notes, prices, reason, buyer = cells
            try:
                values = [int(v) for v in notes.split(",")]
            except ValueError:
                values = []
            if len(values) != len(CRITERIA_V5):
                problems.append((name, f"{len(CRITERIA_V5)} sous-notes attendues ({','.join(CRITERIA_V5)})"))
                continue
            item = {"name": name, **dict(zip(CRITERIA_V5, values))}
            if name not in asked:
                problems.append((name, "nom absent du lot"))
                continue
            if prices not in ("-", ""):
                try:
                    ql, qh, fl, fh = (int(v) for v in prices.split(","))
                except ValueError:
                    problems.append((name, "prix : « - » ou quatre nombres séparés par des virgules"))
                    continue
                item.update(quick_low=ql, quick_high=qh, final_low=fl, final_high=fh)
            if prices in ("-", ""):
                item.update(default_prices(item))
            item["comment"] = f"{reason.rstrip('. ')}. Acheteur type : {buyer.rstrip('. ')}."
            items.append(item)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False)
    return problems


def verify_reports(asked_path: str, reports_path: str) -> tuple[list, list]:
    """Rapports de rentabilité conformes, et problèmes : format, nom pas demandé, nom demandé sans rapport."""
    with open(asked_path, encoding="utf-8") as f:
        asked = [n["name"] for n in json.load(f)["names"]]
    with open(reports_path, encoding="utf-8") as f:
        reports = json.load(f)
    if not isinstance(reports, list):
        return [], [("", "liste JSON attendue")]
    good, problems, seen = [], [], set()
    for item in reports:
        report, reason = clean_report(item)
        name = report["name"] if report else str(item.get("name", "") if isinstance(item, dict) else "")
        if report and name not in asked:
            reason = "nom pas demandé"
        elif report and name in seen:
            reason = "doublon"
        if reason:
            problems.append((name, reason))
            continue
        seen.add(name)
        good.append(item)
    problems += [(n, "pas de rapport") for n in asked if n not in seen]
    return good, problems


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action == "pull":
        limit, target = int(sys.argv[2]), sys.argv[3]
        offset = int(sys.argv[4]) if len(sys.argv) > 4 else 0  # tranche de la file, pour des sessions en parallèle
        found = call("GET", f"/api/ai/pending?limit={limit}&offset={offset}")
        with open(target, "w", encoding="utf-8") as f:
            json.dump(found, f, ensure_ascii=False, indent=1)
        print(f"{len(found['names'])} noms à noter (consigne v{found['version']}) dans {target}")
    elif action == "requested":
        target = sys.argv[2]
        found = call("GET", "/api/ai/pending?requested=1&limit=50")
        # NAS d'avant cette fonction : il renverrait la file de nuit. On ne note alors rien.
        if any(not n.get("requested") for n in found["names"]):
            print("DEMANDES 0 (NAS pas à jour)")
            return
        with open(target, "w", encoding="utf-8") as f:
            json.dump(found, f, ensure_ascii=False, indent=1)
        # Première ligne lue par la tâche horaire : à 0, elle s'arrête sans rien faire d'autre.
        print(f"DEMANDES {len(found['names'])}")
    elif action == "escalation":
        folder = sys.argv[2]
        try:
            found = call("GET", "/api/ai/escalation")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:  # NAS d'avant cette fonction
                print("ESCALADE 0 (NAS pas à jour)")
                return
            raise
        os.makedirs(folder, exist_ok=True)
        counts = {}
        for stage in ("rescue", "panel"):
            # Un lot par étape, au format de la file : check et show s'en servent tels quels.
            lot = {"version": found["version"], "guide": found["guide"], "format": found["format"], "stage": stage,
                   "claim": found["claim"], "votes": found["votes"][stage], "names": found[stage]}
            with open(os.path.join(folder, f"{stage}.json"), "w", encoding="utf-8") as f:
                json.dump(lot, f, ensure_ascii=False, indent=1)
            counts[stage] = len(found[stage])
        # Première ligne lue par la tâche : à 0, rien à faire de ce côté.
        if not any(counts.values()):
            print("ESCALADE 0")
        else:
            print(f"ESCALADE {sum(counts.values())} : repêchage {counts['rescue']} noms x {found['votes']['rescue']} correcteurs, "
                  f"panel {counts['panel']} noms x {found['votes']['panel']} correcteurs")
    elif action == "escalation-push":
        lot_path, files = sys.argv[2], sys.argv[3:]
        with open(lot_path, encoding="utf-8") as f:
            lot = json.load(f)
        if len(files) != lot["votes"]:
            sys.exit(f"{lot['votes']} fichiers d'avis attendus pour l'étape {lot['stage']}, un par correcteur ({len(files)} reçus).")
        bad = {}
        for path in files:
            good, problems = verify(lot_path, path)
            if problems:
                bad[path] = problems
        if bad:
            # Rien n'est envoyé tant qu'un correcteur a un avis manquant ou hors format : le panel doit être complet.
            for path, problems in bad.items():
                print(f"{path} : {len(problems)} problèmes")
                for name, reason in problems[:20]:
                    print(f"  {name} : {reason}")
            sys.exit("Corrige ces fichiers (format seulement, sans changer les jugements) puis renvoie.")
        decided = []
        for voter, path in enumerate(files, 1):
            good, _ = verify(lot_path, path)
            answer = call("POST", "/api/ai/escalation/results",
                          {"stage": lot["stage"], "claim": lot["claim"], "voter": voter, "results": good})
            decided += answer.get("decided", [])
            for r in answer["rejected"][:10]:
                print(f"  correcteur {voter}, {r['name']} : {r['reason']}")
        print(f"NAS : {len(files)} correcteurs envoyés pour l'étape {lot['stage']}, {len(decided)} noms tranchés")
        for d in decided:
            print(f"  {d['name']} : {d['verdict']}")
    elif action == "exploit":
        target = sys.argv[2]
        try:
            found = call("GET", "/api/exploit/pending")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:  # NAS d'avant cette fonction
                print("RENTABILITÉ 0 (NAS pas à jour)")
                return
            raise
        with open(target, "w", encoding="utf-8") as f:
            json.dump(found, f, ensure_ascii=False, indent=1)
        # Première ligne lue par la tâche : à 0, rien à faire de ce côté.
        print(f"RENTABILITÉ {len(found['names'])}")
    elif action in ("exploit-check", "exploit-push"):
        good, problems = verify_reports(sys.argv[2], sys.argv[3])
        print(f"{len(good)} rapports conformes, {len(problems)} problèmes")
        for name, reason in problems[:30]:
            print(f"  {name} : {reason}")
        for item in good:
            print(f"  {item['name']} : {item['verdict']}")
        if action == "exploit-push" and good:
            answer = call("POST", "/api/exploit/results", {"results": good})
            print(f"NAS : {answer['saved']} rapports enregistrés, {len(answer['rejected'])} rejetés")
            for r in answer["rejected"][:20]:
                print(f"  {r['name']} : {r['reason']}")
    elif action == "guide":
        with open(sys.argv[2], encoding="utf-8") as f:
            print(json.load(f)["guide"])
    elif action == "show":
        with open(sys.argv[2], encoding="utf-8") as f:
            names = json.load(f)["names"]
        start, count = int(sys.argv[3]), int(sys.argv[4])
        print(f"# noms {start} à {min(start + count, len(names)) - 1} sur {len(names)} ; réponse : "
              f"nom|{','.join(CRITERIA_V5)}|prix ou -|raison|acheteur type")
        for n in names[start:start + count]:
            print(line(n))
    elif action == "pack":
        problems = pack(sys.argv[2], sys.argv[3], sys.argv[4:])
        print(f"{len(problems)} ligne(s) illisible(s)")
        for name, reason in problems[:30]:
            print(f"  {name} : {reason}")
        print(f"Avis écrits dans {sys.argv[3]} ; contrôle complet : check")
    elif action in ("check", "push"):
        good, problems = verify(sys.argv[2], sys.argv[3])
        print(f"{len(good)} avis conformes, {len(problems)} problèmes")
        for name, reason in problems[:30]:
            print(f"  {name} : {reason}")
        # Grille v5 : le verdict et les notes sont calculés depuis les sous-notes ; on les montre ici.
        verdicts = {}
        for item in good:
            v = derive(item)["verdict"] if is_v5(item) else item["comment"].split(".")[0]
            verdicts[v] = verdicts.get(v, 0) + 1
        if good:
            print("Verdicts calculés : " + ", ".join(f"{v} {n}" for v, n in sorted(verdicts.items())))
            # Les meilleurs d'abord : la tâche de nuit en tire son résumé, la tâche horaire un nom par ligne.
            def best(item):
                d = derive(item) if is_v5(item) else item
                return max(d["exploit"], d["resale"])
            for item in sorted(good, key=best, reverse=True)[:20]:
                print("  " + summary(item) + f", revente rapide {item['quick_low']} à {item['quick_high']} euros")
        if action == "push" and good:
            saved, rejected = 0, []
            for start in range(0, len(good), 1000):
                answer = call("POST", "/api/ai/results", {"results": good[start:start + 1000]})
                saved += answer["saved"]
                rejected += answer["rejected"]
            print(f"NAS : {saved} avis enregistrés, {len(rejected)} rejetés")
            for r in rejected[:20]:
                print(f"  {r['name']} : {r['reason']}")
    else:
        sys.exit("Usage : pull <nombre> <lot.json> | requested <lot.json> | guide <lot.json> | show <lot.json> <début> <nombre> | "
                 "pack <lot.json> <avis.json> <avis-1.txt>... | check <lot.json> <avis.json> | push <lot.json> <avis.json> | "
                 "escalation <dossier> | escalation-push <lot.json> <avis-1.json>... | exploit <rent.json> | exploit-check <rent.json> <rapports.json> | exploit-push <rent.json> <rapports.json>")


if __name__ == "__main__":
    main()
