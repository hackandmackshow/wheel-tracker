#!/usr/bin/env python3
"""
Hack & Mack — Wheel Entry Tracker (Desktop App)

SETUP (do once):
  pip install google-api-python-client pillow

PACKAGE AS .APP:
  See HOW_TO_PACKAGE.txt
"""

import tkinter as tk
from tkinter import scrolledtext, messagebox
import threading
import queue
import json
import time
import imaplib
import email as email_lib
import re
import sys
import os
from pathlib import Path

try:
    from googleapiclient.discovery import build
    GOOGLE_API_AVAILABLE = True
except ImportError:
    GOOGLE_API_AVAILABLE = False

try:
    from PIL import Image, ImageTk, ImageDraw
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


# ── Resource path (works both dev and PyInstaller bundle) ─────────────────────
def resource_path(filename: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, filename)


# ── Constants ─────────────────────────────────────────────────────────────────
MIN_AMOUNT          = 4.99
ENTRY_PRICE         = 5.00
YT_POLL_INTERVAL    = 10
EMAIL_POLL_INTERVAL = 20

VENMO_FROM      = "venmo@venmo.com"
VENMO_AMOUNT_RE = re.compile(r'\$(\d+(?:\.\d{1,2})?)')
VENMO_NAME_PATTERNS = [
    re.compile(r'^(.+?)\s+paid you',            re.IGNORECASE),
    re.compile(r'^(.+?)\s+sent you',            re.IGNORECASE),
    re.compile(r'received .+ from (.+?)[\.\n]', re.IGNORECASE),
    re.compile(r'^(.+?)\s+completed',           re.IGNORECASE),
]

CONFIG_PATH = Path.home() / ".hackandmack_config.json"

# ── Palette ───────────────────────────────────────────────────────────────────
C_BG        = "#111318"   # main background
C_SURFACE   = "#1c1f27"   # cards / panels
C_SURFACE2  = "#252933"   # inputs, secondary surfaces
C_BORDER    = "#2e323d"   # subtle borders
C_ORANGE    = "#e8761e"   # brand orange
C_ORANGE_HV = "#f28c34"   # hover
C_TEXT      = "#f0f0f0"   # primary text
C_MUTED     = "#8a8f9e"   # secondary text
C_GREEN     = "#4caf7d"   # success
C_WARN_BG   = "#2a2010"   # warning banner bg
C_WARN_FG   = "#e8b84b"   # warning text
C_WHITE     = "#ffffff"

FONT        = "Helvetica Neue"


# ── Config ────────────────────────────────────────────────────────────────────
def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except Exception:
            pass
    return {"api_key": "", "email": "", "app_password": ""}


def save_config(cfg: dict):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass


# ── Tracking helpers ──────────────────────────────────────────────────────────
def entries_for_amount(amount: float) -> int:
    if amount < MIN_AMOUNT:
        return 0
    return max(1, int((amount + 0.01) // ENTRY_PRICE))


def extract_video_id(raw: str) -> str:
    m = re.search(r'(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})', raw)
    return m.group(1) if m else raw.strip()


def get_live_chat_id(video_id: str, api_key: str) -> str:
    yt   = build("youtube", "v3", developerKey=api_key, cache_discovery=False)
    resp = yt.videos().list(part="liveStreamingDetails", id=video_id).execute()
    items = resp.get("items", [])
    if not items:
        raise ValueError(f"Video '{video_id}' not found. Double-check the URL.")
    chat_id = items[0].get("liveStreamingDetails", {}).get("activeLiveChatId")
    if not chat_id:
        raise ValueError(
            "No active live chat found.\n\n"
            "Make sure your stream is live before clicking Start."
        )
    return chat_id


def parse_venmo_subject(subject: str):
    name = None
    for pat in VENMO_NAME_PATTERNS:
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


def check_venmo_emails(email_addr, app_password, seen_uids):
    results = []
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(email_addr, app_password)
    mail.select("INBOX")
    _, data = mail.search(None, "UNSEEN", "FROM", f'"{VENMO_FROM}"')
    for uid in data[0].split():
        if uid in seen_uids:
            continue
        seen_uids.add(uid)
        _, msg_data = mail.fetch(uid, "(RFC822)")
        msg     = email_lib.message_from_bytes(msg_data[0][1])
        subject = msg.get("Subject", "")
        name, amount = parse_venmo_subject(subject)
        n = entries_for_amount(amount) if name else 0
        if n > 0:
            results.append((name, amount, n))
    mail.logout()
    return results


# ── Background tracker thread ─────────────────────────────────────────────────
def tracker_thread(api_key, email_addr, app_password, video_id, msg_q, stop_evt):
    def put(kind, **kw):
        msg_q.put({"kind": kind, **kw})

    put("status", text="Connecting to YouTube…")
    try:
        live_chat_id = get_live_chat_id(video_id, api_key)
    except Exception as e:
        put("error", text=str(e))
        return

    put("status", text="Connected — watching for contributions")

    email_enabled = bool(email_addr and app_password)
    if not email_enabled:
        put("log", text="Email not configured — YouTube superchats only")

    page_token    = None
    seen_uids: set = set()
    last_email_ts = 0.0
    poll_ms       = YT_POLL_INTERVAL * 1000

    while not stop_evt.is_set():
        # YouTube
        try:
            yt     = build("youtube", "v3", developerKey=api_key, cache_discovery=False)
            params = dict(liveChatId=live_chat_id, part="snippet,authorDetails",
                          maxResults=200)
            if page_token:
                params["pageToken"] = page_token

            resp = yt.liveChatMessages().list(**params).execute()
            for item in resp.get("items", []):
                snippet = item.get("snippet", {})
                if snippet.get("type") != "superChatEvent":
                    continue
                details = snippet.get("superChatDetails", {})
                amount  = int(details.get("amountMicros", 0)) / 1_000_000
                n       = entries_for_amount(amount)
                if n > 0:
                    name    = item["authorDetails"]["displayName"]
                    display = details.get("amountDisplayString", f"${amount:.2f}")
                    put("entry", name=name, amount_str=display, count=n, source="YouTube")

            page_token = resp.get("nextPageToken")
            poll_ms    = resp.get("pollingIntervalMillis", YT_POLL_INTERVAL * 1000)

        except Exception as e:
            put("log", text=f"YouTube poll error: {e}")

        # Venmo email
        now = time.time()
        if email_enabled and (now - last_email_ts) >= EMAIL_POLL_INTERVAL:
            try:
                for name, amount, n in check_venmo_emails(email_addr, app_password, seen_uids):
                    put("entry", name=name, amount_str=f"${amount:.2f}", count=n, source="Venmo")
            except Exception as e:
                put("log", text=f"Email error: {e}")
            last_email_ts = now

        stop_evt.wait(timeout=max(poll_ms / 1000, YT_POLL_INTERVAL))

    put("status", text="Stopped.")


# ── Custom widgets ────────────────────────────────────────────────────────────
class DarkEntry(tk.Entry):
    """Styled dark-theme entry field."""
    def __init__(self, parent, **kw):
        kw.setdefault("bg",                C_SURFACE2)
        kw.setdefault("fg",                C_TEXT)
        kw.setdefault("insertbackground",  C_TEXT)
        kw.setdefault("relief",            "flat")
        kw.setdefault("font",              (FONT, 13))
        kw.setdefault("highlightthickness", 1)
        kw.setdefault("highlightbackground", C_BORDER)
        kw.setdefault("highlightcolor",    C_ORANGE)
        super().__init__(parent, **kw)


class OrangeButton(tk.Button):
    """Primary CTA button."""
    def __init__(self, parent, **kw):
        kw.setdefault("bg",               C_ORANGE)
        kw.setdefault("fg",               C_WHITE)
        kw.setdefault("activebackground", C_ORANGE_HV)
        kw.setdefault("activeforeground", C_WHITE)
        kw.setdefault("relief",           "flat")
        kw.setdefault("cursor",           "hand2")
        kw.setdefault("font",             (FONT, 13, "bold"))
        kw.setdefault("padx",             16)
        kw.setdefault("pady",             8)
        kw.setdefault("bd",               0)
        super().__init__(parent, **kw)


class GhostButton(tk.Button):
    """Secondary / stop button."""
    def __init__(self, parent, **kw):
        kw.setdefault("bg",               C_SURFACE2)
        kw.setdefault("fg",               C_MUTED)
        kw.setdefault("activebackground", C_BORDER)
        kw.setdefault("activeforeground", C_TEXT)
        kw.setdefault("relief",           "flat")
        kw.setdefault("cursor",           "hand2")
        kw.setdefault("font",             (FONT, 13))
        kw.setdefault("padx",             16)
        kw.setdefault("pady",             8)
        kw.setdefault("bd",               0)
        super().__init__(parent, **kw)


# ── App ───────────────────────────────────────────────────────────────────────
class WheelTrackerApp:
    def __init__(self, root: tk.Tk):
        self.root       = root
        self.root.title("Hack & Mack — Wheel Tracker")
        self.root.configure(bg=C_BG)
        self.root.resizable(True, True)

        self.cfg        = load_config()
        self.entries:   list[str] = []
        self.msg_queue  = queue.Queue()
        self.stop_event = threading.Event()
        self.thread:    threading.Thread | None = None
        self.running    = False
        self._logo_img  = None  # keep reference to avoid GC

        self._build_ui()
        self._poll_queue()

    # ── Build UI ──────────────────────────────────────────────────────────────
    def _build_ui(self):
        root = self.root

        # ── Header ────────────────────────────────────────────────────────────
        header = tk.Frame(root, bg=C_SURFACE, pady=0)
        header.pack(fill="x")

        # Thin orange top bar
        tk.Frame(header, bg=C_ORANGE, height=3).pack(fill="x")

        inner_header = tk.Frame(header, bg=C_SURFACE, padx=20, pady=16)
        inner_header.pack(fill="x")

        # Logo (if image file is present)
        logo_loaded = False
        for logo_name in ("logo.png", "logo.jpg", "logo.jpeg", "hack-and-mack-logo.png"):
            logo_path = resource_path(logo_name)
            if PIL_AVAILABLE and os.path.exists(logo_path):
                try:
                    img = Image.open(logo_path).convert("RGBA")
                    img = img.resize((48, 48), Image.LANCZOS)
                    # Circular crop for clean look
                    mask = Image.new("L", img.size, 0)
                    draw = ImageDraw.Draw(mask)
                    draw.ellipse((0, 0, img.size[0], img.size[1]), fill=255)
                    img.putalpha(mask)
                    self._logo_img = ImageTk.PhotoImage(img)
                    tk.Label(inner_header, image=self._logo_img,
                             bg=C_SURFACE).pack(side="left", padx=(0, 14))
                    logo_loaded = True
                    break
                except Exception:
                    pass

        title_col = tk.Frame(inner_header, bg=C_SURFACE)
        title_col.pack(side="left")
        tk.Label(title_col, text="Hack & Mack",
                 font=(FONT, 20, "bold"), fg=C_ORANGE,
                 bg=C_SURFACE).pack(anchor="w")
        tk.Label(title_col, text="Wheel Entry Tracker",
                 font=(FONT, 12), fg=C_MUTED,
                 bg=C_SURFACE).pack(anchor="w")

        # ── Warning banner ─────────────────────────────────────────────────────
        warn = tk.Frame(root, bg=C_WARN_BG, padx=20, pady=10)
        warn.pack(fill="x")
        tk.Label(warn,
                 text="⚠  Start tracking as soon as your stream goes live — "
                      "BEFORE the wheel segment begins. "
                      "Contributions made before you click Start won't be captured.",
                 font=(FONT, 11), fg=C_WARN_FG, bg=C_WARN_BG,
                 justify="left", wraplength=540).pack(anchor="w")

        # ── Settings card ──────────────────────────────────────────────────────
        self._section_label(root, "Settings")
        settings = tk.Frame(root, bg=C_SURFACE, padx=20, pady=16)
        settings.pack(fill="x", padx=16, pady=(0, 4))

        self.var_api_key   = tk.StringVar(value=self.cfg.get("api_key", ""))
        self.var_email     = tk.StringVar(value=self.cfg.get("email", ""))
        self.var_password  = tk.StringVar(value=self.cfg.get("app_password", ""))

        self._field(settings, 0, "YouTube API Key",    self.var_api_key)
        self._field(settings, 1, "Gmail Address",      self.var_email)
        self._field(settings, 2, "Gmail App Password", self.var_password, password=True)

        tk.Label(settings,
                 text="Saved automatically. Only needed once.",
                 font=(FONT, 10), fg=C_BORDER, bg=C_SURFACE
                 ).grid(row=3, column=1, sticky="w", padx=(10, 0), pady=(2, 0))

        # ── Stream URL card ────────────────────────────────────────────────────
        self._section_label(root, "Stream")
        stream = tk.Frame(root, bg=C_SURFACE, padx=20, pady=16)
        stream.pack(fill="x", padx=16, pady=(0, 4))

        tk.Label(stream, text="YouTube URL or Video ID",
                 font=(FONT, 11), fg=C_MUTED, bg=C_SURFACE
                 ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))

        self.var_url   = tk.StringVar()
        self.url_entry = DarkEntry(stream, textvariable=self.var_url, width=36)
        self.url_entry.grid(row=1, column=0, sticky="w")

        self.start_btn = OrangeButton(stream, text="▶  Start Tracking",
                                      command=self._on_start)
        self.start_btn.grid(row=1, column=1, padx=(10, 0))

        self.stop_btn = GhostButton(stream, text="■  Stop",
                                    command=self._on_stop, state="disabled")
        self.stop_btn.grid(row=1, column=2, padx=(6, 0))

        # Status dot + text
        status_row = tk.Frame(stream, bg=C_SURFACE)
        status_row.grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))

        self.dot_canvas = tk.Canvas(status_row, width=10, height=10,
                                    bg=C_SURFACE, highlightthickness=0)
        self.dot_canvas.pack(side="left", padx=(0, 6))
        self._dot = self.dot_canvas.create_oval(1, 1, 9, 9, fill=C_BORDER, outline="")

        self.status_var = tk.StringVar(value="Ready")
        tk.Label(status_row, textvariable=self.status_var,
                 font=(FONT, 11), fg=C_MUTED, bg=C_SURFACE).pack(side="left")

        # ── Entries card ───────────────────────────────────────────────────────
        self._section_label(root, "Wheel Entries")
        entries_card = tk.Frame(root, bg=C_SURFACE, padx=20, pady=16)
        entries_card.pack(fill="both", expand=True, padx=16, pady=(0, 4))

        # Count badge row
        count_row = tk.Frame(entries_card, bg=C_SURFACE)
        count_row.pack(fill="x", pady=(0, 8))

        self.count_var = tk.StringVar(value="0 entries")
        tk.Label(count_row, textvariable=self.count_var,
                 font=(FONT, 12, "bold"), fg=C_TEXT, bg=C_SURFACE).pack(side="left")

        tk.Label(count_row,
                 text="  ·  every ~$5 = 1 entry",
                 font=(FONT, 10), fg=C_MUTED, bg=C_SURFACE).pack(side="left")

        # Names box
        self.names_box = scrolledtext.ScrolledText(
            entries_card,
            font=("Menlo", 13),
            bg=C_SURFACE2, fg=C_TEXT,
            insertbackground=C_TEXT,
            selectbackground=C_ORANGE,
            selectforeground=C_WHITE,
            relief="flat",
            highlightthickness=1,
            highlightbackground=C_BORDER,
            highlightcolor=C_ORANGE,
            width=50, height=7,
            state="disabled",
            padx=10, pady=8,
        )
        self.names_box.pack(fill="both", expand=True)

        # ── Bottom bar ─────────────────────────────────────────────────────────
        bottom = tk.Frame(root, bg=C_BG, padx=16, pady=14)
        bottom.pack(fill="x")

        self.copy_btn = OrangeButton(
            bottom,
            text="📋  Copy Names to Clipboard",
            command=self._on_copy,
            state="disabled",
            font=(FONT, 14, "bold"),
            padx=20, pady=10,
        )
        self.copy_btn.pack(side="left")

        self.copy_label = tk.Label(bottom, text="",
                                   font=(FONT, 12), fg=C_GREEN, bg=C_BG)
        self.copy_label.pack(side="left", padx=(12, 0))

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _section_label(self, parent, text: str):
        row = tk.Frame(parent, bg=C_BG)
        row.pack(fill="x", padx=16, pady=(12, 4))
        tk.Label(row, text=text.upper(),
                 font=(FONT, 10, "bold"), fg=C_MUTED,
                 bg=C_BG, padx=2).pack(side="left")
        tk.Frame(row, bg=C_BORDER, height=1).pack(
            side="left", fill="x", expand=True, padx=(8, 0))

    def _field(self, parent, row: int, label: str,
               var: tk.StringVar, password=False):
        tk.Label(parent, text=label,
                 font=(FONT, 12), fg=C_MUTED, bg=C_SURFACE,
                 anchor="e", width=18
                 ).grid(row=row, column=0, sticky="e", pady=5)
        e = DarkEntry(parent, textvariable=var, width=36,
                      show="•" if password else "")
        e.grid(row=row, column=1, sticky="w", padx=(10, 0), pady=5)

    def _set_dot(self, color: str):
        self.dot_canvas.itemconfig(self._dot, fill=color)

    # ── Handlers ──────────────────────────────────────────────────────────────
    def _on_start(self):
        api_key      = self.var_api_key.get().strip()
        email_addr   = self.var_email.get().strip()
        app_password = self.var_password.get().strip()
        raw_url      = self.var_url.get().strip()

        if not api_key:
            messagebox.showerror("Missing API Key",
                                 "Please enter your YouTube API Key in Settings.")
            return
        if not raw_url:
            messagebox.showerror("Missing URL",
                                 "Please paste your YouTube stream URL or video ID.")
            return
        if not GOOGLE_API_AVAILABLE:
            messagebox.showerror("Missing Dependency",
                                 "Run:  pip install google-api-python-client")
            return

        save_config({"api_key": api_key, "email": email_addr,
                     "app_password": app_password})

        self.entries.clear()
        self._refresh_names_box()

        self.stop_event.clear()
        self.running = True
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.copy_btn.config(state="disabled")
        self.url_entry.config(state="disabled")
        self._set_dot(C_WARN_FG)
        self.status_var.set("Connecting…")

        self.thread = threading.Thread(
            target=tracker_thread,
            args=(api_key, email_addr, app_password,
                  extract_video_id(raw_url),
                  self.msg_queue, self.stop_event),
            daemon=True
        )
        self.thread.start()

    def _on_stop(self):
        self.stop_event.set()
        self.running = False
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.url_entry.config(state="normal")
        self._set_dot(C_BORDER)
        if self.entries:
            self.copy_btn.config(state="normal")

    def _on_copy(self):
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(self.entries))
        self.copy_label.config(text="✓  Copied!")
        self.root.after(2500, lambda: self.copy_label.config(text=""))

    # ── Queue polling ──────────────────────────────────────────────────────────
    def _poll_queue(self):
        try:
            while True:
                msg  = self.msg_queue.get_nowait()
                kind = msg["kind"]

                if kind == "entry":
                    name  = msg["name"]
                    n     = msg["count"]
                    src   = msg["source"]
                    amt   = msg["amount_str"]
                    self.entries.extend([name] * n)
                    self._refresh_names_box()
                    label = "entry" if n == 1 else "entries"
                    self.status_var.set(
                        f"{src}: {name}  {amt}  →  {n} {label}"
                        f"  ·  {len(self.entries)} total"
                    )
                    self._set_dot(C_GREEN)
                    self.copy_btn.config(state="normal")
                    self.root.after(3000, lambda: self._set_dot(C_ORANGE))

                elif kind == "status":
                    self.status_var.set(msg["text"])
                    if "Connected" in msg["text"]:
                        self._set_dot(C_ORANGE)

                elif kind == "log":
                    self.status_var.set(msg["text"])

                elif kind == "error":
                    self._on_stop()
                    self._set_dot(C_BORDER)
                    messagebox.showerror("Error", msg["text"])

        except queue.Empty:
            pass

        self.root.after(300, self._poll_queue)

    def _refresh_names_box(self):
        self.names_box.config(state="normal")
        self.names_box.delete("1.0", "end")
        self.names_box.insert("end", "\n".join(self.entries))
        self.names_box.config(state="disabled")
        self.names_box.see("end")
        count = len(self.entries)
        self.count_var.set(f"{count} {'entry' if count == 1 else 'entries'}")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    root = tk.Tk()
    root.geometry("620x820")
    root.minsize(600, 750)
    WheelTrackerApp(root)
    root.mainloop()
