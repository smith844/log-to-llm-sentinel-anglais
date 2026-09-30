from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


# ── GlobalConfig ──

class GlobalConfigCreate(BaseModel):
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_recipient: str = ""
    smtp_tls: bool = True  # legacy
    smtp_ssl_mode: str = "starttls"  # 'ssl' | 'starttls' | 'none'
    ollama_url: str = "http://host.docker.internal:11434"
    ollama_model: str = "llama3"
    system_prompt: str = ""
    notification_method: str = "smtp"
    apprise_url: str = ""
    apprise_tags: str = ""
    apprise_max_chars: int = 1900
    max_log_chars: int = 5000
    debug_mode: bool = False
    ollama_prompt_lang: str = 'en'  # 'fr' | 'en'
    discord_webhook_url: Optional[str] = None
    log_rotation_limit_mb: int = 10


class GlobalConfigRead(GlobalConfigCreate):
    id: int
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ── Rule ──

class RuleCreate(BaseModel):
    name: str = Field(..., min_length=1)
    log_file_path: str = Field(..., min_length=1)
    keywords: List[str] = []
    context_lines: int = Field(default=5, ge=0, le=50)
    anti_spam_delay: int = Field(default=60, ge=1, le=3600)
    notify_severity_threshold: str = "info"
    application_context: str = ""
    enabled: bool = True
    notify_on_match: bool = True
    excluded_patterns: List[str] = []


class RuleUpdate(BaseModel):
    name: Optional[str] = None
    log_file_path: Optional[str] = None
    keywords: Optional[List[str]] = None
    context_lines: Optional[int] = None
    anti_spam_delay: Optional[int] = None
    notify_severity_threshold: Optional[str] = None
    application_context: Optional[str] = None
    enabled: Optional[bool] = None
    notify_on_match: Optional[bool] = None
    excluded_patterns: Optional[List[str]] = None


class RuleRead(RuleCreate):
    id: int
    last_position: int = 0
    last_log_line: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ── Analysis ──

class AnalysisRead(BaseModel):
    id: int
    rule_id: int
    triggered_line: str
    context_before: List[str] = []
    context_after: List[str] = []
    ollama_response: str
    severity: str
    notified: bool
    analyzed_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ── File Explorer ──

class FileInfo(BaseModel):
    name: str
    path: str
    size: int
    modified: float
    is_dir: bool
