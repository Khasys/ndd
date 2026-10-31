"""Ventes réelles du marché secondaire : les rapports DNJournal (toutes les deux semaines), lus par le NAS.

Ces ventes servent de comparables : pour un nom, les ventes proches (un mot en commun, même extension, même
forme) disent ce que vaut ce genre de nom quand il se vend. Elles ne disent pas s'il se vendra : DNJournal ne
publie que les ventes d'au moins un ou deux milliers de dollars, et seulement celles qui ont eu lieu. La
compilation appartient à DNJournal : elle reste en base sur le NAS, jamais dans le dépôt.

Une source qui casse (page injoignable, mise en page changée, plus de rapport depuis 5 semaines) est une panne
signalée tout de suite sur Telegram (morning.BLOCKING, clé « market »).
"""

import asyncio
import heapq
import html
import json
import logging
import math
import re
import threading
from datetime import date, datetime, timedelta
from html.parser import HTMLParser

import httpx

from . import wordlists
from .checker import iso, parse_iso, utcnow

log = logging.getLogger(__name__)

SITE = "https://dnjournal.com/archive/"
ARCHIVES = ("domainsales-archive.htm", "domainsales-archive-{year}.htm")  # année en cours, puis les précédentes
FIRST_YEAR = 2015          # historique gardé : de 2015 à aujourd'hui (radar des vendus, 31/10/2026)
PER_RUN = 60               # rapports lus au plus par passage : l'historique depuis 2015 (330 rapports) en six heures
PAUSE = 3                  # secondes entre deux rapports (pages de 5 Mo environ) : poli avec le site
REPORT_LATE = timedelta(days=35)   # DNJournal publie toutes les deux semaines : 5 semaines sans rapport, c'est cassé
CHECK_LATE = timedelta(days=8)     # plus de lecture réussie depuis 8 jours (passage quotidien)
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) domain-watch"}
# Rapports : « domainsales/2024/0626.htm » depuis 2024, « domainsales/2022/20220817.htm » avant, parfois en adresse
# complète (2020).
# Version du lecteur : la changer fait relire les rapports passés faute de vente lisible.
PARSER = 2
REPORT_LINK = re.compile(r'href="(?:https?://(?:www\.)?dnjournal\.com/archive/)?(domainsales/(\d{4})/(\d{4}|\d{8})\.htm)"')


class MarketError(Exception):
    pass


# --- Lecture d'un rapport ------------------------------------------------------------------

class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def text_of(page: str) -> str:
    """Texte d'une page, sur une seule ligne d'espaces simples (les tableaux se lisent alors comme une suite)."""
    parser = _Text()
    parser.feed(page)
    return " ".join(html.unescape(" ".join(parser.parts)).split())


MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
DOM = r"([A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z]{2,}){1,2})"
DOM_SPACED = r"([A-Za-z0-9][A-Za-z0-9-]* ?(?:\.[A-Za-z]{2,}){1,2})"
AMOUNT = r"(?:([€£]|[A-Z]{3})\s?([0-9][0-9,]*)\s*=\s*)?\$([0-9][0-9,]*)"  # € 5,000 = $5,400 ; AUD 100,000 = $70,000
PERIOD = re.compile(r"Highest Reported [A-Za-z .]*?Sales\s*[-:]\s*Mon\.? ?([A-Za-z]+[.,]? ?\d+)(?:, \d{4})?\s*-\s*Sun\.? ?([A-Za-z]+)\.? ?(\d+),? (\d{4})")


def _price(text: str) -> int:
    return int(text.replace(",", ""))


def parse(text: str, url: str) -> list[dict]:
    """Ventes d'un rapport : classements (« Highest Reported … Sales »), tableaux additionnels par place de
    marché et ventes hors classement. Date : dernier jour de la période couverte (le jour exact n'est pas publié)."""
    period = PERIOD.search(text)
    if not period:
        return []
    end = date(int(period.group(4)), MONTHS[period.group(2)[:3].lower()], int(period.group(3))).isoformat()
    found: dict[str, dict] = {}

    def add(name: str, price: str, venue: str) -> None:
        name = name.lower().replace(" ", "")
        if "." in name and name not in found:
            found[name] = {"name": name, "price": _price(price), "date": end, "venue": venue.strip()[:60], "source": url}

    for chart in re.finditer(r"Highest Reported (?:Domain|ccTLD|Non \.Com gTLD) Sales.*?(?=Keep in mind|CLICK|As always|Now that|There w|$)",
                             text, flags=re.S):
        for m in re.finditer(r"\d+\.\s+(?:tie\s+)?" + DOM + r"(?:\s+\([^)]*\))?\s+" + AMOUNT + r"\s+(.*?)(?=\s+\d+\.\s|$)", chart.group(0)):
            add(m.group(1), m.group(4), m.group(5))
    for head in re.finditer(r"Additional ([A-Za-z.]+?)(?: \.com| ccTLD| Non \.com gTLD| gTLD)?(?: \.com)? Sales"
                            r"(?: (?:Between|from) \$[0-9,]+ (?:-|and) \$[0-9,]+)?[^$A-Za-z0-9]*", text):
        venue, pos = head.group(1), head.end()
        row = re.compile(r"\s*" + DOM + r"\s+\$\s?([0-9][0-9,]*)")
        while (m := row.match(text, pos)):
            add(m.group(1), m.group(2), venue)
            pos = m.end()
    for head in re.finditer(r"Domains?\s+Sold\s+For\s+Where\s+Sold\s+(?=[A-Za-z0-9])", text):
        pos = head.end()
        if re.match(r"\s*(\(|1\.)", text[pos:pos + 5]):
            continue  # classement numéroté, déjà lu
        row = re.compile(r"\s*" + DOM_SPACED + r"\s+" + AMOUNT + r"\s+(.{1,45}?)(?=\s+" + DOM
                         + r"\s+(?:(?:[€£]|[A-Z]{3})\s?[0-9,]+\s*=\s*)?\$|\s+\.|\s+[A-Z][a-z]+ [a-z]|$)")
        while (m := row.match(text, pos)):
            add(m.group(1), m.group(4), m.group(5))
            pos = m.end()
    return list(found.values())


def report_links(page: str) -> list[str]:
    """Rapports cités par une page d'archive, du plus récent au plus ancien."""
    links = {f"{SITE}{href}": (int(y), md[-4:]) for href, y, md in REPORT_LINK.findall(page)}
    return sorted((u for u, (y, _) in links.items() if y >= FIRST_YEAR), key=lambda u: links[u], reverse=True)


def sale_reason(sale: dict, name: str) -> str:
    """Raison d'une carte : « vendu 12 000 $ en 2019 » pour ce nom, « vendu 12 000 $ en .com (2019) » pour le même mot
    dans une autre extension."""
    price = f"{sale['price']:,}".replace(",", "\u202f")
    year = sale["date"][:4]
    if sale["name"] == name:
        return f"vendu {price} $ en {year}"
    return f"vendu {price} $ en .{sale['name'].partition('.')[2]} ({year})"


def _read_report(response: httpx.Response, url: str) -> list[dict]:
    return parse(text_of(_decode(response)), url)


async def _inline(fn, *args):
    return fn(*args)


def _decode(response: httpx.Response) -> str:
    try:
        return response.content.decode("utf-8")
    except UnicodeDecodeError:
        return response.content.decode("cp1252", errors="replace")


# --- Base et mise à jour -------------------------------------------------------------------

class Market:
    def __init__(self, conn, store):
        self.conn = conn
        self.store = store
        self.running = False
        self._index = None
        self._by_label: dict[str, dict] | None = None
        self._lock = threading.Lock()
        # Change à chaque liste des vendus publiée (une fois par passage, pas à chaque rapport : au rattrapage de
        # l'historique, l'Explorer referait tous ses scores toutes les quelques secondes) : la raison « vendu … » d'un
        # nom est alors à refaire.
        self.version = 0

    def status(self) -> dict:
        info = json.loads(self.store.get("market_status") or "{}")
        count, last = self.conn.execute("SELECT COUNT(*), MAX(date) FROM market_sales").fetchone()
        reports, skipped = self.conn.execute("SELECT COUNT(*), COALESCE(SUM(sales = 0), 0) FROM market_reports").fetchone()
        # skipped : anciens rapports passés faute de vente lisible (relus quand le lecteur change, PARSER).
        return {**info, "sales": count, "latest_sale": last, "reports": reports, "skipped": skipped, "running": self.running}

    def due(self, now: datetime) -> bool:
        info = json.loads(self.store.get("market_status") or "{}")
        last = parse_iso(info.get("at"))
        if info.get("first_year") != FIRST_YEAR:
            # Historique allongé (2015 en v0.16.0) : le rattrapage part au passage suivant, pas 20 h après le dernier.
            return not self.running
        wait = timedelta(hours=1) if info.get("left") or info.get("error") else timedelta(hours=20)
        return not self.running and (last is None or now - last >= wait)

    def problem(self, now: datetime) -> str | None:
        """Panne de la source, pour la rubrique Santé (et l'alerte Telegram immédiate) ; None si tout va bien."""
        info = json.loads(self.store.get("market_status") or "{}")
        if not info.get("at"):
            return None  # jamais lancé : rien à signaler encore
        if info.get("error"):
            return f"Ventes DNJournal : {info['error']}"
        ok_at = parse_iso(info.get("ok_at"))
        if ok_at and now - ok_at > CHECK_LATE:
            return f"Ventes DNJournal : aucune lecture réussie depuis le {ok_at.strftime('%d/%m')}"
        latest = info.get("latest_report")
        if latest and now.date() - date.fromisoformat(latest) > REPORT_LATE:
            return f"Ventes DNJournal : aucun nouveau rapport depuis le {date.fromisoformat(latest).strftime('%d/%m')} (source changée ?)"
        return None

    async def refresh(self, client: httpx.AsyncClient | None = None, off=None) -> dict:
        """Lit l'archive de l'année et, au besoin, celles des années précédentes ; puis les rapports pas encore lus,
        les plus récents d'abord, PER_RUN au plus. Une archive sans rapport, ou un rapport sans vente, est une
        erreur : la mise en page a changé. off : lecture et écriture d'un rapport hors de la boucle de l'appli
        (pages de 5 Mo, des centaines au rattrapage de l'historique)."""
        off = off or _inline
        if self.running:
            return self.status()
        self.running = True
        now = utcnow()
        info = json.loads(self.store.get("market_status") or "{}")
        own = client is None
        client = client or httpx.AsyncClient(timeout=60, headers=HEADERS, follow_redirects=True)
        added, published = 0, False
        if info.get("parser") != PARSER:
            # Lecteur amélioré : les anciens rapports passés faute de vente lisible sont relus.
            await off(lambda: self.conn.execute("DELETE FROM market_reports WHERE sales = 0"))
        try:
            links: list[str] = []
            for year in range(now.year, FIRST_YEAR - 1, -1):
                page = ARCHIVES[0] if year == now.year else ARCHIVES[1].format(year=year)
                response = await client.get(SITE + page)
                if response.status_code != 200:
                    if year == now.year:
                        raise MarketError(f"archive injoignable (HTTP {response.status_code})")
                    continue
                links += report_links(_decode(response))
            if not links:
                raise MarketError("aucun rapport dans l'archive (mise en page changée ?)")
            done = {r[0] for r in self.conn.execute("SELECT url FROM market_reports")}
            todo = [u for u in links if u not in done][:PER_RUN]
            for n, url in enumerate(todo):
                if n:
                    await asyncio.sleep(PAUSE)
                response = await client.get(url)
                if response.status_code != 200:
                    raise MarketError(f"rapport injoignable (HTTP {response.status_code}) : {url}")
                sales = await off(_read_report, response, url)
                if not sales:
                    if url == links[0]:
                        raise MarketError(f"aucune vente lue dans {url} (mise en page changée ?)")
                    # Ancien rapport à la mise en page d'alors : passé (noté lu, aucune vente), pas bloquant.
                    log.warning("Ventes DNJournal : aucune vente lue dans %s, rapport passé", url)
                added += await off(self.save, url, sales, now)
            if added or not self.sold_file_ok():
                await off(self.publish_sold)
                published = True
            latest = max(self._report_date(u) for u in links)
            info = {"at": iso(now), "ok_at": iso(now), "error": None, "added": added, "latest_report": latest,
                    "left": max(0, len([u for u in links if u not in done]) - len(todo)), "first_year": FIRST_YEAR,
                    "parser": PARSER}
        except (httpx.HTTPError, MarketError) as exc:
            log.warning("Ventes DNJournal : %s", exc)
            info = {**info, "at": iso(now), "error": str(exc) if isinstance(exc, MarketError) else f"site injoignable ({type(exc).__name__})"}
        finally:
            if own:
                await client.aclose()
            self.running = False
        if added and not published:
            await off(self.publish_sold)  # passage arrêté en route : les ventes déjà lues comptent quand même
        self.store.set("market_status", json.dumps(info))
        return self.status()

    @staticmethod
    def _report_date(url: str) -> str:
        m = re.search(r"/(\d{4})/(?:\d{4})?(\d{2})(\d{2})\.htm$", url)
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else "1970-01-01"

    def save(self, url: str, sales: list[dict], now: datetime) -> int:
        before = self.conn.execute("SELECT COUNT(*) FROM market_sales").fetchone()[0]
        self.conn.executemany(
            "INSERT OR IGNORE INTO market_sales (name, price, date, venue, source) VALUES (?, ?, ?, ?, ?)",
            [(s["name"], s["price"], s["date"], s["venue"], s["source"]) for s in sales])
        self.conn.execute("INSERT OR REPLACE INTO market_reports (url, fetched_at, sales) VALUES (?, ?, ?)",
                          (url, iso(now), len(sales)))
        with self._lock:
            self._index = None
            self._by_label = None
        return self.conn.execute("SELECT COUNT(*) FROM market_sales").fetchone()[0] - before

    # --- Radar des vendus -------------------------------------------------------------------

    def label_sales(self) -> dict[str, dict]:
        """Meilleure vente connue de chaque libellé, toutes extensions : {name, price, date, venue}. La plus chère, la
        plus récente à prix égal."""
        with self._lock:
            if self._by_label is None:
                found: dict[str, dict] = {}
                for r in self.conn.execute("SELECT name, price, date, venue FROM market_sales ORDER BY price, date"):
                    label = r["name"].partition(".")[0]
                    if wordlists.valid_label(label):
                        found[label] = {"name": r["name"], "price": r["price"], "date": r["date"], "venue": r["venue"]}
                self._by_label = found
            return self._by_label

    def label_sale(self, label: str) -> dict | None:
        return self.label_sales().get(label)

    def name_sale(self, name: str) -> dict | None:
        """Meilleure vente de ce nom exact (même extension)."""
        row = self.conn.execute("SELECT name, price, date, venue FROM market_sales WHERE name = ? "
                                "ORDER BY price DESC, date DESC LIMIT 1", (name,)).fetchone()
        return dict(row) if row else None

    def publish_sold(self) -> int:
        """Liste « Déjà vendus » (wordlists.SOLD) refaite depuis les ventes en base, du plus cher au moins cher : les
        zones, l'Explorer et la Veille suivent ces noms comme ceux des autres listes."""
        sales = self.label_sales()
        labels = sorted(sales, key=lambda label: (-sales[label]["price"], label))
        wordlists.set_sold(labels)
        with self._lock:
            self.version += 1
        return len(labels)

    @staticmethod
    def sold_file_ok() -> bool:
        return wordlists.sold_ready()

    # --- Comparables ------------------------------------------------------------------------

    def _build(self):
        rows = self.conn.execute("SELECT name, price, date, venue FROM market_sales").fetchall()
        by_word: dict[str, list[int]] = {}
        by_shape: dict[tuple, dict[int, list[int]]] = {}
        sales = []
        for r in rows:
            label, _, tld = r["name"].partition(".")
            words = split_words(label)
            sale = {"name": r["name"], "price": r["price"], "date": r["date"], "venue": r["venue"], "label": label,
                    "tld": tld, "words": words, "stems": keywords(words), "lang": language(words),
                    "day": date.fromisoformat(r["date"]).toordinal()}
            sales.append(sale)
            for w in sale["stems"]:
                by_word.setdefault(w, []).append(len(sales) - 1)
            by_shape.setdefault((tld, min(len(words), 3)), {}).setdefault(len(label), []).append(len(sales) - 1)
        # Même forme, rangées par longueur puis de la plus récente à la plus ancienne : les comparables s'arrêtent dès
        # qu'aucune vente restante ne peut entrer dans les k meilleures (des milliers de ventes en .com).
        for lengths in by_shape.values():
            for group in lengths.values():
                group.sort(key=lambda i: -sales[i]["day"])
        return sales, by_word, by_shape

    def index(self):
        with self._lock:
            if self._index is None:
                self._index = self._build()
            return self._index

    def comparables(self, name: str, k: int = 5, today: date | None = None) -> list[dict]:
        """Les k ventes les plus proches : un mot en commun d'abord, puis même extension, même nombre de mots,
        longueur voisine ; les ventes récentes passent devant les anciennes."""
        sales, by_word, by_shape = self.index()
        if not sales or k <= 0:
            return []
        label, _, tld = name.lower().partition(".")
        words = split_words(label)
        today = (today or date.today()).toordinal()
        shared_words = keywords(words)
        lang = language(words)
        shape = min(len(words), 3)

        def score(s, shared: int) -> float:
            value = 40 * shared + (25 if s["tld"] == tld else 10 if s["tld"] in GENERIC and tld in GENERIC else 0)
            value += 10 if min(len(s["words"]), 3) == shape else 0
            value += 15 if lang and s["lang"] == lang else 0
            value -= 3 * abs(len(s["label"]) - len(label))
            return value - 2 * max(0.0, (today - s["day"]) / 365)

        with_word = {i for w in shared_words for i in by_word.get(w, ())}
        scored = [(score(sales[i], len(shared_words & sales[i]["stems"])), -sales[i]["price"], i) for i in with_word]
        best = heapq.nlargest(k, (s for s, _, _ in scored))  # k meilleures notes jusqu'ici
        heapq.heapify(best)
        # Même forme sans mot commun : 50 points au plus (extension 25, forme 10, langue 15), moins 3 par lettre
        # d'écart et 2 par an. Une vente ne passe que si ce plafond atteint la k-ième note ; à égalité elle reste en
        # lice (départage par le prix), d'où la marge.
        lengths = by_shape.get((tld, shape), {})

        def floor() -> float:
            return best[0] - 1e-9 if len(best) >= k else float("-inf")

        gap = 0
        while lengths and 50 - 3 * gap >= floor() and gap <= max(lengths) + len(label):
            for size in {len(label) - gap, len(label) + gap}:
                for i in lengths.get(size, ()):
                    s = sales[i]
                    if 50 - 3 * gap - 2 * max(0.0, (today - s["day"]) / 365) < floor():
                        break  # les suivantes sont plus anciennes
                    if i in with_word:
                        continue
                    found = score(s, 0)
                    scored.append((found, -s["price"], i))
                    if len(best) < k:
                        heapq.heappush(best, found)
                    elif found > best[0]:
                        heapq.heapreplace(best, found)
            gap += 1
        scored.sort(reverse=True)
        return [{k_: sales[i][k_] for k_ in ("name", "price", "date", "venue")} for _, _, i in scored[:k]]


KEYWORD_RANK = 15000
GENERIC = {"com", "net", "org", "io", "ai", "co", "app", "dev", "info", "biz", "xyz"}
STOP = {"the", "and", "les", "des", "une", "pour", "mon", "ton", "son", "for", "you", "your", "my", "get"}


_vocab_lock = threading.Lock()
_vocab: set[str] | None = None
_french: set[str] | None = None
SHORT_WORDS = {"de", "du", "la", "le", "my", "go", "en", "un", "eu", "to", "on", "in", "up", "me", "we"}


def _vocabulary() -> set[str]:
    """Mots, formes fléchies et mots anglais des listes de référence, pour découper les noms vendus."""
    global _vocab, _french
    with _vocab_lock:
        if _vocab is None:
            from pathlib import Path
            data = Path(__file__).parent / "data"
            words = {name: [w for w in (data / name).read_text().split() if w.isalpha()] if (data / name).exists() else []
                     for name in ("mots.txt", "formes.txt", "anglais.txt")}
            _french = set(words["mots.txt"]) | set(words["formes.txt"])
            _vocab = _french | set(words["anglais.txt"])
        return _vocab


def language(words: list[str]) -> str | None:
    """« fr », « en » ou None (nom inventé, ou autant de l'un que de l'autre)."""
    _vocabulary()
    fr = sum(1 for w in words if len(w) >= 3 and (w in _french or stem(w) in _french))
    en = sum(1 for w in words if len(w) >= 3 and w in _vocab and not (w in _french or stem(w) in _french))
    return "fr" if fr > en else "en" if en > fr else None


def keywords(words: list[str]) -> set[str]:
    """Mots qui rapprochent deux noms : au moins 4 lettres et assez courants (pas « del » ni « cor »)."""
    from . import ai_format

    ranks = ai_format._word_ranks()
    found = set()
    for w in words:
        base = stem(w)
        rank = ranks.get(base) or ranks.get(w)
        if w not in STOP and rank and (len(base) >= 4 and rank <= KEYWORD_RANK or len(base) == 3 and rank <= 5000):
            found.add(base)
    return found


def _known(piece: str, known: set[str]) -> bool:
    if len(piece) < 3:
        return piece in SHORT_WORDS
    if piece in known:
        return True
    # Pluriel ou féminin d'un mot connu : prives, cheres, neuves (forme absente des listes).
    return len(piece) >= 4 and ((piece[-1] in "sx" and (piece[:-1] in known or (piece[-2] == "e" and piece[:-2] in known)))
                                or (piece[-1] == "e" and piece[:-1] in known))


def split_words(label: str) -> list[str]:
    """Découpage d'un nom collé en mots connus (français et anglais), le moins de morceaux possible ; un nom
    inventé reste d'un seul tenant."""
    known = _vocabulary()
    label = label.replace("-", "")
    n = len(label)
    best: list[tuple[int, int] | None] = [None] * (n + 1)  # (morceaux, inconnus) au mieux jusqu'à i
    back = [0] * (n + 1)
    best[0] = (0, 0)
    for i in range(1, n + 1):
        for j in range(max(0, i - 20), i):
            if best[j] is None:
                continue
            piece = label[j:i]
            unknown = 0 if _known(piece, known) else len(piece)
            cand = (best[j][0] + 1, best[j][1] + unknown)
            cand_key = (cand[1], cand[0])
            if best[i] is None or cand_key < (best[i][1], best[i][0]):
                best[i], back[i] = cand, j
    if best[n] is None or best[n][1] > n // 2:
        return [label]
    pieces, i = [], n
    while i > 0:
        pieces.append(label[back[i]:i])
        i = back[i]
    return pieces[::-1]


def stem(word: str) -> str:
    """Singulier approché, pour rapprocher jet et jets, moto et motos."""
    return word[:-1] if len(word) > 3 and word[-1] in "sx" else word


def median_price(comparables: list[dict]) -> int | None:
    prices = sorted(c["price"] for c in comparables)
    if not prices:
        return None
    mid = len(prices) // 2
    return prices[mid] if len(prices) % 2 else round(math.sqrt(prices[mid - 1] * prices[mid]))
