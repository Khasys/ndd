import tomllib
from datetime import datetime, timedelta
from pathlib import Path

ACTIVE = "active"
EXPIRING = "expiring"
EXPIRED = "expired"
REDEMPTION = "redemption"
PENDING_DELETE = "pending_delete"
AVAILABLE = "available"

# Ordre d'urgence, utilisé aussi pour le tri par défaut côté interface.
URGENCY = [AVAILABLE, PENDING_DELETE, REDEMPTION, EXPIRED, EXPIRING, ACTIVE]

LABELS = {
    ACTIVE: "Actif",
    EXPIRING: "Expire bientôt",
    EXPIRED: "Expiré",
    REDEMPTION: "En rédemption",
    PENDING_DELETE: "Suppression imminente",
    AVAILABLE: "Disponible",
}

DELAY_KEYS = ("auto_renew_grace", "redemption", "pending_delete")
DELAYS_FILE = Path(__file__).parent / "tld_delays.toml"


def load_delays(path: Path = DELAYS_FILE) -> dict[str, dict[str, int]]:
    with path.open("rb") as f:
        return tomllib.load(f)


def _normalize(status: str) -> str:
    # RDAP écrit "redemption period", EPP écrit "redemptionPeriod".
    return status.replace(" ", "").replace("_", "").lower()


def compute_state(statuses: list[str], expires_at: datetime | None, now: datetime, expiring_days: int) -> str:
    norm = {_normalize(s) for s in statuses}
    if "redemptionperiod" in norm or "pendingrestore" in norm:
        return REDEMPTION
    if "pendingdelete" in norm:
        return PENDING_DELETE
    if "autorenewperiod" in norm:
        return EXPIRED
    if expires_at is not None:
        if expires_at <= now:
            return EXPIRED
        if expires_at - now <= timedelta(days=expiring_days):
            return EXPIRING
    return ACTIVE


def effective_expiry(state: str | None, expires_at: datetime | None, now: datetime) -> datetime | None:
    # Pendant l'autoRenewPeriod, Verisign affiche déjà la date prolongée d'un an :
    # l'échéance réelle est un an plus tôt.
    if state == EXPIRED and expires_at is not None and expires_at > now:
        return expires_at - timedelta(days=365)
    return expires_at


def estimate_release(
    state: str | None,
    tld: str,
    expires_at: datetime | None,
    state_since: datetime | None,
    now: datetime,
    delays: dict[str, dict[str, int]],
) -> datetime | None:
    tld_delays = delays.get(tld)
    if not tld_delays or any(k not in tld_delays for k in DELAY_KEYS):
        return None
    grace = timedelta(days=tld_delays["auto_renew_grace"])
    redemption = timedelta(days=tld_delays["redemption"])
    pending = timedelta(days=tld_delays["pending_delete"])

    if state in (EXPIRING, EXPIRED) and expires_at is not None:
        return effective_expiry(state, expires_at, now) + grace + redemption + pending
    if state == REDEMPTION and state_since is not None:
        return state_since + redemption + pending
    if state == PENDING_DELETE and state_since is not None:
        return state_since + pending
    return None


def check_interval(state: str | None, intervals: dict[str, int]) -> int:
    return intervals.get(state or "unknown", intervals["unknown"])
