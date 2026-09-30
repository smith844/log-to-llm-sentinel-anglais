from app.utils.language import get_language, language_instruction
import asyncio
import uuid
import json
from typing import List, Optional
from datetime import datetime

from app.database import SessionLocal
from app.models import Rule, Analysis, GlobalConfig
from app.services.ollama_service import OllamaService
from app.services.notification_service import NotificationService
from app.utils.log_utils import clean_log_line
from app import logger
from app.utils.notification_i18n import nt


class Orchestrator:
    """
    Orchestre le pipeline :
    1. Recevoir les nouvelles lignes
    2. Filtrer par mots-clés
    3. Analyser avec Ollama
    4. Sauvegarder en BDD
    5. Notifier si nécessaire
    """

    def __init__(self):
        self.ollama = OllamaService()
        self.notifier = NotificationService()
        self.resolution_service = None
        self._buffers = {}  # rule_id -> {"lines": [], "task": None, "detection_id": None, "matched_keywords": set()}
        self._ollama_semaphore = asyncio.Semaphore(1)

    async def handle_new_lines(self, rule: Rule, lines: List[str]):
        """Traite les nouvelles lignes pour une règle donnée."""
        db = SessionLocal()
        try:
            db_rule = db.query(Rule).filter(Rule.id == rule.id).first()
            if not db_rule or not db_rule.enabled:
                return

            logger.debug("Orchestrator", f"Régle '{db_rule.name}' — {len(lines)} nouvelle(s) ligne(s) reçue(s)")

            # Hook de détection de résolution (retour à la normale)
            if self.resolution_service:
                await self.resolution_service.on_new_lines(db_rule, lines)

            keywords = db_rule.get_keywords()
            if not keywords:
                logger.debug("Orchestrator", f"Règle '{db_rule.name}' : aucun mot-clé configuré, ignorée")
                return

            excluded = db_rule.get_excluded_patterns()

            # Filtrer les lignes contenant au moins un mot-clé
            matching_lines = []
            for line in lines:
                # 0. Skip oversized lines (defense-in-depth, BUG-01c)
                if len(line) > 10_000:
                    logger.warning("Orchestrator", f"Ligne surdimensionnée ignorée ({len(line)} chars) pour '{db_rule.name}'")
                    continue
                # 1. Exclusion patterns (negative keywords) — priorité max
                if excluded and any(pat.lower() in line.lower() for pat in excluded):
                    logger.debug("Orchestrator", f"Ligne exclue (pattern d'exclusion) pour '{db_rule.name}' : {line[:80]}")
                    continue
                # 2. Positive keyword match
                if any(kw.lower() in line.lower() for kw in keywords):
                    matching_lines.append(line)
                    logger.debug("Orchestrator", f"Match '{db_rule.name}' | kw dans : {line[:120]}")

            if not matching_lines:
                logger.debug("Orchestrator", f"Règle '{db_rule.name}' : aucune correspondance")
                return

            rule_id = db_rule.id
            # Ajouter au buffer de cette règle
            if rule_id not in self._buffers:
                self._buffers[rule_id] = {"lines": [], "task": None, "detection_id": None, "matched_keywords": set()}

            # Générer un detection_id unique si c'est la première détection de ce cycle
            if self._buffers[rule_id]["detection_id"] is None:
                self._buffers[rule_id]["detection_id"] = uuid.uuid4().hex[:8]
                logger.debug("Orchestrator", f"Nouveau cycle de détection — ID: {self._buffers[rule_id]['detection_id']}")

            self._buffers[rule_id]["lines"].extend(matching_lines)

            # Collecter les mots-clés matchés
            for line in matching_lines:
                for kw in keywords:
                    if kw.lower() in line.lower():
                        self._buffers[rule_id]["matched_keywords"].add(kw)

            # Démarrer le timer anti-spam si pas déjà en cours
            if self._buffers[rule_id]["task"] is None:
                self._buffers[rule_id]["task"] = asyncio.create_task(self._flush_buffer(rule_id))
        finally:
            db.close()

    async def _flush_buffer(self, rule_id: int):
        """Attend le délai anti-spam puis traite toutes les lignes accumulées."""
        # Phase 1 : lecture du délai (session DB courte, toujours libérée avant le sleep)
        buffer_delay = 60
        db = SessionLocal()
        try:
            rule = db.query(Rule).filter(Rule.id == rule_id).first()
            if not rule or not rule.enabled:
                return
            buffer_delay = rule.anti_spam_delay or 60
        finally:
            db.close()  # Toujours fermée ici, avant le sleep — jamais de double-close

        await asyncio.sleep(buffer_delay)

        lines = self._buffers[rule_id]["lines"]
        detection_id = self._buffers[rule_id]["detection_id"]
        matched_keywords = list(self._buffers[rule_id]["matched_keywords"])
        self._buffers[rule_id] = {"lines": [], "task": None, "detection_id": None, "matched_keywords": set()}

        if not lines:
            return

        # Phase 2 : traitement avec une nouvelle session DB propre
        db = SessionLocal()
        try:
            # Re-fetch rule to ensure it's still enabled after sleep
            rule = db.query(Rule).filter(Rule.id == rule_id).first()
            if not rule or not rule.enabled:
                return

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
                "ollama_prompt_lang": get_language(config),
                "site_lang": get_language(config),
                "notification_method": config.notification_method if config else "smtp",
                "apprise_url": config.apprise_url if config else "",
                "apprise_tags": config.apprise_tags if config else "",
                "apprise_max_chars": config.apprise_max_chars if config else 1900,
                "max_log_chars": config.max_log_chars if config else 5000,
                "debug_mode": config.debug_mode if config else False,
                "instance_name": config.instance_name if config else "",
                "discord_webhook_url": config.discord_webhook_url if config else "",
            }

            max_chars = config_dict.get("max_log_chars", 5000)
            lang = get_language(config_dict)

            if len(lines) == 1:
                line = clean_log_line(lines[0])
                # Si la ligne est trop longue, on la tronque
                if len(line) > max_chars:
                    logger.warning("Orchestrator", f"Ligne trop longue ({len(line)} chars), troncature à {max_chars}")
                    line = line[:max_chars] + ("... [TRUNCATED]" if lang == 'en' else "... [TRONQUÉ]")
                
                await self._process_match(rule, line, config_dict, db, detection_id, matched_keywords)
            else:
                # Regrouper les lignes
                total_lines = len(lines)
                # On limite à 30 lignes max dans le prompt pour ne pas exploser le contexte
                recent_lines = [clean_log_line(l) for l in lines[-30:]]
                
                bundled_text = f"These {total_lines} matching events appeared in the last {buffer_delay} seconds. Here are the most recent:\n" if lang == 'en' else f"Ces {total_lines} événements correspondants sont apparus dans les {buffer_delay} dernières secondes. Voici un extrait des plus récents :\n"
                
                current_length = len(bundled_text)
                lines_added = 0
                for line in reversed(recent_lines):
                    line_to_add = f"\n{line}"
                    if current_length + len(line_to_add) > max_chars:
                        if lines_added == 0:
                            # Guarantee at least 1 line even if it must be truncated
                            truncated_line = line[:max_chars - current_length - 20] + ("…[truncated]" if lang == 'en' else "…[tronqué]")
                            bundled_text += f"\n{truncated_line}"
                        bundled_text += (f"\n... [Truncated: {max_chars} character limit reached ({total_lines} events detected)]"
                                         if lang == 'en' else f"\n... [Tronqué : limite de {max_chars} caractères atteinte ({total_lines} événements détectés)]")
                        break
                    bundled_text += line_to_add
                    current_length += len(line_to_add)
                    lines_added += 1

                
                logger.info("Orchestrator", f"Envoi groupé pour '{rule.name}' : {total_lines} lignes ({len(bundled_text)} chars)")
                await self._process_match(rule, bundled_text, config_dict, db, detection_id, matched_keywords)
        except Exception as e:
            logger.error("Orchestrator", f"Erreur lors du flush buffer : {e}")
        finally:
            db.close()


    async def _process_match(self, rule: Rule, line: str, config: dict, db,
                              detection_id: str = None, matched_keywords: list = None):
        """Traite une ligne correspondante."""
        # 1. Préparer le contexte
        context_before = []
        context_after = []
        # Note: Dans une implémentation complète, on récupérerait le contexte depuis le fichier.
        # Ici, on simule ou on utilise les lignes disponibles si possible.
        # Pour simplifier, on passe le contexte vide ou on le récupère si disponible.

        # 2. Construire le prompt
        lang = get_language(config)
        prompt = self._build_prompt(rule, line, config.get("system_prompt", ""), lang=lang)

        # 3. Appeler Ollama (sous verrou pour éviter de surcharger le CPU)
        logger.debug("Orchestrator", f"Envoi à Ollama — modèle={config.get('ollama_model')} | ligne={line[:80]}")
        # 3. Appeler Ollama (en streaming asynchrone pour éviter les timeouts)
        logger.debug("Orchestrator", f"Envoi à Ollama — modèle={config.get('ollama_model')} | ligne={line[:80]}")
        async with self._ollama_semaphore:
            try:
                response = await asyncio.wait_for(
                    self.ollama.analyze_async(
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
        logger.debug("Orchestrator", f"Réponse Ollama reçue : {response[:200]}")
        logger.add_ollama_log(prompt, response, detection_id)

        # 4. Déterminer la sévérité (simple heuristic ou parsing de la réponse)
        severity = self._detect_severity(response)

        # 5. Sauvegarder en BDD
        analysis = Analysis(
            rule_id=rule.id,
            detection_id=detection_id,
            triggered_line=line,
            matched_keywords_json=json.dumps(matched_keywords or []),
            context_before_json="[]",
            context_after_json="[]",
            ollama_response=response,
            severity=severity,
            notified=False,
            analyzed_at=datetime.utcnow(),
        )
        db.add(analysis)
        db.commit()
        db.refresh(analysis)

        # 6. Notifier
        should_notify = rule.notify_on_match
        if should_notify and getattr(rule, "notify_severity_threshold", "info"):
            severity_levels = {"info": 0, "warning": 1, "critical": 2}
            sev_val = severity_levels.get(severity, 0)
            threshold_val = severity_levels.get(rule.notify_severity_threshold, 0)
            if sev_val < threshold_val:
                should_notify = False
                logger.debug("Orchestrator", f"Notification ignorée: la sévérité '{severity}' est inférieure au seuil '{rule.notify_severity_threshold}'.")

        if should_notify:
            try:
                await self.trigger_notification(analysis, rule, config, db)
            except Exception as e:
                logger.error("Orchestrator", f"Erreur lors de l'envoi de la notification d'alerte : {e}")

        # Hook de signalement d'erreur pour la résolution
        if self.resolution_service:
            await self.resolution_service.on_error_detected(rule.id, severity=severity)
            
    async def trigger_notification(self, analysis: Analysis, rule: Rule, config: dict, db) -> bool:
        """Envoie manuellement ou automatiquement une notification pour une analyse existante."""
        severity = analysis.severity
        detection_id = analysis.detection_id
        line = analysis.triggered_line
        response = analysis.ollama_response
        matched_keywords = []
        if analysis.matched_keywords_json:
            import json
            matched_keywords = json.loads(analysis.matched_keywords_json)
            
        lang = get_language(config)
        logger.debug("Notification", f"Notification lang={lang} (site_lang={config.get('site_lang')}, ollama_prompt_lang={config.get('ollama_prompt_lang')})")
        det_id_label = f" [ID: {detection_id}]" if detection_id else ""
        logger.debug("Notification", f"Envoi notification via '{config.get('notification_method')}' pour règle '{rule.name}'")
        instance_prefix = f"[{config.get('instance_name')}] " if config.get('instance_name') else ""
        subject = f"{instance_prefix}[Sentinel] {nt('alert', lang)} {severity.upper()} : {rule.name}{det_id_label}"
        
        severity_emoji = "🔴" if severity == "critical" else "🟠" if severity == "warning" else "🔵"
        
        body = f"""
        <h2>{severity_emoji} {nt('alert_title', lang)}</h2>
        <p><strong>{nt('rule', lang)}:</strong> {rule.name}</p>
        <p><strong>{nt('detection_id', lang)}:</strong> <code>{detection_id or 'N/A'}</code></p>
        <p><strong>{nt('keywords', lang)}:</strong> {', '.join(matched_keywords) if matched_keywords else 'N/A'}</p>
        <hr/>
        <p><strong>{nt('triggered_line', lang)}:</strong></p>
        <pre><code>{line}</code></pre>
        <p><strong>{nt('ollama_analysis', lang)}:</strong></p>
        <blockquote>{response}</blockquote>
        <p><strong>{nt('severity', lang)}:</strong> {severity.upper()}</p>
        """
        
        # Si Apprise ou Discord, on prépare une version plus lisible pour Discord/Telegram (souvent Markdown)
        if config.get("notification_method") in ("apprise", "discord"):
            body = f"""### {severity_emoji} {nt('alert', lang)} Sentinel : {rule.name}
**ID:** `{detection_id or 'N/A'}` | **{nt('severity', lang)}:** {severity.upper()}
**{nt('keywords', lang)}:** {', '.join(matched_keywords) if matched_keywords else 'N/A'}

**{nt('triggered_line', lang)}:**
`{line}`

**{nt('ollama_analysis', lang)}:**
{response}
"""

        # Gestion du résumé IA si nécessaire (pour Apprise/Discord/etc.)
        max_chars = config.get("apprise_max_chars", 1900)
        notify_body = body
        lang = get_language(config)

        if config.get("notification_method") in ("apprise", "discord") and len(body) > max_chars:
            logger.debug("Notification", f"Analyse trop longue ({len(body)} chars), demande de résumé simplifié à Ollama...")
            summary_prompt = nt('summary_prompt', lang).format(max_chars=max_chars - 500, response=response)
            async with self._ollama_semaphore:
                try:
                    summary = await asyncio.wait_for(
                        self.ollama.analyze_async(
                            prompt=summary_prompt,
                            url=config.get("ollama_url"),
                            model=config.get("ollama_model"),
                            think=False, # Pas de raisonnement pour un résumé court
                            options={
                                "temperature": 0.1, # Résumé toujours à basse température
                                "num_ctx": 2048,    # Résumé n'a pas besoin d'un gros contexte
                            }
                        ),
                        timeout=60.0
                    )
                except asyncio.TimeoutError:
                    summary = "[Erreur Ollama] Délai d'attente dépassé pour le résumé (60s)"
            if not (isinstance(summary, str) and summary.startswith("[Erreur Ollama]")):
                logger.add_ollama_log(summary_prompt, summary, detection_id)
                notify_body = f"""### {severity_emoji} {nt('alert', lang)} Sentinel ({nt('analysis_summary', lang)}) : {rule.name}
**ID:** `{detection_id or 'N/A'}` | **{nt('severity', lang)}:** {severity.upper()}

**{nt('analysis_summary', lang)} :**
{summary}

{nt('full_analysis_available', lang)}"""

        await asyncio.to_thread(self.notifier.send, subject, notify_body, config)
        analysis.notified = True
        db.commit()
        return True

    # La méthode _clean_log_line a été déplacée dans app.utils.log_utils.clean_log_line

    def _build_prompt(self, rule: Rule, line: str, system_prompt: str,
                      context_lines: list = None, lang: str = 'en') -> str:
        """Construit le prompt pour Ollama — EN or FR selon lang."""
        import textwrap
        context_block = ""
        if context_lines:
            ctx_label = "Previous context" if lang == 'en' else "Contexte précédent"
            context_block = f"\n{ctx_label} :\n" + "\n".join(f"{l}" for l in context_lines) + "\n"

        if lang == 'en':
            base_prompt = textwrap.dedent(f"""
            Analyze the following log line and determine its severity.
            Your response MUST start with a line indicating severity in this EXACT format:
            SEVERITY: [info|warning|critical]

            Then provide a short, explanatory summary of the incident.
            Write your explanation in English, even if the logs or application context are in another language.

            Application context: {rule.application_context}
            {context_block}
            Triggering line: {line}
            """).strip()
        else:
            base_prompt = textwrap.dedent(f"""
            Analyse la ligne de log suivante et détermine sa sévérité.
            Ta réponse DOIT impérativement commencer par une ligne indiquant la sévérité sous ce format EXACT :
            SEVERITY: [info|warning|critical]

            Ensuite, fournis un résumé court et explicatif de l'incident.

            Contexte de l'application: {rule.application_context}
            {context_block}
            Ligne déclenchante: {line}
            """).strip()

        base_prompt += "\n\n" + language_instruction(lang)
        if system_prompt:
            return f"{system_prompt.strip()}\n\n{base_prompt}"
        return base_prompt

    def _detect_severity(self, response: str) -> str:
        """Détermine la sévérité à partir de la réponse Ollama."""
        # Priorité : chercher le tag explicite demandé dans le prompt
        lines = response.splitlines()
        for line in lines[:10]: # On regarde les premières lignes
            l = line.upper()
            if "SEVERITY:" in l:
                if "CRITICAL" in l: return "critical"
                if "WARNING" in l: return "warning"
                if "INFO" in l: return "info"

        # Fallback : recherche heuristique plus stricte
        lower_resp = response.lower()
        # On cherche des mentions isolées ou en début de phrase pour éviter les faux positifs
        # comme "ce n'est pas une erreur CRITICAL"
        if "severity: critical" in lower_resp or "sévérité: critical" in lower_resp:
            return "critical"
        if "severity: warning" in lower_resp or "sévérité: warning" in lower_resp:
            return "warning"
        if "severity: info" in lower_resp or "sévérité: info" in lower_resp:
            return "info"
            
        # Ultime fallback (moins fiable)
        if "critical" in lower_resp or "urgent" in lower_resp:
            return "critical"
        elif "warning" in lower_resp or "warn" in lower_resp:
            return "warning"
        return "info"
