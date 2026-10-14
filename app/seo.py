"""Analyse SEO à la demande, pour les noms qui peuvent s'obtenir.

- Open PageRank (DomCop) : note de 0 à 10 tirée des liens qui pointent vers le domaine, d'après
  Common Crawl. Clé gratuite à créer sur domcop.com/openpagerank, 100 domaines par requête.
- Wayback Machine (Internet Archive) : années où le site a été archivé, sans clé. Une requête
  par domaine, espacées d'une seconde.

Ni l'un ni l'autre ne donne le nombre exact de backlinks : seuls des services payants
(Ahrefs, Majestic, Moz) le donnent.
"""

import asyncio
import logging

import httpx

from .checker import iso, utcnow

log = logging.getLogger(__name__)

OPR_URL = "https://openpagerank.com/api/v1.0/getPageRank"
WAYBACK_URL = "https://web.archive.org/cdx/search/cdx"
OPR_BATCH = 100
WAYBACK_PAUSE = 1.0
MAX_NAMES = 100


class Seo:
    def __init__(self, conn, store, http_factory=None):
        self.conn = conn
        self.store = store
        self._http_factory = http_factory or (lambda: httpx.AsyncClient(timeout=30, follow_redirects=True,
                                                                      headers={"User-Agent": "domain-watch"}))
        self.running = False
        self.progress = {"done": 0, "total": 0}

    @property
    def key(self) -> str:
        return self.store.get("opr_key").strip()

    def rows(self, names: list[str]) -> dict:
        found = {}
        for chunk in range(0, len(names), 900):
            part = names[chunk:chunk + 900]
            rows = self.conn.execute(
                f"SELECT * FROM domain_seo WHERE name IN ({','.join('?' * len(part))})", part
            ).fetchall()
            found.update({r["name"]: r for r in rows})
        return found

    async def _pagerank(self, http, names: list[str]) -> dict[str, float | None]:
        if not self.key:
            return {}
        ranks: dict[str, float | None] = {}
        for chunk in range(0, len(names), OPR_BATCH):
            part = names[chunk:chunk + OPR_BATCH]
            response = await http.get(OPR_URL, params=[("domains[]", n) for n in part], headers={"API-OPR": self.key})
            if response.status_code in (401, 403):
                raise SeoError("Clé Open PageRank refusée.")
            response.raise_for_status()
            for item in response.json().get("response", []):
                value = item.get("page_rank_decimal")
                ranks[item.get("domain", "").lower()] = float(value) if value not in (None, "") else 0.0
        return ranks

    async def _wayback(self, http, name: str) -> tuple[str | None, str | None, int]:
        response = await http.get(WAYBACK_URL, params={
            "url": name, "output": "json", "fl": "timestamp", "collapse": "timestamp:4", "limit": "200",
        })
        response.raise_for_status()
        rows = response.json() if response.content.strip() else []
        stamps = [r[0] for r in rows[1:] if r]
        if not stamps:
            return None, None, 0
        day = lambda s: f"{s[:4]}-{s[4:6]}-{s[6:8]}"
        return day(stamps[0]), day(stamps[-1]), len(stamps)

    async def analyze(self, names: list[str]) -> dict:
        """Analyse jusqu'à 100 noms et enregistre le résultat. Renvoie les lignes à jour."""
        names = list(dict.fromkeys(n.lower() for n in names))[:MAX_NAMES]
        self.running = True
        self.progress = {"done": 0, "total": len(names)}
        try:
            async with self._http_factory() as http:
                try:
                    ranks = await self._pagerank(http, names)
                    rank_error = None
                except (httpx.HTTPError, ValueError, SeoError) as exc:
                    ranks, rank_error = {}, str(exc) if isinstance(exc, SeoError) else f"Open PageRank : {exc.__class__.__name__}"
                for i, name in enumerate(names):
                    error = rank_error
                    try:
                        first, last, years = await self._wayback(http, name)
                    except (httpx.HTTPError, ValueError) as exc:
                        first, last, years = None, None, None
                        error = (error + " ; " if error else "") + f"Wayback : {exc.__class__.__name__}"
                    self.conn.execute(
                        """INSERT INTO domain_seo (name, pr, wb_first, wb_last, wb_years, checked_at, error)
                           VALUES (?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(name) DO UPDATE SET pr = COALESCE(excluded.pr, domain_seo.pr),
                             wb_first = excluded.wb_first, wb_last = excluded.wb_last, wb_years = excluded.wb_years,
                             checked_at = excluded.checked_at, error = excluded.error""",
                        (name, ranks.get(name), first, last, years, iso(utcnow()), error),
                    )
                    self.progress["done"] = i + 1
                    if i < len(names) - 1:
                        await asyncio.sleep(WAYBACK_PAUSE)
        finally:
            self.running = False
        return self.rows(names)


class SeoError(Exception):
    pass
