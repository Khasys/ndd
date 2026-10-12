import logging
from datetime import datetime

import httpx

from .states import LABELS

log = logging.getLogger(__name__)


def format_change(
    domain: str,
    old_state: str | None,
    new_state: str | None,
    old_registrar: str | None,
    new_registrar: str | None,
    release: datetime | None,
    app_url: str,
) -> str:
    lines = [domain]
    if old_state != new_state:
        lines.append(f"{LABELS.get(old_state, 'Inconnu')} → {LABELS.get(new_state, 'Inconnu')}")
    if old_registrar != new_registrar:
        lines.append(f"Registrar : {old_registrar or 'aucun'} → {new_registrar or 'aucun'}")
    if release is not None:
        lines.append(f"Libération estimée : ~{release.strftime('%d/%m/%Y')}")
    if app_url:
        lines.append(app_url)
    return "\n".join(lines)


async def send_telegram(http: httpx.AsyncClient, token: str, chat_id: str, text: str) -> str | None:
    """Renvoie None si le message est parti, sinon le motif de l'échec."""
    if not token or not chat_id:
        return "TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID non défini."
    try:
        response = await http.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        )
    except httpx.HTTPError as exc:
        log.warning("Telegram injoignable : %s", exc.__class__.__name__)
        return f"Telegram injoignable ({exc.__class__.__name__})."
    if response.status_code != 200:
        try:
            detail = response.json().get("description", "")
        except ValueError:
            detail = ""
        log.warning("Telegram a refusé le message : %s %s", response.status_code, detail)
        return f"Telegram a refusé le message ({response.status_code} {detail})".strip() + "."
    return None
