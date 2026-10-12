"""Interroge les vrais serveurs RDAP et vérifie que le parsing tient.

Hors des tests unitaires : dépend du réseau et de l'humeur des registres.
    python -m scripts.rdap_smoke
    python -m scripts.rdap_smoke exemple.so exemple.co   # domaines précis, sans contrôle des champs
"""

import asyncio
import secrets
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app import states
from app.rdap import RdapClient

REGISTERED = ["google.com", "wikipedia.org", "verisign.net", "afnic.fr", "lemonde.fr"]


async def inspect(names: list[str]) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        client = RdapClient(Path(tmp) / "bootstrap.json", 2, 20, 1, 2, 30)
        try:
            for name in names:
                print(f"{name} : serveur {await client.server_for(name)}")
                url = f"{(await client.server_for(name) or '').rstrip('/')}/domain/{name}"
                if url.startswith("http"):
                    response = await client.http.get(url)
                    print(f"  HTTP {response.status_code} {response.headers.get('content-type')}")
                    print(f"  {response.text[:600]}")
                result = await client.lookup(name)
                print(f"  lookup : found={result.found} available={result.available} error={result.error}")
                if result.info:
                    print(f"  {result.info}")
        finally:
            await client.close()
    return 0


async def main() -> int:
    if len(sys.argv) > 1:
        return await inspect(sys.argv[1:])
    failures = 0
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory() as tmp:
        client = RdapClient(Path(tmp) / "bootstrap.json", 2, 20, 3, 5, 120)
        try:
            for name in REGISTERED:
                result = await client.lookup(name)
                if not result.found:
                    print(f"ÉCHEC {name} : {result.error or 'déclaré disponible'}")
                    failures += 1
                    continue
                info = result.info
                state = states.compute_state(info.statuses, info.expires_at, now, 30)
                print(f"{name}")
                print(f"  serveur    : {await client.server_for(name)}")
                print(f"  état       : {state}")
                print(f"  statuts    : {info.statuses}")
                print(f"  création   : {info.registered_at}")
                print(f"  mise à jour: {info.updated_at}")
                print(f"  expiration : {info.expires_at}")
                print(f"  registrar  : {info.registrar}")
                print(f"  DNS        : {info.nameservers}")
                missing = [k for k in ("expires_at", "registrar") if not getattr(info, k)]
                if not info.nameservers:
                    missing.append("nameservers")
                if missing:
                    print(f"  ÉCHEC champs vides : {missing}")
                    failures += 1

            for tld in ("com", "fr"):
                name = f"dw-{secrets.token_hex(10)}.{tld}"
                result = await client.lookup(name)
                verdict = "disponible" if result.available else (result.error or "trouvé")
                print(f"{name} : {verdict}")
                if not result.available:
                    print("  ÉCHEC un nom aléatoire devrait être disponible")
                    failures += 1
        finally:
            await client.close()
    print(f"\n{failures} échec(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
