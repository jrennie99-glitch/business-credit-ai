from pydantic_settings import BaseSettings
from pydantic import Field
from typing import Optional
import os


class Settings(BaseSettings):
    anthropic_api_key: str = Field(default="", env="ANTHROPIC_API_KEY")
    database_url: str = Field(default="sqlite:///./business_credit.db", env="DATABASE_URL")
    secret_key: str = Field(default="change-me-in-production", env="SECRET_KEY")
    debug: bool = Field(default=False, env="DEBUG")
    host: str = Field(default="0.0.0.0", env="HOST")
    port: int = Field(default=8000, env="PORT")

    # Browser
    headless: bool = Field(default=True, env="HEADLESS")
    browser_timeout: int = Field(default=30000, env="BROWSER_TIMEOUT")

    # Notifications
    smtp_host: str = Field(default="smtp.gmail.com", env="SMTP_HOST")
    smtp_port: int = Field(default=587, env="SMTP_PORT")
    smtp_user: str = Field(default="", env="SMTP_USER")
    smtp_pass: str = Field(default="", env="SMTP_PASS")
    notify_email: str = Field(default="", env="NOTIFY_EMAIL")

    # Captcha
    captcha_api_key: str = Field(default="", env="CAPTCHA_API_KEY")

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
