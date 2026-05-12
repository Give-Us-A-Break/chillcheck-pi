"""
ChillCheck — Notifications
===========================
Handles sending emails, SMS messages and phone calls.

Email:  SendGrid API
SMS:    Twilio SMS
Calls:  Twilio Voice (text-to-speech via TwiML)
"""

import os
import logging

log = logging.getLogger("chillcheck.notifications")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM_PHONE  = os.getenv("TWILIO_FROM_PHONE")
TWILIO_TWIML_URL   = os.getenv("TWILIO_TWIML_URL")   # Vercel API endpoint

SENDGRID_API_KEY   = os.getenv("SENDGRID_API_KEY")
EMAIL_FROM         = os.getenv("EMAIL_FROM", "alerts@chillcheck.online")
EMAIL_FROM_NAME    = os.getenv("EMAIL_FROM_NAME", "ChillCheck Alerts")


# ════════════════════════════════════════════════════════════
# EMAIL — SendGrid
# ════════════════════════════════════════════════════════════

def send_email(to: str, to_name: str, subject: str, body: str):
    """Send a plain-text alert email via SendGrid."""
    if not SENDGRID_API_KEY:
        log.warning("SENDGRID_API_KEY not set — email not sent")
        return

    try:
        import httpx

        payload = {
            "personalizations": [{
                "to": [{"email": to, "name": to_name}],
                "subject": subject,
            }],
            "from": {
                "email": EMAIL_FROM,
                "name":  EMAIL_FROM_NAME,
            },
            "content": [{
                "type":  "text/plain",
                "value": body,
            }],
        }

        response = httpx.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": f"Bearer {SENDGRID_API_KEY}",
                "Content-Type":  "application/json",
            },
            json=payload,
            timeout=10,
        )

        if response.status_code in (200, 202):
            log.info(f"Email sent to {to}")
        else:
            log.error(f"SendGrid error {response.status_code}: {response.text}")

    except Exception as e:
        log.error(f"Failed to send email to {to}: {e}")


# ════════════════════════════════════════════════════════════
# SMS — Twilio
# ════════════════════════════════════════════════════════════

def send_sms(to: str, message: str):
    """Send an SMS alert via Twilio."""
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_PHONE]):
        log.warning("Twilio credentials not configured — SMS not sent")
        return

    try:
        from twilio.rest import Client
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

        msg = client.messages.create(
            body=message,
            from_=TWILIO_FROM_PHONE,
            to=to,
        )
        log.info(f"SMS sent to {to} (SID: {msg.sid})")

    except Exception as e:
        log.error(f"Failed to send SMS to {to}: {e}")


# ════════════════════════════════════════════════════════════
# PHONE CALL — Twilio Voice
# ════════════════════════════════════════════════════════════

def make_call(to: str, message: str, alert_id: str):
    """
    Make a phone call via Twilio that reads out the alert message.

    The call uses a TwiML endpoint hosted on Vercel that generates
    the <Say> response dynamically based on the alert_id.
    This keeps the message fresh if the alert status has changed.

    TwiML response (from Vercel API route) looks like:
        <?xml version="1.0" encoding="UTF-8"?>
        <Response>
          <Say voice="Polly.Amy">
            This is an automated alert from ChillCheck...
          </Say>
          <Pause length="1"/>
          <Say voice="Polly.Amy">
            Press any key to stop.
          </Say>
          <Gather numDigits="1"/>
          <Redirect/>
        </Response>
    """
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_PHONE, TWILIO_TWIML_URL]):
        log.warning("Twilio credentials or TwiML URL not configured — call not made")
        return

    try:
        from twilio.rest import Client
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

        # Pass alert_id as query param so Vercel can look up the message
        twiml_url = f"{TWILIO_TWIML_URL}?alert_id={alert_id}"

        call = client.calls.create(
            url=twiml_url,
            from_=TWILIO_FROM_PHONE,
            to=to,
            timeout=30,           # Ring for 30 seconds
            machine_detection="Enable",  # Don't leave voicemail for answerphones
        )
        log.info(f"Call initiated to {to} (SID: {call.sid})")

    except Exception as e:
        log.error(f"Failed to call {to}: {e}")
