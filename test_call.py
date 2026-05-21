#!/usr/bin/env python3
"""
Arnold Alert — Test Call Script
--------------------------------
Bypasses Gmail and directly places a test Arnold call to one or both stores.
Usage:
    python3 test_call.py             # calls both stores
    python3 test_call.py glenelg     # calls Glenelg only
    python3 test_call.py brighton    # calls Brighton only
"""

import os
import sys
import logging
import requests
from pathlib import Path

# ── Reuse .env loader from main script ───────────────────────────────────────
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
log = logging.getLogger("arnold-test")

# ── Credentials ───────────────────────────────────────────────────────────────
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

# ── Test message ──────────────────────────────────────────────────────────────
TEST_MESSAGE = (
    "Attention! This is a test call from Arnold Alert, "
    "the automated checklist monitoring system for Francesca's Pizza and Sandos. "
    "The system is working correctly. "
    "Please text Aman on 0 4 1 2, 9 5 0, 6 7 4 to confirm you received this call. "
    "I repeat — please text Aman when you get this call. "
    "Thank you, and get back to work."
)

def generate_audio():
    log.info("Generating ElevenLabs test audio...")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE}"
    headers = {
        "xi-api-key": ELEVENLABS_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {
        "text": TEST_MESSAGE,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.45, "similarity_boost": 0.80},
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    log.info("Audio generated: %d bytes", len(resp.content))
    return resp.content


def host_audio(audio_bytes):
    hosts = [
        ("litterbox.catbox.moe", lambda: requests.post(
            "https://litterbox.catbox.moe/resources/internals/api.php",
            data={"reqtype": "fileupload", "time": "1h"},
            files={"fileToUpload": ("arnold-test.mp3", audio_bytes, "audio/mpeg")},
            timeout=30,
        ).text.strip()),
        ("uguu.se", lambda: requests.post(
            "https://uguu.se/upload",
            files={"files[]": ("arnold-test.mp3", audio_bytes, "audio/mpeg")},
            timeout=30,
        ).json()["files"][0]["url"]),
        ("file.io", lambda: requests.post(
            "https://file.io",
            files={"file": ("arnold-test.mp3", audio_bytes, "audio/mpeg")},
            data={"expires": "1h"},
            timeout=30,
        ).json().get("link", "")),
    ]
    for label, fn in hosts:
        try:
            log.info("Uploading audio to %s...", label)
            url = fn()
            if url and url.startswith("http"):
                log.info("Audio hosted at: %s", url)
                return url
        except Exception as e:
            log.warning("%s failed: %s", label, e)
    log.error("All audio hosts failed — using <Say> fallback.")
    return None


def make_call(store_name, phone, audio_url):
    log.info("─" * 55)
    log.info("Calling %s (%s)...", store_name.upper(), phone)
    if audio_url:
        twiml = f"<Response><Play>{audio_url}</Play></Response>"
    else:
        twiml = f"<Response><Say>{TEST_MESSAGE}</Say></Response>"
    resp = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Calls.json",
        auth=(TWILIO_SID, TWILIO_TOKEN),
        data={"To": phone, "From": TWILIO_FROM, "Twiml": twiml},
        timeout=30,
    )
    resp.raise_for_status()
    call_sid = resp.json().get("sid", "unknown")
    log.info("✅  %s — Call SID: %s", store_name.upper(), call_sid)
    return call_sid


def main():
    # Determine which stores to call
    target = sys.argv[1].lower() if len(sys.argv) > 1 else "both"
    if target == "both":
        stores = list(STORE_PHONES.items())
    elif target in STORE_PHONES:
        stores = [(target, STORE_PHONES[target])]
    else:
        print(f"Unknown store '{target}'. Use: glenelg, brighton, or leave blank for both.")
        sys.exit(1)

    log.info("━" * 55)
    log.info("  Arnold Alert — TEST CALL")
    log.info("  Targets: %s", ", ".join(s[0].title() for s in stores))
    log.info("━" * 55)

    # Log credential presence (not values) to help diagnose failures
    log.info("Credential check — ElevenLabs key: %s, Voice ID: %s, Twilio SID: %s",
             "SET" if ELEVENLABS_KEY else "MISSING",
             "SET" if ELEVENLABS_VOICE else "MISSING",
             "SET" if TWILIO_SID else "MISSING")

    # Try to generate Arnold audio — fall back to <Say> if ElevenLabs fails
    audio_url = None
    try:
        audio_bytes = generate_audio()
        audio_url = host_audio(audio_bytes)
    except requests.HTTPError as e:
        log.error("ElevenLabs error: %s — %s", e, e.response.text if e.response else "")
        log.warning("Falling back to Twilio <Say> for all calls.")
    except Exception as e:
        log.error("Audio generation failed: %s", e)
        log.warning("Falling back to Twilio <Say> for all calls.")

    for store_name, phone in stores:
        try:
            make_call(store_name, phone, audio_url)
        except requests.HTTPError as e:
            log.error("HTTP error calling %s: %s — %s", store_name, e,
                      e.response.text if e.response else "")
        except Exception as e:
            log.error("Error calling %s: %s", store_name, e)

    log.info("━" * 55)
    log.info("  Test complete.")
    log.info("━" * 55)


if __name__ == "__main__":
    main()
