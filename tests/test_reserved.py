import asyncio

import httpx
import pytest

from app import db, drops, explore, frdata, reserved, states
from app.store import Store


def make_pdf(lines: list[str]) -> bytes:
    """Un PDF minimal, une ligne de texte par terme, comme la liste de l'Afnic."""
    text = "BT /F1 12 Tf 50 780 Td 14 TL " + " ".join(f"({l}) Tj T*" for l in lines) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(text)} >>\nstream\n{text}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


TERMS = ["Termes soumis \xe0 examen pr\xe9alable", "Huissier", "Etat", "Pharmacie, Notaire"] + [f"terme{i}" for i in range(60)]
COMMUNES = [{"nom": "Le Mans"}, {"nom": "Saint-\xc9tienne"}, {"nom": "L'Isle-Adam"}] + [{"nom": f"Commune {i}"} for i in range(10_000)]


@pytest.fixture
def env(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn, tmp_path / "backups")
    store = Store(conn, {})

    def handler(request):
        if request.url.host == "geo.api.gouv.fr":
            return httpx.Response(200, json=COMMUNES)
        return httpx.Response(200, content=make_pdf(TERMS))

    lists = reserved.Reserved(conn, store, lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    watcher = drops.DropWatcher(conn, store, tmp_path, rdap=None, delays=states.load_delays())
    fr = frdata.FrWatcher(conn, store, tmp_path, rdap=None, drops_watcher=watcher)
    yield lists, explore.Explorer(conn, watcher, fr, lists)
    conn.close()


def test_canonical_forms():
    assert reserved.canonical("Saint-Étienne") == "saintetienne"
    assert reserved.commune_forms("Le Mans") == {"lemans", "mans"}
    assert reserved.commune_forms("L'Isle-Adam") == {"lisleadam", "isleadam"}


def test_pdf_lines_become_terms():
    terms = reserved.pdf_terms(make_pdf(TERMS))
    assert {"huissier", "etat", "pharmacie", "notaire"} <= terms


def test_refresh_then_explorer_keeps_reserved_apart(env):
    lists, ex = env
    status = asyncio.run(lists.refresh())
    assert status["error"] is None and status["counts"]["afnic"] >= 60 and status["counts"]["commune"] > 10_000
    assert "huissier.fr" in lists.names() and "mans.fr" in lists.names()
    ex.conn.execute("INSERT INTO fr_names (label, created_on) VALUES ('zz', '2010-01-01')")

    item = ex.search("fr", q="huissier", mode="exact", status="")["items"][0]
    assert item["status"] == explore.RESERVED and item["reserved"] == ["Examen préalable Afnic"]
    # Par défaut, retirés des noms à saisir, de la carte et du classement.
    assert not ex.search("fr", q="huissier", mode="exact", status="available")["items"]
    assert "huissier.fr" not in [i["name"] for i in ex.rank("fr", category="word", limit=500)["items"]]
    assert ex.facets("fr", category="word")["reserved"] >= 1

    lists.store.set("reserved_mode", "show")
    assert ex.search("fr", q="huissier", mode="exact", status="available")["items"]


def test_manual_flags_override_the_lists(env):
    lists, ex = env
    asyncio.run(lists.refresh())
    lists.flag("huissier.fr", False)
    assert "huissier.fr" not in lists.names() and lists.why("huissier.fr") == []
    lists.flag("maison.fr", True)
    assert ex.search("fr", q="maison", mode="exact", status="")["items"][0]["reserved"] == ["Marqué à la main"]
    lists.flag("huissier.fr", None)
    assert "huissier.fr" in lists.names()


def test_failed_download_keeps_previous_lists(env):
    lists, ex = env
    asyncio.run(lists.refresh())
    lists._http_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    status = asyncio.run(lists.refresh())
    assert "Liste Afnic" in status["error"] and "Communes" in status["error"]
    assert "huissier.fr" in lists.names()


def test_frequency_order(env):
    lists, ex = env
    found = [i["label"] for i in ex.rank("fr", category="firstname", sort="freq", limit=5)["items"]]
    assert found[:3] == ["jean", "marie", "jeanne"]
    by_length = [i["length"] for i in ex.rank("fr", sort="length", limit=40)["items"]]
    assert by_length == sorted(by_length)


def test_excluded_two_letter_names_never_show_as_free(env):
    lists, ex = env
    ex.conn.execute("INSERT INTO fr_names (label, created_on) VALUES ('zz', '2010-01-01')")
    # Le RDAP répond « inconnu » : vu libre. La liste d'exclusion l'emporte.
    ex.conn.execute("INSERT INTO drops (name, tld, label, kind, lists, state, removed_on) VALUES ('pm.fr', 'fr', 'pm', 'letters', '', ?, '2026-10-01')",
                    (states.AVAILABLE,))
    item = ex.search("fr", q="pm", mode="exact", status="")["items"][0]
    assert item["status"] == explore.RESERVED and item["reserved"] == ["Exclu par l'Afnic"]
    assert not ex.search("fr", q="pm", mode="exact", status="available")["items"]
    lists.flag("pm.fr", False)
    assert "pm.fr" not in lists.names()


def test_one_or_two_characters_in_generic_zones_are_held_by_the_registry(env):
    lists, ex = env
    lists.store.set("czds_approved", '["org"]')
    bits = bytearray(drops.BITMAP_BYTES)
    for label in ("ab", "abc"):
        i = drops.encode(label)
        bits[i // 8] |= 1 << (i % 8)
    ex.drops._save("org", bits, set())
    status = ex.status_bytes("org")
    u = ex.universe
    assert status[u.index("sk")] == explore.RESERVED and status[u.index("x")] == explore.RESERVED
    assert status[u.index("ab")] == explore.REGISTERED and status[u.index("abd")] == explore.ABSENT
    item = ex.search("org", q="sk", mode="exact", status="")["items"][0]
    assert item["reserved"] == ["Réservé par le registre (1 ou 2 caractères)"]
    # Cas réellement ouvert : la correction à la main l'emporte.
    lists.flag("sk.org", False)
    ex.invalidate("org")
    assert ex.status_bytes("org")[u.index("sk")] == explore.ABSENT
