import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from config import settings
from utils.logger import log


async def send_notification(subject: str, body: str):
    """Send email notification about application status changes."""
    if not settings.smtp_user or not settings.notify_email:
        return

    try:
        msg = MIMEMultipart()
        msg["From"] = settings.smtp_user
        msg["To"] = settings.notify_email
        msg["Subject"] = f"[Business Credit AI] {subject}"
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
            server.starttls()
            server.login(settings.smtp_user, settings.smtp_pass)
            server.send_message(msg)

        log.info(f"Notification sent: {subject}")
    except Exception as e:
        log.warning(f"Could not send notification: {e}")
