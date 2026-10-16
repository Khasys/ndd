"""Lecture seule de domain-watch pour une session Claude : diagnostic, écrans, avis IA.

Usage : python3 scripts/read_api.py /api/diag
        python3 scripts/read_api.py "/api/explore/top?cat=sector&n=100&tld=fr,com"
        python3 scripts/read_api.py "/api/ai/rated?verdict=take&limit=200"

Mêmes variables que scripts/ai_exchange.py : DW_URL et DW_AI_TOKEN (souvent ajouté par le proxy).
Seules les lectures (GET) sont ouvertes au jeton ; réglages, écritures et secrets restent fermés.
"""

import json
import sys

from ai_exchange import call


def main() -> None:
    if len(sys.argv) != 2 or not sys.argv[1].startswith("/api/"):
        sys.exit(__doc__)
    print(json.dumps(call("GET", sys.argv[1]), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
