import logging
import re

import httpx

log = logging.getLogger(__name__)

_SEMVER = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def parse_version(value: str) -> tuple[int, int, int] | None:
    match = _SEMVER.match(value or "")
    return tuple(int(x) for x in match.groups()) if match else None


async def latest_version(http: httpx.AsyncClient, repository: str) -> str | None:
    """Plus haute version vX.Y.Z publiée sur Docker Hub, None si injoignable."""
    url = f"https://hub.docker.com/v2/repositories/{repository}/tags"
    try:
        response = await http.get(url, params={"page_size": 100, "ordering": "last_updated"})
        response.raise_for_status()
        data = response.json()
        # Forme attendue : {"results": [{"name": ...}, ...]} ; tout autre JSON vaut « inconnu », pas une erreur.
        results = data.get("results") if isinstance(data, dict) else None
        names = [tag.get("name", "") for tag in results if isinstance(tag, dict)] if isinstance(results, list) else []
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Vérification de version impossible : %s", exc.__class__.__name__)
        return None
    versions = [n for n in names if parse_version(n)]
    return max(versions, key=parse_version) if versions else None


def newer(current: str, latest: str | None) -> str | None:
    """La version publiée si elle dépasse celle qui tourne. Rien pour une version de dev."""
    current_v, latest_v = parse_version(current), parse_version(latest or "")
    if current_v and latest_v and latest_v > current_v:
        return latest
    return None
