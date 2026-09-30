from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
import os

from app.database import SessionLocal
from app.models import Rule, Analysis, GlobalConfig
from app.services.orchestrator import Orchestrator
from app.utils.log_utils import clean_log_line

router = APIRouter(prefix="/api/rules", tags=["rules"])
orchestrator = None

def set_orchestrator(o):
    global orchestrator
    orchestrator = o


class RuleCreate(BaseModel):
    name: str
    log_file_path: str
    keywords: List[str]
    application_context: str = ""
    enabled: bool = True
    notify_on_match: bool = True
    context_lines: int = 5
    anti_spam_delay: int = 60
    notify_severity_threshold: str = "info"
    excluded_patterns: List[str] = []
    inactivity_warning_enabled: bool = True
    inactivity_period_hours: int = 1
    inactivity_notify: bool = True
    # MON-18
    alert_status: str = "normal"
    resolution_mode: str = "timeout"
    resolution_timeout_minutes: int = 30
    resolution_patterns: List[str] = []
    resolution_ai_enabled: bool = False
    resolution_notify_search: bool = False
    resolution_notify_resolved: bool = True


class RuleUpdate(BaseModel):
    name: Optional[str] = None
    log_file_path: Optional[str] = None
    keywords: Optional[List[str]] = None
    application_context: Optional[str] = None
    enabled: Optional[bool] = None
    notify_on_match: Optional[bool] = None
    context_lines: Optional[int] = None
    anti_spam_delay: Optional[int] = None
    notify_severity_threshold: Optional[str] = None
    excluded_patterns: Optional[List[str]] = None
    last_learning_session_id: Optional[int] = None  # pass -1 to explicitly clear
    inactivity_warning_enabled: Optional[bool] = None
    inactivity_period_hours: Optional[int] = None
    inactivity_notify: Optional[bool] = None
    # MON-18
    alert_status: Optional[str] = None
    resolution_mode: Optional[str] = None
    resolution_timeout_minutes: Optional[int] = None
    resolution_patterns: Optional[List[str]] = None
    resolution_ai_enabled: Optional[bool] = None
    resolution_notify_search: Optional[bool] = None
    resolution_notify_resolved: Optional[bool] = None


@router.get("")
def get_rules():
    db = SessionLocal()
    try:
        rules = db.query(Rule).all()
        result = []
        for r in rules:
            last_lines = _read_last_lines(r.log_file_path, n=1)
            last_line = last_lines[0] if last_lines else None
            
            rule_item = {
                "id": r.id,
                "name": r.name,
                "log_file_path": r.log_file_path,
                "keywords": r.get_keywords(),
                "application_context": r.application_context,
                "enabled": r.enabled,
                "notify_on_match": r.notify_on_match,
                "context_lines": r.context_lines or 5,
                "anti_spam_delay": r.anti_spam_delay or 60,
                "notify_severity_threshold": r.notify_severity_threshold or "info",
                "excluded_patterns": r.get_excluded_patterns(),
                "last_log_line": last_line,
                "last_learning_session_id": r.last_learning_session_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "inactivity_warning_enabled": r.inactivity_warning_enabled,
                "inactivity_period_hours": r.inactivity_period_hours,
                "inactivity_notify": r.inactivity_notify,
                "last_line_received_at": r.last_line_received_at.isoformat() + "Z" if r.last_line_received_at else None,
                "last_detection_id": None,
                "last_analysis_severity": None,
                # MON-18
                "alert_status": r.alert_status or "normal",
                "alert_started_at": r.alert_started_at.isoformat() + "Z" if r.alert_started_at else None,
                "resolution_mode": r.resolution_mode or "timeout",
                "resolution_timeout_minutes": r.resolution_timeout_minutes or 30,
                "resolution_patterns": r.get_resolution_patterns(),
                "resolution_patterns_weighted": r.get_weighted_resolution_patterns(),
                "resolution_ai_enabled": r.resolution_ai_enabled or False,
                "resolution_notify_search": r.resolution_notify_search or False,
                "resolution_notify_resolved": r.resolution_notify_resolved or False,
            }
            
            last_analysis = db.query(Analysis).filter(Analysis.rule_id == r.id).order_by(Analysis.analyzed_at.desc()).first()
            if last_analysis:
                rule_item["last_detection_id"] = last_analysis.detection_id
                rule_item["last_analysis_severity"] = last_analysis.severity
                rule_item["last_triggered_line"] = last_analysis.triggered_line
                rule_item["last_analysis_at"] = last_analysis.analyzed_at.strftime('%Y-%m-%dT%H:%M:%SZ') if last_analysis.analyzed_at else None

            result.append(rule_item)
        return result
    finally:
        db.close()


@router.get("/{rule_id}")
def get_rule(rule_id: int):
    db = SessionLocal()
    try:
        rule = db.query(Rule).filter(Rule.id == rule_id).first()
        if not rule:
            raise HTTPException(status_code=404, detail="rule_not_found")
        return {
            "id": rule.id,
            "name": rule.name,
            "log_file_path": rule.log_file_path,
            "keywords": rule.get_keywords(),
            "application_context": rule.application_context,
            "enabled": rule.enabled,
            "notify_on_match": rule.notify_on_match,
            "context_lines": rule.context_lines or 5,
            "anti_spam_delay": rule.anti_spam_delay or 60,
            "notify_severity_threshold": rule.notify_severity_threshold or "info",
            "excluded_patterns": rule.get_excluded_patterns(),
            "last_learning_session_id": rule.last_learning_session_id,
            "inactivity_warning_enabled": rule.inactivity_warning_enabled,
            "inactivity_period_hours": rule.inactivity_period_hours,
            "inactivity_notify": rule.inactivity_notify,
            "last_line_received_at": rule.last_line_received_at.isoformat() + "Z" if rule.last_line_received_at else None,
            # MON-18
            "alert_status": rule.alert_status or "normal",
            "alert_started_at": rule.alert_started_at.isoformat() + "Z" if rule.alert_started_at else None,
            "resolution_mode": rule.resolution_mode or "timeout",
            "resolution_timeout_minutes": rule.resolution_timeout_minutes or 30,
            "resolution_patterns": rule.get_resolution_patterns(),
            "resolution_patterns_weighted": rule.get_weighted_resolution_patterns(),
            "resolution_ai_enabled": rule.resolution_ai_enabled or False,
            "resolution_notify_search": rule.resolution_notify_search or False,
            "resolution_notify_resolved": rule.resolution_notify_resolved or False,
        }
    finally:
        db.close()


@router.post("")
def create_rule(rule_data: RuleCreate):
    db = SessionLocal()
    try:
        rule = Rule(
            name=rule_data.name,
            log_file_path=rule_data.log_file_path,
            application_context=rule_data.application_context,
            enabled=rule_data.enabled,
            notify_on_match=rule_data.notify_on_match,
            context_lines=rule_data.context_lines,
            anti_spam_delay=rule_data.anti_spam_delay,
            notify_severity_threshold=rule_data.notify_severity_threshold,
            inactivity_warning_enabled=rule_data.inactivity_warning_enabled,
            inactivity_period_hours=rule_data.inactivity_period_hours,
            inactivity_notify=rule_data.inactivity_notify,
            # MON-18
            alert_status=rule_data.alert_status,
            resolution_mode=rule_data.resolution_mode,
            resolution_timeout_minutes=rule_data.resolution_timeout_minutes,
            resolution_ai_enabled=rule_data.resolution_ai_enabled,
            resolution_notify_search=rule_data.resolution_notify_search,
            resolution_notify_resolved=rule_data.resolution_notify_resolved,
        )
        rule.set_keywords(rule_data.keywords)
        rule.set_excluded_patterns(rule_data.excluded_patterns)
        rule.set_resolution_patterns(rule_data.resolution_patterns)
        db.add(rule)
        db.commit()
        db.refresh(rule)

        # Recharger les règles Syslog
        try:
            from app.services.syslog_receiver import syslog_receiver
            syslog_receiver.load_config()
        except Exception:
            pass

        return {"id": rule.id, "message": "Règle créée"}
    finally:
        db.close()


@router.put("/{rule_id}")
def update_rule(rule_id: int, rule_data: RuleUpdate):
    db = SessionLocal()
    try:
        rule = db.query(Rule).filter(Rule.id == rule_id).first()
        if not rule:
            raise HTTPException(status_code=404, detail="rule_not_found")

        if rule_data.name is not None:
            rule.name = rule_data.name
        if rule_data.log_file_path is not None:
            rule.log_file_path = rule_data.log_file_path
        if rule_data.keywords is not None:
            rule.set_keywords(rule_data.keywords)
        if rule_data.application_context is not None:
            rule.application_context = rule_data.application_context
        if rule_data.enabled is not None:
            rule.enabled = rule_data.enabled
        if rule_data.notify_on_match is not None:
            rule.notify_on_match = rule_data.notify_on_match
        if rule_data.context_lines is not None:
            rule.context_lines = rule_data.context_lines
        if rule_data.anti_spam_delay is not None:
            rule.anti_spam_delay = rule_data.anti_spam_delay
        if rule_data.notify_severity_threshold is not None:
            rule.notify_severity_threshold = rule_data.notify_severity_threshold
        if rule_data.excluded_patterns is not None:
            rule.set_excluded_patterns(rule_data.excluded_patterns)
        if rule_data.last_learning_session_id is not None:
            # -1 is the sentinel value meaning "clear the session link"
            rule.last_learning_session_id = None if rule_data.last_learning_session_id == -1 else rule_data.last_learning_session_id
        if rule_data.inactivity_warning_enabled is not None:
            rule.inactivity_warning_enabled = rule_data.inactivity_warning_enabled
        if rule_data.inactivity_period_hours is not None:
            rule.inactivity_period_hours = rule_data.inactivity_period_hours
        if rule_data.inactivity_notify is not None:
            rule.inactivity_notify = rule_data.inactivity_notify
        # MON-18
        if rule_data.alert_status is not None:
            rule.alert_status = rule_data.alert_status
        if rule_data.resolution_mode is not None:
            rule.resolution_mode = rule_data.resolution_mode
        if rule_data.resolution_timeout_minutes is not None:
            rule.resolution_timeout_minutes = rule_data.resolution_timeout_minutes
        if rule_data.resolution_patterns is not None:
            rule.set_resolution_patterns(rule_data.resolution_patterns)
        if rule_data.resolution_ai_enabled is not None:
            rule.resolution_ai_enabled = rule_data.resolution_ai_enabled
        if rule_data.resolution_notify_search is not None:
            rule.resolution_notify_search = rule_data.resolution_notify_search
        if rule_data.resolution_notify_resolved is not None:
            rule.resolution_notify_resolved = rule_data.resolution_notify_resolved

        db.commit()

        # Recharger les règles Syslog
        try:
            from app.services.syslog_receiver import syslog_receiver
            syslog_receiver.load_config()
        except Exception:
            pass

        return {"id": rule.id, "message": "Règle mise à jour"}
    finally:
        db.close()


@router.delete("/{rule_id}")
def delete_rule(rule_id: int):
    db = SessionLocal()
    try:
        rule = db.query(Rule).filter(Rule.id == rule_id).first()
        if not rule:
            raise HTTPException(status_code=404, detail="rule_not_found")
        db.delete(rule)
        db.commit()

        # Recharger les règles Syslog
        try:
            from app.services.syslog_receiver import syslog_receiver
            syslog_receiver.load_config()
        except Exception:
            pass

        return {"message": "Règle supprimée"}
    finally:
        db.close()


def _read_last_lines(path: str, n: int = 5, max_bytes: int = 65536) -> List[str]:
    """
    Lit les N dernières lignes non vides d'un fichier.
    Retourne une liste vide si aucune ligne utile.
    """
    if not os.path.exists(path):
        return []

    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            if end == 0:
                return []

            to_read = min(max_bytes, end)
            f.seek(end - to_read)
            chunk = f.read(to_read)

        text = chunk.decode("utf-8", errors="ignore")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            return []
        return lines[-n:]
    except Exception:
        return []


@router.post("/{rule_id}/test")
async def test_rule(rule_id: int, request: Request):
    """
    Envoie les dernières lignes du fichier log de la règle pour analyse,
    sauvegarde l'analyse en BDD, envoie une notification, et renvoie le résultat.
    """
    from app import logger
    from app.services.notification_service import NotificationService

    db = SessionLocal()
    try:
        rule = db.query(Rule).filter(Rule.id == rule_id).first()
        if not rule:
            raise HTTPException(status_code=404, detail="rule_not_found")

        ctx_lines = rule.context_lines or 5
        logger.debug("TestRule", f"Règle '{rule.name}' — lecture des {ctx_lines} dernières lignes de {rule.log_file_path}")

        last_lines = _read_last_lines(rule.log_file_path, n=ctx_lines)
        if not last_lines:
            raise HTTPException(
                status_code=400,
                detail="Impossible de lire les dernières lignes (fichier introuvable, vide, ou illisible).",
            )

        last_line = last_lines[-1]
        logger.debug("TestRule", f"Dernière ligne : {last_line[:120]}")

        config = db.query(GlobalConfig).first()
        config_dict = {
            "smtp_host": config.smtp_host if config else "",
            "smtp_port": config.smtp_port if config else 587,
            "smtp_user": config.smtp_user if config else "",
            "smtp_password": config.smtp_password if config else "",
            "smtp_recipient": config.smtp_recipient if config else "",
            "smtp_tls": config.smtp_tls if config else True,
            "smtp_ssl_mode": config.smtp_ssl_mode if config else "starttls",
            "ollama_url": config.ollama_url if config and config.ollama_url else "http://ollama:11434",
            "ollama_model": config.ollama_model if config and config.ollama_model else "gemma4:e4b",
            "ollama_temp": config.ollama_temp if config else 0.1,
            "ollama_ctx": config.ollama_ctx if config else 4096,
            "ollama_think": config.ollama_think if config else True,
            "system_prompt": config.system_prompt if config else "",
            "notification_method": config.notification_method if config else "smtp",
            "apprise_url": config.apprise_url if config else "",
            "apprise_tags": config.apprise_tags if config else "",
            "apprise_max_chars": config.apprise_max_chars if config else 1900,
            "debug_mode": config.debug_mode if config else False,
            "discord_webhook_url": config.discord_webhook_url if config else "",
            "ollama_prompt_lang": config.ollama_prompt_lang if config else "en",
            "site_lang": config.site_lang if config else "fr",
            "instance_name": config.instance_name if config else "",
        }

        cleaned_context = [clean_log_line(l) for l in last_lines[:-1]]
        cleaned_last_line = clean_log_line(last_line)
        prompt = orchestrator._build_prompt(rule, cleaned_last_line, config_dict.get("system_prompt", ""), context_lines=cleaned_context, lang=config_dict.get("ollama_prompt_lang", "en"))
        logger.debug("TestRule", f"Envoi à Ollama (via stream) — modèle={config_dict.get('ollama_model')}")

        from app.routers.utils import cancel_on_disconnect
        async with orchestrator._ollama_semaphore:
            try:
                coro = orchestrator.ollama.analyze_async(
                    prompt=prompt,
                    url=config_dict.get("ollama_url"),
                    model=config_dict.get("ollama_model"),
                    think=config_dict.get("ollama_think", True),
                    options={
                        "temperature": config_dict.get("ollama_temp", 0.1),
                        "num_ctx": config_dict.get("ollama_ctx", 4096)
                    }
                )
                response = await cancel_on_disconnect(
                    request,
                    asyncio.wait_for(coro, timeout=300.0)
                )
            except asyncio.TimeoutError:
                response = "[Erreur Ollama] Délai d'attente dépassé (300s)"
        logger.debug("TestRule", f"Réponse Ollama reçue ({len(response)} chars)")
        severity = orchestrator._detect_severity(response)
        logger.debug("TestRule", f"Sévérité détectée : {severity}")

        analysis = Analysis(
            rule_id=rule.id,
            triggered_line=last_line,
            context_before_json="[]",
            context_after_json="[]",
            ollama_response=response,
            severity=severity,
            notified=False,
        )
        db.add(analysis)
        db.commit()
        db.refresh(analysis)

        # Envoyer une notification (comme en production)
        if rule.notify_on_match:
            severity_levels = {"info": 0, "warning": 1, "critical": 2}
            sev_val = severity_levels.get(severity, 0)
            threshold_val = severity_levels.get(rule.notify_severity_threshold, 0)
            
            if sev_val < threshold_val:
                logger.debug("TestRule", f"Notification de test ignorée: la sévérité '{severity}' est inférieure au seuil '{rule.notify_severity_threshold}'.")
                return {
                    "status": "ok",
                    "id": analysis.id,
                    "rule_id": analysis.rule_id,
                    "triggered_line": analysis.triggered_line,
                    "ollama_response": analysis.ollama_response,
                    "severity": analysis.severity,
                    "analyzed_at": analysis.analyzed_at.isoformat() if analysis.analyzed_at else None,
                    "notification_skipped": True
                }

            logger.debug("TestRule", f"Envoi notification via '{config_dict.get('notification_method')}'")
            notifier = NotificationService()
            subject = f"[Sentinel TEST] Alerte {severity.upper()} : {rule.name}"
            
            lang = config_dict.get("site_lang", "fr")
            # Si Apprise ou Discord, on prépare une version Markdown plus lisible
            if config_dict.get("notification_method") in ("apprise", "discord"):
                body = f"""### 🧪 Test Log to LLM Sentinel : {rule.name}
**{nt('severity', lang)}:** {severity.upper()}

**Ligne déclenchante:**
`{last_line}`

**Analyse Ollama:**
{response}

_Ceci est un test manuel depuis l'interface Log to LLM Sentinel._
"""
            else:
                body = f"""
                <h2>🧪 Test Log to LLM Sentinel</h2>
                <p><strong>Règle:</strong> {rule.name}</p>
                <p><strong>Ligne déclenchante:</strong> <code>{last_line}</code></p>
                <p><strong>Analyse Ollama:</strong></p>
                <blockquote>{response}</blockquote>
                <p><strong>Sévérité:</strong> {severity}</p>
                <hr><p><em>Ceci est un test manuel depuis l'interface Log to LLM Sentinel.</em></p>
                """

            # Gestion du résumé IA si nécessaire
            max_chars = config_dict.get("apprise_max_chars", 1900)
            notify_body = body

            if config_dict.get("notification_method") in ("apprise", "discord") and len(body) > max_chars:
                logger.debug("TestRule", f"Analyse trop longue ({len(body)} chars), demande de résumé simplifié à Ollama...")
                summary_prompt = f"Résume cette analyse de log :\n{response}"
                async with orchestrator._ollama_semaphore:
                    try:
                        coro = orchestrator.ollama.analyze_async(
                            prompt=summary_prompt,
                            url=config_dict.get("ollama_url"),
                            model=config_dict.get("ollama_model")
                        )
                        summary = await cancel_on_disconnect(
                            request,
                            asyncio.wait_for(coro, timeout=60.0)
                        )
                    except asyncio.TimeoutError:
                        summary = "[Erreur Ollama] Délai d'attente dépassé pour le résumé (60s)"
                if not (isinstance(summary, str) and summary.startswith("[Erreur Ollama]")):
                    notify_body = f"""### 🧪 Test Log to LLM Sentinel (Résumé) : {rule.name}
**{nt('severity', lang)}:** {severity.upper()}

**Résumé:**
{summary}

_(Analyse complète disponible dans l'interface)_
"""

            try:
                ok = notifier.send(subject, notify_body, config_dict)
                if ok:
                    logger.info("TestRule", "Notification de test envoyée avec succès")
                    analysis.notified = True
                    db.commit()
                else:
                    logger.warning("TestRule", "Échec de l'envoi de la notification de test")
            except Exception as e:
                logger.error("TestRule", f"Erreur lors de l'envoi de la notification : {e}")
        else:
            logger.debug("TestRule", "Notification désactivée pour cette règle")

        return {
            "status": "ok",
            "id": analysis.id,
            "rule_id": analysis.rule_id,
            "triggered_line": analysis.triggered_line,
            "ollama_response": analysis.ollama_response,
            "severity": analysis.severity,
            "analyzed_at": analysis.analyzed_at.isoformat() if analysis.analyzed_at else None,
        }
    finally:
        db.close()


@router.get("/{rule_id}/download")
def download_rule_log(rule_id: int):
    from fastapi.responses import FileResponse
    from pathlib import Path
    from app.routers.files import _resolve_under_roots
    
    db = SessionLocal()
    try:
        rule = db.query(Rule).filter(Rule.id == rule_id).first()
        if not rule:
            raise HTTPException(status_code=404, detail="Règle introuvable")
        
        path_str = rule.log_file_path
        target_path = None
        
        if path_str.startswith("[WEBHOOK]:"):
            token = path_str.split(":", 1)[1]
            data_dir = os.environ.get("SENTINEL_DATA_DIR", "/app/data")
            target_path = Path(data_dir) / "webhooks" / f"{token}.log"
        elif path_str.startswith("[SYSLOG]:"):
            hostname = path_str.split(":", 1)[1]
            data_dir = os.environ.get("SENTINEL_DATA_DIR", "/app/data")
            target_path = Path(data_dir) / "syslog" / f"{hostname}.log"
        else:
            target_path = _resolve_under_roots(path_str)
            
        if not target_path or not target_path.exists() or not target_path.is_file():
            raise HTTPException(status_code=404, detail="Fichier log introuvable ou vide")
            
        # Clean file name for header
        safe_name = "".join(c for c in rule.name if c.isalnum() or c in "-_ ").strip().replace(" ", "_")
        if not safe_name:
            safe_name = f"rule_{rule.id}"
            
        return FileResponse(
            path=target_path,
            filename=f"{safe_name}.log",
            media_type="application/octet-stream"
        )
    finally:
        db.close()
