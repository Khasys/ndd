"""Radar des nouveautés : les mots, expressions et événements de l'actualité, en français et en anglais, enregistrés
avant les autres en .fr et en .com.

Deux fois par jour, une tâche IA planifiée (abonnement Claude, pas l'API) lit l'actualité sur le web et envoie au NAS
les noms que l'actualité justifie, avec leur langue, leur nature, la raison et la page vue (POST /api/ai/news, jeton
IA). Le NAS les range (table news), les ajoute à la liste « Nouveautés » (wordlists.NEWS : Explorer, langue, zones),
puis les vérifie au registre en .fr et en .com (Explorer.verify, origine discrète : hors Veille tant qu'ils ne sont pas
en suppression). Un nom libre est demandé à l'IA (ai_requests, source news) : la tâche du quart d'heure le note avec
la grille habituelle, la raison de l'actualité jointe, sans voir qui l'a proposé. Un nom « À prendre » encore libre part
sur Telegram (notify.news_tick), une seule fois ; les « À surveiller » sont cités dans le même message.

Garde-fous : la consigne de la tâche (GUIDE) écarte personnes, marques, entreprises, produits, événements protégés et
drames ; la grille IA note le risque juridique (sous-note risque à 8 et plus : « À laisser », jamais d'alerte). La
mesure (stats) dit ce que le radar rapporte : noms libres à la vérification, bien notés, puis enregistrés par d'autres.
"""

from datetime import datetime, timedelta

from . import ai, drops, explore, states, wordlists
from .checker import iso
from .news_format import FORMAT, GUIDE, KINDS, LANGS, MAX_ITEMS, MAX_LEN, clean  # noqa: F401  (réexportés)

SOURCE = "news"             # ai_requests.source
ANNOUNCED, SEEN = "news", "news_seen"  # alert_sent.kind : « À prendre » annoncé ; vu sans message à son sujet
FR_SOURCE = "nouveautes"    # fr_requests.source (journal des requêtes Afnic)
TLDS = ("fr", "com")
KEEP_DAYS = 365             # liste « Nouveautés » et doublons : un an
CHECK_BATCH = 30            # noms vérifiés par passage (deux requêtes chacun)
MAX_TRIES = 3               # registre en panne : abandon après trois passages
FR_GAP = 2.0                # secondes entre deux requêtes .fr : l'Afnic limite (file d'attente à part, frdata)
RERATE = timedelta(days=30)  # avis plus ancien : redemandé avec le contexte de l'actualité
ALERT_WINDOW = timedelta(days=3)  # nom noté plus tard (tâche IA arrêtée) : pas d'alerte, il reste dans l'Explorer
# États au registre lors de la vérification (colonnes fr et com).
OUTCOMES = {explore.FREE: "free", explore.UNCONFIRMED: "free", explore.COMING: "coming",
            explore.REGISTERED: "taken", explore.SUSPENDED: "taken", explore.RESERVED: "reserved",
            explore.PREMIUM: "premium"}
RATED = ("free", "coming")  # notés par l'IA ; l'alerte ne part que pour « free »
TAKE, WATCH = "À prendre", "À surveiller"

def add(conn, items: list, now: datetime) -> dict:
    """Range les noms proposés ; ceux déjà proposés depuis un an, en double ou hors format sont refusés."""
    saved, rejected, seen = [], [], set()
    since = iso(now - timedelta(days=KEEP_DAYS))
    for item in items:
        found, reason = clean(item)
        label = found["label"] if found else str((item or {}).get("label", "") if isinstance(item, dict) else "")[:MAX_LEN + 10]
        if found and label in seen:
            reason = "doublon"
        elif found:
            row = conn.execute("SELECT added_at FROM news WHERE label = ?", (label,)).fetchone()
            if row is not None and row["added_at"] >= since:
                reason = f"déjà proposé le {row['added_at'][8:10]}/{row['added_at'][5:7]}/{row['added_at'][:4]}"
        if reason:
            rejected.append({"label": label, "reason": reason})
            continue
        seen.add(label)
        # Proposé il y a plus d'un an : la ligne repart de zéro (nouvelle vérification, nouvel avis).
        conn.execute(
            """INSERT INTO news (label, lang, kind, reason, source, added_at) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(label) DO UPDATE SET lang = excluded.lang, kind = excluded.kind, reason = excluded.reason,
                 source = excluded.source, added_at = excluded.added_at, fr = NULL, com = NULL, checked_at = NULL, tries = 0""",
            (label, found["lang"], found["kind"], found["reason"], found["source"], iso(now)))
        saved.append(label)
    return {"saved": saved, "rejected": rejected}


def _kept(conn, now: datetime) -> list:
    return conn.execute("SELECT label, lang, kind, reason, added_at FROM news WHERE added_at >= ? "
                        "ORDER BY added_at DESC, label", (iso(now - timedelta(days=KEEP_DAYS)),)).fetchall()


def publish(conn, now: datetime) -> bool:
    """Liste « Nouveautés » refaite depuis la table : l'univers de l'Explorer et les zones suivent ces noms."""
    return wordlists.set_news([(r["label"], r["lang"]) for r in _kept(conn, now)])


def reasons(conn, now: datetime) -> dict[str, dict]:
    """Libellé vers sa nouveauté (raison, nature, date) : la raison affichée par l'Explorer et la fiche
    (explore.news_reason)."""
    return {r["label"]: {"reason": r["reason"], "kind": r["kind"], "added_at": r["added_at"]} for r in _kept(conn, now)}


def known(conn, now: datetime) -> list[str]:
    return [r["label"] for r in _kept(conn, now)]


def recent(conn, now: datetime, days: int = 2) -> list[dict]:
    """Noms des derniers passages, avec l'état vu au registre et le verdict de l'IA : la tâche y voit ce qui marche."""
    rows = conn.execute("SELECT label, lang, kind, added_at, fr, com FROM news WHERE added_at >= ? ORDER BY added_at DESC, label",
                        (iso(now - timedelta(days=days)),)).fetchall()
    opinions = ai.load(conn, [f"{r['label']}.{t}" for r in rows for t in TLDS])
    out = []
    for r in rows:
        item = dict(r)
        item["verdicts"] = {t: (opinions[f"{r['label']}.{t}"]["comment"] or "").split(".")[0]
                            for t in TLDS if f"{r['label']}.{t}" in opinions}
        out.append(item)
    return out


def to_check(conn, limit: int = CHECK_BATCH) -> list:
    return conn.execute("SELECT * FROM news WHERE checked_at IS NULL AND tries < ? ORDER BY added_at, label LIMIT ?",
                        (MAX_TRIES, limit)).fetchall()


def outcome(code: int) -> str:
    return OUTCOMES.get(code, "unknown")


def record(conn, label: str, found: dict[str, str | None], now: datetime) -> None:
    """États vus au registre ; une extension sans réponse (panne, Afnic en pause) est reprise au passage suivant,
    trois fois au plus."""
    row = conn.execute("SELECT fr, com, tries FROM news WHERE label = ?", (label,)).fetchone()
    if row is None:
        return
    values = {t: found.get(t) or row[t] for t in TLDS}
    done = all(values.values()) or row["tries"] + 1 >= MAX_TRIES
    conn.execute("UPDATE news SET fr = ?, com = ?, tries = tries + 1, checked_at = ? WHERE label = ?",
                 (values["fr"], values["com"], iso(now) if done else None, label))


def ask(conn, label: str, found: dict[str, str | None], now: datetime) -> list[str]:
    """Demande l'avis de l'IA pour les extensions libres (ou bientôt) : sauf avis de moins de 30 jours."""
    asked = []
    for tld in TLDS:
        if found.get(tld) not in RATED:
            continue
        name = f"{label}.{tld}"
        opinion = ai.load(conn, [name]).get(name)
        if opinion and max(opinion["rated_at"] or "", opinion.get("reviewed_at") or "") >= iso(now - RERATE):
            continue
        ai.request(conn, name, SOURCE)
        asked.append(name)
    return asked


def ready(conn, now: datetime) -> list[dict]:
    """Noms libres à la vérification, notés depuis (demande traitée), pas encore annoncés, encore libres au registre."""
    rows = conn.execute("SELECT * FROM news WHERE checked_at IS NOT NULL AND added_at >= ? ORDER BY added_at, label",
                        (iso(now - ALERT_WINDOW),)).fetchall()
    out = []
    for row in rows:
        for tld in TLDS:
            if row[tld] != "free":
                continue
            name = f"{row['label']}.{tld}"
            if conn.execute("SELECT 1 FROM alert_sent WHERE name = ? AND kind IN (?, ?)", (name, ANNOUNCED, SEEN)).fetchone():
                continue
            request = conn.execute("SELECT done_at FROM ai_requests WHERE name = ?", (name,)).fetchone()
            if request is not None and request["done_at"] is None:
                continue  # avis pas encore rendu
            opinion = ai.load(conn, [name]).get(name)
            if opinion is None:
                continue
            drop = conn.execute("SELECT state FROM drops WHERE name = ?", (name,)).fetchone()
            still = drop is None or drop["state"] in (states.AVAILABLE, drops.UNCONFIRMED)
            out.append({"name": name, "tld": tld, "news": dict(row), "ai": opinion, "free": still,
                        "verdict": (opinion["comment"] or "").split(".")[0]})
    return out


def stats(conn, explorer, now: datetime) -> dict:
    """Ce que le radar rapporte : proposés, libres à la vérification, notés, annoncés, puis pris par d'autres."""
    total = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
    week = conn.execute("SELECT COUNT(*) FROM news WHERE added_at >= ?", (iso(now - timedelta(days=7)),)).fetchone()[0]
    last = conn.execute("SELECT MAX(added_at) FROM news").fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM news WHERE checked_at IS NULL AND tries < ?", (MAX_TRIES,)).fetchone()[0]
    out = {"total": total, "last_7_days": week, "last_added_at": last, "to_check": pending, "by_tld": {}}
    owned = {r[0] for r in conn.execute("SELECT name FROM portfolio")}
    u = explorer.universe if explorer is not None else None
    for tld in TLDS:
        seen = dict(conn.execute(f"SELECT {tld}, COUNT(*) FROM news WHERE {tld} IS NOT NULL GROUP BY {tld}").fetchall())
        free = [r[0] + "." + tld for r in conn.execute(f"SELECT label FROM news WHERE {tld} = 'free'")]
        opinions = ai.load(conn, free)
        verdicts: dict[str, int] = {}
        for opinion in opinions.values():
            v = (opinion["comment"] or "").split(".")[0]
            verdicts[v] = verdicts.get(v, 0) + 1
        taken = bought = 0
        if u is not None and free and tld in explorer.tlds():
            data = explorer.status_bytes(tld)
            for name in free:
                i = u.index(name[:-len(tld) - 1])
                if name in owned:
                    bought += 1
                elif i is not None and data[i] in (explore.REGISTERED, explore.SUSPENDED):
                    taken += 1
        alerted = conn.execute("SELECT COUNT(*) FROM alert_sent WHERE kind = ? AND name LIKE ?", (ANNOUNCED, f"%.{tld}")).fetchone()[0]
        out["by_tld"][tld] = {"states": seen, "verdicts": verdicts, "alerted": alerted,
                              "taken_by_others": taken, "bought": bought}
    return out
