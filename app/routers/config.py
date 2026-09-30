from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from pydantic import BaseModel
from typing import Optional
import io
import zipfile

from app.database import SessionLocal
from app.models import GlobalConfig
from app.services.notification_service import NotificationService
from app.services.ollama_service import OllamaService

import os
import json
import urllib.request
import urllib.error
import asyncio
from fastapi.responses import StreamingResponse
import httpx 
from app import logger
from app.utils.notification_i18n import nt

router = APIRouter(prefix="/api/config", tags=["config"])
_orchestrator = None

def set_orchestrator(o):
    global _orchestrator
    _orchestrator = o

def _get_config_dict(config):
    if not config: return {}
    return {
        "ollama_url": config.ollama_url,
        "ollama_model": config.ollama_model,
        "ollama_temp": config.ollama_temp,
        "ollama_ctx": config.ollama_ctx,
        "ollama_think": config.ollama_think,
        "system_prompt": config.system_prompt,
    }

@router.post("/pull-model")
async def pull_model(data: dict):
    model_name = data.get("model")
    if not model_name:
        raise HTTPException(status_code=400, detail="Nom du modèle requis")
    
    db = SessionLocal()
    config = db.query(GlobalConfig).first()
    db.close()
    
    ollama_url = (config.ollama_url or "http://ollama:11434").rstrip("/")
    pull_url = f"{ollama_url}/api/pull"

    async def event_generator():
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("POST", pull_url, json={"name": model_name}) as response:
                    async for line in response.aiter_lines():
                        if line:
                            yield f"data: {line}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


class ConfigUpdate(BaseModel):
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_recipient: Optional[str] = None
    smtp_tls: Optional[bool] = None
    smtp_ssl_mode: Optional[str] = None
    ollama_url: Optional[str] = None
    ollama_model: Optional[str] = None
    ollama_temp: Optional[float] = None
    ollama_ctx: Optional[int] = None
    ollama_think: Optional[bool] = None
    system_prompt: Optional[str] = None
    notification_method: Optional[str] = None
    apprise_url: Optional[str] = None
    apprise_tags: Optional[str] = None
    apprise_max_chars: Optional[int] = None
    max_log_chars: Optional[int] = None
    monitor_log_lines: Optional[int] = None
    debug_mode: Optional[bool] = None
    ollama_prompt_lang: Optional[str] = None  # 'fr' | 'en'
    site_lang: Optional[str] = None  # langue du site (header) pour notifications
    instance_name: Optional[str] = None  # nom de l'instance (multi-déploiement)
    discord_webhook_url: Optional[str] = None
    auto_delete_analyses: Optional[bool] = None
    auto_delete_retention_days: Optional[int] = None
    syslog_enabled: Optional[bool] = None
    syslog_forward_addr: Optional[str] = None
    log_rotation_limit_mb: Optional[int] = None


@router.get("")
def get_config():
    db = SessionLocal()
    try:
        config = db.query(GlobalConfig).first()
        if not config:
            config = GlobalConfig()
            db.add(config)
            db.commit()
            db.refresh(config)
        return {
            "smtp_host": config.smtp_host,
            "smtp_port": config.smtp_port,
            "smtp_user": config.smtp_user,
            "smtp_recipient": config.smtp_recipient,
            "smtp_tls": config.smtp_tls,
            "smtp_ssl_mode": config.smtp_ssl_mode,
            "ollama_url": config.ollama_url,
            "ollama_model": config.ollama_model,
            "ollama_temp": config.ollama_temp,
            "ollama_ctx": config.ollama_ctx,
            "ollama_think": config.ollama_think,
            "system_prompt": config.system_prompt,
            "notification_method": config.notification_method,
            "apprise_url": config.apprise_url,
            "apprise_tags": config.apprise_tags,
            "apprise_max_chars": config.apprise_max_chars,
            "max_log_chars": config.max_log_chars,
            "monitor_log_lines": config.monitor_log_lines,
            "debug_mode": config.debug_mode,
            "ollama_prompt_lang": config.ollama_prompt_lang or 'en',
            "site_lang": config.site_lang or 'fr',
            "instance_name": config.instance_name or '',
            "discord_webhook_url": config.discord_webhook_url or '',
            "auto_delete_analyses": config.auto_delete_analyses,
            "auto_delete_retention_days": config.auto_delete_retention_days,
            "syslog_enabled": config.syslog_enabled,
            "syslog_forward_addr": config.syslog_forward_addr or '',
            "log_rotation_limit_mb": config.log_rotation_limit_mb or 10,
            "server_ip": os.environ.get("SENTINEL_HOST_IP") or "localhost",
        }
    finally:
        db.close()


@router.put("")
async def update_config(config_data: ConfigUpdate):
    db = SessionLocal()
    try:
        config = db.query(GlobalConfig).first()
        if not config:
            config = GlobalConfig()
            db.add(config)

        from app import logger
        logger.debug("ConfigRouter", f"Données reçues: {config_data.dict(exclude={'smtp_password'})}")

        if config_data.smtp_host is not None:
            config.smtp_host = config_data.smtp_host
        if config_data.smtp_port is not None:
            config.smtp_port = config_data.smtp_port
        if config_data.smtp_user is not None:
            config.smtp_user = config_data.smtp_user
        if config_data.smtp_password is not None:
            config.smtp_password = config_data.smtp_password
        if config_data.smtp_recipient is not None:
            config.smtp_recipient = config_data.smtp_recipient
        if config_data.smtp_tls is not None:
            config.smtp_tls = config_data.smtp_tls
        if config_data.smtp_ssl_mode is not None:
            config.smtp_ssl_mode = config_data.smtp_ssl_mode
        if config_data.ollama_url is not None:
            config.ollama_url = config_data.ollama_url
        if config_data.ollama_model is not None:
            config.ollama_model = config_data.ollama_model
        if config_data.ollama_temp is not None:
            config.ollama_temp = config_data.ollama_temp
        if config_data.ollama_ctx is not None:
            config.ollama_ctx = config_data.ollama_ctx
        if config_data.ollama_think is not None:
            config.ollama_think = config_data.ollama_think
        if config_data.system_prompt is not None:
            config.system_prompt = config_data.system_prompt
        if config_data.notification_method is not None:
            config.notification_method = config_data.notification_method
        if config_data.apprise_url is not None:
            config.apprise_url = config_data.apprise_url
        if config_data.apprise_tags is not None:
            config.apprise_tags = config_data.apprise_tags
        if config_data.apprise_max_chars is not None:
            config.apprise_max_chars = config_data.apprise_max_chars
        if config_data.max_log_chars is not None:
            config.max_log_chars = config_data.max_log_chars
        if config_data.monitor_log_lines is not None:
            config.monitor_log_lines = config_data.monitor_log_lines
        if config_data.debug_mode is not None:
            config.debug_mode = config_data.debug_mode
        if config_data.ollama_prompt_lang is not None:
            config.ollama_prompt_lang = config_data.ollama_prompt_lang
        if config_data.site_lang is not None:
            config.site_lang = config_data.site_lang
        if config_data.instance_name is not None:
            config.instance_name = config_data.instance_name
        if config_data.discord_webhook_url is not None:
            config.discord_webhook_url = config_data.discord_webhook_url
        if config_data.auto_delete_analyses is not None:
            config.auto_delete_analyses = config_data.auto_delete_analyses
        if config_data.auto_delete_retention_days is not None:
            config.auto_delete_retention_days = config_data.auto_delete_retention_days
        if config_data.syslog_enabled is not None:
            config.syslog_enabled = config_data.syslog_enabled
        if config_data.syslog_forward_addr is not None:
            config.syslog_forward_addr = config_data.syslog_forward_addr
        if config_data.log_rotation_limit_mb is not None:
            config.log_rotation_limit_mb = config_data.log_rotation_limit_mb

        db.commit()

        # Recharger le récepteur Syslog
        try:
            from app.services.syslog_receiver import syslog_receiver
            await syslog_receiver.reload()
        except Exception as ex:
            logger.error("ConfigRouter", f"Erreur lors du rechargement de SyslogReceiver : {ex}")

        return {"message": "Configuration mise à jour"}
    finally:
        db.close()


@router.put("/site-lang")
def update_site_lang(data: dict):
    """Met à jour la langue du site (utilisée pour les notifications)."""
    lang = data.get("lang", "fr")
    db = SessionLocal()
    try:
        config = db.query(GlobalConfig).first()
        if not config:
            config = GlobalConfig()
            db.add(config)
        config.site_lang = lang
        db.commit()
        return {"ok": True, "site_lang": lang}
    finally:
        db.close()

def _get_config_dict(config: Optional[GlobalConfig]) -> dict:
    return {
        "smtp_host": config.smtp_host if config else "",
        "smtp_port": config.smtp_port if config else 587,
        "smtp_user": config.smtp_user if config else "",
        "smtp_password": config.smtp_password if config else "",
        "smtp_recipient": config.smtp_recipient if config else "",
        "smtp_tls": config.smtp_tls if config else True,
        "smtp_ssl_mode": config.smtp_ssl_mode if config else "starttls",
        "ollama_url": config.ollama_url if config and config.ollama_url else "http://ollama:11434",
        "ollama_model": config.ollama_model if config and config.ollama_model else "gemma4:e4b",
        "system_prompt": config.system_prompt if config else "",
        "notification_method": config.notification_method if config else "smtp",
        "apprise_url": config.apprise_url if config else "",
        "apprise_tags": config.apprise_tags if config else "",
        "apprise_max_chars": config.apprise_max_chars if config else 1900,
        "max_log_chars": config.max_log_chars if config else 5000,
        "monitor_log_lines": config.monitor_log_lines if config else 60,
        "debug_mode": config.debug_mode if config else False,
        "ollama_prompt_lang": (config.ollama_prompt_lang or 'en') if config else 'en',
        "site_lang": (config.site_lang or 'fr') if config else 'fr',
        "instance_name": (config.instance_name or '') if config else '',
        "discord_webhook_url": (config.discord_webhook_url or '') if config else '',
        "auto_delete_analyses": config.auto_delete_analyses if config else False,
        "auto_delete_retention_days": config.auto_delete_retention_days if config else 30,
        "syslog_enabled": config.syslog_enabled if config else False,
        "syslog_forward_addr": (config.syslog_forward_addr or '') if config else '',
    }


@router.post("/test/ollama")
async def test_ollama(request: Request):
    from app.services.ollama_service import OllamaService
    from app.routers.utils import cancel_on_disconnect
    
    ollama = OllamaService()
    db = SessionLocal()
    try:
        config = db.query(GlobalConfig).first()
        cfg = _get_config_dict(config)

        url = (cfg.get("ollama_url") or "").strip()
        model = (cfg.get("ollama_model") or "").strip()
        try:
            if not url:
                raise HTTPException(status_code=400, detail="URL Ollama manquante")
            if not model:
                raise HTTPException(status_code=400, detail="Modèle Ollama manquant")

            # 1) vérification /api/tags (optionnel)
            base = url
            if base.endswith("/"):
                base = base[:-1]
            if base.endswith("/api/generate"):
                base = base[: -len("/api/generate")]
            elif base.endswith("/api"):
                base = base[: -len("/api")]
                
            available_models = []
            try:
                req = urllib.request.Request(f"{base}/api/tags", method="GET")
                with urllib.request.urlopen(req, timeout=8) as r:
                    tags_raw = r.read().decode("utf-8", errors="ignore")
                    tags = json.loads(tags_raw)
                    for m in tags.get("models", []) or []:
                        name = m.get("name")
                        if name: available_models.append(name)
            except Exception as e:
                logger.warning("ConfigRouter", f"Impossible de joindre /api/tags : {e}")

            # 2) generation asynchrone (streaming) protégée par le sémaphore
            prompt = "Réponds uniquement par 'OK'."
            
            try:
                # On utilise le sémaphore global s'il est dispo
                sem = _orchestrator._ollama_semaphore if _orchestrator else asyncio.Semaphore(1)
                async with sem:
                    try:
                        coro = ollama.analyze_async(
                            prompt=prompt, 
                            url=url, 
                            model=model,
                            think=cfg.get("ollama_think", True),
                            options={
                                "temperature": cfg.get("ollama_temp", 0.1),
                                "num_ctx": cfg.get("ollama_ctx", 4096)
                            }
                        )
                        resp = await cancel_on_disconnect(
                            request,
                            asyncio.wait_for(coro, timeout=300.0)
                        )
                    except asyncio.TimeoutError:
                        resp = "[Erreur Ollama] Délai d'attente dépassé (300s)"
                
                if isinstance(resp, str) and resp.startswith("[Erreur Ollama]"):
                    raise HTTPException(status_code=502, detail=resp)

                return {
                    "status": "ok",
                    "ok": True,
                    "detail": "Connexion Ollama OK",
                    "sample": resp,
                    "available_models": available_models,
                }
            except Exception as e:
                if isinstance(e, HTTPException): raise e
                raise HTTPException(status_code=502, detail=f"Erreur génération : {str(e)}")
        except Exception as e:
            import traceback
            error_msg = f"Erreur Test Ollama: {str(e)}\n{traceback.format_exc()}"
            logger.error("ConfigRouter", error_msg)
            if isinstance(e, HTTPException): raise e
            raise HTTPException(status_code=500, detail=f"Erreur interne: {str(e)}")
    finally:
        db.close()


@router.post("/test/smtp")
def test_smtp():
    db = SessionLocal()
    try:
        config = db.query(GlobalConfig).first()
        cfg = _get_config_dict(config)

        # Envoie un email de test vers smtp_user (comportement existant)
        notifier = NotificationService()
        lang = cfg.get("site_lang", "fr")
        subject = "[Log to LLM Sentinel] Test SMTP"
        body = "<p>This is a test email sent by Log-to-LLM-Sentinel.</p>" if lang == "en" else "<p>Ceci est un email de test envoyé par Log-to-LLM-Sentinel.</p>"
        ok = notifier._send_smtp(subject, body, cfg, to_email=cfg.get("smtp_recipient") or cfg.get("smtp_user") or None)

        if not ok:
            raise HTTPException(status_code=502, detail="Échec de l'envoi SMTP (vérifie hôte/port/user/mdp/TLS)")

        return {"ok": True, "detail": "Email SMTP envoyé (si configuration correcte)"}
    finally:
        db.close()


@router.post("/test/apprise")
def test_apprise():
    """
    Test “connexion” Apprise : vérifie que l'URL est joignable.
    (La livraison d'une notification dépend de ta config Apprise.)
    """
    db = SessionLocal()
    try:
        config = db.query(GlobalConfig).first()
        cfg = _get_config_dict(config)

        apprise_url = (cfg.get("apprise_url") or "").strip()
        if not apprise_url:
            raise HTTPException(status_code=400, detail="URL Apprise manquante")

        from app.services.notification_service import NotificationService
        notifier = NotificationService()
        lang = cfg.get("site_lang", "fr")
        subject = "Test Apprise Log to LLM Sentinel"
        body = "This is a Log to LLM Sentinel configuration test" if lang == "en" else "Ceci est un test de configuration Log to LLM Sentinel"
        
        ok = notifier._send_apprise(subject, body, cfg)
        if not ok:
            raise HTTPException(status_code=502, detail="Échec de l'envoi Apprise (vérifie l'URL ou les logs debug)")

        return {"ok": True, "detail": "Notification Apprise envoyée avec succès"}
    finally:
        db.close()


@router.post("/test/discord")
def test_discord():
    """Test connexion Discord Webhook"""
    db = SessionLocal()
    try:
        config = db.query(GlobalConfig).first()
        cfg = _get_config_dict(config)

        webhook_url = (cfg.get("discord_webhook_url") or "").strip()
        if not webhook_url:
            raise HTTPException(status_code=400, detail="Webhook Discord manquant")

        from app.services.notification_service import NotificationService
        notifier = NotificationService()
        lang = cfg.get("site_lang", "fr")
        subject = "Test Discord Log to LLM Sentinel"
        body = "This is a Log to LLM Sentinel configuration test" if lang == "en" else "Ceci est un test de configuration Log to LLM Sentinel"
        
        ok = notifier._send_discord(subject, body, cfg)
        if not ok:
            raise HTTPException(status_code=502, detail="Échec de l'envoi Discord (vérifie l'URL ou les logs debug)")

        return {"ok": True, "detail": "Notification Discord envoyée avec succès"}
    finally:
        db.close()


@router.get("/apprise/tags")
def list_apprise_tags():
    """Tente de récupérer les tags configurés dans l'API Apprise."""
    db = SessionLocal()
    try:
        config = db.query(GlobalConfig).first()
        if not config or not config.apprise_url:
            return {"tags": []}

        url = config.apprise_url.strip()
        if "/notify/" in url:
            json_url = url.replace("/notify/", "/json/urls/")
        else:
            return {"tags": []}

        try:
            req = urllib.request.Request(json_url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            from app import logger
            logger.debug("ConfigRouter", f"Erreur fetch tags Apprise: {e}")
            return {"tags": []}

        tags = set()
        for item in data.get("urls", []):
            item_tags = item.get("tags", [])
            for t in item_tags:
                tags.add(t)

        return {"tags": sorted(list(tags))}
    finally:
        db.close()


@router.get("/ollama/models")
def list_ollama_models():
    """Retourne la liste des modèles disponibles sur le serveur Ollama configuré."""
    db = SessionLocal()
    try:
        config = db.query(GlobalConfig).first()
        cfg = _get_config_dict(config)

        url = (cfg.get("ollama_url") or "").strip()
        if not url:
            raise HTTPException(status_code=400, detail="URL Ollama manquante")

        base = url.rstrip("/")
        if base.endswith("/api/generate"):
            base = base[: -len("/api/generate")]
        elif base.endswith("/api"):
            base = base[: -len("/api")]

        try:
            req = urllib.request.Request(f"{base}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=8) as r:
                tags_raw = r.read().decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"[Erreur Ollama] /api/tags a répondu HTTP {e.code}")
        except urllib.error.URLError as e:
            raise HTTPException(status_code=502, detail=f"[Erreur Ollama] Impossible de joindre Ollama: {str(e)}")

        try:
            tags = json.loads(tags_raw) if tags_raw else {}
        except Exception:
            tags = {}

        available_models = []
        try:
            for m in tags.get("models", []) or []:
                name = m.get("name")
                if name:
                    available_models.append(name)
        except Exception:
            available_models = []

        return {"models": available_models}
    finally:
        db.close()


@router.get("/logs")
def get_debug_logs():
    """Retourne les derniers logs en mémoire."""
    from app.logger import get_logs
    return {"logs": get_logs()}


@router.delete("/logs")
def clear_debug_logs():
    """Efface les logs en mémoire."""
    from app.logger import clear_logs
    clear_logs()
    return {"message": "Logs effacés"}


@router.get("/ollama/logs")
def get_ollama_debug_logs():
    """Retourne les derniers appels Ollama en mémoire."""
    from app.logger import get_ollama_logs
    return {"logs": get_ollama_logs()}


@router.delete("/ollama/logs")
def clear_ollama_debug_logs():
    """Efface les logs Ollama en mémoire."""
    from app.logger import clear_ollama_logs
    clear_ollama_logs()
    return {"message": "Logs Ollama effacés"}


@router.get("/maintenance/stats")
def get_maintenance_stats():
    import os
    from app.models import Analysis, ChatConversation, MetaAnalysisResult, KeywordLearningSession
    from datetime import datetime, timedelta

    db = SessionLocal()
    try:
        config = db.query(GlobalConfig).first()
        retention_days = config.auto_delete_retention_days if config else 30
        threshold_date = datetime.utcnow() - timedelta(days=retention_days)

        # Taille dossier data
        data_dir = "./data/"
        total_size_bytes = 0
        if os.path.exists(data_dir):
            for dirpath, _, filenames in os.walk(data_dir):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    if not os.path.islink(fp):
                        total_size_bytes += os.path.getsize(fp)

        # Stats BDD (old items)
        old_analyses_count = db.query(Analysis).filter(Analysis.analyzed_at < threshold_date).count()
        # Find old chats
        old_chats_count = db.query(ChatConversation).filter(ChatConversation.created_at < threshold_date).count()
        old_meta_count = db.query(MetaAnalysisResult).filter(MetaAnalysisResult.created_at < threshold_date).count()
        old_sessions_count = db.query(KeywordLearningSession).filter(KeywordLearningSession.created_at < threshold_date).count()

        total_old_items = old_analyses_count + old_chats_count + old_meta_count + old_sessions_count

        return {
            "size_bytes": total_size_bytes,
            "retention_days": retention_days,
            "old_analyses": old_analyses_count,
            "old_chats": old_chats_count,
            "old_meta": old_meta_count,
            "old_sessions": old_sessions_count,
            "total_old_items": total_old_items
        }
    finally:
        db.close()


@router.delete("/maintenance/cleanup")
def cleanup_maintenance_data():
    from app.models import Analysis, ChatConversation, ChatMessage, MetaAnalysisResult, KeywordLearningSession
    from datetime import datetime, timedelta

    db = SessionLocal()
    try:
        config = db.query(GlobalConfig).first()
        retention_days = config.auto_delete_retention_days if config else 30
        threshold_date = datetime.utcnow() - timedelta(days=retention_days)

        # Delete old chat messages and conversations
        old_chats = db.query(ChatConversation).filter(ChatConversation.created_at < threshold_date).all()
        for chat in old_chats:
            db.query(ChatMessage).filter(ChatMessage.conversation_id == chat.id).delete()
            db.delete(chat)

        # Delete old analyses
        deleted_analyses = db.query(Analysis).filter(Analysis.analyzed_at < threshold_date).delete()

        # Delete old meta results
        deleted_meta = db.query(MetaAnalysisResult).filter(MetaAnalysisResult.created_at < threshold_date).delete()

        # Delete old keyword sessions
        deleted_sessions = db.query(KeywordLearningSession).filter(KeywordLearningSession.created_at < threshold_date).delete()

        db.commit()

        return {
            "ok": True,
            "detail": "Nettoyage terminé.",
            "deleted_analyses": deleted_analyses,
            "deleted_meta": deleted_meta,
            "deleted_sessions": deleted_sessions
        }
    except Exception as e:
        db.rollback()
        logger.error("ConfigRouter", f"Erreur nettoyage: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@router.get("/export")
def export_config(with_history: bool = False):
    db = SessionLocal()
    try:
        from app.models import Rule, MetaAnalysisConfig, Analysis, ResolutionVerdict, ChatConversation, ChatMessage, ChatCompression, MetaAnalysisResult, KeywordLearningSession
        from datetime import datetime
        
        def to_dict(obj):
            if not obj: return {}
            d = {}
            for column in obj.__table__.columns:
                val = getattr(obj, column.name)
                if isinstance(val, datetime):
                    d[column.name] = val.isoformat()
                else:
                    d[column.name] = val
            return d

        data = {
            "metadata": {
                "app_version": "1.2.284",
                "exported_at": datetime.utcnow().isoformat(),
                "with_history": with_history
            },
            "configuration": {
                "global_config": [to_dict(c) for c in db.query(GlobalConfig).all()],
                "rules": [to_dict(r) for r in db.query(Rule).all()],
                "meta_analysis_configs": [to_dict(m) for m in db.query(MetaAnalysisConfig).all()]
            }
        }
        
        if with_history:
            data["history"] = {
                "analyses": [to_dict(a) for a in db.query(Analysis).all()],
                "resolution_verdicts": [to_dict(rv) for rv in db.query(ResolutionVerdict).all()],
                "chat_conversations": [to_dict(cc) for cc in db.query(ChatConversation).all()],
                "chat_messages": [to_dict(cm) for cm in db.query(ChatMessage).all()],
                "chat_compressions": [to_dict(cp) for cp in db.query(ChatCompression).all()],
                "meta_analysis_results": [to_dict(mr) for mr in db.query(MetaAnalysisResult).all()],
                "keyword_learning_sessions": [to_dict(kl) for kl in db.query(KeywordLearningSession).all()]
            }
        
        # Build ZIP in memory
        json_data = json.dumps(data, indent=2, ensure_ascii=False)
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            zip_file.writestr("backup.json", json_data)
        
        zip_buffer.seek(0)
        date_str = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        suffix = "full" if with_history else "config"
        filename = f"sentinel_backup_{date_str}_{suffix}.zip"
        
        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        logger.error("ConfigRouter", f"Erreur export: {e}")
        raise HTTPException(status_code=500, detail=f"Erreur export: {str(e)}")
    finally:
        db.close()


@router.post("/import")
async def import_config(file: UploadFile = File(...)):
    if not file.filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="Le fichier doit être une archive ZIP.")
    
    db = SessionLocal()
    try:
        # Read zip
        contents = await file.read()
        zip_buffer = io.BytesIO(contents)
        try:
            with zipfile.ZipFile(zip_buffer, "r") as zip_file:
                if "backup.json" not in zip_file.namelist():
                    raise HTTPException(status_code=400, detail="Archive ZIP invalide: 'backup.json' introuvable.")
                json_data = zip_file.read("backup.json").decode("utf-8")
        except zipfile.BadZipFile:
            raise HTTPException(status_code=400, detail="Fichier ZIP corrompu ou invalide.")
        
        try:
            data = json.loads(json_data)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Le fichier backup.json n'est pas un JSON valide.")
        
        from app.models import Rule, MetaAnalysisConfig, Analysis, ResolutionVerdict, ChatConversation, ChatMessage, ChatCompression, MetaAnalysisResult, KeywordLearningSession
        from datetime import datetime
        
        def from_dict(model_cls, data_dict):
            kwargs = {}
            for column in model_cls.__table__.columns:
                if column.name in data_dict:
                    val = data_dict[column.name]
                    if val is not None and column.type.python_type == datetime:
                        try:
                            dt_str = val.replace("Z", "")
                            if "." in dt_str:
                                dt_str = dt_str.split(".")[0]
                            kwargs[column.name] = datetime.fromisoformat(dt_str)
                        except Exception:
                            kwargs[column.name] = None
                    else:
                        kwargs[column.name] = val
            return model_cls(**kwargs)

        config_data = data.get("configuration", {})
        
        # Begin transaction
        # Clear existing configs
        db.query(GlobalConfig).delete()
        db.query(Rule).delete()
        db.query(MetaAnalysisConfig).delete()
        
        # Restore configs
        for row in config_data.get("global_config", []):
            db.add(from_dict(GlobalConfig, row))
            
        for row in config_data.get("rules", []):
            db.add(from_dict(Rule, row))
            
        for row in config_data.get("meta_analysis_configs", []):
            db.add(from_dict(MetaAnalysisConfig, row))
            
        # Restore history if present
        history_data = data.get("history", {})
        if history_data:
            # Clear existing history tables
            db.query(ChatMessage).delete()
            db.query(ChatCompression).delete()
            db.query(ChatConversation).delete()
            db.query(Analysis).delete()
            db.query(ResolutionVerdict).delete()
            db.query(MetaAnalysisResult).delete()
            db.query(KeywordLearningSession).delete()
            
            # Restore in proper dependency order
            for row in history_data.get("analyses", []):
                db.add(from_dict(Analysis, row))
            for row in history_data.get("resolution_verdicts", []):
                db.add(from_dict(ResolutionVerdict, row))
            for row in history_data.get("chat_conversations", []):
                db.add(from_dict(ChatConversation, row))
            for row in history_data.get("chat_messages", []):
                db.add(from_dict(ChatMessage, row))
            for row in history_data.get("chat_compressions", []):
                db.add(from_dict(ChatCompression, row))
            for row in history_data.get("meta_analysis_results", []):
                db.add(from_dict(MetaAnalysisResult, row))
            for row in history_data.get("keyword_learning_sessions", []):
                db.add(from_dict(KeywordLearningSession, row))
        
        db.commit()
        
        # Reload syslog receiver if config changed
        try:
            from app.services.syslog_receiver import syslog_receiver
            await syslog_receiver.reload()
        except Exception as ex:
            logger.error("ConfigRouter", f"Erreur lors du rechargement de SyslogReceiver après import : {ex}")
            
        return {"ok": True, "message": "Importation réussie."}
    except Exception as e:
        db.rollback()
        logger.error("ConfigRouter", f"Erreur import: {e}")
        raise HTTPException(status_code=500, detail=f"Erreur d'importation: {str(e)}")
    finally:
        db.close()
