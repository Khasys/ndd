"""Échange avec le NAS pour la notation IA (utilisé par la tâche planifiée).

  python scripts/ai_exchange.py pull 1000 lot.json      # liste à noter + consigne + format
  python scripts/ai_exchange.py check lot.json avis.json # contrôle le format, sans rien envoyer
  python scripts/ai_exchange.py push lot.json avis.json  # contrôle puis envoie les avis conformes

Variables d'environnement : DW_URL (ex. https://domaines.exemple.fr) et DW_AI_TOKEN (jeton créé
dans Paramètres > Avis IA). Le jeton n'est jamais affiché ni écrit dans un fichier.
À lancer depuis la racine du dépôt : le contrôle est celui du NAS (app/ai.py).
"""

import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.ai import check  # noqa: E402  même règle que le NAS


def call(method: str, path: str, body: dict | None = None) -> dict:
    url = os.environ.get("DW_URL", "https://domaines.skky.fr").rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
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
        reason = check(item, lambda name: name in wanted)
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


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action == "pull":
        limit, target = int(sys.argv[2]), sys.argv[3]
        found = call("GET", f"/api/ai/pending?limit={limit}")
        with open(target, "w", encoding="utf-8") as f:
            json.dump(found, f, ensure_ascii=False, indent=1)
        print(f"{len(found['names'])} noms à noter (consigne v{found['version']}) dans {target}")
    elif action in ("check", "push"):
        good, problems = verify(sys.argv[2], sys.argv[3])
        print(f"{len(good)} avis conformes, {len(problems)} problèmes")
        for name, reason in problems[:30]:
            print(f"  {name} : {reason}")
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
        sys.exit("Usage : pull <nombre> <lot.json> | check <lot.json> <avis.json> | push <lot.json> <avis.json>")


if __name__ == "__main__":
    main()
