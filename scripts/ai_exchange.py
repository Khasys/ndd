"""Échange avec le NAS pour la notation IA (utilisé par la tâche planifiée).

  python scripts/ai_exchange.py pull 1000 lot.json   # liste à noter + consigne
  python scripts/ai_exchange.py push avis.json       # renvoie les avis

Variables d'environnement : DW_URL (ex. https://domaines.exemple.fr) et DW_AI_TOKEN (jeton créé
dans Paramètres > Avis IA). Le jeton n'est jamais affiché ni écrit dans un fichier.
"""

import json
import os
import sys
import urllib.request


def call(method: str, path: str, body: dict | None = None) -> dict:
    url = os.environ["DW_URL"].rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + os.environ["DW_AI_TOKEN"],
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read())


def main() -> None:
    action = sys.argv[1]
    if action == "pull":
        limit, target = int(sys.argv[2]), sys.argv[3]
        found = call("GET", f"/api/ai/pending?limit={limit}")
        with open(target, "w", encoding="utf-8") as f:
            json.dump(found, f, ensure_ascii=False, indent=1)
        print(f"{len(found['names'])} noms à noter (consigne v{found['version']}) dans {target}")
    elif action == "push":
        with open(sys.argv[2], encoding="utf-8") as f:
            results = json.load(f)
        total = {"saved": 0, "rejected": []}
        for start in range(0, len(results), 1000):
            answer = call("POST", "/api/ai/results", {"results": results[start:start + 1000]})
            total["saved"] += answer["saved"]
            total["rejected"] += answer["rejected"]
        print(f"{total['saved']} avis enregistrés, {len(total['rejected'])} rejetés")
        for r in total["rejected"][:20]:
            print(f"  {r['name']} : {r['reason']}")
    else:
        sys.exit("Usage : pull <nombre> <fichier> | push <fichier>")


if __name__ == "__main__":
    main()
