"""Email household updates and log dashboard notices. Notification failures never break the agent."""
import hashlib
import hmac
import html
import os
import smtplib
import time
from email.message import EmailMessage
from urllib.parse import urlencode

from db.database import J, q


def _base():
    return os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")


def _secret():
    secret = os.getenv("APP_SECRET")
    if not secret:
        raise RuntimeError("APP_SECRET must be set before generating approval links")
    return secret.encode()


def _sig(run_id, action, rule_id, expires):
    payload = f"{run_id}|{action}|{rule_id}|{expires}".encode()
    return hmac.new(_secret(), payload, hashlib.sha256).hexdigest()


def action_link(run_id, action, rule_id="", ttl=6 * 3600):
    expires = int(time.time()) + ttl
    query = urlencode({
        "run": run_id,
        "a": action,
        "r": rule_id,
        "exp": expires,
        "sig": _sig(run_id, action, rule_id, expires),
    })
    return f"{_base()}/act?{query}"


def verify_link(run_id, action, rule_id, expires, signature):
    try:
        expires = int(expires)
        if expires <= time.time():
            return False
        expected = _sig(run_id, action, rule_id, expires)
    except (TypeError, ValueError, RuntimeError):
        return False
    return bool(signature) and hmac.compare_digest(expected, signature)


def log_message(run_id, household_id, channel, recipient, subject, body, status,
                response=None, direction="out"):
    q(
        """INSERT INTO messages
           (run_id,household_id,channel,direction,recipient,subject,body,status,provider_response)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (run_id, household_id, channel, direction, recipient, subject, body, status,
         J(response) if response is not None else None),
    )


def _send_email(recipient, subject, text, html_body):
    user = os.environ["GMAIL_USER"]
    password = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
    message = EmailMessage()
    message["From"] = f"Rasoi <{user}>"
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(text)
    message.add_alternative(html_body, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as smtp:
        smtp.login(user, password)
        smtp.send_message(message)
    return {"smtp": "smtp.gmail.com:465", "accepted_for": recipient}


def notify(run_id, household_id, title, body, options=None):
    """Write to dashboard and send email when enabled and configured."""
    try:
        log_message(run_id, household_id, "dashboard", "dashboard", title, body, "sent")
        channels = {channel.strip().lower() for channel in
                    os.getenv("NOTIFY_CHANNELS", "dashboard").split(",") if channel.strip()}
        recipient = os.getenv("NOTIFY_EMAIL_TO")
        if "email" not in channels or not recipient:
            return

        links = [
            (option["label"], action_link(
                run_id, option["action"], str(option.get("rule_id") or "")
            ))
            for option in (options or [])
        ]
        text = body + ("\n\n" + "\n".join(f"{label}: {url}" for label, url in links) if links else "")
        html_body = (
            "<div style='margin:0;background:#f1f4f2;padding:24px 12px;font-family:Arial,sans-serif'>"
            "<table role='presentation' style='width:100%;max-width:560px;margin:0 auto;background:#fff;"
            "border:1px solid #d5ddd9;border-radius:10px;border-collapse:separate;border-spacing:0'>"
            f"<tr><td style='padding:24px 24px 8px;color:#14201c;font-size:20px;font-weight:700'>"
            f"{html.escape(title)}</td></tr>"
            f"<tr><td style='padding:0 24px 20px;color:#33443d;font-size:16px;line-height:1.5'>"
            f"{html.escape(body)}</td></tr>"
        )
        if links:
            html_body += (
                "<tr><td style='padding:0 24px 10px;color:#62746d;font-size:13px'>"
                "Choose an action</td></tr>"
            )
            for label, url in links:
                html_body += (
                    "<tr><td style='padding:0 24px 14px'>"
                    f"<a href='{html.escape(url, quote=True)}' "
                    "style='display:inline-block;background:#14201c;color:#fff;padding:12px 18px;"
                    "border-radius:7px;text-decoration:none;font-size:15px;font-weight:600'>"
                    f"{html.escape(label)}</a></td></tr>"
                )
        html_body += (
            "<tr><td style='padding:8px 24px 20px;color:#62746d;font-size:12px;line-height:1.4'>"
            "For security, opening an action link asks you to confirm before Rasoi proceeds."
            "</td></tr></table></div>"
        )
        try:
            response = _send_email(recipient, title, text, html_body)
            log_message(run_id, household_id, "email", recipient, title, body, "sent", response)
        except Exception as error:
            log_message(run_id, household_id, "email", recipient, title, body, "failed",
                        {"error": repr(error)})
    except Exception as error:
        print("[notifier] failed:", repr(error))
