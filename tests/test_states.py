from datetime import datetime, timedelta, timezone

import pytest

from app import states
from app.config import parse_duration
from app.rdap import parse_domain

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
DELAYS = {
    "com": {"auto_renew_grace": 45, "redemption": 30, "pending_delete": 5},
    "fr": {"redemption": 30},
}
INTERVALS = {
    "active": 86400,
    "expiring": 21600,
    "expired": 21600,
    "redemption": 3600,
    "pending_delete": 300,
    "available": 86400,
    "unknown": 3600,
}


def state_of(fixture, name):
    info = parse_domain(fixture(name))
    return states.compute_state(info.statuses, info.expires_at, NOW, 30)


def test_states_from_fixtures(fixture):
    assert state_of(fixture, "com_active.json") == states.ACTIVE
    assert state_of(fixture, "com_redemption.json") == states.REDEMPTION
    assert state_of(fixture, "com_pending_delete.json") == states.PENDING_DELETE
    assert state_of(fixture, "com_auto_renew.json") == states.EXPIRED
    assert state_of(fixture, "fr_active.json") == states.EXPIRING


@pytest.mark.parametrize(
    "statuses, expires_in_days, expected",
    [
        ([], 31, states.ACTIVE),
        ([], 30, states.EXPIRING),
        ([], 1, states.EXPIRING),
        ([], -1, states.EXPIRED),
        (["autoRenewPeriod"], 300, states.EXPIRED),
        (["redemptionPeriod", "pendingDelete"], 300, states.REDEMPTION),
        (["pending restore"], 300, states.REDEMPTION),
        (["pendingDelete"], 300, states.PENDING_DELETE),
        (["client hold"], 400, states.ACTIVE),
        ([], None, states.ACTIVE),
    ],
)
def test_compute_state(statuses, expires_in_days, expected):
    expires = NOW + timedelta(days=expires_in_days) if expires_in_days is not None else None
    assert states.compute_state(statuses, expires, NOW, 30) == expected


def test_release_from_redemption():
    since = datetime(2026, 10, 1, tzinfo=timezone.utc)
    release = states.estimate_release(states.REDEMPTION, "com", None, since, NOW, DELAYS)
    assert release == since + timedelta(days=35)


def test_release_from_pending_delete():
    since = datetime(2026, 10, 10, tzinfo=timezone.utc)
    assert states.estimate_release(states.PENDING_DELETE, "com", None, since, NOW, DELAYS) == since + timedelta(days=5)


def test_release_upper_bound_when_expiring():
    expires = NOW + timedelta(days=10)
    release = states.estimate_release(states.EXPIRING, "com", expires, NOW, NOW, DELAYS)
    assert release == expires + timedelta(days=80)


def test_release_during_auto_renew_uses_real_expiry():
    # Date déjà prolongée d'un an par le registre : l'échéance réelle était il y a 5 jours.
    bumped = NOW - timedelta(days=5) + timedelta(days=365)
    release = states.estimate_release(states.EXPIRED, "com", bumped, NOW, NOW, DELAYS)
    assert release == NOW - timedelta(days=5) + timedelta(days=80)


def test_no_release_for_active_or_available():
    expires = NOW + timedelta(days=200)
    assert states.estimate_release(states.ACTIVE, "com", expires, NOW, NOW, DELAYS) is None
    assert states.estimate_release(states.AVAILABLE, "com", None, NOW, NOW, DELAYS) is None


def test_no_release_when_tld_delays_incomplete_or_missing():
    assert states.estimate_release(states.REDEMPTION, "fr", None, NOW, NOW, DELAYS) is None
    assert states.estimate_release(states.REDEMPTION, "io", None, NOW, NOW, DELAYS) is None


def test_shipped_delays_file_is_consistent():
    delays = states.load_delays()
    for tld, values in delays.items():
        assert set(values) <= set(states.DELAY_KEYS), tld
        assert all(isinstance(v, int) and v >= 0 for v in values.values()), tld


@pytest.mark.parametrize(
    "state, seconds",
    [
        (states.ACTIVE, 86400),
        (states.EXPIRING, 21600),
        (states.EXPIRED, 21600),
        (states.REDEMPTION, 3600),
        (states.PENDING_DELETE, 300),
        (None, 3600),
    ],
)
def test_check_interval(state, seconds):
    assert states.check_interval(state, INTERVALS) == seconds


@pytest.mark.parametrize("raw, seconds", [("90", 90), ("30s", 30), ("5m", 300), ("6h", 21600), ("7d", 604800)])
def test_parse_duration(raw, seconds):
    assert parse_duration(raw) == seconds


def test_parse_duration_rejects_garbage():
    with pytest.raises(ValueError):
        parse_duration("6 heures")
