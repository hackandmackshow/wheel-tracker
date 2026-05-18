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
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, request
from flask_cors import CORS
from googleapiclient.discovery import build

app = Flask(__name__)
CORS(app, origins=os.environ.get("CORS_ORIGIN", "*"))

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
EMAIL_ADDRESS   = os.environ.get("EMAIL_ADDRESS", "")
EMAIL_PASSWORD  = os.environ.get("EMAIL_APP_PASSWORD", "")

VENMO_FROM      = "venmo@venmo.com"
VENMO_AMOUNT_RE = re.compile(r'\$(\d+(?:\.\d{1,2})?)')
VENMO_NAME_PATS = [
    re.compile(r'^(.+?)\s+paid you',             re.IGNORECASE),
    re.compile(r'^(.+?)\s+sent you',             re.IGNORECASE),
    re.compile(r'received .+ from (.+?)[\.\n]',  re.IGNORECASE),
    re.compile(r'^(.+?)\s+completed',            re.IGNORECASE),
]

# Build YouTube client once at startup rather than on every request
_yt = build("youtube", "v3", developerKey=YOUTUBE_API_KEY, cache_discovery=False) \
      if YOUTUBE_API_KEY else None


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
        "status":        "ok",
        "youtube_ready": bool(YOUTUBE_API_KEY),
        "email_ready":   bool(EMAIL_ADDRESS and EMAIL_PASSWORD),
    })


@app.route("/api/superchats")
def superchats():
    """
    Polls YouTube live chat for superchats.

    First call — pass video_id to resolve the live chat:
      GET /api/superchats?video_id=dQw4w9WgXcQ

    Subsequent calls — pass live_chat_id + page_token:
      GET /api/superchats?live_chat_id=xxx&page_token=yyy
    """
    if not _yt:
        return jsonify({"entries": [], "error": "YouTube API key not configured on server."})

    video_id     = request.args.get("video_id", "").strip()
    live_chat_id = request.args.get("live_chat_id", "").strip()
    page_token   = request.args.get("page_token", "").strip() or None

    try:
        # Resolve live chat ID from video ID on first call
        if not live_chat_id:
            if not video_id:
                return jsonify({"entries": [], "error": "Provide video_id or live_chat_id."})
            resp  = _yt.videos().list(part="liveStreamingDetails", id=video_id).execute()
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

        resp    = _yt.liveChatMessages().list(**params).execute()
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
    Only searches the last 7 days of email to keep memory usage low.

    Query params:
      since (float) — Unix timestamp. Only emails after this time are returned.
    """
    since_ts = request.args.get("since", type=float, default=0.0)

    if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
        return jsonify({"entries": [], "error": "Email not configured on server."})

    entries = []
    error   = None
    mail    = None

    try:
        # timeout=10 applies to all socket operations on this connection
        mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=10)
        mail.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        mail.select("INBOX")

        # Limit search to last 7 days — a stream won't span longer than that
        since_date = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%d-%b-%Y")
        _, data = mail.search(None, "FROM", f'"{VENMO_FROM}"', "SINCE", since_date)
        uids = data[0].split() if data and data[0] else []

        if uids:
            # Single FETCH for all UIDs — one round-trip instead of N
            uid_set = b",".join(uids)
            _, all_msg_data = mail.fetch(uid_set, "(BODY.PEEK[HEADER.FIELDS (DATE SUBJECT MESSAGE-ID)])")

            for i, item in enumerate(all_msg_data):
                if not isinstance(item, tuple):
                    continue
                msg = email_lib.message_from_bytes(item[1])

                date_str = msg.get("Date", "")
                try:
                    email_ts = email.utils.parsedate_to_datetime(date_str).timestamp()
                except Exception:
                    continue

                if email_ts < since_ts:
                    continue

                subject = msg.get("Subject", "")
                msg_id  = msg.get("Message-ID", "")
                name, amount = parse_venmo_subject(subject)

                if name and amount >= 4.99:
                    entries.append({
                        "id":     msg_id,
                        "name":   name,
                        "amount": amount,
                        "ts":     email_ts,
                    })

    except imaplib.IMAP4.error as e:
        error = f"Email login failed: {e}. Check EMAIL_ADDRESS and EMAIL_APP_PASSWORD."
    except (TimeoutError, socket.timeout):
        error = "Gmail connection timed out. Try again."
    except Exception as e:
        error = str(e)
    finally:
        if mail:
            try:
                mail.shutdown()  # close socket directly — avoids hanging on LOGOUT
            except Exception:
                pass

    return jsonify({"entries": entries, "error": error})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
