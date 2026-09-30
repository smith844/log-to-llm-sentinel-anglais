from app.utils.language import language_instruction
import re
import asyncio
from app.logger import debug as log_debug, add_ollama_log


def run_truncation(messages: list, max_tokens: int, chars_per_token: float = 3.5) -> tuple[list, list]:
    """
    Split messages into (dropped, kept) based on token budget.
    Keeps recent messages, drops oldest.
    Returns (dropped_messages, kept_messages).
    """
    if not messages:
        return [], []
    
    max_chars = int(max_tokens * chars_per_token * 0.6)  # 60% budget for history
    
    kept = []
    total_chars = 0
    
    for m in reversed(messages):
        msg_len = len(m.content) + 20
        if total_chars + msg_len > max_chars and kept:
            break
        kept.insert(0, m)
        total_chars += msg_len
    
    kept_ids = {m.id for m in kept}
    dropped = [m for m in messages if m.id not in kept_ids]
    
    return dropped, kept


def _estimate_tokens(text: str, chars_per_token: float = 3.5) -> int:
    return int(len(text) / chars_per_token)


# ── Blocs d'instructions (placés EN FIN de prompt) ─────────────────────────

_COMPACT_INSTRUCTIONS = (
    "\n\n=== INSTRUCTIONS (CRITICAL — READ CAREFULLY) ===\n"
    "You are a text compactor. Rewrite the text ABOVE using minimum characters.\n\n"
    "RULES:\n"
    "1. Output ONLY the compacted text. No commentary, no intro.\n"
    "2. Keep the SAME language as the input (French→French, English→English).\n"
    "3. Use U: for user, A: for assistant.\n"
    "4. Remove ALL greetings, politeness, filler, reformulations, repetitions.\n"
    "5. Convert verbose explanations to telegraphic notes.\n"
    "6. KEEP ALL technical details: paths, IPs, ports, errors, commands, configs.\n"
    "7. KEEP conversation flow (who said what, in order).\n"
    "8. If the text starts with '=== Analyse initiale ===' or similar header, "
    "KEEP that section and compact it too. It is the original context that started the conversation.\n"
    "9. Target: ~50% of original size.\n\n"
    "EXAMPLE:\n"
    "BEFORE: Utilisateur : Bonjour, j'ai un problème avec mon conteneur Docker. "
    "Il ne démarre plus. L'erreur est 'OCI runtime create failed'.\n"
    "AFTER: U: Conteneur Docker ne démarre plus. Erreur: 'OCI runtime create failed'\n\n"
    "NOW COMPACT THE TEXT ABOVE. Output ONLY the compacted result:"
)

_SUMMARY_INSTRUCTIONS = (
    "\n\n=== INSTRUCTIONS (CRITICAL — READ CAREFULLY) ===\n"
    "You are a conversation summarizer. Produce an ULTRA-COMPRESSED structured summary "
    "of the text ABOVE.\n\n"
    "CRITICAL RULES:\n"
    "1. Output ONLY the summary. No intro, no commentary.\n"
    "2. Use the SAME LANGUAGE as the conversation.\n"
    "3. You are NOT answering questions. You are SUMMARIZING what was discussed.\n"
    "4. DO NOT continue the conversation or provide new analysis.\n"
    "5. If the text starts with '=== Analyse initiale ===' or similar header, "
    "include that context in your summary under CONTEXTE or FAITS.\n\n"
    "USE THIS FORMAT:\n"
    "SUJET: [1 line]\n"
    "CONTEXTE: [initial analysis context, if present]\n"
    "FAITS:\n"
    "- [fact]\n"
    "RÉSOLU:\n"
    "- [resolved point]\n"
    "EN COURS:\n"
    "- [open question]\n\n"
    "STYLE: Telegraphic, no articles, no filler. "
    "Keep ALL technical data (paths, IPs, ports, errors, commands). "
    "Max 1 line per bullet. Omit empty sections. Target: ~30% of original size.\n\n"
    "NOW SUMMARIZE THE TEXT ABOVE. Output ONLY the structured summary:"
)


def _fit_text_to_context(text: str, instructions: str, num_ctx: int,
                         chars_per_token: float = 3.5) -> tuple[str, bool]:
    """
    Tronque le texte depuis le DÉBUT pour que texte + instructions
    tiennent dans num_ctx (en gardant une marge pour la réponse).
    
    Returns (text_possibly_truncated, was_truncated).
    """
    instructions_tokens = _estimate_tokens(instructions)
    response_budget = 1024  # réserver 1024 tokens pour la réponse
    
    text_budget_tokens = num_ctx - instructions_tokens - response_budget
    text_budget_chars = int(text_budget_tokens * chars_per_token)
    
    if len(text) <= text_budget_chars:
        return text, False
    
    # Tronquer depuis le début (garder la fin = messages récents + instructions)
    truncated = "...[tronqué]...\n" + text[-text_budget_chars:]
    return truncated, True


async def run_compaction(text: str, ollama_service, url: str, model: str,
                         num_ctx: int = 4096, lang: str = "en") -> str:
    """
    AI-powered compaction. Respecte num_ctx de l'utilisateur.
    Pre-tronque le texte si nécessaire avant envoi à Ollama.
    """
    instructions = _COMPACT_INSTRUCTIONS.replace("Keep the SAME language as the input (French→French, English→English).", language_instruction(lang))
    fitted_text, was_truncated = _fit_text_to_context(text, instructions, num_ctx)
    prompt = fitted_text + instructions
    
    prompt_tokens = _estimate_tokens(prompt)
    log_debug("Compression", f"[compact] Prompt: {len(prompt)} chars (~{prompt_tokens} tokens), num_ctx={num_ctx}")
    if was_truncated:
        log_debug("Compression", f"[compact] ⚠️ Texte pré-tronqué pour tenir dans num_ctx={num_ctx}")
    
    response = await ollama_service.analyze_async(
        prompt, url=url, model=model,
        options={"temperature": 0.1, "num_ctx": num_ctx},
        think=False
    )
    
    result = response.strip()
    log_debug("Compression", f"[compact] Réponse ({len(result)} chars, réduction {100 - int(len(result)/max(len(text),1)*100)}%)")
    log_debug("Compression", f"[compact] Réponse (début): {result[:300]}...")
    add_ollama_log(prompt, result, detection_id="compression-compact")
    
    return result


async def run_summary(text: str, ollama_service, url: str, model: str,
                      num_ctx: int = 4096, lang: str = "en") -> str:
    """
    AI-powered summary. Respecte num_ctx de l'utilisateur.
    Pre-tronque le texte si nécessaire avant envoi à Ollama.
    """
    instructions = _SUMMARY_INSTRUCTIONS.replace("Use the SAME LANGUAGE as the conversation.", language_instruction(lang))
    if lang == "en":
        for original, translated in [("SUJET:", "TOPIC:"), ("CONTEXTE:", "CONTEXT:"), ("FAITS:", "FACTS:"), ("RÉSOLU:", "RESOLVED:"), ("EN COURS:", "OPEN:")]:
            instructions = instructions.replace(original, translated)
    fitted_text, was_truncated = _fit_text_to_context(text, instructions, num_ctx)
    prompt = fitted_text + instructions
    
    prompt_tokens = _estimate_tokens(prompt)
    log_debug("Compression", f"[summary] Prompt: {len(prompt)} chars (~{prompt_tokens} tokens), num_ctx={num_ctx}")
    if was_truncated:
        log_debug("Compression", f"[summary] ⚠️ Texte pré-tronqué pour tenir dans num_ctx={num_ctx}")
    
    response = await ollama_service.analyze_async(
        prompt, url=url, model=model,
        options={"temperature": 0.1, "num_ctx": num_ctx},
        think=False
    )
    
    result = response.strip()
    log_debug("Compression", f"[summary] Réponse ({len(result)} chars, réduction {100 - int(len(result)/max(len(text),1)*100)}%)")
    log_debug("Compression", f"[summary] Réponse (début): {result[:300]}...")
    add_ollama_log(prompt, result, detection_id="compression-summary")
    
    return result
