"""Connecteur MCP en lecture seule : ChatGPT, Claude ou un autre assistant interroge domain-watch.

Protocole MCP (JSON-RPC 2.0 sur HTTP, réponse JSON simple, sans flux). Chaque outil lit un écran déjà ouvert au
jeton en lecture seule (READ_PATHS de main.py) : fiche, Explorer, Aujourd'hui, Veille, listes, avis IA, noms
surveillés. Rien n'écrit, aucun réglage ni secret ne sort. Les réponses sont allégées (champs techniques et vides
retirés) pour tenir dans le contexte de l'assistant.

Accès : adresse secrète /mcp/<jeton> (ChatGPT n'envoie pas d'en-tête), ou /mcp avec « Authorization: Bearer ».
Jetons de type « mcp » de la table tokens (Paramètres, Jetons d'accès).
"""

import json
from urllib.parse import urlencode

from . import explore, wordlists

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_TEXT = 60_000  # caractères par réponse d'outil : au-delà, la liste est raccourcie
# Champs techniques ou redondants : utiles à l'écran, du bruit pour un assistant.
NOISE = frozenset({
    "rdap", "rdap_next", "grid", "parts", "next_check_at", "checked_at", "state_since", "cc_dns", "reserved",
    "status", "tld", "length", "known", "mine", "domain_id", "ai_eta", "ai_request", "setup", "sort",
    "kinds", "natures", "seo_filters", "ai_verdicts", "langs", "ai_fields", "statuses", "absent_label", "error",
    "stale", "pending", "currency", "price_ht", "kind", "steps", "cursor", "id", "unicode_name", "epp_statuses",
    "nameservers", "last_checked_at", "last_success_at", "source", "updated_at", "reviewed_at",
})


def compact(value, keep_lists: int = 100):
    """Sans champs techniques, valeurs vides ni faux : ce qu'un assistant a besoin de lire."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in NOISE:
                continue
            item = compact(item, keep_lists)
            if item is None or item is False or item in ("", [], {}):  # 0 reste : une note nulle compte
                continue
            out[key] = item
        return out
    if isinstance(value, list):
        return [compact(v, keep_lists) for v in value[:keep_lists]]
    return value


def _limit(args: dict, default: int, top: int) -> int:
    try:
        n = int(args.get("limit", default))
    except (TypeError, ValueError):
        n = default
    return max(1, min(top, n))


def _tld(args: dict) -> dict:
    tld = str(args.get("tld") or "").strip()
    return {"tld": tld} if tld else {}


TLD_HELP = "Extension(s) sans point, séparées par des virgules : fr, com, fr,com… Par défaut la première connue."
PERIOD = {"type": "string", "enum": ["24h", "7d", "30d"], "description": "Période : 24h, 7d ou 30d (24h par défaut)."}
READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}


def tools() -> list[dict]:
    lists = ", ".join(f"{k} ({v})" for k, v in wordlists.SHOWN.items())
    return [
        {
            "name": "search",
            "title": "Chercher des noms",
            "description": "Cherche dans l'Explorer les noms de domaine qui contiennent un mot, les meilleures affaires "
                           "d'abord. Chaque résultat : nom, état (libre, bientôt libre, pris…), score, avis IA "
                           "(revente, exploitation, prix, verdict), prix Gandi.",
            "inputSchema": {"type": "object", "properties": {
                "query": {"type": "string", "description": "Mot ou morceau de nom, sans extension (ex. medecin)."},
                "tld": {"type": "string", "description": TLD_HELP},
                "status": {"type": "string", "enum": list(explore.STATUS_FILTERS),
                           "description": "available = tous non pris (défaut), free = libres vérifiés, "
                                          "coming = bientôt libres, registered = pris, all = tout."},
                "sort": {"type": "string", "enum": ["score", "resale", "exploit", "ai", "length", "release", "recent"],
                         "description": "Tri : score (défaut), resale, exploit, ai, length, release, recent."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "20 par défaut."},
            }, "required": ["query"]},
            "annotations": READ_ONLY,
        },
        {
            "name": "list_names",
            "title": "Lister des noms par critères",
            "description": "Liste les noms de l'Explorer sans mot à chercher : par longueur, catégorie et état (ex. tous "
                           "les .fr de 3 lettres libres), les meilleurs d'abord, avec les compteurs de la sélection "
                           "(counts : nombre de noms par état, avant la limite). Par défaut, les suites imprononçables, "
                           "les mélanges et les tirets sont écartés : weak=true les garde.",
            "inputSchema": {"type": "object", "properties": {
                "tld": {"type": "string", "description": TLD_HELP},
                "length": {"type": "integer", "minimum": 1, "maximum": 63, "description": "Longueur exacte, sans l'extension."},
                "category": {"type": "string", "enum": list(explore.KINDS),
                             "description": "letters, digits, mixed, word, english… (une seule)."},
                "status": {"type": "string", "enum": list(explore.STATUS_FILTERS),
                           "description": "available = tous non pris (défaut), free = libres vérifiés, "
                                          "maybe_free = probablement libres, coming = bientôt libres, all = tout."},
                "weak": {"type": "boolean", "description": "true : garder aussi les noms faibles (imprononçables…)."},
                "query": {"type": "string", "description": "Facultatif : morceau de nom que les résultats contiennent."},
                "sort": {"type": "string", "enum": ["score", "resale", "exploit", "ai", "length", "release", "recent"],
                         "description": "Tri : score (défaut)."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "50 par défaut."},
            }},
            "annotations": READ_ONLY,
        },
        {
            "name": "fetch",
            "title": "Fiche d'un nom",
            "description": "Fiche complète d'un nom de domaine (ex. medecindusport.com) : état et dates, autres "
                           "extensions, score et raisons, avis IA détaillé, prix Gandi, ventes réelles comparables, "
                           "noms comparables notés.",
            "inputSchema": {"type": "object", "properties": {
                "id": {"type": "string", "description": "Nom de domaine complet, extension comprise."},
            }, "required": ["id"]},
            "annotations": READ_ONLY,
        },
        {
            "name": "today",
            "title": "Aujourd'hui",
            "description": "Écran Aujourd'hui : les meilleurs noms à prendre maintenant (libérés ou bientôt libres), "
                           "les pépites et les compteurs de la période.",
            "inputSchema": {"type": "object", "properties": {
                "period": PERIOD,
                "sort": {"type": "string", "enum": ["resale", "exploit"], "description": "resale (défaut) ou exploit."},
            }},
            "annotations": READ_ONLY,
        },
        {
            "name": "drops",
            "title": "Veille",
            "description": "Veille : noms libérés, bientôt libres, ou nouvellement « À prendre » sur la période.",
            "inputSchema": {"type": "object", "properties": {
                "focus": {"type": "string", "enum": ["freed", "coming", "new_take"],
                          "description": "freed = libérés, coming = bientôt libres, new_take = nouveaux À prendre."},
                "period": PERIOD,
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "50 par défaut."},
            }, "required": ["focus"]},
            "annotations": READ_ONLY,
        },
        {
            "name": "top_list",
            "title": "Top d'une liste",
            "description": "Les N premiers noms d'une liste de mots, pris ou non, avec leur état par extension. "
                           "Listes : " + lists + ".",
            "inputSchema": {"type": "object", "properties": {
                "list": {"type": "string", "enum": list(wordlists.SHOWN)},
                "tld": {"type": "string", "description": TLD_HELP},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "50 par défaut."},
            }, "required": ["list"]},
            "annotations": READ_ONLY,
        },
        {
            "name": "ai_rated",
            "title": "Avis IA",
            "description": "Noms déjà notés par l'IA, meilleure revente d'abord, filtrables par verdict et extension.",
            "inputSchema": {"type": "object", "properties": {
                "verdict": {"type": "string", "enum": list(explore.AI_VERDICTS),
                            "description": "take = À prendre, watch = À surveiller, skip = À laisser."},
                "tld": {"type": "string", "description": "Une extension sans point (fr, com…)."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "50 par défaut."},
            }},
            "annotations": READ_ONLY,
        },
        {
            "name": "watched",
            "title": "Noms surveillés",
            "description": "Les noms que l'utilisateur surveille (écran Domaines) : état, dates d'expiration et de "
                           "libération, score, avis IA.",
            "inputSchema": {"type": "object", "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 300, "description": "100 par défaut."},
            }},
            "annotations": READ_ONLY,
        },
    ]


def request_for(name: str, args: dict) -> tuple[str, dict]:
    """(chemin d'un écran en lecture seule, paramètres) pour un outil. ValueError : outil ou argument refusé."""
    if name == "search":
        query = str(args.get("query") or "").strip().lower()
        if not query:
            raise ValueError("query est requis.")
        params = {"q": query, "mode": "contains", "limit": _limit(args, 20, 50),
                  "sort": args.get("sort") or "score", **_tld(args)}
        if args.get("status"):
            params["status"] = args["status"]
        return "/api/explore/rank", params
    if name == "list_names":
        params = {"limit": _limit(args, 50, 200), "sort": args.get("sort") or "score", **_tld(args)}
        if args.get("length") is not None:
            try:
                params["len"] = int(args["length"])
            except (TypeError, ValueError):
                raise ValueError("length doit être un nombre.") from None
        for key, param in (("category", "cat"), ("status", "status")):
            if args.get(key):
                params[param] = str(args[key])
        if args.get("weak") is True:
            params["junk"] = "1"
        query = str(args.get("query") or "").strip().lower()
        if query:
            params.update(q=query, mode="contains")
        return "/api/explore/rank", params
    if name == "fetch":
        ident = str(args.get("id") or "").strip().lower()
        if not ident:
            raise ValueError("id est requis (nom de domaine complet).")
        return "/api/explore/fiche", {"name": ident}
    if name == "today":
        return "/api/today", {"period": args.get("period") or "24h", "sort": args.get("sort") or "resale"}
    if name == "drops":
        return "/api/drops", {"focus": args.get("focus") or "freed", "period": args.get("period") or "24h"}
    if name == "top_list":
        return "/api/explore/top", {"cat": args.get("list") or "premium", "n": _limit(args, 50, 100), **_tld(args)}
    if name == "ai_rated":
        params = {"limit": _limit(args, 50, 100), **_tld(args)}
        if args.get("verdict"):
            params["verdict"] = args["verdict"]
        return "/api/ai/rated", params
    if name == "watched":
        return "/api/domains", {}
    raise ValueError(f"Outil inconnu : {name}")


def shape(name: str, args: dict, data, base_url: str):
    """Réponse allégée d'un outil ; search et fetch gardent id, title et url (forme attendue par ChatGPT)."""
    def head(n):
        # Lien vers la fiche dans l'appli, si son adresse publique est connue.
        return {"id": n, "title": n, **({"url": f"{base_url.rstrip('/')}/?{urlencode({'fiche': n})}"} if base_url else {})}

    if name == "search":
        items = data.get("items", []) if isinstance(data, dict) else []
        return {"results": [{**head(i.get("name")), **compact(i)} for i in items]}
    if name == "list_names":
        # Une ligne courte par nom (beaucoup de noms tiennent dans la réponse), et les compteurs de la sélection.
        items = data.get("items", []) if isinstance(data, dict) else []
        rows = [compact({"name": i.get("name"), "state": i.get("status_label"), "score": i.get("score"),
                         "categories": i.get("categories"), "verdict": ((i.get("ai") or {}).get("comment") or "").split(".")[0],
                         "final_low": (i.get("ai") or {}).get("final_low"), "final_high": (i.get("ai") or {}).get("final_high"),
                         "premium_price": i.get("premium_price")}) for i in items]
        return {"counts": (data.get("counts") if isinstance(data, dict) else None) or {}, "results": rows}
    if name == "fetch":
        return {**head(data.get("name")), **compact(data)}
    if name == "drops":
        items = data.get("items", data) if isinstance(data, dict) else data
        # La Veille arrive dans l'ordre du registre : les meilleurs d'abord, coupés à la limite.
        items = sorted(items, key=lambda i: -(i.get("score") or 0))
        return {"items": compact(items, _limit(args, 50, 100))}
    if name == "watched":
        return {"items": compact(data, _limit(args, 100, 300))}
    return compact(data)


def text_of(result) -> str:
    """JSON lisible ; trop long : listes raccourcies de moitié jusqu'à tenir."""
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    keep = 100
    while len(text) > MAX_TEXT and keep > 1:
        keep //= 2
        result = _cut(result, keep)
        text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    return text[:MAX_TEXT]


def _cut(value, keep: int):
    if isinstance(value, dict):
        return {k: _cut(v, keep) for k, v in value.items()}
    if isinstance(value, list):
        return [_cut(v, keep) for v in value[:keep]]
    return value


INSTRUCTIONS = ("domain-watch suit des noms de domaine (.fr, .com…) qui se libèrent, avec un score, un avis IA "
                "(potentiel de revente prioritaire, exploitation, prix, verdict À prendre / À surveiller / À laisser) "
                "et les prix Gandi. Accès en lecture seule : aucun outil ne modifie quoi que ce soit. Les prix et "
                "verdicts sont des estimations, pas des ventes constatées.")


def _ok(ident, result) -> dict:
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def _error(ident, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}


async def handle(message, read, version: str, base_url: str):
    """Un message JSON-RPC (ou un lot) ; None pour une notification (pas de réponse).
    read(chemin, paramètres) -> données de l'écran, ou ValueError avec le message à montrer."""
    if isinstance(message, list):
        replies = [r for r in [await handle(m, read, version, base_url) for m in message] if r is not None]
        return replies or None
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return _error(message.get("id") if isinstance(message, dict) else None, -32600, "Requête invalide.")
    ident, method = message.get("id"), message["method"]
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if "id" not in message:
        return None  # notification (initialized, cancelled…)
    if method == "initialize":
        asked = params.get("protocolVersion")
        return _ok(ident, {
            "protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "domain-watch", "title": "domain-watch", "version": version},
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return _ok(ident, {})
    if method == "tools/list":
        return _ok(ident, {"tools": tools()})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if name not in {t["name"] for t in tools()}:
            return _error(ident, -32602, f"Outil inconnu : {name}")
        try:
            path, query = request_for(name, args)
            data = await read(path, query)
            text, failed = text_of(shape(name, args, data, base_url)), False
        except ValueError as exc:
            text, failed = str(exc), True
        return _ok(ident, {"content": [{"type": "text", "text": text}], "isError": failed})
    return _error(ident, -32601, f"Méthode inconnue : {method}")
