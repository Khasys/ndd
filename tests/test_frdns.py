import asyncio
from datetime import datetime, timezone

import dns.resolver

from app import ccdns, db, frdns
from app.store import Store


class FakeExplorer:
    def tlds(self):
        return ["fr"]


def make(tmp_path, cls=frdns.FrDns, *args):
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn, tmp_path / "backups")
    checker = cls(*args, conn, Store(conn, {}), FakeExplorer(), timezone.utc)
    checker.pending = lambda limit, now=None: ["velo", "auto", "maison"]

    async def no_servers():
        raise dns.resolver.NoNameservers()  # résolveur du NAS muet à minuit

    checker._servers = no_servers
    return checker


def test_resolver_outage_leaves_the_day_open_for_the_next_tick(tmp_path):
    checker = make(tmp_path)
    now = datetime(2026, 10, 17, 0, 5, tzinfo=timezone.utc)
    assert checker.due(now)
    stats = asyncio.run(checker.run())
    assert stats["checked"] == 0 and stats["error"]
    # Rien vérifié : le jour n'est pas marqué (ni le plafond touché), la passe est retentée au tick suivant.
    assert checker.due(now) and checker.store.get("fr_dns_cap") == ""


def test_cc_dns_resolver_outage_is_retried_too(tmp_path):
    checker = make(tmp_path, ccdns.CcDns, "io")
    asyncio.run(checker.run())
    assert checker.store.get("io_dns_last") == ""


def test_query_without_servers_is_a_network_failure_not_a_crash(tmp_path):
    checker = make(tmp_path)
    failure = asyncio.run(checker._query("velo"))
    assert isinstance(failure, frdns.Failure) and failure.reason == frdns.NETWORK
    assert "NoNameservers" in failure.detail
