#!/usr/bin/env python3
"""
Arnold Alert — Restoke Incomplete Checklist Notifier
-----------------------------------------------------
Polls Gmail for Restoke "Procedure was not completed" emails,
generates an Arnold Schwarzenegger voice message via ElevenLabs,
and places an outbound Twilio call to the relevant store.

Run on a schedule (every 10 min) via launchd — see README in this folder.
"""

import imaplib
import email
import re
import os
import sys
import json
import logging
import requests
from email.header import decode_header
from datetime import datetime
from pathlib import Path

# ── Load .env manually (no external dep needed) ───────────────────────────────
def load_env(env_path):
    if not Path(env_path).exists():
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))

load_env(Path(__file__).parent / ".env")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("arnold-alert")

# ── Credentials ───────────────────────────────────────────────────────────────
GMAIL_ADDRESS    = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASS   = os.environ.get("GMAIL_APP_PASSWORD", "")
ELEVENLABS_KEY   = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE = os.environ.get("ELEVENLABS_VOICE_ID", "")
TWILIO_SID       = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN     = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM      = os.environ.get("TWILIO_PHONE_NUMBER", "")

# ── Store phone map ───────────────────────────────────────────────────────────
STORE_PHONES = {
    "glenelg": "+61882694075",
    "brighton": "+61883678417",
}

# ── Business hours (Adelaide / ACST) ─────────────────────────────────────────
HOUR_START = 12  # 12 pm
HOUR_END   = 22  # 10 pm (covers 9:30pm cutoff)

# ── State file — tracks processed email IDs to avoid repeat calls ─────────────
STATE_FILE = Path(__file__).parent / ".processed_emails.json"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def validate_credentials():
    missing = [k for k, v in {
        "GMAIL_ADDRESS":     GMAIL_ADDRESS,
        "GMAIL_APP_PASSWORD":GMAIL_APP_PASS,
        "ELEVENLABS_API_KEY":ELEVENLABS_KEY,
        "ELEVENLABS_VOICE_ID":ELEVENLABS_VOICE,
        "TWILIO_ACCOUNT_SID":TWILIO_SID,
        "TWILIO_AUTH_TOKEN": TWILIO_TOKEN,
        "TWILIO_PHONE_NUMBER":TWILIO_FROM,
    }.items() if not v or "REPLACE" in v]
    if missing:
        log.error("Missing credentials in .env: %s", ", ".join(missing))
        sys.exit(1)


def is_business_hours():
    """Return True if current Adelaide time is between 7am and 10pm."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Australia/Adelaide")
    except Exception:
        try:
            import pytz
            tz = pytz.timezone("Australia/Adelaide")
        except Exception:
            # Fallback: UTC+9:30, close enough
            from datetime import timezone, timedelta
            tz = timezone(timedelta(hours=9, minutes=30))
    now = datetime.now(tz)
    return HOUR_START <= now.hour < HOUR_END


def decode_header_str(value):
    """Decode an email header value to a plain string."""
    if value is None:
        return ""
    parts = decode_header(value)
    result = []
    for part, enc in parts:
        if isinstance(part, bytes):
            result.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result)


def load_processed():
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()))
        except Exception:
            pass
    return set()


def save_processed(ids):
    STATE_FILE.write_text(json.dumps(sorted(ids), indent=2))


# ─────────────────────────────────────────────────────────────────────────────
# Gmail
# ─────────────────────────────────────────────────────────────────────────────

def fetch_restoke_emails():
    """
    Connect to Gmail via IMAP and return a list of (msg_id, subject) tuples
    for Restoke incomplete-checklist emails not yet processed.
    Checks both INBOX (unread) and All Mail (today's emails, catches archived).
    """
    processed = load_processed()
    results = []
    seen_msg_ids = set()

    log.info("Connecting to Gmail IMAP...")
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_ADDRESS, GMAIL_APP_PASS)

    def scan_folder(folder, search_criteria):
        try:
            mail.select(folder)
        except Exception as e:
            log.warning("Could not select folder %s: %s", folder, e)
            return

        _, data = mail.search(None, search_criteria)
        ids = data[0].split() if data[0] else []
        log.info("%s: %d candidate(s).", folder, len(ids))

        for eid in ids:
            _, msg_data = mail.fetch(eid, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])

            # Use Message-ID header as a globally unique key
            msg_id  = msg.get("Message-ID", f"{folder}-{eid.decode()}").strip()
            subject = decode_header_str(msg.get("Subject", ""))
            sender  = decode_header_str(msg.get("From", ""))

            # Deduplicate within this run (All Mail includes inbox emails too)
            if msg_id in seen_msg_ids:
                continue
            seen_msg_ids.add(msg_id)

            # Already handled in a previous run
            if msg_id in processed:
                continue

            if "restoke" not in sender.lower():
                log.info("Skipping — not from Restoke: %s", sender)
                processed.add(msg_id)
                continue

            if "procedure was not completed" not in subject.lower():
                processed.add(msg_id)
                continue

            log.info("Matched Restoke email: %s", subject)
            results.append((msg_id, subject))

    # Search 1: INBOX — unread only (fast path, most common case)
    scan_folder("inbox", '(UNSEEN SUBJECT "Procedure was not completed")')

    # Search 2: All Mail — today's AND yesterday's emails in Adelaide time.
    # Uses Adelaide local date (not UTC) so the search matches the correct calendar day.
    # Also checks yesterday to catch late-night emails where Adelaide date differs from UTC date.
    try:
        from zoneinfo import ZoneInfo as _ZoneInfo
        _tz = _ZoneInfo("Australia/Adelaide")
    except Exception:
        try:
            import pytz as _pytz
            _tz = _pytz.timezone("Australia/Adelaide")
        except Exception:
            from datetime import timezone as _tz_mod, timedelta as _td
            _tz = _tz_mod(_td(hours=9, minutes=30))
    from datetime import timedelta as _timedelta
    _now_adl = datetime.now(_ty)
    today_str     = _now_adl.strftime("%d-%b-%Y")
    yesterday_str = (_now_adl - _timedelta(days=1)).strftime("%d-%b-%Y")
    log.info("Searching All Mail for Adelaide dates: %s and %s", today_str, yesterday_str)
    scan_folder('"[Gmail]/All Mail"', f'(ON {today_str} SUBJECT "Procedure was not completed")')
    scan_folder('"[Gmail]/All Mail"', f'(ON {yesterday_str} SUBJECT "Procedure was not completed")')

    mail.logout()
    return results, processed


def mark_email_read(msg_id):
    """Find the email by Message-ID in All Mail and mark it as read."""
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_ADDRESS, GMAIL_APP_PASS)
        mail.select('"[Gmail]/All Mail"')
        # Strip angle brackets for the search
        clean_id = msg_id.strip("<>").replace('"', "")
        _, data = mail.search(None, f'HEADER Message-ID "{clean_id}"')
        ids = data[0].split() if data[0] else []
        for eid in ids:
            mail.store(eid, "+FLAGS", "\\Seen")
        mail.logout()
        log.info("Marked email as read: %s", msg_id[:60])
    except Exception as e:
        log.warning("Could not mark email as read: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# Subject parser
# ─────────────────────────────────────────────────────────────────────────────

SUBJECT_PATTERN = re.compile(
    r"^(.+?)\s*-\s*Procedure was not completed\s*-\s*(.+)$",
    re.IGNORECASE,
)

def parse_subject(subject):
    """Return (location, checklist_name) or (None, None)."""
    m = SUBJECT_PATTERN.match(subject.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, None


def get_phone_number(location):
    """Map location string to store phone number."""
    loc_lower = location.lower()
    for keyword, phone in STORE_PHONES.items():
        if keyword in loc_lower:
            return phone
    log.warning("No store match for location '%s' — defaulting to Brighton.", location)
    return STORE_PHONES["brighton"]


# ─────────────────────────────────────────────────────────────────────────────
# ElevenLabs TTS
# ─────────────────────────────────────────────────────────────────────────────

ARNOLD_TEMPLATE = (
    "Listen up! This is an urgent alert for {location}. "
    "The {checklist} checklist has NOT been completed. "
    "I repeat — the {checklist} checklist was NOT done. "
    "This is completely unacceptable. "
    "Stop what you are doing and complete it NOW. "
    "I will be back to check. That is a promise."
)

def generate_arnold_audio(location, checklist):
    """Generate Arnold TTS via ElevenLabs. Returns raw mp3 bytes."""
    message = ARNOLD_TEMPLATE.format(location=location, checklist=checklist)
    log.info("Generating ElevenLabs audio for: %s", message[:60] + "...")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE}"
    headers = {
        "xi-api-key": ELEVENLABS_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {
        "text": message,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.45, "similarity_boost": 0.80},
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    log.info("ElevenLabs audio generated: %d bytes", len(resp.content))
    return resp.content


# ─────────────────────────────────────────────────────────────────────────────
# Audio hosting — file.io (free, no account needed, auto-expires after 1h)
# ─────────────────────────────────────────────────────────────────────────────

def host_audio(audio_bytes):
    """Upload mp3 and return a public URL. Tries multiple hosts, returns None if all fail."""
    hosts = [
        ("litterbox.catbox.moe", lambda: requests.post(
            "https://litterbox.catbox.moe/resources/internals/api.php",
            data={"reqtype": "fileupload", "time": "1h"},
            files={"fileToUpload": ("arnold.mp3", audio_bytes, "audio/mpeg")},
            timeout=30,
        ).text.strip()),
        ("uguu.se", lambda: requests.post(
            "https://uguu.se/upload",
            files={"files[]": ("arnold.mp3", audio_bytes, "audio/mpeg")},
            timeout=30,
        ).json()["files"][0]["url"]),
        ("oshi.at", lambda: requests.post(
            "https://oshi.at",
            files={"f": ("arnold.mp3", audio_bytes, "audio/mpeg")},
            data={"expire": "60"},
            timeout=30,
        ).json()["url"]),
        ("file.io", lambda: requests.post(
            "https://file.io",
            files={"file": ("arnold.mp3", audio_bytes, "audio/mpeg")},
            data={"expires": "1h"},
            timeout=30,
        ).json().get("link", "")),
        ("transfer.sh", lambda: requests.put(
            "https://transfer.sh/arnold-alert.mp3",
            data=audio_bytes,
            headers={"Content-Type": "audio/mpeg", "Max-Days": "1"},
            timeout=30,
        ).text.strip()),
    ]
    for label, fn in hosts:
        try:
            log.info("Uploading audio to %s...", label)
            url = fn()
            if url and url.startswith("http"):
                log.info("Audio hosted at: %s", url)
                return url
            raise RuntimeError(f"Unexpected response: {url}")
        except Exception as e:
            log.warning("%s failed: %s", label, e)
    log.error("All audio hosts failed — will fall back to <Say>.")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Twilio call
# ─────────────────────────────────────────────────────────────────────────────

def make_twilio_call(to_number, audio_url, location="the store", checklist="unknown"):
    """Place an outbound Twilio call. Uses Arnold audio if available, else <Say>."""
    log.info("Placing Twilio call to %s...", to_number)
    if audio_url:
        twiml = f"<Response><Play>{audio_url}</Play></Response>"
    else:
        msg = (f"Alert. The {checklist} checklist at {location} was not completed. "
               "Please action this immediately.")
        twiml = f"<Response><Say>{msg}</Say></Response>"
        log.warning("Using <Say> fallback for this call.")

    resp = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Calls.json",
        auth=(TWILIO_SID, TWILIO_TOKEN),
        data={
            "To":    to_number,
            "From":  TWILIO_FROM,
            "Twiml": twiml,
        },
        timeout=30,
    )
    resp.raise_for_status()
    call_sid = resp.json().get("sid", "unknown")
    log.info("Twilio call placed — SID: %s", call_sid)
    return call_sid


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    validate_credentials()

    if not is_business_hours():
        log.info("Outside business hours (7am–10pm Adelaide) — nothing to do.")
        return

    emails, processed = fetch_restoke_emails()

    if not emails:
        log.info("No new Restoke incomplete checklist emails.")
        save_processed(processed)
        return

    for email_id, subject in emails:
        log.info("─" * 60)
        log.info("Processing email: %s", subject)

        location, checklist = parse_subject(subject)
        if not location or not checklist:
            log.warning("Could not parse subject — skipping. Subject: %s", subject)
            processed.add(email_id)
            continue

        log.info("Location:  %s", location)
        log.info("Checklist: %s", checklist)

        phone = get_phone_number(location)
        log.info("Target phone: %s", phone)

        # Try to generate Arnold audio; fall back to <Say> if ElevenLabs fails
        audio_url = None
        try:
            audio_bytes = generate_arnold_audio(location, checklist)
            audio_url   = host_audio(audio_bytes)
        except requests.HTTPError as e:
            log.warning("ElevenLabs HTTP error (will use <Say> fallback): %s — %s",
                        e, e.response.text if e.response else "")
        except Exception as e:
            log.warning("ElevenLabs error (will use <Say> fallback): %s", e)

        try:
            call_sid = make_twilio_call(phone, audio_url, location, checklist)
            log.info("✅  Call placed successfully — SID: %s", call_sid)
        except requests.HTTPError as e:
            log.error("Twilio HTTP error: %s — %s", e, e.response.text if e.response else "")
            continue
        except Exception as e:
            log.error("Twilio unexpected error: %s", e)
            continue

        processed.add(email_id)
        mark_email_read(email_id)

    save_processed(processed)
    log.info("─" * 60)
    log.info("Done.")


if __name__ == "__main__":
    main()
