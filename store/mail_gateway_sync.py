import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def extract_emails_from_text(text):
    seen = set()
    emails = []
    for match in EMAIL_RE.findall(text or ""):
        email = match.strip().lower()
        if email in seen:
            continue
        seen.add(email)
        emails.append(email)
    return emails


def sync_emails_to_mail_gateway(emails, notes=""):
    deduped = []
    seen = set()
    for email in emails:
        normalized = (email or "").strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)

    if not deduped:
        return {"ok": True, "count": 0, "inserted": 0, "updated": 0, "skipped": 0}

    from .models import MailGatewaySyncConfig

    config = MailGatewaySyncConfig.get_solo()
    auto_sync_enabled = config.auto_sync_on_import if config else getattr(settings, "MAIL_GATEWAY_SYNC_ENABLED", True)
    if not auto_sync_enabled:
        return {
            "ok": False,
            "disabled": True,
            "error": "mail_gateway_sync_disabled",
            "count": len(deduped),
        }

    sync_url = getattr(settings, "MAIL_GATEWAY_SYNC_URL", "").strip()
    sync_token = getattr(settings, "MAIL_GATEWAY_SYNC_TOKEN", "").strip()
    if not sync_url or not sync_token:
        return {"ok": False, "error": "mail_gateway_sync_not_configured"}

    payload = {"emails": deduped, "notes": notes}
    request = Request(
        sync_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {sync_token}",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            response_data = json.loads(response.read().decode("utf-8", "ignore") or "{}")
            if response.status >= 400 or not response_data.get("ok"):
                return {"ok": False, "error": response_data.get("error") or f"http_{response.status}"}
            return response_data
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")
        return {"ok": False, "error": f"http_{exc.code}", "detail": detail}
    except (URLError, OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
