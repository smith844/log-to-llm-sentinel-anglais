from fastapi import APIRouter, HTTPException, Query, Request
from typing import Optional, Any
import json
import asyncio
from datetime import datetime

from app.database import SessionLocal
from app.models import Rule, Analysis, GlobalConfig
from app.services.orchestrator import Orchestrator
from app.services.task_manager import task_manager
from app.utils.log_utils import clean_log_line

router = APIRouter(prefix="/api/monitor", tags=["monitor"])

# Référence à l'orchestrateur partagé (accès aux buffers en mémoire)
_orchestrator: Optional[Orchestrator] = None
_resolution_service: Optional[Any] = None

def set_orchestrator(orch: Orchestrator):
    global _orchestrator
    _orchestrator = orch

def set_resolution_service(res_service):
    global _resolution_service
    _resolution_service = res_service


@router.post("/notify/{analysis_id}")
async def notify_analysis(analysis_id: int):
    """Envoie manuellement une notification pour une analyse."""
    if _orchestrator is None:
        raise HTTPException(status_code=500, detail="orchestrator_not_initialized")

    db = SessionLocal()
    try:
        analysis = db.query(Analysis).filter(Analysis.id == analysis_id).first()
        if not analysis:
            raise HTTPException(status_code=404, detail="analysis_not_found")

        rule = db.query(Rule).filter(Rule.id == analysis.rule_id).first()
        if not rule:
            raise HTTPException(status_code=404, detail="rule_not_found")

        from app.models import GlobalConfig
        from app.routers.config import _get_config_dict
        config_obj = db.query(GlobalConfig).first()
        if not config_obj:
            raise HTTPException(status_code=500, detail="global_config_not_found")
            
        config = _get_config_dict(config_obj)
        
        await _orchestrator.trigger_notification(analysis, rule, config, db)
        
        return {"status": "ok", "message": "Notification envoyée"}
    finally:
        db.close()


@router.get("/rules")
def get_monitored_rules():
    """Retourne toutes les règles actives avec leurs métadonnées pour le monitor."""
    db = SessionLocal()
    try:
        from app.models import GlobalConfig
        config = db.query(GlobalConfig).first()
        monitor_lines = config.monitor_log_lines if config else 60

        from sqlalchemy import func
        from app.models import Analysis
        
        # Récupération des statistiques par règle (uniquement non consultées)
        stats_query = db.query(
            Analysis.rule_id,
            Analysis.severity,
            func.count(Analysis.id).label("count")
        ).filter(Analysis.viewed == False).group_by(Analysis.rule_id, Analysis.severity).all()
        
        stats_dict = {}
        for rule_id, severity, count in stats_query:
            if rule_id not in stats_dict:
                stats_dict[rule_id] = {"total": 0, "info": 0, "warning": 0, "critical": 0}
            if severity in stats_dict[rule_id]:
                stats_dict[rule_id][severity] += count
            stats_dict[rule_id]["total"] += count

        # Récupération du nombre d'analyses non consultées par règle
        unviewed_query = db.query(
            Analysis.rule_id,
            func.count(Analysis.id).label("count")
        ).filter(Analysis.viewed == False).group_by(Analysis.rule_id).all()
        unviewed_dict = {rule_id: count for rule_id, count in unviewed_query}

        # Récupération de la dernière analyse par règle
        last_analysis_query = db.query(
            Analysis.rule_id,
            func.max(Analysis.analyzed_at).label("last_analyzed_at")
        ).group_by(Analysis.rule_id).all()
        last_analysis_dict = {row.rule_id: row.last_analyzed_at for row in last_analysis_query}

        rules = db.query(Rule).filter(Rule.enabled == True).all()
        return {
            "monitor_log_lines": monitor_lines,
            "rules": [
                {
                    "id": r.id,
                    "name": r.name,
                    "log_file_path": r.log_file_path,
                    "keywords": r.get_keywords(),
                    "excluded_patterns": r.get_excluded_patterns(),
                    "application_context": r.application_context,
                    "anti_spam_delay": r.anti_spam_delay or 60,
                    "notify_severity_threshold": r.notify_severity_threshold or "info",
                    "notify_on_match": r.notify_on_match,
                    "stats": stats_dict.get(r.id, {"total": 0, "info": 0, "warning": 0, "critical": 0}),
                    "unviewed_count": unviewed_dict.get(r.id, 0),
                    "last_learning_session_id": r.last_learning_session_id,
                    "last_line_received_at": r.last_line_received_at.isoformat() + "Z" if r.last_line_received_at else None,
                    "last_analysis_at": last_analysis_dict.get(r.id).isoformat() + "Z" if last_analysis_dict.get(r.id) else None,
                    "inactivity_warning_enabled": r.inactivity_warning_enabled,
                    "inactivity_period_hours": r.inactivity_period_hours,
                    # MON-18
                    "alert_status": r.alert_status or "normal",
                    "alert_started_at": r.alert_started_at.isoformat() + "Z" if r.alert_started_at else None,
                    "resolution_mode": r.resolution_mode or "timeout",
                    "resolution_timeout_minutes": r.resolution_timeout_minutes or 30,
                    "resolution_patterns": r.get_resolution_patterns(),
                    "resolution_ai_enabled": r.resolution_ai_enabled or False,
                    "resolution_notify_search": r.resolution_notify_search or False,
                    "resolution_notify_resolved": r.resolution_notify_resolved or False,
                }
                for r in rules
            ]
        }
    finally:
        db.close()


@router.post("/analyses/{analysis_id}/view")
def mark_analysis_viewed(analysis_id: int):
    """Marque une analyse comme consultée/lue."""
    db = SessionLocal()
    try:
        analysis = db.query(Analysis).filter(Analysis.id == analysis_id).first()
        if not analysis:
            raise HTTPException(status_code=404, detail="analysis_not_found")
        analysis.viewed = True
        db.commit()
        return {"status": "ok"}
    finally:
        db.close()


@router.post("/rules/{rule_id}/view-all")
def mark_all_analyses_viewed(rule_id: int):
    """Marque toutes les analyses d'une règle comme consultées/lues."""
    db = SessionLocal()
    try:
        db.query(Analysis).filter(Analysis.rule_id == rule_id, Analysis.viewed == False).update({Analysis.viewed: True})
        db.commit()
        return {"status": "ok"}
    finally:
        db.close()




@router.get("/syslog/tail/{hostname}")
def tail_syslog(
    hostname: str,
    lines: int = Query(60, description="Nombre de lignes"),
    keywords: Optional[str] = Query(None, description="Mots-clés pour colorisation")
):
    """Retourne les dernières lignes reçues par syslog pour un hôte donné (ring buffer mémoire, persisté sur disque)."""
    from app.services.syslog_receiver import _get_buffer
    buf = _get_buffer(hostname)
    
    kw_list = [kw.strip().lower() for kw in keywords.split(",") if kw.strip()] if keywords else []
    
    tail = list(buf)[-lines:]
    result = []
    for raw in tail:
        matched_kws = [kw for kw in kw_list if kw in raw.lower()] if kw_list else []
        result.append({
            "text": raw,
            "matched": len(matched_kws) > 0,
            "matched_keywords": matched_kws
        })
    return {"lines": result}


@router.get("/buffer/{rule_id}")
def get_buffer_status(rule_id: int):
    """Retourne l'état actuel du buffer anti-spam pour une règle."""
    if _orchestrator is None:
        return {"active": False, "lines": [], "detection_id": None, "matched_keywords": []}

    buf = _orchestrator._buffers.get(rule_id)
    db = SessionLocal()
    try:
        rule = db.query(Rule).filter(Rule.id == rule_id).first()
        last_line_received_at = rule.last_line_received_at.isoformat() + "Z" if rule and rule.last_line_received_at else None
        inactivity_warning_enabled = rule.inactivity_warning_enabled if rule else False
        inactivity_period_hours = rule.inactivity_period_hours if rule else 12
        inactivity_notify = rule.inactivity_notify if rule else True
        alert_status = rule.alert_status or "normal" if rule else "normal"
        alert_started_at = rule.alert_started_at.isoformat() + "Z" if rule and rule.alert_started_at else None
        
        from sqlalchemy import func
        from app.models import Analysis
        last_analysis = db.query(func.max(Analysis.analyzed_at)).filter(Analysis.rule_id == rule_id).scalar()
        last_analysis_at = last_analysis.isoformat() + "Z" if last_analysis else None
    finally:
        db.close()

    if not buf or not buf.get("task"):
        return {
            "active": False, "lines": [], "detection_id": None, "matched_keywords": [],
            "last_line_received_at": last_line_received_at,
            "last_analysis_at": last_analysis_at,
            "inactivity_warning_enabled": inactivity_warning_enabled,
            "inactivity_period_hours": inactivity_period_hours,
            "inactivity_notify": inactivity_notify,
            "alert_status": alert_status,
            "alert_started_at": alert_started_at
        }

    return {
        "active": True,
        "detection_id": buf.get("detection_id"),
        "line_count": len(buf.get("lines", [])),
        "matched_keywords": list(buf.get("matched_keywords", set())),
        "preview_lines": buf.get("lines", [])[-5:],  # 5 dernières lignes du buffer
        "last_line_received_at": last_line_received_at,
        "last_analysis_at": last_analysis_at,
        "inactivity_warning_enabled": inactivity_warning_enabled,
        "inactivity_period_hours": inactivity_period_hours,
        "inactivity_notify": inactivity_notify,
        "alert_status": alert_status,
        "alert_started_at": alert_started_at
    }


@router.get("/analyses/{rule_id}")
def get_rule_analyses(rule_id: int, limit: int = Query(20)):
    """Retourne les analyses récentes d'une règle, enrichies avec detection_id et keywords."""
    db = SessionLocal()
    try:
        analyses = (
            db.query(Analysis)
            .filter(Analysis.rule_id == rule_id)
            .order_by(Analysis.analyzed_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": a.id,
                "detection_id": a.detection_id,
                "triggered_line": a.triggered_line,
                "matched_keywords": json.loads(a.matched_keywords_json or "[]"),
                "severity": a.severity,
                "ollama_response": a.ollama_response,
                "notified": a.notified,
                "viewed": a.viewed,
                "analyzed_at": a.analyzed_at.isoformat() if a.analyzed_at else None,
                "resolved_at": a.resolved_at.isoformat() if a.resolved_at else None,
                "resolution_status": a.resolution_status,
                "resolution_line": a.resolution_line,
                "resolution_patterns": json.loads(a.resolution_patterns_json or "[]"),
                "resolution_ai_explanation": a.resolution_ai_explanation,
                "resolution_ai_confidence": a.resolution_ai_confidence,
            }
            for a in analyses
        ]
    finally:
        db.close()


@router.get("/search")
def search_by_detection_id(id: str = Query(..., description="ID de détection à rechercher")):
    """Retrouve une critique par son detection_id."""
    db = SessionLocal()
    try:
        analysis = db.query(Analysis).filter(Analysis.detection_id == id).first()
        if not analysis:
            return {"found": False}

        rule = db.query(Rule).filter(Rule.id == analysis.rule_id).first()
        return {
            "found": True,
            "analysis": {
                "id": analysis.id,
                "detection_id": analysis.detection_id,
                "rule_id": analysis.rule_id,
                "rule_name": rule.name if rule else f"Règle #{analysis.rule_id}",
                "triggered_line": analysis.triggered_line,
                "matched_keywords": json.loads(analysis.matched_keywords_json or "[]"),
                "severity": analysis.severity,
                "ollama_response": analysis.ollama_response,
                "notified": analysis.notified,
                "viewed": analysis.viewed,
                "analyzed_at": analysis.analyzed_at.isoformat() if analysis.analyzed_at else None,
                "resolved_at": analysis.resolved_at.isoformat() if analysis.resolved_at else None,
                "resolution_status": analysis.resolution_status,
                "resolution_line": analysis.resolution_line,
                "resolution_patterns": json.loads(analysis.resolution_patterns_json or "[]"),
                "resolution_ai_explanation": analysis.resolution_ai_explanation,
                "resolution_ai_confidence": analysis.resolution_ai_confidence,
            }
        }
    finally:
        db.close()


@router.post("/retry/{analysis_id}")
async def retry_analysis(analysis_id: int):
    """
    Relance une analyse Ollama en arrière-plan (fire & forget).
    Retourne immédiatement un task_id. Le client pollingue GET /task/{task_id}.
    """
    if _orchestrator is None:
        raise HTTPException(status_code=500, detail="orchestrator_not_initialized")

    db = SessionLocal()
    try:
        analysis = db.query(Analysis).filter(Analysis.id == analysis_id).first()
        if not analysis:
            raise HTTPException(status_code=404, detail="analysis_not_found")

        rule = db.query(Rule).filter(Rule.id == analysis.rule_id).first()
        if not rule:
            raise HTTPException(status_code=404, detail="rule_not_found")

        from app.models import GlobalConfig
        from app.routers.config import _get_config_dict
        config_obj = db.query(GlobalConfig).first()
        if not config_obj:
            raise HTTPException(status_code=500, detail="global_config_not_found")
            
        config = _get_config_dict(config_obj)
        cleaned_line = clean_log_line(analysis.triggered_line)
        prompt = _orchestrator._build_prompt(rule, cleaned_line, config.get("system_prompt", ""), lang=config.get("ollama_prompt_lang", "en"))

        # Capturer les IDs nécessaires pour la tâche en arrière-plan
        analysis_id_captured = analysis.id
        detection_id = analysis.detection_id

        # Créer l'entrée dans le gestionnaire de tâches
        entry = task_manager.create_analysis_task()

        async def _do_retry():
            try:
                async with _orchestrator._ollama_semaphore:
                    try:
                        response = await asyncio.wait_for(
                            _orchestrator.ollama.analyze_async(
                                prompt=prompt,
                                url=config.get("ollama_url"),
                                model=config.get("ollama_model"),
                                think=config.get("ollama_think", True),
                                options={
                                    "temperature": config.get("ollama_temp", 0.1),
                                    "num_ctx": config.get("ollama_ctx", 4096)
                                }
                            ),
                            timeout=300.0
                        )
                    except asyncio.TimeoutError:
                        response = "[Erreur Ollama] Délai d'attente dépassé (300s)"

                from app import logger
                logger.add_ollama_log(prompt, response, detection_id)
                severity = _orchestrator._detect_severity(response)

                retry_db = SessionLocal()
                try:
                    a = retry_db.query(Analysis).filter(Analysis.id == analysis_id_captured).first()
                    if a:
                        a.ollama_response = response
                        a.severity = severity
                        a.analyzed_at = datetime.utcnow()
                        retry_db.commit()
                        entry.analysis_id = a.id
                finally:
                    retry_db.close()

                entry.status = "done"
            except Exception as e:
                entry.error = str(e)
                entry.status = "error"

        asyncio.create_task(_do_retry())
        return {"status": "pending", "task_id": entry.task_id}
    finally:
        db.close()


@router.get("/task/{task_id}")
def get_task_status(task_id: str):
    """Retourne le statut d'une tâche d'analyse en arrière-plan."""
    entry = task_manager.get_analysis_task(task_id)
    if not entry:
        raise HTTPException(status_code=404, detail="task_not_found")
    return {
        "status": entry.status,
        "analysis_id": entry.analysis_id,
        "error": entry.error,
    }
        
@router.post("/analyze-line")
async def analyze_line(data: dict):
    """
    Effectue une analyse manuelle d'une ligne spécifique en arrière-plan.
    Retourne immédiatement un task_id. Le client pollingue GET /task/{task_id}.
    """
    line = data.get("line")
    rule_id = data.get("rule_id")
    
    if not line or not rule_id:
        raise HTTPException(status_code=400, detail="missing_data")
        
    if _orchestrator is None:
        raise HTTPException(status_code=500, detail="orchestrator_not_initialized")

    db = SessionLocal()
    try:
        rule = db.query(Rule).filter(Rule.id == rule_id).first()
        if not rule:
            db.close()
            raise HTTPException(status_code=404, detail="rule_not_found")
            
        cfg = db.query(GlobalConfig).first()
        config = {
            "ollama_url":   cfg.ollama_url   if cfg else "http://ollama:11434",
            "ollama_model": cfg.ollama_model if cfg else "qwen3.5:0.8b",
            "ollama_temp":  cfg.ollama_temp  if cfg else 0.1,
            "ollama_ctx":   cfg.ollama_ctx   if cfg else 4096,
            "ollama_think": cfg.ollama_think if cfg else True,
            "system_prompt": cfg.system_prompt if cfg else "",
            "ollama_prompt_lang": (cfg.ollama_prompt_lang or "en") if cfg else "en"
        }
        rule_id_cap = rule.id
        cleaned_line = clean_log_line(line)
        prompt = _orchestrator._build_prompt(rule, cleaned_line, config.get("system_prompt", ""), lang=config.get("ollama_prompt_lang", "en"))
    finally:
        db.close()

    entry = task_manager.create_analysis_task()

    async def _do_analyze():
        try:
            async with _orchestrator._ollama_semaphore:
                try:
                    response = await asyncio.wait_for(
                        _orchestrator.ollama.analyze_async(
                            prompt=prompt,
                            url=config.get("ollama_url"),
                            model=config.get("ollama_model"),
                            think=config.get("ollama_think", True),
                            options={
                                "temperature": config.get("ollama_temp", 0.1),
                                "num_ctx":    config.get("ollama_ctx", 4096)
                            }
                        ),
                        timeout=300.0
                    )
                except asyncio.TimeoutError:
                    response = "[Erreur Ollama] Délai d'attente dépassé (300s)"

            from app import logger
            import uuid
            logger.add_ollama_log(prompt, response, "MANUAL")
            severity = _orchestrator._detect_severity(response)
            det_id = f"MANUAL-{uuid.uuid4().hex[:8]}"

            save_db = SessionLocal()
            try:
                analysis = Analysis(
                    rule_id=rule_id_cap,
                    detection_id=det_id,
                    triggered_line=line,
                    ollama_response=response,
                    severity=severity,
                    analyzed_at=datetime.utcnow(),
                )
                save_db.add(analysis)
                save_db.commit()
                save_db.refresh(analysis)
                entry.analysis_id = analysis.id
            finally:
                save_db.close()

            entry.status = "done"
        except Exception as e:
            entry.error = str(e)
            entry.status = "error"

    asyncio.create_task(_do_analyze())
    return {"status": "pending", "task_id": entry.task_id}

@router.post("/chat")
async def chat_analysis(data: dict, request: Request):
    """Continue la conversation sur une analyse."""
    analysis_id = data.get("analysis_id")
    question = data.get("question")
    context_prompt = data.get("context_prompt")  # Pour les analyses manuelles non sauvegardées
    context_response = data.get("context_response")
    
    if not question:
        raise HTTPException(status_code=400, detail="missing_question")
        
    db = SessionLocal()
    try:
        prompt = ""
        cfg = db.query(GlobalConfig).first()
        lang = (cfg.ollama_prompt_lang or "en") if cfg else "en"
        if analysis_id and str(analysis_id).isdigit():
            analysis = db.query(Analysis).filter(Analysis.id == analysis_id).first()
            if not analysis:
                raise HTTPException(status_code=404, detail="analysis_not_found")
            rule = db.query(Rule).filter(Rule.id == analysis.rule_id).first()
            cfg = db.query(GlobalConfig).first()
            
            # Reconstruire le prompt original (ou le stocker en BDD ?)
            # Ici on va construire un prompt de chat
            base_prompt = _orchestrator._build_prompt(rule, analysis.triggered_line, cfg.system_prompt if cfg else "", lang=(cfg.ollama_prompt_lang or "en") if cfg else "en")
            previous_label = "Your previous response" if lang == "en" else "Ta réponse précédente"
            question_label = "User question" if lang == "en" else "Question de l'utilisateur"
            prompt = f"{base_prompt}\n\n{previous_label}:\n{analysis.ollama_response}\n\n{question_label}: {question}"
        else:
            # Mode manuel ou contextuel
            if lang == "en":
                prompt = f"Analysis context:\n{context_prompt}\n\nPrevious response:\n{context_response}\n\nUser question: {question}\n\nRespond in English."
            else:
                prompt = f"Contexte de l'analyse :\n{context_prompt}\n\nRéponse précédente :\n{context_response}\n\nQuestion de l'utilisateur : {question}"

        cfg = db.query(GlobalConfig).first()
        if not cfg:
            raise HTTPException(status_code=500, detail="config_not_found")

        from app.routers.utils import cancel_on_disconnect
        async with _orchestrator._ollama_semaphore:
            try:
                coro = _orchestrator.ollama.analyze_async(
                    prompt=prompt,
                    url=cfg.ollama_url,
                    model=cfg.ollama_model,
                    think=cfg.ollama_think if hasattr(cfg, 'ollama_think') else True,
                    options={
                        "temperature": cfg.ollama_temp if hasattr(cfg, 'ollama_temp') else 0.1,
                        "num_ctx": cfg.ollama_ctx if hasattr(cfg, 'ollama_ctx') else 4096
                    }
                )
                response = await cancel_on_disconnect(
                    request,
                    asyncio.wait_for(coro, timeout=300.0)
                )
            except asyncio.TimeoutError:
                response = "[Erreur Ollama] Délai d'attente dépassé (300s)"
            
        from app import logger
        logger.add_ollama_log(f"CHAT: {question}", response, "CHAT")
        
        return {"status": "ok", "response": response}
    finally:
        db.close()


@router.post("/rules/{rule_id}/resolve")
async def resolve_rule_manually(rule_id: int):
    """Résolution manuelle par l'utilisateur."""
    if _resolution_service is None:
        raise HTTPException(status_code=500, detail="resolution_service_not_initialized")
    
    await _resolution_service.mark_resolved_manually(rule_id)
    return {"status": "ok", "message": "Règle marquée comme résolue"}


@router.get("/rules/{rule_id}/resolution-status")
def get_resolution_status(rule_id: int):
    """Retourne l'état de résolution courant d'une règle."""
    if _resolution_service is None:
        raise HTTPException(status_code=500, detail="resolution_service_not_initialized")

    state = _resolution_service._alert_states.get(rule_id)
    if not state:
        return {
            "status": "normal",
            "started_at": None,
            "last_error_at": None,
            "max_severity": "info",
        }

    return {
        "status": state.get("status", "normal"),
        "started_at": state.get("started_at").isoformat() + "Z" if state.get("started_at") else None,
        "last_error_at": state.get("last_error_at").isoformat() + "Z" if state.get("last_error_at") else None,
        "max_severity": state.get("max_severity", "info"),
    }


@router.get("/rules/{rule_id}/resolution-history")
def get_resolution_history(rule_id: int, limit: int = Query(20), outcome: str = Query(None)):
    """Retourne l'historique pagine des verdicts de resolution d'une regle.
    Optionnel : filtrer par outcome (accepted, rejected_ai, rejected_low_confidence, manual, false_positive_user)."""
    from app.models import ResolutionVerdict
    db = SessionLocal()
    try:
        q = db.query(ResolutionVerdict).filter(ResolutionVerdict.rule_id == rule_id)
        if outcome:
            q = q.filter(ResolutionVerdict.outcome == outcome)
        total = q.count()
        verdicts = q.order_by(ResolutionVerdict.created_at.desc()).limit(limit).all()
        return {
            "total": total,
            "verdicts": [
                {
                    "id": v.id,
                    "trigger": v.trigger,
                    "outcome": v.outcome,
                    "ai_resolved": v.ai_resolved,
                    "ai_confidence": v.ai_confidence,
                    "ai_explanation": v.ai_explanation,
                    "max_severity": v.max_severity,
                    "resolution_line": v.resolution_line,
                    "resolution_patterns": json.loads(v.resolution_patterns_json or "[]"),
                    "context_lines": json.loads(v.context_lines_json or "[]"),
                    "created_at": v.created_at.isoformat() + "Z" if v.created_at else None,
                }
                for v in verdicts
            ]
        }
    finally:
        db.close()


@router.post("/verdicts/{verdict_id}/mark-false-positive")
async def mark_verdict_false_positive(verdict_id: int):
    """Marque un verdict comme faux-positif, decremente les poids des patterns impliques,
    et remet la regle en alerte si elle est actuellement 'normal'."""
    from app.models import ResolutionVerdict
    db = SessionLocal()
    try:
        verdict = db.query(ResolutionVerdict).filter(ResolutionVerdict.id == verdict_id).first()
        if not verdict:
            raise HTTPException(status_code=404, detail="verdict_not_found")

        rule = db.query(Rule).filter(Rule.id == verdict.rule_id).first()
        if not rule:
            raise HTTPException(status_code=404, detail="rule_not_found")

        # Mise a jour du verdict
        verdict.outcome = "false_positive_user"
        
        # Decrementation du poids des patterns impliques
        patterns = json.loads(verdict.resolution_patterns_json or "[]")
        for pattern in patterns:
            rule.decrement_pattern_weight(pattern)

        # Si la regle est actuellement 'normal' suite a ce verdict accepte, on la remet en alerte
        if rule.alert_status == "normal" and verdict.outcome in ("accepted", "accepted_no_ai", "manual"):
            rule.alert_status = "alert"
            if _resolution_service and verdict.rule_id in _resolution_service._alert_states:
                _resolution_service._alert_states[verdict.rule_id]["status"] = "alert"

        db.commit()
        return {"status": "ok", "patterns_decremented": patterns}
    finally:
        db.close()


@router.get("/rules/{rule_id}/weighted-patterns")
def get_weighted_patterns(rule_id: int):
    """Retourne les patterns de resolution enrichis (poids, last_validated_at, error_keywords)."""
    db = SessionLocal()
    try:
        rule = db.query(Rule).filter(Rule.id == rule_id).first()
        if not rule:
            raise HTTPException(status_code=404, detail="rule_not_found")
        weighted = rule.get_weighted_resolution_patterns()
        return {"rule_id": rule_id, "patterns": weighted, "count": len(weighted)}
    finally:
        db.close()


@router.delete("/rules/{rule_id}/patterns/{pattern}")
def delete_single_pattern(rule_id: int, pattern: str):
    """Supprime un pattern de resolution specifique (via son nom encode en URL)."""
    from urllib.parse import unquote
    pattern_decoded = unquote(pattern)
    db = SessionLocal()
    try:
        rule = db.query(Rule).filter(Rule.id == rule_id).first()
        if not rule:
            raise HTTPException(status_code=404, detail="rule_not_found")
        before = len(rule.get_weighted_resolution_patterns())
        rule.remove_pattern(pattern_decoded)
        after = len(rule.get_weighted_resolution_patterns())
        db.commit()
        return {"status": "ok", "removed": pattern_decoded, "count_before": before, "count_after": after}
    finally:
        db.close()


@router.post("/rules/{rule_id}/audit-patterns")
async def audit_patterns(rule_id: int):
    """Demande au LLM d'auditer la pertinence des patterns de resolution d'une regle.
    Supprime automatiquement les patterns juges non pertinents."""
    if _resolution_service is None:
        raise HTTPException(status_code=500, detail="resolution_service_not_initialized")

    result = await _resolution_service.audit_patterns_with_ai(rule_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result)
    return result
