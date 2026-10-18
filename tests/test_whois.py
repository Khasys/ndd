from datetime import datetime, timezone

import pytest

from app.whois import WhoisError, parse_date, parse_whois, rate_limited, referral


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


def test_identity_digital_free_despite_throttling_terms():
    text = (
        "Domain not found.\n>>> Last update of WHOIS database: 2026-10-12T13:54:38Z <<<\n\n"
        "Terms of Use: Access to WHOIS information is provided to assist persons in determining the contents "
        "of a domain name registration record. Queries to the Whois services are throttled. If too many queries "
        "are received from a single IP address within a specified time, the service will begin to reject further queries.\n"
    )
    assert parse_whois(text) is None


def test_aligned_status_available():
    assert parse_whois("Domain:             libre.it\nStatus:             AVAILABLE\n") is None


def test_short_rate_limit_line_raises():
    with pytest.raises(WhoisError, match="Limite"):
        parse_whois("%% Too many requests, try again later\n")


def test_rate_limit_is_detected_in_a_bare_answer():
    # Réponse de l'IANA sans champ « whois » : une limite, pas une extension sans serveur.
    assert rate_limited("% Too many queries, try again later\n")
    assert not rate_limited("whois:        whois.nic.so\n")


# Réponses écrites à la main d'après les formats publiés (Identity Digital pour .io, registre du .ai) :
# rien n'est vérifié d'ici, ce conteneur ne joint pas les registres.
IO_FOUND = """Domain Name: github.io
Registry Domain ID: REDACTED
Registrar WHOIS Server: whois.markmonitor.com
Registrar URL: http://www.markmonitor.com
Updated Date: 2024-02-05T09:29:35Z
Creation Date: 2013-03-08T21:36:42Z
Registry Expiry Date: 2027-03-08T21:36:42Z
Registrar: MarkMonitor Inc.
Registrar IANA ID: 292
Domain Status: clientDeleteProhibited https://icann.org/epp#clientDeleteProhibited
Domain Status: clientTransferProhibited https://icann.org/epp#clientTransferProhibited
Name Server: DNS1.P05.NSONE.NET
Name Server: NS-1622.AWSDNS-10.CO.UK
DNSSEC: unsigned
URL of the ICANN Whois Inaccuracy Complaint Form: https://www.icann.org/wicf/
>>> Last update of WHOIS database: 2026-10-18T08:00:00Z <<<

Terms of Use: Access to WHOIS information is provided to assist persons in determining the contents of a domain name registration record in the registry database. Queries to the Whois services are throttled. If too many queries are received from a single IP address within a specified time, the service will begin to reject further queries.
"""

AI_FOUND = """Domain Name: google.ai
Registry Domain ID: 2a2b0e9c1f_DOMAIN_AI-VRSN
Registrar WHOIS Server: whois.markmonitor.com
Updated Date: 2025-08-26T09:12:44.512Z
Creation Date: 2017-12-16T05:37:20.801Z
Registry Expiry Date: 2027-09-25T15:03:13.391Z
Registrar: MarkMonitor Inc.
Domain Status: clientUpdateProhibited https://icann.org/epp#clientUpdateProhibited
Name Server: NS1.GOOGLE.COM
Name Server: NS2.GOOGLE.COM
DNSSEC: unsigned
>>> Last update of WHOIS database: 2026-10-18T08:00:00.000Z <<<
"""


def test_identity_digital_io_registered():
    info = parse_whois(IO_FOUND)
    assert info.expires_at == datetime(2027, 3, 8, 21, 36, 42, tzinfo=timezone.utc)
    assert info.registered_at == datetime(2013, 3, 8, 21, 36, 42, tzinfo=timezone.utc)
    assert info.registrar == "MarkMonitor Inc."
    assert info.nameservers == ["dns1.p05.nsone.net", "ns-1622.awsdns-10.co.uk"]
    assert info.statuses == ["clientDeleteProhibited", "clientTransferProhibited"]


def test_ai_registered_with_milliseconds():
    info = parse_whois(AI_FOUND)
    assert info.expires_at == datetime(2027, 9, 25, 15, 3, 13, tzinfo=timezone.utc)
    assert info.registered_at == datetime(2017, 12, 16, 5, 37, 20, tzinfo=timezone.utc)
    assert info.registrar == "MarkMonitor Inc." and info.nameservers == ["ns1.google.com", "ns2.google.com"]


@pytest.mark.parametrize("text", [
    "NOT FOUND\n",
    "Domain not found.\n>>> Last update of WHOIS database: 2026-10-18T08:00:00Z <<<\n",
    "No Object Found\n",
    "\n>>> Last update of WHOIS database: 2026-10-18T08:00:00.000Z <<<\nNOT FOUND\n",
])
def test_io_and_ai_free_answers(text):
    assert parse_whois(text) is None
