import tomllib
from datetime import datetime, timedelta
from pathlib import Path

ACTIVE = "active"
EXPIRING = "expiring"
EXPIRED = "expired"
# Renouvelé automatiquement à l'échéance (statut « auto renew period », échéance repoussée) : le titulaire le
# garde en général ; le registrar peut encore le supprimer pendant la période de grâce. Redevient « Actif »
# quand le statut disparaît, ou passe en rédemption si le registrar le supprime.
RENEWED = "renewed"
REDEMPTION = "redemption"
PENDING_DELETE = "pending_delete"
AVAILABLE = "available"

# Ordre d'urgence, utilisé aussi pour le tri par défaut côté interface.
URGENCY = [AVAILABLE, PENDING_DELETE, REDEMPTION, EXPIRED, RENEWED, EXPIRING, ACTIVE]

LABELS = {
    ACTIVE: "Actif",
    EXPIRING: "Expire bientôt",
    EXPIRED: "Expiré",
    RENEWED: "Renouvelé d'office",
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


def compute_state(statuses: list[str], expires_at: datetime | None, now: datetime, expiring_days: int,
                  tld: str | None = None) -> str:
    norm = {_normalize(s) for s in statuses}
    if "redemptionperiod" in norm or "pendingrestore" in norm:
        return REDEMPTION
    if "pendingdelete" in norm:
        return PENDING_DELETE
    if "autorenewperiod" in norm:
        # .fr : l'Afnic renouvelle à l'échéance ; statut et échéance repoussée disent un renouvellement fait.
        # Extensions génériques : le registre affiche l'échéance repoussée même si personne n'a payé, et le
        # registrar peut encore supprimer le nom pendant la période de grâce (la plupart des libérations
        # passent par là) : expiré, avec l'estimation de libération.
        return RENEWED if tld == "fr" and expires_at is not None and expires_at > now else EXPIRED
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
    tld_delays = delays.get(tld) or {}
    # Seuls les délais utiles à l'état sont exigés : le .fr n'a pas de période de grâce sourcée.
    def days(key: str) -> timedelta | None:
        return timedelta(days=tld_delays[key]) if key in tld_delays else None

    redemption, pending = days("redemption"), days("pending_delete")
    if state in (EXPIRING, EXPIRED) and expires_at is not None:
        grace = days("auto_renew_grace")
        if grace is None or redemption is None or pending is None:
            return None
        return effective_expiry(state, expires_at, now) + grace + redemption + pending
    if state == REDEMPTION and state_since is not None and redemption is not None and pending is not None:
        return state_since + redemption + pending
    if state == PENDING_DELETE and state_since is not None and pending is not None:
        return state_since + pending
    return None


def drop_time(tld: str, delays: dict[str, dict]) -> tuple[int, int] | None:
    value = str(delays.get(tld, {}).get("drop_time_utc", ""))
    if not value:
        return None
    hour, minute = value.split(":")
    return int(hour), int(minute)


def drop_minute(tld: str, delays: dict[str, dict]) -> int | None:
    """Minute de chaque heure à laquelle le registre libère les noms dont le délai est écoulé (lots horaires)."""
    value = delays.get(tld, {}).get("drop_minute_utc")
    return int(value) if value is not None else None


def has_drop_time(tld: str, delays: dict[str, dict]) -> bool:
    """La libération estimée a une heure, pas seulement un jour."""
    return drop_time(tld, delays) is not None or drop_minute(tld, delays) is not None


def at_drop_time(tld: str, value: datetime | None, delays: dict[str, dict]) -> datetime | None:
    """Fin du délai ramenée à l'heure où le registre libère : fenêtre quotidienne (drop_time_utc), ou premier lot
    horaire qui suit (drop_minute_utc). Sans l'une ni l'autre, la fin du délai elle-même."""
    if value is None:
        return None
    daily = drop_time(tld, delays)
    if daily:
        return value.replace(hour=daily[0], minute=daily[1], second=0, microsecond=0)
    minute = drop_minute(tld, delays)
    if minute is None:
        return value
    slot = value.replace(minute=minute, second=0, microsecond=0)
    return slot if slot >= value else slot + timedelta(hours=1)


# Au .fr, une rédemption plus ancienne est impossible : le nom serait déjà libre.
FR_REDEMPTION_WINDOW = timedelta(days=31)


def redemption_since(tld: str, last_changed: datetime | None, expires_at: datetime | None,
                     now: datetime) -> datetime | None:
    """Début de la rédemption d'après la dernière modification RDAP ; None si elle n'en dit rien.
    Extensions génériques : seulement si elle suit l'échéance, sinon c'est une modification ancienne, sans rapport.
    .fr : l'Afnic compte ses 30 jours depuis l'ordre de suppression du registrar, qui précède le plus souvent
    l'échéance d'un ou deux jours (691 noms relevés le 22/11/2026, aucun encore en rédemption 30 jours après cet
    ordre). La date vaut donc même avant l'échéance, tant qu'elle a moins de 31 jours."""
    if last_changed is None:
        return None
    if tld == "fr":
        return min(last_changed, now) if last_changed >= now - FR_REDEMPTION_WINDOW else None
    if expires_at is None or last_changed >= expires_at:
        return min(last_changed, now)
    return None


def check_interval(state: str | None, intervals: dict[str, int]) -> int:
    return intervals.get(state or "unknown", intervals["unknown"])
