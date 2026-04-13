"""
Hack & Mack — Wheel Tracker Server

Checks Gmail for Venmo payment notifications and returns parsed entries.
Deployed on Railway. YouTube superchats are handled client-side.

Environment variables (set in Railway dashboard):
  EMAIL_ADDRESS       — Gmail address that receives Venmo notifications
  EMAIL_APP_PASSWORD  — Gmail App Password (not your regular password)
  CORS_ORIGIN         — Your GitHub Pages URL, e.g. https://yourname.github.io
                        Set to * to allow all origins during testing
"""

import os
import re
import imaplib
import email as email_lib
import email.utils
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)

# Allow requests from your GitHub Pages URL.
# Set CORS_ORIGIN env var to your GitHub Pages URL in Railway dashboard.
CORS(app, origins=os.environ.get("CORS_ORIGIN", "*"))

EMAIL_ADDRESS    = os.environ.get("EMAIL_ADDRESS", "")
EMAIL_PASSWORD   = os.environ.get("EMAIL_APP_PASSWORD", "")

VENMO_FROM      = "venmo@venmo.com"
VENMO_AMOUNT_RE = re.compile(r'\$(\d+(?:\.\d{1,2})?)')
VENMO_NAME_PATS = [
    re.compile(r'^(.+?)\s+paid you',             re.IGNORECASE),
    re.compile(r'^(.+?)\s+sent you',             re.IGNORECASE),
    re.compile(r'received .+ from (.+?)[\.\n]',  re.IGNORECASE),
    re.compile(r'^(.+?)\s+completed',            re.IGNORECASE),
]


def parse_venmo_subject(subject: str):
    name = None
    for pat in VENMO_NAME_PATS:
        m = pat.search(subject)
        if m:
            name = m.group(1).strip()
            break
    amount = 0.0
    m = VENMO_AMOUNT_RE.search(subject)
    if m:
        try:
            amount = float(m.group(1))
        except ValueError:
            pass
    return name, amount


@app.route("/health")
def health():
    """Simple uptime check."""
    return jsonify({"status": "ok"})


@app.route("/api/venmo")
def venmo():
    """
    Returns Venmo payment entries received after the given Unix timestamp.

    Query params:
      since (float) — Unix timestamp. Only emails after this time are returned.

    Response:
      {
        "entries": [
          { "id": "<Message-ID>", "name": "Jane Doe", "amount": 10.0, "ts": 1234567890.0 },
          ...
        ],
        "error": null   // or an error string
      }
    """
    since_ts = request.args.get("since", type=float, default=0.0)

    if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
        return jsonify({"entries": [], "error": "Email not configured on server."})

    entries = []
    error   = None

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        mail.select("INBOX")

        _, data = mail.search(None, "FROM", f'"{VENMO_FROM}"')
        uids = data[0].split()

        for uid in uids:
            _, msg_data = mail.fetch(uid, "(RFC822)")
            msg = email_lib.message_from_bytes(msg_data[0][1])

            # Parse email date and skip if before stream start
            date_str = msg.get("Date", "")
            try:
                email_ts = email.utils.parsedate_to_datetime(date_str).timestamp()
            except Exception:
                continue

            if email_ts < since_ts:
                continue

            subject = msg.get("Subject", "")
            msg_id  = msg.get("Message-ID", uid.decode())
            name, amount = parse_venmo_subject(subject)

            if name and amount >= 4.99:
                entries.append({
                    "id":     msg_id,
                    "name":   name,
                    "amount": amount,
                    "ts":     email_ts,
                })

        mail.logout()

    except imaplib.IMAP4.error as e:
        error = f"Email login failed: {e}. Check EMAIL_ADDRESS and EMAIL_APP_PASSWORD."
    except Exception as e:
        error = str(e)

    return jsonify({"entries": entries, "error": error})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
