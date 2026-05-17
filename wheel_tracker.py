#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════╗
║         Hack & Mack — Wheel of Names Entry Tracker       ║
║  Monitors YouTube Superchats + Venmo emails ($4.99+)     ║
╚══════════════════════════════════════════════════════════╝

QUICK SETUP (do once):
  1. Install dependencies:
       pip install google-api-python-client

  2. Get a YouTube Data API v3 key:
       - Go to https://console.cloud.google.com/
       - Create a project → Enable "YouTube Data API v3"
       - Credentials → Create API Key → paste below

  3. Set up Gmail App Password for Venmo emails:
       - Go to https://myaccount.google.com/apppasswords
       - Generate a password for "Mail" → paste below
       (Note: requires 2FA to be enabled on your Google account)

  4. Fill in the CONFIG section below, then run:
       python wheel_tracker.py <youtube_video_id_or_url>

HOW IT WORKS:
  - Polls your YouTube live chat every ~10 seconds for Superchats
  - Checks your Gmail inbox every 20 seconds for Venmo payment emails
  - Contributions >= $4.99 add the sender's name to the wheel list
  - $10 = 2 entries, $15 = 3 entries, etc.
  - Press Ctrl+C to stop and get the final copyable name list
  - Names are also auto-saved to a timestamped .txt file
"""

import time
import imaplib
import email as email_lib
import re
import sys
from datetime import datetime

try:
    from googleapiclient.discovery import build
except ImportError:
    print("[ERROR] Missing dependency. Run: pip install google-api-python-client")
    sys.exit(1)


# ─── CONFIG — fill these in ───────────────────────────────────────────────────

YOUTUBE_API_KEY    = "AIzaSyAJUk6uw1wZQrmVMdRo2WumNJeKxSQm73s"

# Gmail address that receives Venmo payment notifications
EMAIL_ADDRESS      = "hackandmackshow@gmail.com"
# Gmail App Password (NOT your regular password) from:
# https://myaccount.google.com/apppasswords
EMAIL_APP_PASSWORD = "lyfq uxza wddd fabv"

# Contribution settings
MIN_AMOUNT         = 4.98   # Minimum $ to earn an entry
ENTRY_PRICE        = 5.00   # $ per entry (e.g. $10 superchat = 2 entries)

# Poll intervals (seconds) — don't go below 5 for YouTube (API quota)
YT_POLL_INTERVAL   = 10
EMAIL_POLL_INTERVAL = 20

# ─────────────────────────────────────────────────────────────────────────────


# ── Venmo email patterns ──────────────────────────────────────────────────────
# Venmo sends emails like: "[Name] paid you $5.00" or "You received $5 from [Name]"
VENMO_FROM = "venmo@venmo.com"
VENMO_AMOUNT_RE = re.compile(r'\$(\d+(?:\.\d{1,2})?)')
VENMO_NAME_PATTERNS = [
    re.compile(r'^(.+?)\s+paid you',           re.IGNORECASE),
    re.compile(r'^(.+?)\s+sent you',           re.IGNORECASE),
    re.compile(r'received .+ from (.+?)[\.\n]', re.IGNORECASE),
    re.compile(r'^(.+?)\s+completed',          re.IGNORECASE),
]


def entries_for_amount(amount: float) -> int:
    """How many wheel entries does this amount earn?"""
    if amount < MIN_AMOUNT:
        return 0
    return max(1, int(amount // ENTRY_PRICE))


# ── YouTube ───────────────────────────────────────────────────────────────────

def extract_video_id(input_str: str) -> str:
    """Accept a full URL or bare video ID."""
    match = re.search(r'(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})', input_str)
    return match.group(1) if match else input_str.strip()


def get_live_chat_id(video_id: str) -> str:
    youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY, cache_discovery=False)
    resp = youtube.videos().list(
        part="liveStreamingDetails",
        id=video_id
    ).execute()

    items = resp.get("items", [])
    if not items:
        print(f"\n[ERROR] Video '{video_id}' not found. Check the video ID and try again.")
        sys.exit(1)

    chat_id = items[0].get("liveStreamingDetails", {}).get("activeLiveChatId")
    if not chat_id:
        print("\n[ERROR] No active live chat found.")
        print("        Make sure the stream is currently live (not scheduled or ended).")
        sys.exit(1)

    return chat_id


def poll_superchats(live_chat_id: str, page_token: str | None) -> tuple[list, str | None, int]:
    """
    Returns (new_names, next_page_token, poll_interval_ms).
    Names are repeated if the contribution earns multiple entries.
    """
    youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY, cache_discovery=False)
    params = dict(
        liveChatId=live_chat_id,
        part="snippet,authorDetails",
        maxResults=200,
    )
    if page_token:
        params["pageToken"] = page_token

    resp = youtube.liveChatMessages().list(**params).execute()
    new_names = []

    for item in resp.get("items", []):
        snippet = item.get("snippet", {})
        if snippet.get("type") != "superChatEvent":
            continue

        details = snippet.get("superChatDetails", {})
        amount_micros = int(details.get("amountMicros", 0))
        amount = amount_micros / 1_000_000
        name = item["authorDetails"]["displayName"]
        n = entries_for_amount(amount)

        if n > 0:
            new_names.extend([name] * n)
            display = details.get("amountDisplayString", f"${amount:.2f}")
            print(f"  ★ YouTube SC  {name:<30}  {display}  →  {n} {'entry' if n == 1 else 'entries'}")

    next_token = resp.get("nextPageToken")
    poll_ms = resp.get("pollingIntervalMillis", YT_POLL_INTERVAL * 1000)
    return new_names, next_token, poll_ms


# ── Venmo email ───────────────────────────────────────────────────────────────

seen_email_uids: set[bytes] = set()


def parse_venmo_email(subject: str) -> tuple[str | None, float]:
    """Extract (name, amount) from a Venmo notification subject line."""
    name = None
    for pattern in VENMO_NAME_PATTERNS:
        m = pattern.search(subject)
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


def check_venmo_emails() -> list[str]:
    """Connect to Gmail via IMAP, check for new Venmo payment emails."""
    new_names = []
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
        mail.select("INBOX")

        # Search for unread emails from Venmo
        _, data = mail.search(None, "UNSEEN", "FROM", f'"{VENMO_FROM}"')
        uids = data[0].split()

        for uid in uids:
            if uid in seen_email_uids:
                continue
            seen_email_uids.add(uid)

            _, msg_data = mail.fetch(uid, "(RFC822)")
            msg = email_lib.message_from_bytes(msg_data[0][1])
            subject = msg.get("Subject", "")

            name, amount = parse_venmo_email(subject)
            n = entries_for_amount(amount) if name else 0

            if n > 0:
                new_names.extend([name] * n)
                print(f"  ★ Venmo       {name:<30}  ${amount:.2f}  →  {n} {'entry' if n == 1 else 'entries'}")
            elif name and amount > 0:
                print(f"  · Venmo       {name} — ${amount:.2f} (below minimum, skipped)")

        mail.logout()

    except imaplib.IMAP4.error as e:
        print(f"  [Email login failed] {e}")
        print("  Check EMAIL_ADDRESS and EMAIL_APP_PASSWORD in the config.")
    except Exception as e:
        print(f"  [Email error] {e}")

    return new_names


# ── Output ────────────────────────────────────────────────────────────────────

def print_wheel_list(all_entries: list[str]) -> None:
    print()
    print("╔" + "═" * 52 + "╗")
    print(f"║  WHEEL ENTRIES — {len(all_entries)} total{' ' * (34 - len(str(len(all_entries))))}║")
    print("╠" + "═" * 52 + "╣")
    if all_entries:
        for name in all_entries:
            truncated = name[:48]
            print(f"║  {truncated:<50}║")
    else:
        print("║  (none yet)                                      ║")
    print("╚" + "═" * 52 + "╝")
    print()


def save_entries(all_entries: list[str]) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"wheel_entries_{timestamp}.txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write("\n".join(all_entries))
    return filename


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Validate config
    if YOUTUBE_API_KEY == "YOUR_YOUTUBE_API_KEY_HERE":
        print("[ERROR] Please set YOUTUBE_API_KEY in the CONFIG section of this script.")
        sys.exit(1)

    # Get video ID from CLI arg or prompt
    if len(sys.argv) > 1:
        raw = sys.argv[1]
    else:
        raw = input("Enter YouTube video URL or ID: ").strip()

    video_id = extract_video_id(raw)

    print(f"\n[YouTube] Fetching live chat ID for video: {video_id}")
    live_chat_id = get_live_chat_id(video_id)
    print(f"[YouTube] Live chat found ✓")

    email_enabled = EMAIL_ADDRESS != "your@gmail.com" and EMAIL_APP_PASSWORD != "xxxx xxxx xxxx xxxx"
    if email_enabled:
        print(f"[Venmo]   Watching {EMAIL_ADDRESS} for payment emails ✓")
    else:
        print("[Venmo]   Email not configured — skipping (YouTube only)")

    print(f"\nTracking contributions ≥ ${MIN_AMOUNT:.2f}  |  ${ENTRY_PRICE:.2f} per entry")
    print("Press Ctrl+C to stop and print the final list.\n")
    print("─" * 56)

    all_entries: list[str] = []
    page_token: str | None = None
    last_email_check: float = 0

    try:
        while True:
            # ── YouTube poll ──
            try:
                new_yt, page_token, poll_ms = poll_superchats(live_chat_id, page_token)
            except Exception as e:
                print(f"  [YouTube poll error] {e}")
                poll_ms = YT_POLL_INTERVAL * 1000
                new_yt = []

            if new_yt:
                all_entries.extend(new_yt)
                print_wheel_list(all_entries)

            # ── Venmo email poll ──
            now = time.time()
            if email_enabled and (now - last_email_check) >= EMAIL_POLL_INTERVAL:
                new_venmo = check_venmo_emails()
                if new_venmo:
                    all_entries.extend(new_venmo)
                    print_wheel_list(all_entries)
                last_email_check = now

            wait = max(poll_ms / 1000, YT_POLL_INTERVAL)
            time.sleep(wait)

    except KeyboardInterrupt:
        print("\n\n[Stopped]")

    print_wheel_list(all_entries)

    if all_entries:
        saved = save_entries(all_entries)
        print(f"[Saved] {saved}")
        print("\nPaste the names above into wheelofnames.com — one per line.\n")
    else:
        print("No qualifying contributions recorded.\n")


if __name__ == "__main__":
    main()
