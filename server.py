"""
Hack & Mack — Wheel Tracker Server

Handles YouTube superchat polling and Venmo email checking.
Deployed on Fly.io — all external API calls happen server-side (no CORS issues).

Environment variables (set via: fly secrets set KEY=value):
  YOUTUBE_API_KEY     — YouTube Data API v3 key from Google Cloud Console
  EMAIL_ADDRESS       — Gmail address that receives Venmo notifications
  EMAIL_APP_PASSWORD  — Gmail App Password (not your regular password)
  CORS_ORIGIN         — Your GitHub Pages URL, e.g. https://yourname.github.io
"""

import os
import re
import imaplib
import email as email_lib
import email.utils
from flask import Flask, jsonify, request
from flask_cors import CORS
from googleapiclient.discovery import build

app = Flask(__name__)
CORS(app, origins=os.environ.get("CORS_ORIGIN", "*"))

YOUTUBE_API_KEY  = os.environ.get("YOUTUBE_API_KEY", "")
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
    return jsonify({
        "status":          "ok",
        "youtube_ready":   bool(YOUTUBE_API_KEY),
        "email_ready":     bool(EMAIL_ADDRESS and EMAIL_PASSWORD),
    })


@app.route("/api/superchats")
def superchats():
    """
    Polls YouTube live chat for superchats.

    First call — pass video_id to resolve the live chat:
      GET /api/superchats?video_id=dQw4w9WgXcQ

    Subsequent calls — pass live_chat_id + page_token:
      GET /api/superchats?live_chat_id=xxx&page_token=yyy

    Response:
      {
        "live_chat_id":        "...",
        "entries":             [{"name": "...", "amount": 5.0, "amount_str": "$5.00"}, ...],
        "next_page_token":     "...",
        "polling_interval_ms": 10000,
        "error":               null
      }
    """
    if not YOUTUBE_API_KEY:
        return jsonify({"entries": [], "error": "YouTube API key not configured on server."})

    video_id     = request.args.get("video_id", "").strip()
    live_chat_id = request.args.get("live_chat_id", "").strip()
    page_token   = request.args.get("page_token", "").strip() or None

    try:
        yt = build("youtube", "v3", developerKey=YOUTUBE_API_KEY, cache_discovery=False)

        # Resolve live chat ID from video ID on first call
        if not live_chat_id:
            if not video_id:
                return jsonify({"entries": [], "error": "Provide video_id or live_chat_id."})
            resp  = yt.videos().list(part="liveStreamingDetails", id=video_id).execute()
            items = resp.get("items", [])
            if not items:
                return jsonify({"entries": [], "error": f"Video '{video_id}' not found."})
            live_chat_id = items[0].get("liveStreamingDetails", {}).get("activeLiveChatId")
            if not live_chat_id:
                return jsonify({"entries": [], "error":
                    "No active live chat found. Make sure the stream is live before clicking Start."})

        # Fetch live chat messages
        params = dict(liveChatId=live_chat_id, part="snippet,authorDetails", maxResults=200)
        if page_token:
            params["pageToken"] = page_token

        resp    = yt.liveChatMessages().list(**params).execute()
        entries = []

        for item in resp.get("items", []):
            snippet = item.get("snippet", {})
            if snippet.get("type") != "superChatEvent":
                continue
            details = snippet.get("superChatDetails", {})
            amount  = int(details.get("amountMicros", 0)) / 1_000_000
            if amount >= 4.99:
                entries.append({
                    "name":       item["authorDetails"]["displayName"],
                    "amount":     amount,
                    "amount_str": details.get("amountDisplayString", f"${amount:.2f}"),
                })

        return jsonify({
            "live_chat_id":        live_chat_id,
            "entries":             entries,
            "next_page_token":     resp.get("nextPageToken"),
            "polling_interval_ms": resp.get("pollingIntervalMillis", 10000),
            "error":               None,
        })

    except Exception as e:
        return jsonify({"entries": [], "error": str(e)})


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
