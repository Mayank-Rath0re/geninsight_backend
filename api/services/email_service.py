# services/email_service.py
"""
OTP email delivery and verification.
"""

import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from dotenv import load_dotenv
from fastapi import HTTPException

from core import config, db

load_dotenv()


def send_otp_mail(receiver_mail: str, usageCode: str):
    try:
        otp = secrets.randbelow(900000) + 100000
        otp_body = f"""
        <!DOCTYPE html>
        <html>
        <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Your OTP Code</title>
        </head>
        <body style="margin:0; padding:0; background-color:#f4f4f7; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f4f7; padding: 40px 0;">
            <tr>
            <td align="center">
                <table role="presentation" width="480" cellpadding="0" cellspacing="0" style="background-color:#ffffff; border-radius:12px; overflow:hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.06);">

                <!-- Header -->
                <tr>
                    <td style="padding: 36px 40px 0 40px; text-align:center;">
                    <div style="width:48px; height:48px; background-color:#4f46e5; border-radius:12px; margin:0 auto 20px auto; line-height:48px; text-align:center;">
                        <span style="color:#ffffff; font-size:22px; font-weight:600;">🔒</span>
                    </div>
                    <h1 style="margin:0; font-size:20px; color:#111827; font-weight:600;">Verify your identity</h1>
                    </td>
                </tr>

                <!-- Body -->
                <tr>
                    <td style="padding: 16px 40px 0 40px; text-align:center;">
                    <p style="margin:0; font-size:14px; line-height:22px; color:#6b7280;">
                        Use the code below to complete your verification. This code is valid for the next 10 minutes.
                    </p>
                    </td>
                </tr>

                <!-- OTP Box -->
                <tr>
                    <td style="padding: 28px 40px 0 40px; text-align:center;">
                    <div style="background-color:#f9fafb; border:1px solid #e5e7eb; border-radius:10px; padding:20px; display:inline-block; min-width:220px;">
                        <span style="font-size:32px; font-weight:700; letter-spacing:8px; color:#111827;">{otp}</span>
                    </div>
                    </td>
                </tr>

                <!-- Note -->
                <tr>
                    <td style="padding: 24px 40px 0 40px; text-align:center;">
                    <p style="margin:0; font-size:13px; line-height:20px; color:#9ca3af;">
                        Didn't request this code? You can safely ignore this email.
                    </p>
                    </td>
                </tr>

                <!-- Divider -->
                <tr>
                    <td style="padding: 32px 40px 0 40px;">
                    <hr style="border:none; border-top:1px solid #eeeeee; margin:0;">
                    </td>
                </tr>

                <!-- Footer -->
                <tr>
                    <td style="padding: 24px 40px 36px 40px; text-align:center;">
                    <p style="margin:0; font-size:12px; color:#b0b3ba;">
                        &copy; 2026 Geninsight. All rights reserved.
                    </p>
                    </td>
                </tr>

                </table>
            </td>
            </tr>
        </table>
        </body>
        </html>
        """

        msg = MIMEMultipart("alternative")
        msg["Subject"] = "GENINSIGHT: Verification OTP"
        msg["From"] = config.SENDER_EMAIL
        msg["To"] = receiver_mail
        msg.attach(MIMEText(otp_body, "html"))

        with smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT) as server:
            server.starttls()
            server.login(config.SENDER_EMAIL, config.SENDER_PASSWORD)
            server.sendmail(config.SENDER_EMAIL, receiver_mail, msg.as_string())

        inserted_time = datetime.now(timezone.utc)
        insert_query = "INSERT INTO OTP(email_id, otp_no, usageCode, insertedTime) VALUES(%s, %s, %s, %s)"
        db.run(insert_query, (receiver_mail, otp, usageCode, inserted_time))
        print(f"-- Email sent to: {receiver_mail}")
    except Exception as e:
        print(f"-- OTP send failed: {type(e).__name__}: {e}")
        raise HTTPException(status_code=400, detail="Error sending mail. Please try again later.")


def verify_mail(email: str, otp: str, usageCode: str) -> bool:
    try:
        otp_query = """
            SELECT TOP 1 otp_no, insertedTime
            FROM OTP
            WHERE email_id = %s AND usageCode = %s
            ORDER BY insertedTime DESC
        """
        rows = db.fetch(otp_query, (email, usageCode))

        if not rows:
            return False

        stored_otp, inserted_time = rows[0][0], rows[0][1]

        # Check expiry - 10 minutes. inserted_time is stored/returned as
        # UTC (see send_otp_mail); compare against UTC "now" to match.
        if inserted_time.tzinfo is None:
            inserted_time = inserted_time.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - inserted_time > timedelta(minutes=10):
            return False

        return str(stored_otp) == str(otp)
    except Exception:
        return False