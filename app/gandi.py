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
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta

import httpx

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
KIND_LABELS = {"available": "Libre (Gandi)", "premium": "Premium (Gandi)", "unavailable": "Indisponible chez Gandi",
               "reserved": "Réservé (Gandi)", "restricted": "Restreint (Gandi)", "error": "Erreur Gandi"}

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


class GandiError(Exception):
    """code : refused (jeton refusé), limit (trop de requêtes), network, http, unreadable, missing."""

    def __init__(self, message: str, code: str = "", retry_after: float | None = None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


def read(raw: dict) -> dict:
    """Réponse brute de /v5/domain/check -> sorte, statut brut, prix TTC et HT d'un an de création.

    premium : une ligne de prix de type « premium », ou le statut unavailable_premium ; reserved :
    available_reserved, reserved_corporate ; restricted : unavailable_restricted ; error : pending,
    error_* et tout statut inconnu. Prix : la première ligne d'une durée minimale d'un an (TVA comprise
    seulement quand le nom est disponible, comme Gandi l'affiche)."""
    product = next((p for p in raw.get("products") or [] if p.get("process", "create") == "create"), None)
    if product is None:
        return {"kind": "error", "status": "", "price": None, "price_ht": None, "currency": raw.get("currency") or "EUR"}
    status = str(product.get("status") or "")
    prices = [p for p in product.get("prices") or [] if isinstance(p, dict)]
    premium = status == "unavailable_premium" or any(p.get("type") == "premium" for p in prices)
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


# --- Résultats gardés -------------------------------------------------------------------

def load(conn, names: list[str]) -> dict[str, dict]:
    found = {}
    for chunk in range(0, len(names), 900):
        part = names[chunk:chunk + 900]
        rows = conn.execute(f"SELECT * FROM gandi_checks WHERE name IN ({','.join('?' * len(part))})", part)
        found.update({r["name"]: dict(r) for r in rows})
    return found


def applies(row: dict | None, since: str | None = None) -> bool:
    """Le résultat compte pour l'état du nom : pas une erreur, et pas antérieur à un changement d'état
    (since : entrée du nom dans son état actuel, Veille). Un résultat dépassé attend la passe suivante."""
    return bool(row) and row["kind"] != "error" and not (since and since > row["checked_at"])


def public(row: dict | None, since: str | None = None) -> dict | None:
    """Ce que l'interface montre : sorte, statut, prix, date, erreur en clair, et si le résultat compte."""
    if not row:
        return None
    error = row.get("error")
    return {"kind": row["kind"], "status": row["status"], "label": KIND_LABELS.get(row["kind"], ""),
            "price": row["price"], "price_ht": row["price_ht"], "currency": row["currency"] or "EUR",
            "checked_at": row["checked_at"],
            "error": ERROR_LABELS.get(error, error) if error else (ERROR_LABELS.get(row["status"], row["status"] or "réponse illisible")
                                                                 if row["kind"] == "error" else None),
            "stale": row["kind"] != "error" and bool(since and since > row["checked_at"])}


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


def totals(conn) -> dict:
    by_kind = dict(conn.execute("SELECT kind, COUNT(*) FROM gandi_checks GROUP BY kind").fetchall())
    last = conn.execute("SELECT MAX(checked_at) FROM gandi_checks").fetchone()[0]
    return {"known": sum(by_kind.values()), "by_kind": by_kind, "last": last}


# --- Client ---------------------------------------------------------------------------------

class Gandi:
    def __init__(self, store, http_factory=None, conn=None):
        self.store = store
        self.conn = conn if conn is not None else store.conn
        self._http_factory = http_factory or (lambda: httpx.AsyncClient(timeout=20, headers={"User-Agent": "domain-watch"}))
        # Un résultat qui change l'état ou le prix d'un nom : l'Explorer le corrige en place (touch).
        self.on_change = None

    @property
    def key(self) -> str:
        return (self.store.get("gandi_pat") or "").strip()

    def set_key(self, key: str) -> None:
        self.store.set("gandi_pat", "".join(key.split()))
        self.store.set("gandi_error", "")

    async def check(self, name: str, http=None) -> dict:
        """Réponse brute de Gandi pour un nom (prix en euros, création seulement)."""
        if not self.key:
            raise GandiError("Jeton Gandi non configuré", "missing")
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
        before, after = save(self.conn, name, found, now or utcnow())
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
        """Extensions que Gandi ne vend pas (réponses invalides d'affilée), écartées 30 jours."""
        try:
            found = json.loads(self.store.get("gandi_unsold") or "{}")
        except ValueError:
            return set()
        today = utcnow().date().isoformat()
        return {t for t, until in found.items() if until > today}

    def _mark_unsold(self, tld: str) -> None:
        try:
            found = json.loads(self.store.get("gandi_unsold") or "{}")
        except ValueError:
            found = {}
        found[tld] = (utcnow() + timedelta(days=UNSOLD_DAYS)).date().isoformat()
        self.store.set("gandi_unsold", json.dumps(found))

    # --- File -----------------------------------------------------------------------------

    @staticmethod
    def _in_window(now_local: datetime) -> bool:
        return START <= (now_local.hour, now_local.minute) < END

    def due(self, now_local: datetime) -> bool:
        return (self.enabled and bool(self.gandi.key) and self._in_window(now_local)
                and self.store.get(f"{self.key}_last") != now_local.date().isoformat() and bool(self.explorer.tlds()))

    def pending(self, limit: int, now: datetime | None = None) -> list[str]:
        """Noms à vérifier, dans l'ordre de la file IA : libres et bientôt libres d'abord, par score complet ;
        puis probablement libres, libres selon le WHOIS, premiums et indisponibles déjà vus chez Gandi, par score
        de base, toutes extensions mêlées. Sautés : vérifiés depuis moins de N jours (un jour pour un nom qui a
        changé d'état depuis, ou une réponse en erreur). Seuls les noms qui ont un sens."""
        from .explore import ABSENT, COMING, FREE, GONE, PREMIUM, RESERVED, UNCONFIRMED  # import local : explore importe ce module

        now = now or utcnow()
        ex = self.explorer
        u = ex.universe
        buckets = u.buckets()
        unsold = self.unsold()
        tlds = [t for t in ex.tlds() if t not in unsold]
        data = {t: ex.status_bytes(t) for t in tlds}
        seen = {r[0]: (r[1], r[2], r[3]) for r in self.conn.execute("SELECT name, checked_at, kind, error FROM gandi_checks")}
        since = dict(self.conn.execute("SELECT name, state_since FROM drops WHERE state_since IS NOT NULL").fetchall())
        listed = ex.reserved.names() if ex.reserved is not None else frozenset()
        fresh_after = iso(now - timedelta(days=self.days) + timedelta(hours=2))
        recent_after = iso(now - timedelta(days=CHANGED_DAYS) + timedelta(hours=2))

        def due(name: str) -> bool:
            found = seen.get(name)
            if found is None:
                return True
            at, kind, error = found
            if kind == "error" or error or (since.get(name) or "") > at:
                return at <= recent_after
            return at <= fresh_after

        picked: list[str] = []
        taken: set[str] = set()

        def take(name: str) -> bool:
            if name in taken or not due(name):
                return False
            taken.add(name)
            picked.append(name)
            return len(picked) >= limit

        soon = []
        for tld in tlds:
            ctx = ex._bonus_context(tld)
            for code in (FREE, COMING):
                at = data[tld].find(code)
                while at != -1:
                    if u.good[at]:
                        label = u.label(at)
                        soon.append((ex._value(tld, at, label, ctx)["score"], f"{label}.{tld}"))
                    at = data[tld].find(code, at + 1)
        for _, name in sorted(soon, key=lambda x: (-x[0], x[1])):
            if take(name):
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
        """Une alerte par jour au plus : un jeton refusé ne relance pas Telegram à chaque essai."""
        today = datetime.now(self.local_zone).date().isoformat()
        if not self.notify or self.store.get("gandi_alert_day") == today:
            return
        self.store.set("gandi_alert_day", today)
        try:
            await self.notify(text)
        except Exception as exc:  # noqa: BLE001  une alerte ratée ne doit pas casser la passe
            log.warning("Gandi : alerte non envoyée (%s)", exc)

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
