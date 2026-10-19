"""Gandi : disponibilité et prix d'un nom, par l'API v5 (vérification /v5/domain/check).

Source d'information seulement : rien n'est acheté, aucune autre route de Gandi n'est appelée. Jeton
d'accès personnel (PAT) créé dans le compte Gandi, en lecture ; gardé dans les réglages, jamais renvoyé
à l'interface. Limite de l'API : 1 000 requêtes par minute et par adresse IP.

Le bouton « Tester » de Paramètres interroge quelques noms et garde leurs réponses brutes (aucun secret
dedans) : elles ont servi à caler la lecture des états et des prix (read).

Chaque nuit, entre les passes DNS et la notation IA, la passe Gandi (NightPass) vérifie les noms non pris
(libres, bientôt libres, probablement libres, libres selon le WHOIS) du meilleur score au moins bon, à un
rythme prudent. Le résultat (gandi_checks) précise l'état du nom dans l'Explorer, la Veille et la fiche :
premium avec son prix, libre au prix standard, ou indisponible.

Extension « par Gandi seul » (only_tlds, .com par défaut) tant qu'aucune zone ne la lit (pas d'accès CZDS, ou
zone pas encore lue) : jour et nuit, la passe continue (LivePass) demande à Gandi chaque nom qui a un sens, les
listes fournies d'abord. Sa réponse fait l'état (explore.gandi_effect). Les deux passes partagent le client et
son plafond commun (GLOBAL_PER_MINUTE), sous la limite de Gandi.
"""

import asyncio
import json
import logging
import re
import time
from collections import deque
from datetime import datetime, timedelta

import httpx

from . import wordlists
from .checker import iso, utcnow

log = logging.getLogger(__name__)

CHECK_URL = "https://api.gandi.net/v5/domain/check"
# Un nom réservé probable (.co), des noms courants, un premium probable, un nom inventé.
SAMPLES = ("menu.co", "voyage.fr", "maison.com", "ai.io", "chat.ai", "qzkwyxv.com")

# Lecture d'une réponse : sorte de résultat tirée du statut de Gandi (et des lignes de prix).
KINDS = ("available", "premium", "unavailable", "reserved", "restricted", "error")
AVAILABLE = ("available", "available_preorder")
RESERVED = ("available_reserved", "reserved_corporate")
RESTRICTED = ("unavailable_restricted",)
# Sortes qui disent qu'on ne peut pas acheter le nom : jamais affiché libre.
BLOCKED = ("unavailable", "reserved", "restricted")
# Statuts d'attente ou d'erreur : rien n'est conclu, l'état du nom ne change pas.
ERROR_LABELS = {
    "pending": "réponse du registre en attente",
    "error_invalid": "nom refusé comme invalide",
    "error_refused": "vérification refusée par le registre",
    "error_timeout": "le registre n'a pas répondu à temps",
    "error_unknown": "erreur inconnue chez le registre",
    "error_eoi": "extension en phase de manifestation d'intérêt",
}
# Libellés montrés : sans le nom du fournisseur (source d'information, l'achat se fait ailleurs).
KIND_LABELS = {"available": "Libre (vérifié)", "premium": "Premium", "unavailable": "Indisponible à l'achat",
               "reserved": "Réservé par le registre", "restricted": "Restreint", "error": "Prix non vérifié"}

# Passe de nuit : après les lectures et les passes DNS, avant la notation IA de 5 h (heure locale, TZ).
START = (3, 30)
END = (4, 45)
NIGHTLY = 8_000           # noms par nuit au plus (réglable)
RATE = 3.0                # requêtes par seconde au plus (réglable), loin de la limite de Gandi
MAX_RATE = 10.0
MIN_RATE = 0.2
FRESH_DAYS = 7            # un nom vérifié depuis moins longtemps est sauté (réglable)
CHANGED_DAYS = 1          # nom qui a changé d'état depuis sa vérification, ou réponse en erreur
FAIL_STOP = 20            # échecs d'affilée (réseau, réponse illisible) avant d'arrêter la passe
LIMIT_STOP = 3            # « trop de requêtes » d'affilée avant d'arrêter la passe
LIMIT_WAIT = 60           # attente par défaut après un « trop de requêtes », en secondes
UNSOLD_STOP = 5           # réponses invalides d'affilée sur une extension : Gandi ne la vend pas
UNSOLD_DAYS = 30
# Plafond commun à toutes les requêtes de l'appli (passes, Vérifier, domaines suivis), sur 60 s glissantes :
# sous la limite de Gandi (1 000 par minute et par adresse IP), même avec les deux passes au plus haut.
GLOBAL_PER_MINUTE = 900

# Passe continue des extensions par Gandi seul (LivePass), jour et nuit.
ONLY_DEFAULT = "com"      # extensions par Gandi seul tant qu'aucune zone ne les lit (réglage gandi_only_tlds)
LIVE_RATE = 5.0           # requêtes par seconde (réglable)
LIVE_MAX_RATE = 15.0      # 900 par minute : le plafond commun
LIVE_TOUR = 1800          # secondes de travail par tour : la file est refaite ensuite (libres revus chaque jour)
LIVE_IDLE = 900           # rien à revoir : tour suivant dans 15 minutes
LIVE_FAIL_PAUSE = 900     # FAIL_STOP échecs réseau d'affilée : pause de 15 minutes, puis reprise
LIVE_LIMIT_MAX = 3600     # « trop de requêtes » répétés : attente doublée à chaque refus, une heure au plus
LIVE_HALT_HOURS = 24      # jeton refusé : nouvel essai le lendemain, ou dès qu'un jeton est enregistré
# Ordre de la file : listes fournies d'abord, termes premium, mots-clés et marques mêlés par rang, puis métier + ville.
FIRST_LISTS = (("premium", "keyword", "brand"), ("local",))
DAILY_KINDS = ("available", "premium", "error")  # revus chaque jour : un nom à prendre, ou sans réponse exploitable


class GandiError(Exception):
    """code : refused (jeton refusé), limit (trop de requêtes), network, http, unreadable, missing."""

    def __init__(self, message: str, code: str = "", retry_after: float | None = None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


def read(raw: dict) -> dict:
    """Réponse brute de /v5/domain/check -> sorte, statut brut, prix TTC et HT d'un an de création.

    premium : un nom disponible avec une ligne de prix de type « premium » (vu en vrai : statut available) ;
    unavailable_premium, premium déjà enregistré, ne s'achète pas : unavailable ; reserved :
    available_reserved, reserved_corporate ; restricted : unavailable_restricted ; error : pending,
    error_* et tout statut inconnu. Prix : la première ligne d'une durée minimale d'un an (TVA comprise
    seulement quand le nom est disponible, comme Gandi l'affiche)."""
    product = next((p for p in raw.get("products") or [] if p.get("process", "create") == "create"), None)
    if product is None:
        return {"kind": "error", "status": "", "price": None, "price_ht": None, "currency": raw.get("currency") or "EUR"}
    status = str(product.get("status") or "")
    prices = [p for p in product.get("prices") or [] if isinstance(p, dict)]
    premium = status in AVAILABLE and any(p.get("type") == "premium" for p in prices)
    yearly = [p for p in prices if p.get("min_duration", 1) == 1 and p.get("duration_unit", "y") == "y"]
    if premium:
        yearly = [p for p in yearly if p.get("type") == "premium"] or yearly
    line = yearly[0] if yearly else {}
    if status == "pending" or status.startswith("error_"):
        kind = "error"
    elif premium:
        kind = "premium"
    elif status in RESERVED:
        kind = "reserved"
    elif status in RESTRICTED:
        kind = "restricted"
    elif status in AVAILABLE:
        kind = "available"
    elif status.startswith("unavailable"):
        kind = "unavailable"
    else:
        kind = "error"

    def amount(key):
        value = line.get(key)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    return {"kind": kind, "status": status, "price": amount("price_after_taxes"), "price_ht": amount("price_before_taxes"),
            "currency": raw.get("currency") or "EUR"}


def brief(raw: dict) -> dict:
    """Résumé du bouton Tester : état et prix de création tels que Gandi les donne."""
    product = next((p for p in raw.get("products", []) if p.get("process", "create") == "create"), None)
    if product is None:
        return {"status": "inconnu"}
    prices = product.get("prices") or []
    first = prices[0] if prices else {}
    return {"status": product.get("status") or "inconnu", "price": first.get("price_after_taxes"),
            "type": first.get("type") or "", "currency": raw.get("currency") or "EUR"}


def only_tlds(store) -> list[str]:
    """Extensions à vérifier par Gandi seul quand aucune zone ne les lit (réglage gandi_only_tlds, « com » par défaut)."""
    raw = store.get("gandi_only_tlds", ONLY_DEFAULT)
    return sorted({t.strip(".") for t in re.split(r"[\s,]+", raw.lower()) if re.fullmatch(r"[a-z]{2,24}", t.strip("."))})


def unsold(store) -> set[str]:
    """Extensions que Gandi ne vend pas (réponses invalides d'affilée), écartées 30 jours."""
    try:
        found = json.loads(store.get("gandi_unsold") or "{}")
    except ValueError:
        return set()
    today = utcnow().date().isoformat()
    return {t for t, until in found.items() if until > today}


def mark_unsold(store, tld: str) -> None:
    try:
        found = json.loads(store.get("gandi_unsold") or "{}")
    except ValueError:
        found = {}
    found[tld] = (utcnow() + timedelta(days=UNSOLD_DAYS)).date().isoformat()
    store.set("gandi_unsold", json.dumps(found))


async def alert(store, local_zone, notify, text: str) -> None:
    """Une alerte par jour au plus, toutes passes confondues : un jeton refusé ne relance pas Telegram à chaque essai."""
    today = datetime.now(local_zone).date().isoformat()
    if not notify or store.get("gandi_alert_day") == today:
        return
    store.set("gandi_alert_day", today)
    try:
        await notify(text)
    except Exception as exc:  # noqa: BLE001  une alerte ratée ne doit pas casser la passe
        log.warning("Gandi : alerte non envoyée (%s)", exc)


# --- Résultats gardés -------------------------------------------------------------------

def load(conn, names: list[str]) -> dict[str, dict]:
    found = {}
    for chunk in range(0, len(names), 900):
        part = names[chunk:chunk + 900]
        rows = conn.execute(f"SELECT * FROM gandi_checks WHERE name IN ({','.join('?' * len(part))})", part)
        found.update({r["name"]: dict(r) for r in rows})
    return found


def applies(row: dict | None, since: str | None = None, checked: str | None = None) -> bool:
    """Le résultat compte pour l'état du nom : pas une erreur, et pas antérieur à un changement d'état
    (since : entrée du nom dans son état actuel, Veille). Un résultat dépassé attend la passe suivante.
    Indisponible sur un nom de la Veille (checked : dernière vérification au registre) : compte seulement
    une fois le registre revu après Gandi, sinon c'est sans doute un nom enregistré depuis (voir pending)."""
    return bool(row) and row["kind"] != "error" and not (since and since > row["checked_at"]) and not pending(row, checked)


def pending(row: dict | None, checked: str | None) -> bool:
    """Indisponible chez Gandi, registre pas encore revu depuis (ou jamais) : la revérification décide (pris ou réservé)."""
    return bool(row) and row["kind"] in BLOCKED and checked is not None and checked < row["checked_at"]


def registry_checked(drop) -> str | None:
    """checked de applies et pending : None sans ligne de Veille, "" pour une ligne pas encore vérifiée."""
    return None if drop is None else drop["checked_at"] or ""


def public(row: dict | None, since: str | None = None, checked: str | None = None) -> dict | None:
    """Ce que l'interface montre : sorte, statut, prix, date, erreur en clair, et si le résultat compte."""
    if not row:
        return None
    error = row.get("error")
    return {"kind": row["kind"], "status": row["status"], "label": KIND_LABELS.get(row["kind"], ""),
            "price": row["price"], "price_ht": row["price_ht"], "currency": row["currency"] or "EUR",
            "checked_at": row["checked_at"],
            "error": ERROR_LABELS.get(error, error) if error else (ERROR_LABELS.get(row["status"], row["status"] or "réponse illisible")
                                                                 if row["kind"] == "error" else None),
            "stale": row["kind"] != "error" and bool(since and since > row["checked_at"]),
            "pending": pending(row, checked)}


def summary(row: dict | None, since: str | None = None) -> dict | None:
    """Forme courte pour les listes et la file IA : {"kind": "premium", "price": 878}, seulement si le résultat compte."""
    if not applies(row, since):
        return None
    return {"kind": row["kind"], "price": round(row["price"]) if row["price"] is not None else None}


def save(conn, name: str, found: dict, now: datetime) -> tuple[dict | None, dict]:
    """Enregistre une lecture ; renvoie (ligne d'avant, ligne d'après). Une réponse en erreur ne remplace
    pas un résultat connu : elle est notée à côté, l'état du nom ne change pas."""
    before = load(conn, [name]).get(name)
    if found["kind"] == "error" and before and before["kind"] != "error":
        conn.execute("UPDATE gandi_checks SET error = ? WHERE name = ?", (found["status"] or "illisible", name))
    else:
        conn.execute(
            """INSERT INTO gandi_checks (name, status, kind, price, price_ht, currency, checked_at, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET status = excluded.status, kind = excluded.kind, price = excluded.price,
                 price_ht = excluded.price_ht, currency = excluded.currency, checked_at = excluded.checked_at,
                 error = excluded.error""",
            (name, found["status"], found["kind"], found["price"], found["price_ht"], found.get("currency") or "EUR",
             iso(now), (found["status"] or "illisible") if found["kind"] == "error" else None))
    return before, load(conn, [name])[name]


def totals(conn, tlds: list[str] | None = None) -> dict:
    """Réponses gardées par sorte (toutes extensions, ou seulement celles-ci) et date de la dernière."""
    where, args = (f"WHERE tld IN ({','.join('?' * len(tlds))})", tlds) if tlds is not None else ("", [])
    by_kind = dict(conn.execute(f"SELECT kind, COUNT(*) FROM gandi_checks {where} GROUP BY kind", args).fetchall())
    last = conn.execute(f"SELECT MAX(checked_at) FROM gandi_checks {where}", args).fetchone()[0]
    return {"known": sum(by_kind.values()), "by_kind": by_kind, "last": last}


# --- Client ---------------------------------------------------------------------------------

class Gandi:
    def __init__(self, store, http_factory=None, conn=None):
        self.store = store
        self.conn = conn if conn is not None else store.conn
        self._http_factory = http_factory or (lambda: httpx.AsyncClient(timeout=20, headers={"User-Agent": "domain-watch"}))
        # Un résultat qui change l'état ou le prix d'un nom : l'Explorer le corrige en place (touch).
        self.on_change = None
        # Indisponible pour un nom qu'on croit libre : le registre est revu tout de suite (Explorer.recheck).
        self.on_blocked = None
        # Plafond commun (GLOBAL_PER_MINUTE) : heures des requêtes de la dernière minute.
        self._sent: deque[float] = deque()
        self.clock = time.monotonic
        self.sleep = asyncio.sleep  # remplaçables dans les tests

    @property
    def key(self) -> str:
        return (self.store.get("gandi_pat") or "").strip()

    def set_key(self, key: str) -> None:
        self.store.set("gandi_pat", "".join(key.split()))
        self.store.set("gandi_error", "")
        self.store.set("gandi_live_halt", "")  # nouveau jeton : la passe continue repart

    async def _slot(self) -> None:
        """Attend une place sous le plafond commun : jamais plus de GLOBAL_PER_MINUTE requêtes sur 60 s glissantes,
        quelles que soient les passes en cours (une seule boucle asyncio : pas de verrou)."""
        while True:
            now = self.clock()
            while self._sent and self._sent[0] <= now - 60:
                self._sent.popleft()
            if len(self._sent) < GLOBAL_PER_MINUTE:
                self._sent.append(now)
                return
            await self.sleep(self._sent[0] + 60 - now)

    async def check(self, name: str, http=None) -> dict:
        """Réponse brute de Gandi pour un nom (prix en euros, création seulement)."""
        if not self.key:
            raise GandiError("Jeton Gandi non configuré", "missing")
        await self._slot()
        client = http or self._http_factory()
        try:
            response = await client.get(CHECK_URL, params={"name": name, "currency": "EUR"},
                                        headers={"Authorization": f"Bearer {self.key}"})
        except httpx.HTTPError as exc:
            raise GandiError(f"Gandi injoignable ({exc.__class__.__name__})", "network") from None
        finally:
            if http is None:
                await client.aclose()
        if response.status_code in (401, 403):
            raise GandiError("Jeton Gandi refusé (expiré, révoqué ou sans la permission de lecture des domaines)", "refused")
        if response.status_code == 429:
            try:
                wait = float(response.headers.get("retry-after") or 0) or None
            except ValueError:
                wait = None
            raise GandiError("Limite de requêtes Gandi atteinte", "limit", wait)
        if response.status_code != 200:
            raise GandiError(f"Réponse Gandi inattendue ({response.status_code})", "http")
        try:
            return response.json()
        except ValueError:
            raise GandiError("Réponse Gandi illisible", "unreadable") from None

    def record(self, name: str, found: dict, now: datetime | None = None) -> bool:
        """Garde une lecture ; True si la sorte ou le prix du nom a changé. L'Explorer corrige le nom dans tous
        les cas : une vérification plus récente qu'un changement d'état peut seule rendre le résultat valable."""
        now = now or utcnow()
        before, after = save(self.conn, name, found, now)
        if after["kind"] in BLOCKED and self.on_blocked:
            self.on_blocked(name, now)
        if self.on_change:
            self.on_change(name)
        return before is None or (before["kind"], before["price"]) != (after["kind"], after["price"])

    async def refresh(self, name: str, http=None) -> dict:
        """Vérification d'un nom (bouton Vérifier, domaine suivi) : interrogé, lu, gardé. Lève GandiError."""
        try:
            found = read(await self.check(name, http))
        except GandiError as exc:
            if exc.code == "refused":
                self.store.set("gandi_error", str(exc))  # visible dans Paramètres
            raise
        self.record(name, found)
        if self.store.get("gandi_error"):
            self.store.set("gandi_error", "")  # le jeton marche de nouveau
        return found

    async def test(self) -> dict:
        """Interroge les noms témoins ; garde les réponses brutes et un résumé lisible."""
        samples, summary_ = {}, []
        async with self._http_factory() as http:
            for name in SAMPLES:
                try:
                    raw = await self.check(name, http)
                except GandiError as exc:
                    samples[name] = {"error": str(exc)}
                    summary_.append({"name": name, "error": str(exc)})
                    if exc.code == "refused":
                        break  # jeton refusé : inutile d'insister sur les autres noms
                    continue
                samples[name] = raw
                summary_.append({"name": name, **brief(raw)})
        self.store.set("gandi_samples", json.dumps({"at": iso(utcnow()), "samples": samples}))
        ok = any("error" not in s for s in summary_)
        self.store.set("gandi_error", "" if ok else (summary_[0]["error"] if summary_ else ""))
        return {"ok": ok, "results": summary_}

    def samples(self) -> dict | None:
        try:
            return json.loads(self.store.get("gandi_samples") or "null")
        except ValueError:
            return None

    def status(self) -> dict:
        found = self.samples() or {}
        return {"key_set": bool(self.key), "tested_at": found.get("at"), "error": self.store.get("gandi_error") or None}


# --- Passe de nuit -----------------------------------------------------------------------------

def _number(store, key: str, default: float, lo: float, hi: float) -> float:
    try:
        return min(hi, max(lo, float(store.get(key) or default)))
    except ValueError:
        return default


class NightPass:
    """Chaque nuit, les noms non pris de toutes les extensions lues, du meilleur score au moins bon."""

    key = "gandi_pass"

    def __init__(self, gandi: Gandi, conn, store, explorer, local_zone, notify=None):
        self.gandi = gandi
        self.conn = conn
        self.store = store
        self.explorer = explorer
        self.local_zone = local_zone
        self.notify = notify  # async (texte) -> motif d'échec ou None : Telegram
        self.running = False
        self.progress: dict | None = None
        self.sleep = asyncio.sleep  # remplaçable dans les tests

    # --- Réglages ---------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.store.get(f"{self.key}_enabled", "1") == "1"

    @property
    def rate(self) -> float:
        return _number(self.store, f"{self.key}_rate", RATE, MIN_RATE, MAX_RATE)

    @property
    def nightly(self) -> int:
        return int(_number(self.store, f"{self.key}_nightly", NIGHTLY, 1, 50_000))

    @property
    def days(self) -> int:
        return int(_number(self.store, f"{self.key}_days", FRESH_DAYS, 1, 90))

    def settings(self) -> dict:
        return {"enabled": self.enabled, "rate": self.rate, "nightly": self.nightly, "days": self.days,
                "window": f"{START[0]:02d}:{START[1]:02d}-{END[0]:02d}:{END[1]:02d}"}

    def unsold(self) -> set[str]:
        return unsold(self.store)

    def _mark_unsold(self, tld: str) -> None:
        mark_unsold(self.store, tld)

    def tlds(self) -> list[str]:
        """Extensions lues (.fr, zones ICANN, .co, .ai, .io) : pas celles par Gandi seul, que la passe continue couvre."""
        ex = self.explorer
        skip = self.unsold() | set(ex.gandi_tlds())
        return [t for t in ex.tlds() if t not in skip]

    # --- File -----------------------------------------------------------------------------

    @staticmethod
    def _in_window(now_local: datetime) -> bool:
        return START <= (now_local.hour, now_local.minute) < END

    def due(self, now_local: datetime) -> bool:
        return (self.enabled and bool(self.gandi.key) and self._in_window(now_local)
                and self.store.get(f"{self.key}_last") != now_local.date().isoformat() and bool(self.tlds()))

    def pending(self, limit: int, now: datetime | None = None) -> list[str]:
        """Noms à vérifier, dans l'ordre de la file IA : libres d'abord, par score complet, revus chaque jour ;
        puis probablement libres, libres selon le WHOIS, premiums et indisponibles déjà vus chez Gandi, par score
        de base, toutes extensions mêlées. Sautés : vérifiés depuis moins de N jours (un jour pour un nom qui a
        changé d'état depuis, ou une réponse en erreur). Seuls les noms qui ont un sens."""
        from .explore import ABSENT, FREE, GONE, PREMIUM, RESERVED, UNCONFIRMED  # import local : explore importe ce module

        now = now or utcnow()
        ex = self.explorer
        u = ex.universe
        buckets = u.buckets()
        tlds = self.tlds()
        if not tlds:
            return []
        data = {t: ex.status_bytes(t) for t in tlds}
        seen = {r[0]: (r[1], r[2], r[3]) for r in self.conn.execute(
            f"SELECT name, checked_at, kind, error FROM gandi_checks WHERE tld IN ({','.join('?' * len(tlds))})", tlds)}
        since = dict(self.conn.execute("SELECT name, state_since FROM drops WHERE state_since IS NOT NULL").fetchall())
        listed = ex.reserved.names() if ex.reserved is not None else frozenset()
        fresh_after = iso(now - timedelta(days=self.days) + timedelta(hours=2))
        recent_after = iso(now - timedelta(days=CHANGED_DAYS) + timedelta(hours=2))

        def due(name: str, daily: bool = False) -> bool:
            found = seen.get(name)
            if found is None:
                return True
            at, kind, error = found
            if daily or kind == "error" or error or (since.get(name) or "") > at:
                return at <= recent_after
            return at <= fresh_after

        picked: list[str] = []
        taken: set[str] = set()

        def take(name: str, daily: bool = False) -> bool:
            if name in taken or not due(name, daily):
                return False
            taken.add(name)
            picked.append(name)
            return len(picked) >= limit

        soon = []
        for tld in tlds:
            ctx = ex._bonus_context(tld)
            # Libres : revus chaque jour (un nom enregistré depuis passe indisponible, revérifié au registre).
            # Bientôt libres (rédemption, suppression) : pas demandés, Gandi les dit toujours indisponibles.
            for code in (FREE,):
                at = data[tld].find(code)
                while at != -1:
                    if u.good[at]:
                        label = u.label(at)
                        soon.append((ex._value(tld, at, label, ctx)["score"], f"{label}.{tld}"))
                    at = data[tld].find(code, at + 1)
        for _, name in sorted(soon, key=lambda x: (-x[0], x[1])):
            if take(name, daily=True):
                return picked
        maybe = (GONE, ABSENT, UNCONFIRMED, PREMIUM)
        for score in range(len(buckets) - 1, -1, -1):
            for i in buckets[score]:
                if not u.good[i]:
                    continue
                for tld in tlds:
                    code = data[tld][i]
                    if code not in maybe and code != RESERVED:
                        continue
                    label = u.label(i)
                    name = f"{label}.{tld}"
                    # Réservé : seulement un nom déjà dit indisponible par Gandi (revu à son tour), pas un terme Afnic.
                    if code == RESERVED and (name not in seen or name in listed):
                        continue
                    if code == ABSENT and len(label) <= 2:
                        continue  # formats gardés par les registres
                    if take(name):
                        return picked
        return picked

    # --- Passe ----------------------------------------------------------------------------

    async def _alert(self, text: str) -> None:
        await alert(self.store, self.local_zone, self.notify, text)

    async def run(self, limit: int | None = None, manual: bool = False) -> dict:
        if self.running:
            return {"started": False}
        if not self.gandi.key:
            return {"started": False, "error": "Jeton Gandi non configuré"}
        self.running = True
        loop = asyncio.get_running_loop()
        stats = {"started_at": iso(utcnow()), "manual": manual, "queued": 0, "checked": 0, "changed": 0,
                 "kinds": {}, "failures": 0, "rate": self.rate}
        self.progress = stats
        halted = None
        try:
            names = await loop.run_in_executor(None, self.pending, limit or self.nightly)
            stats["queued"] = len(names)
            interval = 1 / self.rate
            fails = limited = 0
            invalid: dict[str, int] = {}
            skipped_tlds: set[str] = set()
            async with self.gandi._http_factory() as http:
                index = 0
                while index < len(names):
                    name = names[index]
                    tld = name.rpartition(".")[2]
                    if tld in skipped_tlds:
                        index += 1
                        continue
                    if not manual and not self._in_window(datetime.now(self.local_zone)):
                        stats["stopped"] = "heure de fin atteinte"
                        break
                    started = loop.time()
                    try:
                        raw = await self.gandi.check(name, http)
                    except GandiError as exc:
                        if exc.code in ("refused", "missing"):
                            halted = str(exc)
                            self.store.set("gandi_error", str(exc))
                            break
                        if exc.code == "limit":
                            limited += 1
                            if limited >= LIMIT_STOP:
                                halted = f"{LIMIT_STOP} refus d'affilée pour trop de requêtes"
                                break
                            # Gandi demande d'attendre : on attend, puis on reprend au même nom.
                            await self.sleep(min(600.0, exc.retry_after or LIMIT_WAIT))
                            continue
                        stats["failures"] += 1
                        fails += 1
                        if exc.code == "http":
                            invalid[tld] = invalid.get(tld, 0) + 1
                        if fails >= FAIL_STOP:
                            halted = f"{FAIL_STOP} échecs d'affilée ({exc})"
                            break
                        index += 1
                    else:
                        fails = limited = 0
                        found = read(raw)
                        if found["kind"] == "error" and found["status"] in ("", "error_invalid"):
                            invalid[tld] = invalid.get(tld, 0) + 1
                        else:
                            invalid[tld] = 0
                        if self.gandi.record(name, found):
                            stats["changed"] += 1
                        stats["checked"] += 1
                        stats["kinds"][found["kind"]] = stats["kinds"].get(found["kind"], 0) + 1
                        index += 1
                    if invalid.get(tld, 0) >= UNSOLD_STOP:
                        # Extension que Gandi ne vend pas : écartée de cette passe et des 30 prochains jours.
                        skipped_tlds.add(tld)
                        self._mark_unsold(tld)
                        log.info("Gandi : .%s écartée (%s réponses invalides d'affilée)", tld, UNSOLD_STOP)
                    wait = interval - (loop.time() - started)
                    if wait > 0:
                        await self.sleep(wait)
            if skipped_tlds:
                stats["unsold"] = sorted(skipped_tlds)
            if stats["checked"] and not halted:
                self.store.set("gandi_error", "")
        except Exception as exc:  # noqa: BLE001  le journal garde la cause, l'écran l'affiche
            log.warning("Gandi : %s", exc)
            stats["error"] = str(exc)
        finally:
            self.running = False
            self.progress = None
            stats["finished_at"] = iso(utcnow())
            if halted:
                stats["stopped"] = halted
                stats["error"] = halted
            self.store.set(f"{self.key}_status", json.dumps(stats))
            if stats["checked"] or "error" not in stats:
                self.store.set(f"{self.key}_last", datetime.now(self.local_zone).date().isoformat())
        log.info("Gandi : %s noms vérifiés sur %s, %s changements%s", stats["checked"], stats["queued"], stats["changed"],
                 f", arrêt : {stats['stopped']}" if stats.get("stopped") else "")
        if halted:
            await self._alert(f"Gandi : passe de nuit arrêtée ({halted}). {stats['checked']} noms vérifiés sur {stats['queued']}.")
        return stats

    def status(self) -> dict:
        """État de la dernière passe et totaux : Paramètres, Veille, /api/diag (aucun secret)."""
        try:
            info = json.loads(self.store.get(f"{self.key}_status") or "{}")
        except ValueError:
            info = {}
        if self.progress is not None:
            info = {**info, "current": dict(self.progress)}
        return {**info, **self.settings(), "running": self.running,
                "last_day": self.store.get(f"{self.key}_last") or None, "unsold": sorted(self.unsold()),
                "totals": totals(self.conn)}


# --- Passe continue : extensions par Gandi seul ------------------------------------------------

class LivePass:
    """Jour et nuit, les extensions par Gandi seul (explorer.gandi_tlds : .com sans zone lisible). Chaque nom qui a un
    sens, les listes fournies d'abord ; libres et premiums revus chaque jour, les autres après N jours (réglable).
    Même client et même plafond commun que la passe de nuit. La zone lue, l'extension sort de cette passe."""

    key = "gandi_live"

    def __init__(self, gandi: Gandi, conn, store, explorer, local_zone, notify=None):
        self.gandi = gandi
        self.conn = conn
        self.store = store
        self.explorer = explorer
        self.local_zone = local_zone
        self.notify = notify
        self.running = False
        self.progress: dict | None = None
        self.sleep = asyncio.sleep  # remplaçable dans les tests

    # --- Réglages ---------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.store.get(f"{self.key}_enabled", "1") == "1"

    @property
    def rate(self) -> float:
        return _number(self.store, f"{self.key}_rate", LIVE_RATE, MIN_RATE, LIVE_MAX_RATE)

    @property
    def days(self) -> int:
        return int(_number(self.store, f"{self.key}_days", FRESH_DAYS, 1, 90))

    def tlds(self) -> list[str]:
        """Extensions concernées en ce moment : par Gandi seul, jeton enregistré, vendues par Gandi."""
        return [t for t in self.explorer.gandi_tlds() if t not in unsold(self.store)]

    def halted(self) -> str | None:
        """Jeton refusé il y a moins de LIVE_HALT_HOURS : la passe attend (un nouveau jeton la relance)."""
        at = self.store.get(f"{self.key}_halt") or ""
        return at if at and at > iso(utcnow() - timedelta(hours=LIVE_HALT_HOURS)) else None

    def due(self) -> bool:
        return not self.running and self.enabled and bool(self.gandi.key) and not self.halted() and bool(self.tlds())

    # --- File -----------------------------------------------------------------------------

    def plan(self, limit: int, now: datetime | None = None) -> tuple[list[str], dict]:
        """(noms à demander, dans l'ordre ; compte des noms concernés et de ceux à revoir). Ordre : libres et premiums
        à revoir (revus chaque jour), puis les listes fournies (termes premium, mots-clés et marques mêlés par rang,
        puis métier + ville), puis le reste par score de base. Noms qui ont un sens seulement ; pas un terme réservé,
        ni un nom enregistré ou bientôt libre d'après le registre (Vérifier), sauf « enregistré » revu après N jours."""
        from .explore import FREE, PREMIUM, REGISTERED, TAKEN_GANDI, UNCHECKED  # import local : explore importe ce module

        now = now or utcnow()
        tlds = self.tlds()
        info = {"total": 0, "due": 0, "planned_at": iso(now)}
        if not tlds:
            return [], info
        ex = self.explorer
        u = ex.universe
        buckets = u.buckets()
        listed = ex.reserved.names() if ex.reserved is not None else frozenset()
        fresh_after = iso(now - timedelta(days=self.days) + timedelta(hours=2))
        recent_after = iso(now - timedelta(days=CHANGED_DAYS) + timedelta(hours=2))
        wanted = (UNCHECKED, FREE, PREMIUM, TAKEN_GANDI, REGISTERED)
        table = bytes(1 if c in wanted else 0 for c in range(256))
        data, fresh, daily = {}, {}, []
        for tld in tlds:
            data[tld] = ex.status_bytes(tld)
            since = dict(self.conn.execute("SELECT label, state_since FROM drops WHERE tld = ? AND state_since IS NOT NULL",
                                           (tld,)).fetchall())
            done = bytearray(u.size)
            cut = len(tld) + 1
            for name, at, kind, error in self.conn.execute(
                    "SELECT name, checked_at, kind, error FROM gandi_checks WHERE tld = ?", (tld,)):
                label = name[:-cut]
                i = u.index(label)
                if i is None:
                    continue
                again = kind in DAILY_KINDS or bool(error) or (since.get(label) or "") > at
                if at > (recent_after if again else fresh_after):
                    done[i] = 1
                elif kind in ("available", "premium") and u.good[i]:
                    daily.append((-(u.base_table[i] & 127), i, tld))
            fresh[tld] = done
            eligible = int.from_bytes(data[tld].translate(table), "big") & u.good_int
            info["total"] += eligible.bit_count()
            info["due"] += (eligible & ~int.from_bytes(done, "big")).bit_count()
        picked: list[str] = []
        chosen = {t: bytearray(u.size) for t in tlds}

        def take(i: int, tld: str) -> bool:
            if chosen[tld][i] or fresh[tld][i] or not u.good[i] or data[tld][i] not in wanted:
                return False
            name = f"{u.label(i)}.{tld}"
            if name in listed:
                return False
            chosen[tld][i] = 1
            picked.append(name)
            return len(picked) >= limit

        for _, i, tld in sorted(daily):
            if take(i, tld):
                return picked, info
        ranks = wordlists.ranks()
        for keys in FIRST_LISTS:
            keys = [k for k in keys if ranks.get(k)]
            if not keys:
                continue
            for i in u.frequency(",".join(keys)):
                for tld in tlds:
                    if take(i, tld):
                        return picked, info
        for score in range(len(buckets) - 1, -1, -1):
            for i in buckets[score]:
                if not u.good[i]:
                    continue
                for tld in tlds:
                    if take(i, tld):
                        return picked, info
        return picked, info

    # --- Passe ----------------------------------------------------------------------------

    def _save(self, stats: dict) -> None:
        self.store.set(f"{self.key}_status", json.dumps(stats))

    async def _wait(self, seconds: float) -> None:
        """Attente par tranches de 5 s : décocher la case (ou retirer le jeton) arrête la passe sans attendre la fin."""
        left = seconds
        while left > 0 and self.enabled and self.gandi.key:
            await self.sleep(min(5.0, left))
            left -= 5.0

    async def run(self, tours: int | None = None) -> dict:
        """Tourne jusqu'à l'arrêt : case décochée, jeton retiré ou refusé, extension lue par sa zone. tours : nombre
        de tours au plus (tests)."""
        if self.running:
            return {"started": False}
        if not self.gandi.key:
            return {"started": False, "error": "Jeton Gandi non configuré"}
        self.running = True
        loop = asyncio.get_running_loop()
        stats = {"started_at": iso(utcnow()), "checked": 0, "changed": 0, "kinds": {}, "failures": 0, "tours": 0,
                 "queued": 0, "tour_checked": 0, "next_tour": None}
        self.progress = stats
        halted = None
        try:
            async with self.gandi._http_factory() as http:
                while tours is None or stats["tours"] < tours:
                    if not (self.enabled and self.gandi.key and self.tlds()):
                        break
                    names, info = await loop.run_in_executor(None, self.plan, max(1, int(self.rate * LIVE_TOUR)))
                    stats.update(info, queued=len(names), tour_checked=0, tours=stats["tours"] + 1)
                    if not names:
                        stats["next_tour"] = iso(utcnow() + timedelta(seconds=LIVE_IDLE))
                        self._save(stats)
                        await self._wait(LIVE_IDLE)
                        continue
                    stats["next_tour"] = None
                    halted = await self._tour(names, http, stats, loop)
                    self._save(stats)
                    if halted:
                        break
        except Exception as exc:  # noqa: BLE001  le journal garde la cause, l'écran l'affiche
            log.warning("Gandi, passe continue : %s", exc)
            stats["error"] = str(exc)
        finally:
            self.running = False
            self.progress = None
            stats["finished_at"] = iso(utcnow())
            if halted:
                stats["stopped"] = stats["error"] = halted
            elif "error" not in stats:
                stats["stopped"] = ("arrêtée dans Paramètres" if not self.enabled else "jeton retiré" if not self.gandi.key
                                    else "zone lue : fonctionnement normal" if not self.tlds() else "fin des tours demandés")
            self._save(stats)
        log.info("Gandi, passe continue : %s noms vérifiés, %s changements, arrêt : %s", stats["checked"], stats["changed"],
                 stats.get("stopped") or stats.get("error"))
        if halted:
            await alert(self.store, self.local_zone, self.notify,
                        f"Gandi : passe continue arrêtée ({halted}). {stats['checked']} noms vérifiés.")
        return stats

    async def _tour(self, names: list[str], http, stats: dict, loop) -> str | None:
        """Un tour : chaque nom au rythme réglé. Renvoie le motif d'un arrêt définitif (jeton refusé), sinon None."""
        fails = limited = 0
        invalid: dict[str, int] = {}
        index = 0
        tlds: set[str] = set()
        interval = 1.0
        while index < len(names):
            if not self.enabled:
                return None  # case décochée : arrêt au nom suivant
            if index % 100 == 0 or not tlds:
                # Jeton, rythme et extensions relus tous les 100 noms : une zone lue entre-temps sort de la passe.
                if not self.gandi.key:
                    return None
                tlds, interval = set(self.tlds()), 1 / self.rate
                if not tlds:
                    return None
                if index:
                    self._save(stats)
            name = names[index]
            tld = name.rpartition(".")[2]
            if tld not in tlds:
                index += 1
                continue
            started = loop.time()
            try:
                raw = await self.gandi.check(name, http)
            except GandiError as exc:
                if exc.code in ("refused", "missing"):
                    self.store.set("gandi_error", str(exc))
                    self.store.set(f"{self.key}_halt", iso(utcnow()))
                    return str(exc)
                if exc.code == "limit":
                    # Gandi demande d'attendre : on attend (deux fois plus à chaque refus d'affilée), puis on reprend au même nom.
                    limited += 1
                    stats["limited"] = stats.get("limited", 0) + 1
                    await self._wait(min(LIVE_LIMIT_MAX, (exc.retry_after or LIMIT_WAIT) * 2 ** (limited - 1)))
                    continue
                stats["failures"] += 1
                fails += 1
                if exc.code == "http":
                    invalid[tld] = invalid.get(tld, 0) + 1
                if fails >= FAIL_STOP:
                    fails = 0
                    stats["paused_at"] = iso(utcnow())
                    log.info("Gandi, passe continue : %s échecs d'affilée (%s), pause", FAIL_STOP, exc)
                    await self._wait(LIVE_FAIL_PAUSE)
                index += 1
            else:
                fails = limited = 0
                found = read(raw)
                if found["kind"] == "error" and found["status"] in ("", "error_invalid"):
                    invalid[tld] = invalid.get(tld, 0) + 1
                else:
                    invalid[tld] = 0
                if self.gandi.record(name, found):
                    stats["changed"] += 1
                stats["checked"] += 1
                stats["tour_checked"] += 1
                stats["kinds"][found["kind"]] = stats["kinds"].get(found["kind"], 0) + 1
                index += 1
            if invalid.get(tld, 0) >= UNSOLD_STOP:
                mark_unsold(self.store, tld)
                tlds.discard(tld)
                log.info("Gandi : .%s écartée (%s réponses invalides d'affilée)", tld, UNSOLD_STOP)
            wait = interval - (loop.time() - started)
            if wait > 0:
                await self.sleep(wait)
        if stats["tour_checked"]:
            self.store.set("gandi_error", "")
        return None

    def status(self) -> dict:
        """Carte Gandi de Paramètres et /api/diag (aucun secret) : extensions concernées, noms vérifiés, restant,
        rythme, prochain tour, dernier arrêt."""
        try:
            info = json.loads(self.store.get(f"{self.key}_status") or "{}")
        except ValueError:
            info = {}
        if self.progress is not None:
            info = dict(self.progress)
        tlds = self.tlds()
        known = totals(self.conn, tlds) if tlds else {"known": 0, "by_kind": {}, "last": None}
        remaining = max(0, (info.get("due") or 0) - (info.get("tour_checked") or 0)) if info.get("planned_at") else None
        return {**info, "enabled": self.enabled, "rate": self.rate, "days": self.days, "max_rate": LIVE_MAX_RATE,
                "per_minute_cap": GLOBAL_PER_MINUTE, "tlds": tlds, "configured": only_tlds(self.store),
                "running": self.running, "halted_at": self.halted(), "remaining": remaining,
                "verified": known["known"] - known["by_kind"].get("error", 0), "totals": known}
