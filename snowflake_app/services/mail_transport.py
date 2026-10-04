"""
Mail transports: deliver an HTML email (optionally with attachments) to any
address.

  snowflake - SYSTEM$SEND_EMAIL through a notification integration. No setup
              outside Snowflake, but recipients must be verified users of this
              account and attachments are not supported (the report PDF is sent
              as a pre-signed stage link instead). See services/mailer.py.
  smtp      - any SMTP relay (Office 365, Gmail, SES, Mailgun, a company relay)
              over STARTTLS or SSL. Reaches any address; the PDF is attached.
  sendgrid  - the SendGrid v3 HTTP API. Reaches any address; the PDF is attached.

Inside Snowflake the SMTP and SendGrid transports need an external access
integration with a network rule for the host and a secret attached to the
Streamlit app (sql/08_external_email.sql). Credentials are read from, in order:

  1. secrets attached to the app, exposed as environment variables (work from
     background threads): mail_smtp_USERNAME / mail_smtp_PASSWORD (a PASSWORD
     secret mapped as 'mail_smtp') and mail_sendgrid_key (a GENERIC_STRING
     secret mapped as 'mail_sendgrid_key');
  2. st.secrets with the same names (local .streamlit/secrets.toml);
  3. the values entered in Settings (local runs only; never shown again).
"""

import base64
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

TRANSPORTS = {"snowflake": "Snowflake email", "smtp": "SMTP server", "sendgrid": "SendGrid"}
SENDGRID_URL = "https://api.sendgrid.com/v3/mail/send"
TIMEOUT_S = 30


class TransportError(RuntimeError):
    pass


def _secret(name: str, field: str | None = None) -> str:
    env = os.environ.get(f"{name}_{field.upper()}" if field else name)
    if env:
        return env
    try:
        import streamlit as st
        v = st.secrets.get(name)
        if v is not None:
            return str(v[field] if field else v)
    except Exception:
        pass
    return ""


def credentials(settings: dict, transport: str) -> dict:
    em = settings.get("email") or {}
    if transport == "smtp":
        c = dict(em.get("smtp") or {})
        c["username"] = _secret("mail_smtp", "username") or c.get("username", "")
        c["password"] = _secret("mail_smtp", "password") or c.get("password", "")
        c["source"] = "app secret" if _secret("mail_smtp", "password") else "settings"
        return c
    if transport == "sendgrid":
        c = dict(em.get("sendgrid") or {})
        c["api_key"] = _secret("mail_sendgrid_key") or c.get("api_key", "")
        c["source"] = "app secret" if _secret("mail_sendgrid_key") else "settings"
        return c
    return {}


def check(settings: dict, transport: str | None = None) -> dict:
    """Static readiness for the external transports (the Snowflake transport is checked in mailer.check)."""
    t = transport or (settings.get("email") or {}).get("transport", "snowflake")
    c = credentials(settings, t)
    issues = []
    if t == "smtp":
        if not c.get("host"):
            issues.append("Enter the SMTP host.")
        if not c.get("from_addr"):
            issues.append("Enter the sender address.")
        if c.get("security") != "none" and not (c.get("username") and c.get("password")):
            issues.append("Add the SMTP username and password (or attach the mail_smtp secret to the app).")
    elif t == "sendgrid":
        if not c.get("api_key"):
            issues.append("Add the SendGrid API key (or attach the mail_sendgrid_key secret to the app).")
        if not c.get("from_addr"):
            issues.append("Enter a sender address verified in SendGrid.")
    return {"ok": not issues, "issues": issues, "transport": t, "source": c.get("source")}


def _sender(c: dict) -> str:
    return formataddr((c.get("from_name") or "8D Intelligence", c["from_addr"]))


def send_smtp(settings: dict, to: list[str], subject: str, html: str, text: str = "",
              attachments: list[tuple[str, bytes, str]] = ()) -> dict:
    c = credentials(settings, "smtp")
    chk = check(settings, "smtp")
    if not chk["ok"]:
        raise TransportError(" ".join(chk["issues"]))
    msg = EmailMessage()
    msg["From"] = _sender(c)
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=c["from_addr"].split("@")[-1])
    msg.set_content(text or "This message is best viewed in an HTML-capable email client.")
    msg.add_alternative(html, subtype="html")
    for name, data, mime in attachments:
        main, sub = mime.split("/", 1)
        msg.add_attachment(data, maintype=main, subtype=sub, filename=name)
    port = int(c.get("port") or 587)
    ctx = ssl.create_default_context()
    try:
        if c.get("security") == "ssl":
            server = smtplib.SMTP_SSL(c["host"], port, timeout=TIMEOUT_S, context=ctx)
        else:
            server = smtplib.SMTP(c["host"], port, timeout=TIMEOUT_S)
        with server:
            server.ehlo()
            if c.get("security", "starttls") == "starttls":
                server.starttls(context=ctx)
                server.ehlo()
            if c.get("username"):
                server.login(c["username"], c["password"])
            refused = server.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        raise TransportError(f"The SMTP server rejected the login ({e.smtp_code}). Check the username and password or app password.") from e
    except (smtplib.SMTPException, OSError) as e:
        hint = " Inside Snowflake, the app needs an external access integration for this host and port." if "resolve" in str(e).lower() or "timed out" in str(e).lower() else ""
        raise TransportError(f"SMTP delivery failed: {str(e).splitlines()[0][:240]}.{hint}") from e
    return {"accepted": [a for a in to if a not in (refused or {})], "refused": list((refused or {}).keys()),
            "message_id": msg["Message-ID"]}


def send_sendgrid(settings: dict, to: list[str], subject: str, html: str, text: str = "",
                  attachments: list[tuple[str, bytes, str]] = ()) -> dict:
    import requests

    c = credentials(settings, "sendgrid")
    chk = check(settings, "sendgrid")
    if not chk["ok"]:
        raise TransportError(" ".join(chk["issues"]))
    body = {
        "personalizations": [{"to": [{"email": a} for a in to]}],
        "from": {"email": c["from_addr"], "name": c.get("from_name") or "8D Intelligence"},
        "subject": subject,
        "content": ([{"type": "text/plain", "value": text}] if text else []) + [{"type": "text/html", "value": html}],
    }
    if attachments:
        body["attachments"] = [{"content": base64.b64encode(data).decode(), "filename": name, "type": mime,
                                "disposition": "attachment"} for name, data, mime in attachments]
    try:
        r = requests.post(SENDGRID_URL, json=body, timeout=TIMEOUT_S,
                          headers={"Authorization": f"Bearer {c['api_key']}", "Content-Type": "application/json"})
    except requests.RequestException as e:
        raise TransportError(f"Could not reach SendGrid: {str(e)[:200]}. Inside Snowflake, the app needs an external "
                             "access integration for api.sendgrid.com.") from e
    if r.status_code >= 300:
        try:
            detail = "; ".join(x.get("message", "") for x in r.json().get("errors", []))
        except ValueError:
            detail = r.text[:200]
        raise TransportError(f"SendGrid returned {r.status_code}: {detail or 'request rejected'}")
    return {"accepted": list(to), "refused": [], "message_id": r.headers.get("X-Message-Id")}


def send(settings: dict, transport: str, to: list[str], subject: str, html: str, text: str = "",
         attachments: list[tuple[str, bytes, str]] = ()) -> dict:
    if transport == "smtp":
        return send_smtp(settings, to, subject, html, text, attachments)
    if transport == "sendgrid":
        return send_sendgrid(settings, to, subject, html, text, attachments)
    raise TransportError(f"Unknown transport {transport}")
