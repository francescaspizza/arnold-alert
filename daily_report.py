#!/usr/bin/env python3
"""
Arnold Alert — Daily Report
----------------------------
Reads today's log, summarises what happened, and emails it to Aman.
Runs nightly at 10pm via launchd.
"""

import os, re, smtplib, sys
from pathlib import Path
from datetime import datetime, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ── Load .env ────────────────────────────────────────────────────────────────
def load_env(env_path):
    if not Path(env_path).exists():
        return  # Running in GitHub Actions — credentials come from environment secrets
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

load_env(Path(__file__).parent / ".env")

GMAIL_ADDRESS  = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASS = os.environ.get("GMAIL_APP_PASSWORD", "")
LOG_FILE       = Path(__file__).parent / "arnold_alert.log"
TODAY          = date.today().strftime("%Y-%m-%d")
REPORT_TO      = GMAIL_ADDRESS  # sends to yourself


# ── Parse today's log ────────────────────────────────────────────────────────
def parse_log():
    if not LOG_FILE.exists():
        return [], [], [], []  # No log — GitHub Actions doesn't write a local log file

    runs, calls, errors, checklist_hits = [], [], [], []

    with open(LOG_FILE) as f:
        for line in f:
            if not line.startswith(TODAY):
                continue

            # Each time the script ran
            if "Connecting to Gmail IMAP" in line:
                time_str = line.split()[1]
                runs.append(time_str)

            # Successful calls placed
            if "Call placed successfully" in line or "✅  Call placed" in line:
                calls.append(line.strip())

            # Matched a Restoke email
            if "Matched Restoke email" in line:
                subject = line.split("Matched Restoke email:")[-1].strip()
                checklist_hits.append(subject)

            # Any errors
            if "ERROR" in line:
                errors.append(line.strip())

    return runs, calls, errors, checklist_hits


# ── Build email ───────────────────────────────────────────────────────────────
def build_email(runs, calls, errors, checklist_hits):
    today_str = date.today().strftime("%A, %d %B %Y")

    if runs:
        run_status = f"✅  Ran {len(runs)} time(s) today at: {', '.join(runs)}"
    else:
        run_status = "⚠️   No runs detected today (log not available — this is normal when running via GitHub Actions)."

    if checklist_hits:
        hit_lines = "\n".join(f"  • {h}" for h in checklist_hits)
        hit_section = f"🚨  Missed checklists detected ({len(checklist_hits)}):\n{hit_lines}"
    else:
        hit_section = "✅  No missed checklists detected today."

    if calls:
        call_lines = "\n".join(f"  • {c}" for c in calls)
        call_section = f"📞  Calls placed ({len(calls)}):\n{call_lines}"
    else:
        call_section = "📞  No calls were needed today." if not checklist_hits else "⚠️   Calls were expected but none recorded — check the error log."

    if errors:
        err_lines = "\n".join(f"  • {e}" for e in errors[-5:])  # last 5 errors
        error_section = f"❌  Errors ({len(errors)}):\n{err_lines}"
    else:
        error_section = "✅  No errors."

    body = f"""Arnold Alert — Daily Report
{today_str}
{'─' * 50}

SYSTEM STATUS
{run_status}

CHECKLIST MONITORING
{hit_section}

CALLS
{call_section}

ERRORS
{error_section}

{'─' * 50}
Arnold Alert is monitoring: Glenelg & Brighton
Schedule: 11:35am · 3:35pm · 9:35pm (Adelaide time)
"""
    return body


# ── Send email ────────────────────────────────────────────────────────────────
def send_report(body):
    today_str = date.today().strftime("%d %b %Y")
    msg = MIMEMultipart()
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = REPORT_TO
    msg["Subject"] = f"Arnold Alert — Daily Report {today_str}"
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASS)
        smtp.sendmail(GMAIL_ADDRESS, REPORT_TO, msg.as_string())

    print(f"✅  Report sent to {REPORT_TO}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"Building Arnold Alert daily report for {TODAY}...")
    runs, calls, errors, hits = parse_log()
    body = build_email(runs, calls, errors, hits)
    print(body)
    send_report(body)

if __name__ == "__main__":
    main()
