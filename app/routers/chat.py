from app.utils.language import get_language, language_instruction
from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from datetime import datetime
from typing import Optional
import asyncio
import json

from app.database import SessionLocal
from app.models import Rule, Analysis, GlobalConfig, ChatConversation, ChatMessage, ChatCompression
from app.services.orchestrator import Orchestrator
from app.services.task_manager import task_manager, ChatTaskEntry
from app import logger

router = APIRouter(prefix="/chat", tags=["chat"])
templates = Jinja2Templates(directory="templates")

# Share the same globals as main.py templates (needed because base.html uses them)
from app.main import APP_VERSION, check_for_updates
templates.env.globals['APP_VERSION'] = APP_VERSION
templates.env.globals['UPDATE_STATUS'] = check_for_updates(APP_VERSION)

_orchestrator: Optional[Orchestrator] = None

def set_orchestrator(orch: Orchestrator):
    global _orchestrator
    _orchestrator = orch

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.get("/", response_class=HTMLResponse)
async def chat_page(request: Request, id: Optional[int] = None):
    return templates.TemplateResponse("chat.html", {"request": request, "conversation_id": id})

@router.get("/api/conversations")
async def list_conversations(db: Session = Depends(get_db)):
    convs = db.query(ChatConversation).order_by(ChatConversation.created_at.desc()).all()
    return [
        {
            "id": c.id,
            "title": c.title,
            "created_at": c.created_at.isoformat(),
            "analysis_id": c.analysis_id
        } for c in convs
    ]

@router.post("/api/create")
async def create_conversation(data: dict, db: Session = Depends(get_db)):
    analysis_id = data.get("analysis_id")
    lang = get_language(db.query(GlobalConfig).first())
    title = data.get("title") or ("New conversation" if lang == "en" else "Nouvelle conversation")
    raw_prompt = data.get("raw_context_prompt")
    raw_response = data.get("raw_context_response")
    
    if analysis_id:
        analysis = db.query(Analysis).filter(Analysis.id == analysis_id).first()
        if analysis:
            title = f"{'Analysis' if lang == 'en' else 'Analyse'} #{analysis.detection_id or analysis.id}"
    elif raw_prompt:
        title = "Manual analysis" if lang == "en" else "Analyse manuelle"
    
    conv = ChatConversation(title=title, analysis_id=analysis_id)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    
    if raw_prompt and raw_response:
        msg = ChatMessage(conversation_id=conv.id, role="assistant", content=f"**{'Manual context' if lang == 'en' else 'Contexte Manuel'}:**\n{raw_prompt}\n\n**{'Initial analysis' if lang == 'en' else 'Analyse initiale'}:**\n{raw_response}")
        db.add(msg)
        db.commit()
    
    return {"status": "ok", "id": conv.id}

@router.get("/api/history/{conv_id}")
async def get_history(conv_id: int, db: Session = Depends(get_db)):
    conv = db.query(ChatConversation).filter(ChatConversation.id == conv_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation non trouvée")
    
    messages = db.query(ChatMessage).filter(ChatMessage.conversation_id == conv_id).order_by(ChatMessage.created_at.asc()).all()
    
    analysis = None
    if conv.analysis_id:
        a = db.query(Analysis).filter(Analysis.id == conv.analysis_id).first()
        if a:
            analysis = {
                "triggered_line": a.triggered_line,
                "ollama_response": a.ollama_response,
                "detection_id": a.detection_id
            }
            
    # 09-B : récupérer l'historique des compressions
    compressions = db.query(ChatCompression).filter(
        ChatCompression.conversation_id == conv_id
    ).order_by(ChatCompression.compressed_at.asc()).all()

    return {
        "title": conv.title,
        "analysis_id": conv.analysis_id,
        "analysis": analysis,
        "compression_mode": conv.compression_mode,
        "compressed_context": conv.compressed_context,
        "compressed_at": conv.compressed_at.isoformat() if conv.compressed_at else None,
        "auto_compression_mode": conv.auto_compression_mode,  # 09-C
        "compressions": [
            {
                "id": c.id,
                "mode": c.mode,
                "content": c.content,
                "compressed_at": c.compressed_at.isoformat() if c.compressed_at else None
            }
            for c in compressions
        ],
        "messages": [
            {"id": m.id, "role": m.role, "content": m.content, "created_at": m.created_at.isoformat()}
            for m in messages
        ]
    }


def build_chat_prompt(conv_id: int, db: Session) -> tuple[str, int]:
    conv = db.query(ChatConversation).filter(ChatConversation.id == conv_id).first()
    if not conv:
        return "", 4096
        
    history_query = db.query(ChatMessage).filter(ChatMessage.conversation_id == conv_id)
    if conv.compressed_at:
        history_query = history_query.filter(ChatMessage.created_at > conv.compressed_at)
    history = history_query.order_by(ChatMessage.created_at.asc()).all()
    
    # 1. Charger la configuration de base (System Prompt & Mode Text)
    cfg = db.query(GlobalConfig).first()
    system_prompt = cfg.chat_system_prompt.strip() if cfg and cfg.chat_system_prompt else ""
    lang = get_language(cfg)
    lang_file = f"static/i18n/{lang}.json"
    mode_text = ""
    
    import os, json
    from datetime import datetime
    if os.path.exists(lang_file):
        with open(lang_file, 'r', encoding='utf-8-sig') as f:
            lang_data = json.load(f)
            mode_text = lang_data.get("chat", {}).get("mode_de_reponse", "")

    current_dt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    active_rules = db.query(Rule).filter(Rule.enabled == True).all()
    rules_lines = []
    for r in active_rules:
        ctx = f" (Context: {r.application_context})" if r.application_context else ""
        rules_lines.append(f"- {r.name}{ctx}")
    rules_list_str = "\n".join(rules_lines) if rules_lines else ("- No rules configured" if lang == "en" else "- Aucune règle configurée")
    
    if mode_text:
        mode_text = mode_text.replace("{datetime}", current_dt)
        mode_text = mode_text.replace("{lang}", lang.upper())
        mode_text = mode_text.replace("{rules_list}", rules_list_str)

    # 2. Construire le bloc de contexte système
    block_lines = []
    if system_prompt:
        block_lines.extend([system_prompt, ""])
        
    if conv.compressed_context:
        block_lines.extend([
            "=== Compressed context ===" if lang == "en" else "=== Contexte Compressé ===",
            conv.compressed_context,
            ""
        ])
    elif conv.analysis_id:
        analysis = db.query(Analysis).filter(Analysis.id == conv.analysis_id).first()
        if analysis:
            rule = db.query(Rule).filter(Rule.id == analysis.rule_id).first()
            block_lines.extend([
                "You are an expert in Linux, Docker and infrastructure." if lang == "en" else "Tu es un assistant expert en systèmes Linux, Docker et infrastructure.",
                "A log analysis was performed. The user is asking follow-up questions." if lang == "en" else "Une analyse de log a été effectuée. L'utilisateur te pose des questions de suivi.",
                "",
                f"=== {'Rule' if lang == 'en' else 'Règle'} : {rule.name if rule else ('Unknown' if lang == 'en' else 'Inconnue')} ===",
                f"Application : {rule.application_context if rule else ''}",
                "",
                "=== Triggering log line ===" if lang == "en" else "=== Ligne de log concernée ===",
                analysis.triggered_line,
                "",
                "=== Initial analysis ===" if lang == "en" else "=== Analyse initiale ===",
                analysis.ollama_response,
                ""
            ])

    if mode_text:
        block_lines.append(mode_text)

    prompt = "\n".join(block_lines).strip() + "\n\n" if block_lines else ""

    # 3. Ajouter l'historique de la conversation
    # On limite à 20 messages récents au lieu de 10 car le contexte est géré
    for m in history[-20:]:
        role_label = ("User" if lang == "en" else "Utilisateur") if m.role == "user" else "Assistant"
        prompt += f"{role_label} : {m.content}\n"
    
    prompt += "\n" + language_instruction(lang) + "\n"
    ollama_ctx = cfg.ollama_ctx if cfg else 4096
    
    return prompt, ollama_ctx


import uuid
from pydantic import BaseModel
from app.utils.compression import run_compaction, run_summary, run_truncation

_compression_tasks = {}

class CompressRequest(BaseModel):
    mode: str
    remember: bool = False  # 09-C : mémoriser ce mode pour la conv

@router.post("/api/compress/{conv_id}")
async def start_compression(conv_id: int, req: CompressRequest, db: Session = Depends(get_db)):
    lang = get_language(db.query(GlobalConfig).first())
    conv = db.query(ChatConversation).filter(ChatConversation.id == conv_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
        
    history = db.query(ChatMessage).filter(ChatMessage.conversation_id == conv_id).order_by(ChatMessage.created_at.asc()).all()
    
    # Filtrer les messages déjà compressés
    messages_to_process = []
    for m in history:
        if conv.compressed_at and m.created_at <= conv.compressed_at:
            continue
        messages_to_process.append(m)
    
    if not messages_to_process:
        raise HTTPException(status_code=400, detail="Aucun message à compresser")
    
    if req.mode == "truncate":
        # Truncate : garder les récents, résumer les anciens en une ligne chacun
        cfg = db.query(GlobalConfig).first()
        max_ctx = cfg.ollama_ctx if cfg else 4096
        
        dropped, kept = run_truncation(messages_to_process, max_ctx)
        
        if not dropped:
            return {"status": "done", "mode": "truncate", "message": "Rien à tronquer"}
        
        # Construire le résumé des messages tronqués
        dropped_lines = []
        for m in dropped:
            role_label = "U" if m.role == "user" else "A"
            first_line = m.content.strip().split('\n')[0][:120]
            dropped_lines.append(f"- {role_label}: {first_line}")
        
        compressed = ("[Truncated]\n" if lang == "en" else "[Tronqué]\n") + "\n".join(dropped_lines)
        
        # Cutoff = timestamp du premier message GARDÉ (les kept restent visibles)
        cutoff_time = kept[0].created_at if kept else datetime.utcnow()
        # Reculer d'1 seconde pour que le premier kept passe le filtre ">"
        from datetime import timedelta
        cutoff_time = cutoff_time - timedelta(seconds=1)
        
        conv.compression_mode = "truncate"
        conv.compressed_context = compressed
        conv.compressed_at = cutoff_time
        # 09-C : mémoriser le mode auto
        if req.mode:
            conv.auto_compression_mode = req.mode
        # 09-B : enregistrer dans l'historique des compressions
        new_comp = ChatCompression(
            conversation_id=conv_id,
            mode="truncate",
            content=compressed,
            compressed_at=cutoff_time
        )
        db.add(new_comp)
        db.commit()
        
        return {"status": "done", "mode": "truncate"}
    
    # Pour compact et summary : construire le texte complet
    text_to_compress = ""
    # 09-A : compression chaînée — inclure le contexte précédent en tête
    if conv.compressed_context:
        text_to_compress += f"=== Contexte Précédent (compressé) ===\n{conv.compressed_context}\n\n"
    elif conv.analysis_id:
        a = db.query(Analysis).filter(Analysis.id == conv.analysis_id).first()
        if a:
            text_to_compress += f"=== Analyse initiale ===\n{a.triggered_line}\n{a.ollama_response}\n\n"
            
    for m in messages_to_process:
        role_label = ("User" if lang == "en" else "Utilisateur") if m.role == "user" else "Assistant"
        text_to_compress += f"{role_label} : {m.content}\n\n"
        
    text_to_compress = text_to_compress.strip()
    cutoff_time = datetime.utcnow()
    
    if req.mode in ("compact", "summary"):
        task_id = str(uuid.uuid4())
        _compression_tasks[task_id] = {"status": "running", "result": None, "error": None}
        
        async def background_compress(t_id, txt, cid, dt, mode):
            try:
                if not _orchestrator:
                    raise Exception("Orchestrator non initialisé")
                    
                url = "http://ollama:11434"
                model = "gemma4:e4b"
                ctx_size = 4096
                with SessionLocal() as s:
                    cfg = s.query(GlobalConfig).first()
                    if cfg:
                        url = cfg.ollama_url or url
                        model = cfg.ollama_model or model
                        ctx_size = cfg.ollama_ctx or ctx_size

                lang = get_language(cfg)
                if mode == "compact":
                    res = await run_compaction(txt, _orchestrator.ollama, url, model, num_ctx=ctx_size, lang=lang)
                else:
                    res = await run_summary(txt, _orchestrator.ollama, url, model, num_ctx=ctx_size, lang=lang)
                
                with SessionLocal() as s:
                    c = s.query(ChatConversation).filter(ChatConversation.id == cid).first()
                    if c:
                        c.compression_mode = mode
                        c.compressed_context = res
                        c.compressed_at = dt
                        if mode:  # 09-C : mémoriser mode auto par conv
                            c.auto_compression_mode = mode
                        # 09-B : enregistrer dans l'historique des compressions
                        new_comp = ChatCompression(
                            conversation_id=cid,
                            mode=mode,
                            content=res,
                            compressed_at=dt
                        )
                        s.add(new_comp)
                        s.commit()
                _compression_tasks[t_id]["status"] = "done"
                _compression_tasks[t_id]["result"] = res
            except Exception as e:
                _compression_tasks[t_id]["status"] = "error"
                _compression_tasks[t_id]["error"] = str(e)
                
        asyncio.create_task(background_compress(task_id, text_to_compress, conv_id, cutoff_time, req.mode))
        return {"status": "running", "task_id": task_id}
        
    else:
        raise HTTPException(status_code=400, detail="Mode invalide")

@router.get("/api/compress/status/{task_id}")
async def get_compression_status(task_id: str):
    if task_id not in _compression_tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    return _compression_tasks[task_id]

@router.delete("/api/compress/{conv_id}")
async def revert_compression(conv_id: int, db: Session = Depends(get_db)):
    conv = db.query(ChatConversation).filter(ChatConversation.id == conv_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conv.compression_mode = None
    conv.compressed_context = None
    conv.compressed_at = None
    conv.auto_compression_mode = None  # 09-C : réinitialiser le mode auto
    # 09-B : supprimer tout l'historique des compressions de cette conv
    db.query(ChatCompression).filter(ChatCompression.conversation_id == conv_id).delete()
    db.commit()
    return {"status": "ok"}

@router.put("/api/compress/{conv_id}")
async def update_compression(conv_id: int, data: dict, db: Session = Depends(get_db)):
    conv = db.query(ChatConversation).filter(ChatConversation.id == conv_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    new_context = data.get("compressed_context")
    if not new_context:
        raise HTTPException(status_code=400, detail="compressed_context requis")
    # Mettre à jour conv + dernière entrée ChatCompression (09-B)
    conv.compressed_context = new_context
    last_comp = db.query(ChatCompression).filter(
        ChatCompression.conversation_id == conv_id
    ).order_by(ChatCompression.compressed_at.desc()).first()
    if last_comp:
        last_comp.content = new_context
    db.commit()
    return {"status": "ok"}

@router.delete("/api/auto-compression/{conv_id}")
async def reset_auto_compression(conv_id: int, db: Session = Depends(get_db)):
    """09-C : réinitialise uniquement le mode auto sans toucher aux compressions existantes."""
    conv = db.query(ChatConversation).filter(ChatConversation.id == conv_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conv.auto_compression_mode = None
    db.commit()
    return {"status": "ok"}

@router.delete("/api/message/{msg_id}")
async def delete_message(msg_id: int, db: Session = Depends(get_db)):
    """Supprime un message individuel par son ID."""
    msg = db.query(ChatMessage).filter(ChatMessage.id == msg_id).first()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    db.delete(msg)
    db.commit()
    return {"status": "ok"}

@router.get("/api/context/{conv_id}")
async def get_chat_context(conv_id: int, db: Session = Depends(get_db)):
    base_prompt, ollama_ctx = build_chat_prompt(conv_id, db)
    return {"base_prompt": base_prompt, "ollama_ctx": ollama_ctx}

@router.post("/api/send")
async def send_message(data: dict, db: Session = Depends(get_db)):
    """
    Démarre la génération d'une réponse chat en arrière-plan.
    Retourne immédiatement un task_id. Le client se connecte ensuite
    à /api/stream/{task_id} pour lire les tokens au fur et à mesure.
    La génération continue même si le client se déconnecte.
    """
    conv_id = data.get("conversation_id")
    content = data.get("content")
    
    logger.info("ChatRouter", f"Requête reçue pour conv {conv_id}")
    
    if not conv_id or not content:
        raise HTTPException(status_code=400, detail="missing_data")
        
    conv = db.query(ChatConversation).filter(ChatConversation.id == conv_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation non trouvée")

    # 1. Sauvegarder le message utilisateur
    user_msg = ChatMessage(conversation_id=conv_id, role="user", content=content)
    db.add(user_msg)
    db.commit()

    # 2. Construire le contexte
    prompt, ollama_ctx = build_chat_prompt(conv_id, db)
    prompt += "Assistant : "


    cfg = db.query(GlobalConfig).first()
    ollama_url    = (cfg.ollama_url    or "http://ollama:11434") if cfg else "http://ollama:11434"
    ollama_model  = (cfg.ollama_model  or "gemma4:e4b")         if cfg else "gemma4:e4b"
    ollama_temp   = cfg.ollama_temp   if cfg else 0.1
    ollama_ctx    = cfg.ollama_ctx    if cfg else 4096
    ollama_think  = cfg.ollama_think  if cfg else True

    # 3. Créer une tâche en arrière-plan
    entry = task_manager.create_chat_task(conv_id)
    task_id = entry.task_id

    # 4. Lancer la génération sans attendre (fire & forget)
    asyncio.create_task(
        _run_chat_generation(
            entry=entry,
            conv_id=conv_id,
            prompt=prompt,
            ollama_url=ollama_url,
            ollama_model=ollama_model,
            ollama_temp=ollama_temp,
            ollama_ctx=ollama_ctx,
            ollama_think=ollama_think,
        )
    )

    return {"status": "ok", "task_id": task_id}


async def _run_chat_generation(
    entry: ChatTaskEntry,
    conv_id: int,
    prompt: str,
    ollama_url: str,
    ollama_model: str,
    ollama_temp: float,
    ollama_ctx: int,
    ollama_think: bool,
):
    """
    Coroutine arrière-plan : génère les tokens Ollama, les accémule dans le buffer
    de l'entrée et sauvegarde la réponse complète en BDD à la fin.
    S'exécute indépendamment de la connexion HTTP du client.
    """
    full_response = ""
    is_thinking = False

    try:
        if not _orchestrator:
            entry.error = "Orchestrateur non initialisé"
            entry.status = "error"
            return

        async with _orchestrator._ollama_semaphore:
            async for chunk in _orchestrator.ollama.generate_stream(
                prompt=prompt,
                url=ollama_url,
                model=ollama_model,
                think=ollama_think,
                options={"temperature": ollama_temp, "num_ctx": ollama_ctx}
            ):
                if "error" in chunk:
                    entry.error = chunk["error"]
                    entry.status = "error"
                    return

                text = chunk.get("message", {}).get("content", "") or chunk.get("response", "")
                if text:
                    if not is_thinking:
                        if "<think>" in text:
                            is_thinking = True
                            to_send = text.split("<think>", 1)[0]
                            if to_send:
                                full_response += to_send
                                task_manager.append_chat_token(entry, to_send)
                        else:
                            full_response += text
                            task_manager.append_chat_token(entry, text)
                    else:
                        if "</think>" in text:
                            is_thinking = False
                            to_send = text.split("</think>", 1)[-1]
                            if to_send:
                                full_response += to_send
                                task_manager.append_chat_token(entry, to_send)

                if chunk.get("done"):
                    break

        # Sauvegarde BDD
        save_db = SessionLocal()
        try:
            ai_msg = ChatMessage(conversation_id=conv_id, role="assistant", content=full_response)
            save_db.add(ai_msg)
            save_db.commit()
            logger.info("ChatRouter", f"Réponse sauvegardée ({len(full_response)} car.)")
        finally:
            save_db.close()

        entry.status = "done"

    except Exception as e:
        logger.error("ChatRouter", f"Erreur génération arrière-plan : {str(e)}")
        entry.error = str(e)
        entry.status = "error"


@router.get("/api/stream/{task_id}")
async def stream_chat(task_id: str, from_pos: int = 0):
    """
    Endpoint SSE reconnectable : diffuse les tokens d'une tâche chat.
    Le paramètre `from_pos` permet de reprendre là où le client s'est arrêté.
    La connexion reste ouverte jusqu'à ce que la génération soit terminée.
    """
    entry = task_manager.get_chat_task(task_id)
    if not entry:
        raise HTTPException(status_code=404, detail="task_not_found")

    async def generator():
        pos = from_pos
        while True:
            buf = entry.token_buffer
            if pos < len(buf):
                # Envoyer les tokens en attente (rattrapage ou nouveaux)
                for token in buf[pos:]:
                    pos += 1
                    yield f"data: {json.dumps({'text': token, 'pos': pos})}\n\n"
            elif entry.status == "done":
                yield f"data: {json.dumps({'done': True})}\n\n"
                break
            elif entry.status == "error":
                yield f"data: {json.dumps({'error': entry.error})}\n\n"
                break
            else:
                # Attendre de nouveaux tokens (polling léger)
                await task_manager.wait_for_token(entry, timeout=2.0)

    return StreamingResponse(generator(), media_type="text/event-stream")


@router.get("/api/pending/{conv_id}")
async def get_pending_chat(conv_id: int):
    """
    Indique si une génération est en cours pour cette conversation.
    Utilisé par le frontend lors du retour sur l'onglet (visibilitychange).
    """
    entry = task_manager.get_pending_chat_for_conv(conv_id)
    if not entry:
        return {"task_id": None}
    return {
        "task_id": entry.task_id,
        "from_pos": len(entry.token_buffer),  # Le client reprend depuis la fin du buffer connu
        "buffered_tokens": len(entry.token_buffer),
    }

@router.delete("/api/delete/{conv_id}")
async def delete_conversation(conv_id: int, db: Session = Depends(get_db)):
    conv = db.query(ChatConversation).filter(ChatConversation.id == conv_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation non trouvée")
    
    db.delete(conv)
    db.commit()
    return {"status": "ok"}


@router.post("/api/regenerate/{conv_id}")
async def regenerate_last(conv_id: int, db: Session = Depends(get_db)):
    """
    Supprime le dernier message assistant de la conversation et retourne
    le contenu du dernier message utilisateur pour redéclencher la génération.
    Utilisé par le bouton 'Ré-essayer' sur les bulles IA.
    """
    # Supprimer le dernier message assistant
    last_assistant = (
        db.query(ChatMessage)
        .filter(ChatMessage.conversation_id == conv_id, ChatMessage.role == "assistant")
        .order_by(ChatMessage.created_at.desc())
        .first()
    )
    if last_assistant:
        db.delete(last_assistant)
        db.commit()

    # Retourner le dernier message utilisateur (pour le reré-envoyer)
    last_user = (
        db.query(ChatMessage)
        .filter(ChatMessage.conversation_id == conv_id, ChatMessage.role == "user")
        .order_by(ChatMessage.created_at.desc())
        .first()
    )
    if not last_user:
        raise HTTPException(status_code=404, detail="Aucun message utilisateur trouvé")

    return {"status": "ok", "last_user_content": last_user.content}


@router.post("/api/auto-title/{conv_id}")
async def auto_title_conversation(conv_id: int, db: Session = Depends(get_db)):
    """
    Génère automatiquement un titre court pour la conversation
    à partir du premier échange (1er message user + 1ère réponse assistant).
    Si la conversation est liée à une analyse, préfixe avec [#detection_id].
    """
    conv = db.query(ChatConversation).filter(ChatConversation.id == conv_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation non trouvée")

    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.conversation_id == conv_id)
        .order_by(ChatMessage.created_at.asc())
        .limit(4)
        .all()
    )

    user_msg = next((m for m in messages if m.role == "user"), None)
    ai_msg = next((m for m in messages if m.role == "assistant"), None)

    if not user_msg or not ai_msg:
        return {"title": conv.title}

    # Construire le mini-prompt de titrage
    excerpt_user = user_msg.content[:300]
    excerpt_ai = ai_msg.content[:300]
    title_prompt = (
        "Tu dois générer un titre ultra-court (4 à 6 mots maximum) pour cette conversation de diagnostic système.\n"
        "Réponds UNIQUEMENT avec le titre, sans ponctuation finale, sans guillemets, sans explication.\n\n"
        f"Question : {excerpt_user}\n"
        f"Réponse : {excerpt_ai}\n\n"
        "Titre :"
    )

    cfg = db.query(GlobalConfig).first()
    title_prompt += "\n\n" + language_instruction(get_language(cfg))
    ollama_url   = (cfg.ollama_url   or "http://ollama:11434") if cfg else "http://ollama:11434"
    ollama_model = (cfg.ollama_model or "gemma4:e4b")         if cfg else "gemma4:e4b"

    generated_title = ""
    try:
        if _orchestrator:
            async with _orchestrator._ollama_semaphore:
                generated_title = await _orchestrator.ollama.analyze_async(
                    prompt=title_prompt,
                    url=ollama_url,
                    model=ollama_model,
                    options={"temperature": 0.3, "num_ctx": 512},
                    think=True  # Laisse le modèle réfléchir si besoin, analyze_async nettoiera les balises <think>
                )
    except Exception as e:
        logger.error("ChatRouter", f"Auto-title error: {e}")
        return {"title": conv.title}

    # Nettoyer le titre généré
    generated_title = generated_title.strip().strip('"\'\'"').rstrip('.!?')
    # Garder seulement la première ligne
    generated_title = generated_title.split('\n')[0].strip()
    if not generated_title:
        return {"title": conv.title}

    # Préfixer avec l'ID de détection si disponible
    if conv.analysis_id:
        analysis = db.query(Analysis).filter(Analysis.id == conv.analysis_id).first()
        if analysis and analysis.detection_id:
            generated_title = f"[#{analysis.detection_id}] {generated_title}"

    # Sauvegarder en BDD
    conv.title = generated_title
    db.commit()

    return {"title": generated_title}


@router.delete("/api/messages/{conv_id}")
async def delete_last_messages(conv_id: int, count: int = 2, db: Session = Depends(get_db)):
    """
    Supprime les N derniers messages d'une conversation.
    Utilisé pour l'édition d'un message (supprime le message original
    et la réponse IA associée avant de renvoyer le message modifié).
    """
    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.conversation_id == conv_id)
        .order_by(ChatMessage.created_at.desc())
        .limit(count)
        .all()
    )
    for m in messages:
        db.delete(m)
    db.commit()
    return {"status": "ok", "deleted": len(messages)}

@router.get("/api/settings")
async def get_chat_settings(db: Session = Depends(get_db)):
    cfg = db.query(GlobalConfig).first()
    lang = get_language(cfg)
    
    active_rules = db.query(Rule).filter(Rule.enabled == True).all()
    rules_lines = []
    for r in active_rules:
        ctx = f" (Context: {r.application_context})" if r.application_context else ""
        rules_lines.append(f"- {r.name}{ctx}")
    rules_list_str = "\n".join(rules_lines) if rules_lines else ("- No rules configured" if lang == "en" else "- Aucune règle configurée")
    
    if not cfg:
        return {"chat_lang": lang, "chat_system_prompt": "", "rules_list_str": rules_list_str}
        
    return {
        "chat_lang": lang,
        "chat_system_prompt": cfg.chat_system_prompt or "",
        "rules_list_str": rules_list_str
    }

@router.post("/api/settings")
async def save_chat_settings(data: dict, db: Session = Depends(get_db)):
    cfg = db.query(GlobalConfig).first()
    if not cfg:
        cfg = GlobalConfig()
        db.add(cfg)
    
    # Language is controlled by the header; stale chat forms cannot override it.
    if "chat_system_prompt" in data:
        cfg.chat_system_prompt = data["chat_system_prompt"]
        
    db.commit()
    return {"status": "ok"}

@router.post("/api/translate-prompt")
async def translate_chat_prompt(data: dict, db: Session = Depends(get_db)):
    prompt_text = data.get("prompt", "")
    target_lang = data.get("lang", "fr")
    
    if not prompt_text:
        return {"translated": ""}
        
    cfg = db.query(GlobalConfig).first()
    ollama_url   = (cfg.ollama_url   or "http://ollama:11434") if cfg else "http://ollama:11434"
    ollama_model = (cfg.ollama_model or "gemma4:e4b")         if cfg else "gemma4:e4b"
    
    translation_prompt = (
        f"Traduisez précisément ce prompt système en '{target_lang}'. "
        "Conservez toutes les variables, le ton et les instructions techniques exactes. "
        "Ne répondez QUE par la traduction, sans aucun autre texte ou guillemets.\n\n"
        f"Texte à traduire:\n{prompt_text}"
    )
    
    translated = ""
    try:
        if _orchestrator:
            async with _orchestrator._ollama_semaphore:
                translated = await _orchestrator.ollama.analyze_async(
                    prompt=translation_prompt,
                    url=ollama_url,
                    model=ollama_model,
                    options={"temperature": 0.2, "num_ctx": 2048},
                    think=True
                )
    except Exception as e:
        logger.error("ChatRouter", f"Translate error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
        
    # Nettoyage
    translated = translated.strip()
    return {"translated": translated}
