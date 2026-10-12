from datetime import datetime, timezone

import pytest

from app.rdap import InvalidDomain, clean_domain, parse_domain


def test_parse_verisign_active(fixture):
    info = parse_domain(fixture("com_active.json"))
    assert info.statuses == ["client delete prohibited", "client transfer prohibited", "client update prohibited"]
    assert info.registered_at == datetime(1997, 9, 15, 4, tzinfo=timezone.utc)
    assert info.expires_at == datetime(2027, 9, 14, 4, tzinfo=timezone.utc)
    assert info.updated_at == datetime(2025, 8, 20, 10, 11, 12, tzinfo=timezone.utc)
    assert info.registrar == "Exemple Registrar SAS"
    assert info.nameservers == ["ns1.exemple-dns.net", "ns2.exemple-dns.net"]


def test_parse_afnic_fractional_seconds_and_trailing_dots(fixture):
    info = parse_domain(fixture("fr_active.json"))
    assert info.expires_at == datetime(2026, 10, 30, 10, tzinfo=timezone.utc)
    assert info.registrar == "REGISTRAR EXEMPLE"
    assert info.nameservers == ["ns1.exemple-dns.fr", "ns2.exemple-dns.fr"]


def test_registrar_falls_back_to_iana_id():
    data = {"entities": [{"roles": ["registrar"], "publicIds": [{"identifier": "146"}]}]}
    assert parse_domain(data).registrar == "IANA 146"


def test_parse_empty_response():
    info = parse_domain({})
    assert info.statuses == [] and info.expires_at is None and info.registrar is None


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("exemple.fr", "exemple.fr"),
        ("  https://www.Exemple.FR/page?x=1 ", "exemple.fr"),
        ("http://exemple.com:8080/", "exemple.com"),
        ("www.exemple.co.uk/", "exemple.co.uk"),
        ("exemple.fr.", "exemple.fr"),
        ("café.fr", "xn--caf-dma.fr"),
        ("www.com", "www.com"),
    ],
)
def test_clean_domain(raw, expected):
    assert clean_domain(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "exemple", "-exemple.fr", "exe mple.fr", "https://", "192.168.1.1"])
def test_clean_domain_rejects(raw):
    with pytest.raises(InvalidDomain):
        clean_domain(raw)
