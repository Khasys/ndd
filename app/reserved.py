"""Noms .fr qu'on ne peut pas simplement réserver, même libres.

- Termes soumis à examen préalable (Afnic) : professions réglementées, institutions, termes
  heurtant la sensibilité du public… Enregistrables seulement en justifiant d'un intérêt
  légitime. Liste publique en PDF, lue telle quelle : chaque ligne du PDF, mise sous forme
  canonique, est un terme. Une ligne de titre peut s'y glisser : sans effet si elle ne
  correspond à aucun nom suivi, et un nom marqué à tort se corrige à la main.
- Noms de communes : protégés par l'Afnic sous forme canonique (sans espaces, tirets, accents
  ni apostrophes, avec et sans l'article). Liste officielle via geo.api.gouv.fr.
- Noms à deux caractères exclus de l'enregistrement : codes des territoires et cas historiques
  (liste fixe ci-dessous). Le RDAP de l'Afnic répond « inconnu » pour ces noms, comme pour un
  nom libre : sans cette liste, ils passeraient pour réservables.
- Marquages manuels : réservé, ou pas réservé (corrige une liste automatique).

Téléchargés depuis le NAS une fois par semaine ; rien n'est appelé au fil de la navigation.
"""

import io
import json
import logging
import re
import unicodedata
from datetime import datetime

import httpx

from .checker import iso, utcnow

log = logging.getLogger(__name__)

AFNIC_PDF = "https://www.afnic.fr/wp-media/uploads/2021/01/Termes-soumis-a%CC%80-examen-pre%CC%81alable-version-2021.pdf"
COMMUNES_API = "https://geo.api.gouv.fr/communes?fields=nom&format=json"
SOURCES = {"afnic": "Examen préalable Afnic", "commune": "Nom de commune", "exclu": "Exclu par l'Afnic",
           "court": "Réservé par le registre (1 ou 2 caractères)", "manuel": "Marqué à la main"}
# Exclus de l'ouverture des noms à un et deux caractères : codes de territoires français, fr, et tm
# (ancienne zone des marques). Charte de nommage Afnic, version de décembre 2014 pour pm, re, tf,
# wf, yt ; les autres d'après un relevé à confirmer dans la charte en vigueur.
EXCLUDED = frozenset(f"{c}.fr" for c in ("fr", "tm", "pm", "re", "tf", "wf", "yt", "nc", "gf", "gp", "mq", "mf", "bl"))
ARTICLES = ("les ", "le ", "la ", "l'", "l’")


def canonical(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.strip().lower().replace("œ", "oe").replace("æ", "ae"))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[\s'’\-_.]", "", text)


def commune_forms(name: str) -> set[str]:
    """Forme canonique, avec et sans l'article (« Le Mans » : lemans et mans)."""
    lowered = name.strip().lower()
    forms = {canonical(lowered)}
    for article in ARTICLES:
        if lowered.startswith(article):
            forms.add(canonical(lowered[len(article):]))
    return {f for f in forms if re.fullmatch(r"[a-z0-9]+", f)}


def pdf_terms(data: bytes) -> set[str]:
    from pypdf import PdfReader

    terms = set()
    for page in PdfReader(io.BytesIO(data)).pages:
        for line in (page.extract_text() or "").splitlines():
            # Une ligne peut porter plusieurs termes séparés par des virgules ou des points-virgules.
            for part in re.split(r"[,;]", line):
                term = canonical(part)
                if re.fullmatch(r"[a-z0-9]{1,63}", term):
                    terms.add(term)
    return terms


class Reserved:
    def __init__(self, conn, store, http_factory=None):
        self.conn = conn
        self.store = store
        self._http_factory = http_factory or (lambda: httpx.AsyncClient(timeout=60, follow_redirects=True,
                                                                      headers={"User-Agent": "domain-watch"}))
        self.running = False
        self._unflagged: frozenset[str] | None = None
        self._names: frozenset[str] | None = None
        self._indices: dict[str, list[int]] = {}

    # --- Lecture ---------------------------------------------------------------------

    def names(self) -> frozenset[str]:
        """Noms réservés, marquages manuels compris (et corrections « pas réservé » retirées)."""
        if self._names is None:
            auto = {r[0] for r in self.conn.execute("SELECT DISTINCT name FROM reserved_names")}
            flags = dict(self.conn.execute("SELECT name, reserved FROM name_flags").fetchall())
            self._names = frozenset((auto | EXCLUDED | {n for n, r in flags.items() if r})
                                    - {n for n, r in flags.items() if not r})
            self._indices = {}
        return self._names

    def indices(self, tld: str, universe) -> list[int]:
        """Positions des noms réservés d'une extension dans l'univers de l'Explorer."""
        names = self.names()
        if tld not in self._indices:
            suffix = "." + tld
            found = (universe.index(n[: -len(suffix)]) for n in names if n.endswith(suffix))
            self._indices[tld] = [i for i in found if i is not None]
        return self._indices[tld]

    def short_reserved(self, name: str) -> bool:
        """Extension générique (.com, .org…) : les noms d'un ou deux caractères absents de la zone
        sont gardés par le registre (codes pays, lettres seules). Le RDAP les dit inconnus, comme un
        nom libre. Un marquage « pas réservé » corrige un cas réellement ouvert."""
        label, _, tld = name.partition(".")
        return tld != "fr" and len(label) <= 2 and name not in self.unflagged()

    def unflagged(self) -> frozenset[str]:
        if self._unflagged is None:
            self._unflagged = frozenset(r[0] for r in self.conn.execute("SELECT name FROM name_flags WHERE reserved = 0"))
        return self._unflagged

    def why(self, name: str) -> list[str]:
        flag = self.conn.execute("SELECT reserved FROM name_flags WHERE name = ?", (name,)).fetchone()
        if flag is not None:
            return [SOURCES["manuel"]] if flag[0] else []
        found = [SOURCES[r[0]] for r in self.conn.execute("SELECT source FROM reserved_names WHERE name = ?", (name,))]
        if self.short_reserved(name):
            found.append(SOURCES["court"])
        return ([SOURCES["exclu"]] if name in EXCLUDED else []) + found

    @property
    def mode(self) -> str:
        """hide : retirés des noms à saisir (carte, classement, compteurs) ; show : mêlés aux autres."""
        return "show" if self.store.get("reserved_mode") == "show" else "hide"

    def status(self) -> dict:
        info = json.loads(self.store.get("reserved_status") or "{}")
        counts = dict(self.conn.execute("SELECT source, COUNT(*) FROM reserved_names GROUP BY source").fetchall())
        manual = self.conn.execute("SELECT SUM(reserved = 1), SUM(reserved = 0) FROM name_flags").fetchone()
        return {**info, "counts": counts, "manual_reserved": manual[0] or 0, "manual_ok": manual[1] or 0,
                "mode": self.mode, "running": self.running}

    # --- Écriture --------------------------------------------------------------------

    def flag(self, name: str, reserved: bool | None) -> None:
        """reserved=None retire le marquage manuel : la liste automatique s'applique de nouveau."""
        if reserved is None:
            self.conn.execute("DELETE FROM name_flags WHERE name = ?", (name,))
        else:
            self.conn.execute(
                """INSERT INTO name_flags (name, reserved, at) VALUES (?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET reserved = excluded.reserved, at = excluded.at""",
                (name, 1 if reserved else 0, iso(utcnow())),
            )
        self._names = None
        self._unflagged = None
        self._indices = {}

    def _replace(self, source: str, names: set[str]) -> None:
        self.conn.execute("BEGIN")
        try:
            self.conn.execute("DELETE FROM reserved_names WHERE source = ?", (source,))
            self.conn.executemany("INSERT OR IGNORE INTO reserved_names (name, source) VALUES (?, ?)",
                                  ((f"{n}.fr", source) for n in names))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        self._names = None
        self._indices = {}

    def due(self) -> bool:
        info = json.loads(self.store.get("reserved_status") or "{}")
        last = info.get("at")
        return not last or (utcnow() - datetime.fromisoformat(last)).days >= 7

    async def refresh(self) -> dict:
        if self.running:
            return self.status()
        self.running = True
        errors = []
        try:
            async with self._http_factory() as http:
                try:
                    response = await http.get(self.store.get("reserved_pdf_url") or AFNIC_PDF)
                    response.raise_for_status()
                    terms = pdf_terms(response.content)
                    if len(terms) < 50:
                        raise ValueError(f"seulement {len(terms)} termes lus")
                    self._replace("afnic", terms)
                except Exception as exc:  # PDF illisible, réseau : la liste précédente reste
                    errors.append(f"Liste Afnic : {exc.__class__.__name__}" + (f" ({exc})" if isinstance(exc, ValueError) else ""))
                try:
                    response = await http.get(COMMUNES_API)
                    response.raise_for_status()
                    forms = set()
                    for commune in response.json():
                        forms |= commune_forms(commune.get("nom", ""))
                    if len(forms) < 10_000:
                        raise ValueError(f"seulement {len(forms)} communes lues")
                    self._replace("commune", forms)
                except Exception as exc:
                    errors.append(f"Communes : {exc.__class__.__name__}" + (f" ({exc})" if isinstance(exc, ValueError) else ""))
        finally:
            self.running = False
        self.store.set("reserved_status", json.dumps({"at": iso(utcnow()), "error": " ; ".join(errors) or None}))
        log.info("Noms réservés : %s", self.status()["counts"])
        return self.status()
