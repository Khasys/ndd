from datetime import datetime, timezone

import pytest

from app.whois import WhoisError, parse_date, parse_whois, referral


def test_parse_centralnic_co(fixture_text):
    info = parse_whois(fixture_text("whois_co_found.txt"))
    assert info.expires_at == datetime(2027, 2, 24, 23, 59, 59, tzinfo=timezone.utc)
    assert info.registered_at == datetime(2010, 2, 25, 1, 4, 59, tzinfo=timezone.utc)
    assert info.registrar == "MarkMonitor, Inc."
    assert info.nameservers == ["ns1.google.com", "ns2.google.com", "ns3.google.com", "ns4.google.com"]
    assert info.statuses == ["clientTransferProhibited", "clientUpdateProhibited", "clientDeleteProhibited"]


def test_parse_sonic_so(fixture_text):
    info = parse_whois(fixture_text("whois_so_found.txt"))
    assert info.registrar == "1API"
    assert info.expires_at == datetime(2027, 3, 31, tzinfo=timezone.utc)
    assert info.statuses == ["active", "clientTransferProhibited"]


@pytest.mark.parametrize("name", ["whois_co_free.txt", "whois_so_free.txt"])
def test_explicit_not_found_means_free(fixture_text, name):
    assert parse_whois(fixture_text(name)) is None


def test_denic_style_free():
    assert parse_whois("Domain: exemple-libre.de\nStatus: free\n") is None


def test_status_alone_means_registered():
    info = parse_whois("Domain: exemple.de\nNserver: ns1.exemple.de\nStatus: connect\n")
    assert info.statuses == ["connect"]


def test_found_domain_with_not_found_in_disclaimer_stays_registered(fixture_text):
    text = fixture_text("whois_co_found.txt") + "\nIf the domain is not found, contact your registrar.\n"
    assert parse_whois(text) is not None


@pytest.mark.parametrize("text", ["", "Bienvenue.\n", "% Terms of use only\n"])
def test_unreadable_answer_raises(text):
    with pytest.raises(WhoisError, match="non reconnue"):
        parse_whois(text)


def test_rate_limit_raises():
    with pytest.raises(WhoisError, match="Limite"):
        parse_whois("Query rate limit exceeded. Try again later.\n")


def test_iana_referral(fixture_text):
    assert referral(fixture_text("whois_iana_co.txt")) == "whois.registry.co"
    assert referral("domain: ZZ\nstatus: ACTIVE\n") is None


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2027-02-24T23:59:59.0Z", datetime(2027, 2, 24, 23, 59, 59, tzinfo=timezone.utc)),
        ("2027-03-31", datetime(2027, 3, 31, tzinfo=timezone.utc)),
        ("2027.03.31 10:00:00", datetime(2027, 3, 31, 10, tzinfo=timezone.utc)),
        ("24-Feb-2027", datetime(2027, 2, 24, tzinfo=timezone.utc)),
        ("31.03.2027", datetime(2027, 3, 31, tzinfo=timezone.utc)),
        ("03/31/2027", None),
        ("bientôt", None),
    ],
)
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


def test_terms_of_use_mentioning_limits_do_not_hide_data(fixture_text):
    text = fixture_text("whois_co_found.txt") + (
        "\nTERMS OF USE: excessive querying, too many requests or high volume access is prohibited.\n"
    )
    assert parse_whois(text).registrar == "MarkMonitor, Inc."


def test_eurid_style_blocks():
    text = """Domain: google.eu
Script: LATIN

Registrar:
        Name: MarkMonitor Inc.
        Website: https://www.markmonitor.com

Name servers:
        ns1.google.com
        ns2.google.com
"""
    info = parse_whois(text)
    assert info.registrar == "MarkMonitor Inc."
    assert info.nameservers == ["ns1.google.com", "ns2.google.com"]


def test_jprs_brackets():
    text = """[ JPRS database provides information on network administration. ]
Domain Information:
[Domain Name]                   GOOGLE.JP
[Name Server]                   ns1.google.com
[Name Server]                   ns2.google.com
[Created on]                    2005/05/30
[State]                         Connected (2027/05/31)
"""
    info = parse_whois(text)
    assert info.nameservers == ["ns1.google.com", "ns2.google.com"]
    assert info.registered_at == datetime(2005, 5, 30, tzinfo=timezone.utc)


def test_status_phrase_kept_whole():
    info = parse_whois("Domain: google.be\nStatus: NOT AVAILABLE\nRegistered: Tue Dec 12 1999\n")
    assert info.statuses == ["NOT AVAILABLE"]
