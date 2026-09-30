from sqlalchemy import Column, Integer, String, Boolean, Float, DateTime, Text, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from datetime import datetime
import json as _json

from app.database import Base


class Rule(Base):
    __tablename__ = "rules"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    log_file_path = Column(String, nullable=False)
    keywords_json = Column(Text, default="[]")
    application_context = Column(Text, default="")
    enabled = Column(Boolean, default=True)
    notify_on_match = Column(Boolean, default=True)
    context_lines = Column(Integer, default=5)
    anti_spam_delay = Column(Integer, default=60)
    notify_severity_threshold = Column(String, default="info")
    last_position = Column(Float, default=0.0)
    last_learning_session_id = Column(Integer, nullable=True)  # dernière session d'auto-apprentissage
    excluded_patterns_json = Column(Text, default="[]")  # patterns d'exclusion (negative keywords)
    last_line_received_at = Column(DateTime, nullable=True)
    inactivity_warning_enabled = Column(Boolean, default=True)
    inactivity_period_hours = Column(Integer, default=1)
    inactivity_notify = Column(Boolean, default=True)
    inactivity_notified = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # ── Resolution surveillance (MON-18) ──
    alert_status = Column(String, default="normal")          # 'normal' | 'alert' | 'resolving'
    alert_started_at = Column(DateTime, nullable=True)       # Timestamp du passage en alerte
    resolution_mode = Column(String, default="timeout")      # 'timeout' | 'pattern' | 'both'
    resolution_timeout_minutes = Column(Integer, default=30) # Durée sans erreur → résolu
    resolution_patterns_json = Column(Text, default="[]")    # Patterns de résolution (ex: ["connected", "restored"])
    resolution_ai_enabled = Column(Boolean, default=False)   # Validation IA optionnelle
    resolution_notify_search = Column(Boolean, default=False)# Notifier quand l'IA cherche
    resolution_notify_resolved = Column(Boolean, default=True)# Notifier quand résolu

    def get_keywords(self):
        import json
        try:
            return json.loads(self.keywords_json) if self.keywords_json else []
        except json.JSONDecodeError:
            return []

    def set_keywords(self, keywords):
        import json
        self.keywords_json = json.dumps(keywords)

    def get_excluded_patterns(self):
        import json
        try:
            return json.loads(self.excluded_patterns_json) if self.excluded_patterns_json else []
        except json.JSONDecodeError:
            return []

    def set_excluded_patterns(self, patterns):
        import json
        self.excluded_patterns_json = json.dumps(patterns)

    def get_resolution_patterns(self):
        """Retourne une liste simple de strings pour la compatibilite avec le code existant."""
        try:
            data = _json.loads(self.resolution_patterns_json) if self.resolution_patterns_json else []
            if not data:
                return []
            # Format enrichi : [{"pattern": ..., "weight": ..., "error_keywords": [...]}]
            if isinstance(data[0], dict):
                return [item["pattern"] for item in data if item.get("pattern")]
            # Format legacy : ["pattern1", "pattern2"]
            return [str(p) for p in data if p]
        except Exception:
            return []

    def get_weighted_resolution_patterns(self):
        """Retourne la liste enrichie avec poids et mots-cles d'erreur associes."""
        try:
            data = _json.loads(self.resolution_patterns_json) if self.resolution_patterns_json else []
            if not data:
                return []
            if isinstance(data[0], dict):
                return data
            # Migration depuis le format legacy
            return [{"pattern": str(p), "weight": 1, "error_keywords": []} for p in data if p]
        except Exception:
            return []

    def set_resolution_patterns(self, patterns):
        """Accepte une liste de strings (format legacy) ou d'objets enrichis."""
        if patterns and isinstance(patterns[0], str):
            # Conversion depuis format legacy : preserve le poids existant si disponible
            existing = {item["pattern"]: item for item in self.get_weighted_resolution_patterns()}
            enriched = []
            for p in patterns:
                if p in existing:
                    enriched.append(existing[p])
                else:
                    enriched.append({"pattern": p, "weight": 1, "error_keywords": []})
            self.resolution_patterns_json = _json.dumps(enriched)
        else:
            self.resolution_patterns_json = _json.dumps(patterns)

    def set_weighted_resolution_patterns(self, weighted_patterns):
        """Enregistre directement la liste enrichie."""
        self.resolution_patterns_json = _json.dumps(weighted_patterns)

    def increment_pattern_weight(self, pattern: str, error_keywords: list = None):
        """Incremente le poids d'un pattern existant ou l'ajoute avec weight=1.
        Met a jour last_validated_at a la date courante."""
        weighted = self.get_weighted_resolution_patterns()
        now_iso = datetime.utcnow().isoformat() + "Z"
        found = False
        for item in weighted:
            if item.get("pattern", "").lower() == pattern.lower():
                item["weight"] = item.get("weight", 1) + 1
                item["last_validated_at"] = now_iso
                if error_keywords:
                    existing_kw = item.get("error_keywords", [])
                    for kw in error_keywords:
                        if kw.lower() not in [e.lower() for e in existing_kw]:
                            existing_kw.append(kw)
                    item["error_keywords"] = existing_kw
                found = True
                break
        if not found:
            weighted.append({
                "pattern": pattern,
                "weight": 1,
                "error_keywords": error_keywords or [],
                "last_validated_at": now_iso
            })
        self.set_weighted_resolution_patterns(weighted)

    def decrement_pattern_weight(self, pattern: str):
        """Decremente le poids d'un pattern (faux-positif utilisateur). Supprime si poids <= 0."""
        weighted = self.get_weighted_resolution_patterns()
        updated = []
        for item in weighted:
            if item.get("pattern", "").lower() == pattern.lower():
                new_weight = item.get("weight", 1) - 1
                if new_weight > 0:
                    item["weight"] = new_weight
                    updated.append(item)
                # Supprime si poids <= 0
            else:
                updated.append(item)
        self.set_weighted_resolution_patterns(updated)

    def remove_pattern(self, pattern: str):
        """Supprime un pattern specifique de la liste."""
        weighted = self.get_weighted_resolution_patterns()
        updated = [item for item in weighted if item.get("pattern", "").lower() != pattern.lower()]
        self.set_weighted_resolution_patterns(updated)


class Analysis(Base):
    __tablename__ = "analyses"

    id = Column(Integer, primary_key=True, index=True)
    rule_id = Column(Integer, index=True)
    detection_id = Column(String, index=True, nullable=True)  # UUID court de detection
    triggered_line = Column(Text, nullable=False)
    matched_keywords_json = Column(Text, default="[]")  # mots-cles ayant declenche la regle
    context_before_json = Column(Text, default="[]")
    context_after_json = Column(Text, default="[]")
    ollama_response = Column(Text)
    severity = Column(String, default="info")
    notified = Column(Boolean, default=False)
    viewed = Column(Boolean, default=False)
    analyzed_at = Column(DateTime, server_default=func.now())
    resolved_at = Column(DateTime, nullable=True)    # Quand la resolution a ete confirmee
    resolution_status = Column(String, nullable=True) # 'pending' | 'resolved' | 'false_positive'
    resolution_line = Column(Text, nullable=True)
    resolution_patterns_json = Column(Text, default="[]")
    resolution_ai_explanation = Column(Text, nullable=True)
    resolution_ai_confidence = Column(Integer, nullable=True)
    exclude_from_mttr = Column(Boolean, default=False)


class ResolutionVerdict(Base):
    """Trace chaque tentative de resolution (acceptee, rejetee, faux-positif).
    Permet l'audit complet des echanges IA internes lies au retour a la normale."""
    __tablename__ = "resolution_verdicts"

    id = Column(Integer, primary_key=True, index=True)
    rule_id = Column(Integer, index=True)
    trigger = Column(String)                          # Ex : "Pattern match: 'restored'" ou "Timeout (30 min)"
    ai_resolved = Column(Boolean, nullable=True)      # True/False/None (None = pas de validation IA)
    ai_confidence = Column(Integer, nullable=True)    # 0-100
    ai_explanation = Column(Text, nullable=True)      # Explication brute de l'IA
    outcome = Column(String)                          # 'accepted' | 'rejected_ai' | 'rejected_low_confidence' | 'accepted_no_ai' | 'manual' | 'false_positive_user'
    max_severity = Column(String, nullable=True)      # Severite max de l'alerte au moment du verdict
    context_lines_json = Column(Text, default="[]")   # Lignes de log envoyees a l'IA pour audit
    resolution_line = Column(Text, nullable=True)     # Ligne qui a declenche la tentative de resolution
    resolution_patterns_json = Column(Text, default="[]")  # Patterns ayant matche
    created_at = Column(DateTime, default=datetime.utcnow)





class GlobalConfig(Base):
    __tablename__ = "global_config"

    id = Column(Integer, primary_key=True, index=True)
    smtp_host = Column(String, default="")
    smtp_port = Column(Integer, default=587)
    smtp_user = Column(String, default="")
    smtp_password = Column(String, default="")
    smtp_recipient = Column(String, default="")
    smtp_tls = Column(Boolean, default=True)  # legacy
    smtp_ssl_mode = Column(String, default="starttls")  # 'ssl' | 'starttls' | 'none'
    ollama_url = Column(String, default="http://ollama:11434")
    ollama_model = Column(String, default="gemma4:e4b")
    ollama_temp = Column(Float, default=0.1)
    ollama_ctx = Column(Integer, default=4096)
    ollama_think = Column(Boolean, default=True)
    system_prompt = Column(Text, default="")
    notification_method = Column(String, default="smtp")
    apprise_url = Column(String, nullable=True)
    apprise_tags = Column(String, nullable=True)
    apprise_max_chars = Column(Integer, default=1900)
    max_log_chars = Column(Integer, default=5000)
    monitor_log_lines = Column(Integer, default=60)
    debug_mode = Column(Boolean, default=False)
    ollama_prompt_lang = Column(String, default='en')  # 'fr' | 'en' — langue des prompts d'analyse
    site_lang = Column(String, default='en')  # langue du site (header) — utilisée pour les notifications
    instance_name = Column(String, default='')  # nom de l'instance pour différencier les notifications multi-déploiement
    chat_system_prompt = Column(Text, default="")
    chat_lang = Column(String, default="")  # Default header lang if empty
    auto_delete_analyses = Column(Boolean, default=False)
    auto_delete_retention_days = Column(Integer, default=30)
    discord_webhook_url = Column(String, nullable=True)
    syslog_enabled = Column(Boolean, default=False)
    syslog_forward_addr = Column(String, nullable=True)  # Format 'IP:port' or 'IP' (default port 514)
    log_rotation_limit_mb = Column(Integer, default=10)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

class ChatConversation(Base):
    __tablename__ = "chat_conversations"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    analysis_id = Column(Integer, ForeignKey("analyses.id"), nullable=True)
    
    # Compression fields
    compression_mode = Column(String, nullable=True) # 'compact', 'summary'
    compressed_context = Column(Text, nullable=True)
    compressed_at = Column(DateTime, nullable=True)
    auto_compression_mode = Column(String, nullable=True)  # mode auto mémorisé pour cette conv
    
    messages = relationship("ChatMessage", back_populates="conversation", cascade="all, delete-orphan")
    compressions = relationship("ChatCompression", back_populates="conversation", cascade="all, delete-orphan", order_by="ChatCompression.compressed_at")

class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("chat_conversations.id"))
    role = Column(String) # user, assistant
    content = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    conversation = relationship("ChatConversation", back_populates="messages")


class ChatCompression(Base):
    """Historique de toutes les compressions d'une conversation (09-B)."""
    __tablename__ = "chat_compressions"
    id              = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("chat_conversations.id"))
    mode            = Column(String)   # 'compact' | 'summary' | 'truncate'
    content         = Column(Text)     # résumé compressé
    compressed_at   = Column(DateTime, default=datetime.utcnow)  # timestamp cutoff
    created_at      = Column(DateTime, default=datetime.utcnow)

    conversation = relationship("ChatConversation", back_populates="compressions")


class MetaAnalysisConfig(Base):
    __tablename__ = "meta_analysis_configs"
    id = Column(Integer, primary_key=True, index=True)
    rule_ids_json = Column(Text, default="[]")  # List of rule IDs to include. Empty means all rules.
    name = Column(String, nullable=False)
    schedule_type = Column(String, default="daily") # 'daily', 'weekly', 'monthly'
    schedule_time = Column(String, default="00:00") # 'HH:MM'
    schedule_day = Column(Integer, default=1)       # 1-7 for weekly, 1-31 for monthly
    enabled = Column(Boolean, default=True)
    notify_enabled = Column(Boolean, default=True)
    context_size = Column(Integer, default=16000)
    system_prompt = Column(Text, default="Tu es un expert DevOps. Analyse ces événements et fais une synthèse globale de la situation du service.")
    max_analyses = Column(Integer, default=50)
    last_run_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class MetaAnalysisResult(Base):
    __tablename__ = "meta_analysis_results"
    id = Column(Integer, primary_key=True, index=True)
    config_id = Column(Integer, ForeignKey("meta_analysis_configs.id", ondelete="CASCADE"))
    period_start = Column(DateTime)
    period_end = Column(DateTime)
    analyses_count = Column(Integer, default=0)
    detection_ids_json = Column(Text, default="[]")  # List of detection IDs included
    matched_keywords_json = Column(Text, default="[]")  # List of all keywords matched across all included analyses
    context_sent = Column(Text, nullable=True)  # Prompt exact envoyé au LLM
    ollama_response = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


class KeywordLearningSession(Base):
    __tablename__ = "keyword_learning_sessions"
    id                     = Column(Integer, primary_key=True, index=True)
    rule_id                = Column(Integer, nullable=True)          # null avant création de la règle
    log_file_path          = Column(String)
    period_start           = Column(DateTime)                        # UTC
    period_end             = Column(DateTime)                        # UTC
    granularity_s          = Column(Integer)                         # secondes par paquet
    max_chars_per_packet   = Column(Integer, default=5000)
    status                 = Column(String, default="pending")       # pending|scanning|refining|validated|reverted|error
    total_packets          = Column(Integer, default=0)
    completed_packets      = Column(Integer, default=0)
    raw_keywords_json      = Column(Text, default="[]")              # candidats bruts accumulés
    final_keywords_json    = Column(Text, default="[]")              # après raffinement
    raw_exclusions_json    = Column(Text, default="[]")              # exclusions brutes
    final_exclusions_json  = Column(Text, default="[]")              # exclusions retenues
    previous_keywords_json = Column(Text, default="[]")             # keywords avant learning (revert)
    refine_rationale_json  = Column(Text, default="{}")             # {keyword: raison}
    ollama_log_json        = Column(Text, default="[]")              # [{packet_idx, window, chars, keywords}]
    error_message          = Column(Text, nullable=True)
    created_at             = Column(DateTime, default=datetime.utcnow)
    validated_at           = Column(DateTime, nullable=True)
