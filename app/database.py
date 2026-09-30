from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
import os
SQLALCHEMY_DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./data/sentinel.db")

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def init_db():
    """Crée les tables si elles n'existent pas."""
    from app import models  # noqa: F401
    Base.metadata.create_all(bind=engine)

    # Migrations manuelles (sûres : ignorées si la colonne existe déjà)
    from sqlalchemy import text
    migrations = [
        "ALTER TABLE global_config ADD COLUMN smtp_recipient VARCHAR DEFAULT ''",
        "ALTER TABLE global_config ADD COLUMN smtp_ssl_mode VARCHAR DEFAULT 'starttls'",
        "ALTER TABLE global_config ADD COLUMN debug_mode BOOLEAN DEFAULT 0",
        "ALTER TABLE rules ADD COLUMN context_lines INTEGER DEFAULT 5",
        "ALTER TABLE rules ADD COLUMN anti_spam_delay INTEGER DEFAULT 60",
        "ALTER TABLE rules ADD COLUMN notify_severity_threshold VARCHAR DEFAULT 'info'",
        "ALTER TABLE global_config ADD COLUMN apprise_tags VARCHAR DEFAULT ''",
        "ALTER TABLE global_config ADD COLUMN apprise_max_chars INTEGER DEFAULT 1900",
        "ALTER TABLE global_config ADD COLUMN max_log_chars INTEGER DEFAULT 5000",
        "ALTER TABLE analyses ADD COLUMN detection_id VARCHAR DEFAULT NULL",
        "ALTER TABLE analyses ADD COLUMN matched_keywords_json TEXT DEFAULT '[]'",
        "ALTER TABLE global_config ADD COLUMN monitor_log_lines INTEGER DEFAULT 60",
        "ALTER TABLE global_config ADD COLUMN ollama_temp FLOAT DEFAULT 0.1",
        "ALTER TABLE global_config ADD COLUMN ollama_ctx INTEGER DEFAULT 4096",
        "ALTER TABLE global_config ADD COLUMN ollama_think BOOLEAN DEFAULT 1",
        "ALTER TABLE meta_analysis_configs ADD COLUMN schedule_type VARCHAR DEFAULT 'daily'",
        "ALTER TABLE meta_analysis_configs ADD COLUMN schedule_time VARCHAR DEFAULT '00:00'",
        "ALTER TABLE meta_analysis_configs ADD COLUMN schedule_day INTEGER DEFAULT 1",
        "ALTER TABLE meta_analysis_results ADD COLUMN context_sent TEXT DEFAULT NULL",
        "ALTER TABLE rules ADD COLUMN last_learning_session_id INTEGER DEFAULT NULL",
        "ALTER TABLE global_config ADD COLUMN ollama_prompt_lang VARCHAR DEFAULT 'en'",
        "ALTER TABLE rules ADD COLUMN excluded_patterns_json TEXT DEFAULT '[]'",
        "ALTER TABLE global_config ADD COLUMN auto_delete_analyses BOOLEAN DEFAULT 0",
        "ALTER TABLE global_config ADD COLUMN auto_delete_retention_days INTEGER DEFAULT 30",
        "ALTER TABLE global_config ADD COLUMN chat_system_prompt TEXT DEFAULT ''",
        "ALTER TABLE global_config ADD COLUMN chat_lang VARCHAR DEFAULT ''",
        "ALTER TABLE rules ADD COLUMN last_line_received_at DATETIME DEFAULT NULL",
        "ALTER TABLE rules ADD COLUMN inactivity_warning_enabled BOOLEAN DEFAULT 1",
        "ALTER TABLE rules ADD COLUMN inactivity_period_hours INTEGER DEFAULT 1",
        "ALTER TABLE rules ADD COLUMN inactivity_notify BOOLEAN DEFAULT 1",
        "ALTER TABLE rules ADD COLUMN inactivity_notified BOOLEAN DEFAULT 0",
        "ALTER TABLE global_config ADD COLUMN site_lang VARCHAR DEFAULT 'fr'",
        "ALTER TABLE chat_conversations ADD COLUMN compression_mode VARCHAR DEFAULT NULL",
        "ALTER TABLE chat_conversations ADD COLUMN compressed_context TEXT DEFAULT NULL",
        "ALTER TABLE chat_conversations ADD COLUMN compressed_at DATETIME DEFAULT NULL",
        "ALTER TABLE global_config ADD COLUMN instance_name VARCHAR DEFAULT ''",
        "ALTER TABLE global_config ADD COLUMN discord_webhook_url VARCHAR DEFAULT NULL",
        "ALTER TABLE keyword_learning_sessions ADD COLUMN raw_exclusions_json TEXT DEFAULT '[]'",
        "ALTER TABLE keyword_learning_sessions ADD COLUMN final_exclusions_json TEXT DEFAULT '[]'",
        # CHAT-09
        "ALTER TABLE chat_conversations ADD COLUMN auto_compression_mode VARCHAR DEFAULT NULL",
        """CREATE TABLE IF NOT EXISTS chat_compressions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER REFERENCES chat_conversations(id),
            mode TEXT,
            content TEXT,
            compressed_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""",
        "ALTER TABLE analyses ADD COLUMN viewed BOOLEAN DEFAULT 0",
        # MON-18: Resolution surveillance
        "ALTER TABLE rules ADD COLUMN alert_status VARCHAR DEFAULT 'normal'",
        "ALTER TABLE rules ADD COLUMN alert_started_at DATETIME DEFAULT NULL",
        "ALTER TABLE rules ADD COLUMN resolution_mode VARCHAR DEFAULT 'timeout'",
        "ALTER TABLE rules ADD COLUMN resolution_timeout_minutes INTEGER DEFAULT 30",
        "ALTER TABLE rules ADD COLUMN resolution_patterns_json TEXT DEFAULT '[]'",
        "ALTER TABLE rules ADD COLUMN resolution_ai_enabled BOOLEAN DEFAULT 0",
        "ALTER TABLE rules ADD COLUMN resolution_notify_search BOOLEAN DEFAULT 0",
        "ALTER TABLE rules ADD COLUMN resolution_notify_resolved BOOLEAN DEFAULT 1",
        "ALTER TABLE analyses ADD COLUMN resolved_at DATETIME DEFAULT NULL",
        "ALTER TABLE analyses ADD COLUMN resolution_status VARCHAR DEFAULT NULL",
        "ALTER TABLE analyses ADD COLUMN resolution_line TEXT DEFAULT NULL",
        "ALTER TABLE analyses ADD COLUMN resolution_patterns_json TEXT DEFAULT '[]'",
        "ALTER TABLE analyses ADD COLUMN resolution_ai_explanation TEXT DEFAULT NULL",
        "ALTER TABLE analyses ADD COLUMN resolution_ai_confidence INTEGER DEFAULT NULL",
        "ALTER TABLE analyses ADD COLUMN exclude_from_mttr BOOLEAN DEFAULT 0",
        "UPDATE analyses SET resolved_at = datetime(analyzed_at, '+5 minutes'), exclude_from_mttr = 1 WHERE resolution_status = 'resolved' AND resolved_at IS NULL",
        # MON-19 / MON-20: Resolution verdict tracing + severity filtering
        """CREATE TABLE IF NOT EXISTS resolution_verdicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id INTEGER NOT NULL,
            trigger TEXT,
            ai_resolved BOOLEAN,
            ai_confidence INTEGER,
            ai_explanation TEXT,
            outcome TEXT NOT NULL,
            max_severity TEXT,
            context_lines_json TEXT DEFAULT '[]',
            resolution_line TEXT,
            resolution_patterns_json TEXT DEFAULT '[]',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""",
        "ALTER TABLE global_config ADD COLUMN syslog_enabled BOOLEAN DEFAULT 0",
        "ALTER TABLE global_config ADD COLUMN syslog_forward_addr VARCHAR DEFAULT NULL",
        "ALTER TABLE global_config ADD COLUMN log_rotation_limit_mb INTEGER DEFAULT 10",
    ]

    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                pass  # Colonne déjà présente
