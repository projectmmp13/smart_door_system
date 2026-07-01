"""
Smart Door Security System - Email Notifier Module
Handles sending email alerts when security events occur (e.g., unknown face detected 4 times).
Sends emails asynchronously in a background thread to prevent GUI blocking.
"""

import smtplib
import threading
import logging
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
import cv2

from config.settings import (
    SMTP_SERVER, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, EMAIL_RECIPIENT
)
from database.db_manager import SystemLogRepository

logger = logging.getLogger(__name__)


def send_unknown_face_alert_async(frame=None):
    """Starts a background thread to send an email alert."""
    thread = threading.Thread(
        target=_send_email_worker,
        args=(frame,),
        daemon=True
    )
    thread.start()
    return thread


def _send_email_worker(frame=None):
    """Worker function that connects to SMTP and sends the alert email."""
    system_log = SystemLogRepository()

    # Check if configurations are set
    if not SMTP_USERNAME or not SMTP_PASSWORD or not EMAIL_RECIPIENT:
        msg = "SMTP settings or recipient not configured. Alert email skipped."
        logger.warning(msg)
        system_log.warning("EmailNotifier", msg)
        return

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    subject = f"[ALERT] Security Alert: Unknown Face Detected Multiple Times - {timestamp}"
    
    body = (
        "Security Notification,\n\n"
        f"An unknown face has been detected 4 times consecutively at the smart door.\n"
        f"Time: {timestamp}\n\n"
        "This is an automated security alert. Please check the dashboard or logs immediately."
    )

    try:
        msg = MIMEMultipart()
        msg['Subject'] = subject
        msg['From'] = SMTP_USERNAME
        msg['To'] = EMAIL_RECIPIENT

        msg.attach(MIMEText(body, 'plain'))

        # If a frame was captured, attach it to the email
        if frame is not None:
            try:
                # Encode frame to JPEG
                success, encoded_image = cv2.imencode('.jpg', frame)
                if success:
                    image_data = encoded_image.tobytes()
                    mime_image = MIMEImage(image_data, name="unknown_face.jpg")
                    msg.attach(mime_image)
                    logger.info("Attached captured frame to the email alert.")
            except Exception as attachment_err:
                logger.error(f"Failed to attach frame to email: {attachment_err}")

        # Connect to SMTP server
        logger.info(f"Connecting to SMTP server {SMTP_SERVER}:{SMTP_PORT}...")
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.sendmail(SMTP_USERNAME, EMAIL_RECIPIENT, msg.as_string())
        server.close()

        success_msg = f"Security alert email sent successfully to {EMAIL_RECIPIENT}"
        logger.info(success_msg)
        system_log.info("EmailNotifier", success_msg)

    except Exception as e:
        error_msg = f"Failed to send security alert email: {str(e)}"
        logger.error(error_msg)
        system_log.error("EmailNotifier", error_msg)
