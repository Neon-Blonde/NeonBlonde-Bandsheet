#!/usr/bin/env python3
"""
Neon Blonde Daily Check
Runs 3 checks and emails a status report to neonblondevc@gmail.com
"""

import json
import os
import base64
import requests
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ── Config ──────────────────────────────────────────────────────────────────
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "check_token.json"
BANDSHEET_URL = "https://neon-blonde.github.io/NeonBlonde-Bandsheet/bandsheet-data.json"
ACTIONS_URL = "https://api.github.com/repos/Neon-Blonde/NeonBlonde-Bandsheet/actions/runs?per_page=5"
REPORT_TO = ["neonblondevc@gmail.com"]
SEND_FROM = "neonblondevc@gmail.com"
PT_TZ = ZoneInfo("America/Los_Angeles")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]

KNOWN_VENUES = [
    "leashless", "sewer", "parquee", "parque", "cruisery", "figueroa",
    "fig mountain", "tonys", "tony's", "fess parker", "babaloo",
    "fox wine", "m special", "cruisery"
]


# ── Auth ─────────────────────────────────────────────────────────────────────
def get_credentials():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds


# ── Check 1: Band Sheet Health ───────────────────────────────────────────────
def check_bandsheet_health():
    lines = []
    status = "GREEN"

    try:
        r = requests.get(BANDSHEET_URL, timeout=10)
        data = r.json()
        updated_str = data.get("updated", "")
        gig_count = len(data.get("booked_gigs", []))
        out_count = len(data.get("members_out", []))

        # Parse timestamp
        try:
            updated = datetime.strptime(updated_str, "%B %d, %Y @ %I:%M %p PT")
            updated = updated.replace(tzinfo=PT_TZ)
            age_hours = (datetime.now(PT_TZ) - updated).total_seconds() / 3600
            if age_hours > 25:
                status = "CRITICAL"
                lines.append(f"CRITICAL: Band sheet is {age_hours:.0f} hours old — workflow likely failed")
            elif age_hours > 20:
                status = "WARNING"
                lines.append(f"WARNING: Band sheet is {age_hours:.0f} hours old — approaching stale threshold")
            else:
                lines.append(f"Last updated: {updated_str} ({age_hours:.0f}h ago)")
        except Exception:
            lines.append(f"Last updated: {updated_str}")

        lines.append(f"{gig_count} booked gigs, {out_count} member out entries")

    except Exception as e:
        status = "CRITICAL"
        lines.append(f"CRITICAL: Could not fetch band sheet — {e}")

    # GitHub Actions status
    try:
        r = requests.get(ACTIONS_URL, timeout=10)
        runs = r.json().get("workflow_runs", [])
        if runs:
            last = runs[0]
            conclusion = last.get("conclusion", "unknown")
            name = last.get("name", "")
            ran_at = last.get("updated_at", "")
            if conclusion == "success":
                lines.append(f"GitHub Actions: PASSED ({name} @ {ran_at[:10]})")
            elif conclusion in ("failure", "cancelled"):
                status = "CRITICAL" if status != "CRITICAL" else status
                lines.append(f"GitHub Actions: FAILED ({name} @ {ran_at[:10]}) — check Actions tab")
            else:
                lines.append(f"GitHub Actions: {conclusion} ({name})")
        else:
            lines.append("GitHub Actions: No runs found")
    except Exception as e:
        lines.append(f"GitHub Actions: Could not check — {e}")

    return status, "\n".join(lines)


# ── Check 2: Email Scan ──────────────────────────────────────────────────────
def check_emails(gmail):
    lines = []
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime("%Y/%m/%d")

    venue_query = " OR ".join([f'"{v}"' for v in KNOWN_VENUES])
    queries = [
        f"after:{cutoff} ({venue_query} OR booking OR gig OR schedule OR cancel OR confirm)",
    ]

    flagged = []
    for q in queries:
        try:
            results = gmail.users().messages().list(userId="me", q=q, maxResults=20).execute()
            messages = results.get("messages", [])
            for m in messages:
                msg = gmail.users().messages().get(userId="me", id=m["id"], format="metadata",
                    metadataHeaders=["From", "Subject", "Date"]).execute()
                headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
                subject = headers.get("Subject", "(no subject)")
                sender = headers.get("From", "unknown")
                date = headers.get("Date", "")

                # Simple urgency heuristic
                low = subject.lower()
                if any(w in low for w in ["cancel", "urgent", "asap", "today", "tomorrow"]):
                    flag = "URGENT"
                elif any(w in low for w in ["confirm", "request", "available", "book", "date"]):
                    flag = "NEEDS REPLY"
                else:
                    flag = "INFO"

                flagged.append(f"  [{flag}] {sender}\n    Subject: {subject}\n    Date: {date}")
        except Exception as e:
            lines.append(f"  Error scanning inbox: {e}")

    if flagged:
        lines.extend(flagged)
    else:
        lines.append("  No schedule-related emails in the last 48 hours")

    return "\n".join(lines)


# ── Check 3: Calendar Consistency ───────────────────────────────────────────
def check_calendar(calendar, bandsheet_gigs):
    lines = []
    now = datetime.now(timezone.utc).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(days=180)).isoformat()

    try:
        events_result = calendar.events().list(
            calendarId="neonblondevc@gmail.com",
            timeMin=now,
            timeMax=future,
            maxResults=100,
            singleEvents=True,
            orderBy="startTime"
        ).execute()
        events = events_result.get("items", [])

        out_dates = set()
        out_events = []
        gig_dates = set()

        for e in events:
            summary = e.get("summary", "").lower()
            start = e.get("start", {}).get("date") or e.get("start", {}).get("dateTime", "")[:10]
            if any(w in summary for w in ["out", "unavailable", "off", "vacation", "absent"]):
                out_dates.add(start)
                out_events.append((e.get("summary", ""), start))
            else:
                gig_dates.add(start)

        # Check for member out conflicts with gig dates
        conflicts = []
        for name, date in out_events:
            if date in gig_dates:
                conflicts.append(f"  CONFLICT: {name} is out on {date} — check gig that day")

        if conflicts:
            lines.extend(conflicts)
        else:
            lines.append("  No member-out conflicts with gig dates")

        lines.append(f"  {len([e for e in events if not any(w in e.get('summary','').lower() for w in ['out','unavailable','off','vacation','absent'])])} gigs on calendar")

    except Exception as e:
        lines.append(f"  Error checking calendar: {e}")

    return "\n".join(lines)


# ── Send Email ───────────────────────────────────────────────────────────────
def send_report(gmail, subject, body):
    message = MIMEText(body)
    message["to"] = ", ".join(REPORT_TO)
    message["from"] = SEND_FROM
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    gmail.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"[OK] Report sent: {subject}")


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("NEON BLONDE DAILY CHECK")
    print("=" * 60)

    creds = get_credentials()
    gmail = build("gmail", "v1", credentials=creds)
    calendar = build("calendar", "v3", credentials=creds)

    today = datetime.now(PT_TZ).strftime("%B %d, %Y")

    # Run checks
    print("[1] Band sheet health...")
    bs_status, bs_report = check_bandsheet_health()

    print("[2] Email scan...")
    email_report = check_emails(gmail)

    # Get bandsheet gigs for check 3
    try:
        bandsheet_gigs = requests.get(BANDSHEET_URL, timeout=10).json().get("booked_gigs", [])
    except Exception:
        bandsheet_gigs = []

    print("[3] Calendar consistency...")
    cal_report = check_calendar(calendar, bandsheet_gigs)

    # Determine subject
    issues = bs_status != "GREEN" or "CONFLICT" in cal_report or "NEEDS REPLY" in email_report or "URGENT" in email_report
    subject = f"Neon Blonde Daily Check - {'Action Needed' if issues else 'All Clear'} {today}"

    # Upcoming gigs (next 30 days)
    upcoming = []
    today_dt = datetime.now(PT_TZ).date()
    for gig in bandsheet_gigs:
        try:
            date_str = gig.split(" ")[1]  # e.g. "5-16-2026"
            parts = date_str.split("-")
            gig_date = datetime(int(parts[2]), int(parts[0]), int(parts[1])).date()
            if gig_date <= today_dt + timedelta(days=30):
                upcoming.append(f"  {gig}")
        except Exception:
            pass

    body = f"""NEON BLONDE - DAILY STATUS REPORT
{today} @ 8AM PT

BAND SHEET STATUS: {bs_status}
{bs_report}

EMAIL SCAN:
{email_report}

SCHEDULE CONFLICTS:
{cal_report}

UPCOMING GIGS (next 30 days):
{chr(10).join(upcoming) if upcoming else '  None in the next 30 days'}

---
Neon Blonde Validation Agent
"""

    print("[4] Sending report...")
    send_report(gmail, subject, body)
    print("[DONE]")


if __name__ == "__main__":
    main()
