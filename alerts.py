"""
Alert transport (Telegram).

CONTRACT
--------
`send` NEVER raises and NEVER blocks a strategy run. It returns True on
delivery and False otherwise, and every attempt -- success or failure --
is logged. A notification failure must not cost you a trading decision;
the decision is already made and persisted by the time we get here.

WHY TELEGRAM
------------
Chosen over WhatsApp because business-initiated WhatsApp messages require
Meta Business onboarding, a dedicated phone number, and pre-approved
message templates. Telegram needs a BotFather token. See CONTEXT.md.

EOD LIMITATION
--------------
These alerts are end-of-day. The data source is daily bhavcopy, not a
broker feed, so a stop or target that fills intra-day is only known about
at the NEXT evening's run. This channel tells you what to do at tomorrow's
open; it does not tell you what is happening right now.
"""

import html
import logging
import time

import requests

import config

logger = logging.getLogger("momentum_tracker.alerts")


def recipients() -> list:
    """
    Chat IDs to deliver to.

    TELEGRAM_CHAT_ID accepts a comma-separated list, so the same note can
    go to a personal DM and a group at once. Group IDs are NEGATIVE
    (e.g. -5561496881); that is normal, not a typo.
    """
    raw = str(config.TELEGRAM_CHAT_ID or "")
    return [c.strip() for c in raw.split(",") if c.strip()]


def _configured() -> tuple:
    """(ok, reason). Never raises."""
    if not config.ALERTS_ENABLED:
        return False, "alerts disabled in config"
    if config.ALERT_CHANNEL != "telegram":
        return False, f"unsupported ALERT_CHANNEL {config.ALERT_CHANNEL!r}"
    if not config.TELEGRAM_BOT_TOKEN:
        return False, "TELEGRAM_BOT_TOKEN is not set (check .env)"
    if not recipients():
        return False, "TELEGRAM_CHAT_ID is not set (check .env)"
    return True, ""


def _chunks(text: str, limit: int):
    """Split on line boundaries so a message never tears mid-row."""
    if len(text) <= limit:
        return [text]
    out, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > limit:
            if buf:
                out.append(buf)
            # A single line longer than the limit is hard-split as a last resort.
            while len(line) > limit:
                out.append(line[:limit])
                line = line[limit:]
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        out.append(buf)
    return out


def send(text: str) -> bool:
    """
    Deliver `text` to the configured chat. Returns True only if EVERY
    chunk was accepted. Retries with the same exponential backoff the NSE
    client uses (2s, 4s, 8s).
    """
    ok, reason = _configured()
    if not ok:
        logger.warning("Alert not sent: %s", reason)
        return False

    url = config.TELEGRAM_API_URL.format(
        token=config.TELEGRAM_BOT_TOKEN, method="sendMessage")
    parts = _chunks(text, config.ALERT_MAX_CHARS)
    chats = recipients()
    all_ok = True

    for chat_id in chats:
        for idx, part in enumerate(parts, 1):
            delivered = False
            for attempt in range(1, config.MAX_RETRIES + 1):
                try:
                    resp = requests.post(
                        url,
                        json={
                            "chat_id": chat_id,
                            "text": part,
                            "parse_mode": config.TELEGRAM_PARSE_MODE,
                            "disable_web_page_preview": True,
                        },
                        timeout=config.REQUEST_TIMEOUT_SECONDS,
                    )
                except requests.RequestException as exc:
                    logger.warning("chat %s chunk %d/%d attempt %d/%d: "
                                   "transport error: %s", chat_id, idx,
                                   len(parts), attempt, config.MAX_RETRIES, exc)
                else:
                    if resp.status_code == 200:
                        logger.info("chat %s chunk %d/%d delivered (%d chars)",
                                    chat_id, idx, len(parts), len(part))
                        delivered = True
                        break
                    # 400 = malformed payload, 403 = bot removed or blocked.
                    # Neither is fixed by retrying; fail this chat fast and
                    # keep going so one bad recipient cannot stop the rest.
                    if resp.status_code in (400, 403):
                        logger.error("chat %s chunk %d/%d rejected (%d): %s",
                                     chat_id, idx, len(parts),
                                     resp.status_code, resp.text[:200])
                        break
                    logger.warning("chat %s chunk %d/%d attempt %d/%d: HTTP "
                                   "%d: %s", chat_id, idx, len(parts), attempt,
                                   config.MAX_RETRIES, resp.status_code,
                                   resp.text[:160])
                if attempt < config.MAX_RETRIES:
                    time.sleep(config.RETRY_BACKOFF_BASE_SECONDS
                               * (2 ** (attempt - 1)))

            if not delivered:
                logger.error("chat %s chunk %d/%d FAILED after %d attempts",
                             chat_id, idx, len(parts), config.MAX_RETRIES)
                all_ok = False

    return all_ok


def send_failure(context: str, exc: BaseException) -> bool:
    """
    Report a failed run. Without this, a broken fetch and a quiet market
    look identical from your phone -- both are silence.
    """
    if not config.ALERT_ON_FAILURE:
        return False
    body = (f"<b>⚠ Momentum Tracker run failed</b>\n\n"
            f"<b>Stage:</b> {esc(context)}\n"
            f"<b>Error:</b> {esc(type(exc).__name__)}: {esc(str(exc)[:400])}\n\n"
            f"No basket was generated. Check logs/app.log.")
    return send(body)


def esc(value) -> str:
    """Escape for Telegram HTML parse mode. Symbols can contain & and <."""
    return html.escape(str(value), quote=False)
