import json
from datetime import datetime, timedelta
import asyncio
from sqlalchemy.orm import Session
from sqlalchemy import desc

from app.database import SessionLocal
from app.models import Rule, Analysis, GlobalConfig, MetaAnalysisConfig, MetaAnalysisResult
from app.services.ollama_service import OllamaService
from app.services.notification_service import NotificationService
from app import logger
from app.utils.notification_i18n import nt


def _last_paragraph(text: str, max_len: int = 500) -> str:
    """Retourne le dernier paragraphe non vide du texte (souvent le Résumé court IA)."""
    paras = [p.strip() for p in text.split('\n\n') if p.strip()]
    last = paras[-1] if paras else text.strip()
    return (last[:max_len] + '…') if len(last) > max_len else last


class MetaAnalysisService:
    """
    Service gérant les méta-analyses périodiques.
    """

    def __init__(self, orchestrator=None):
        self.orchestrator = orchestrator


    def _should_run_schedule(self, config, now) -> bool:
        if not config.schedule_time: return False
        try:
            h, m = map(int, config.schedule_time.split(':'))
        except:
            return False

        # Si déjà exécuté aujourd'hui (UTC) → ne pas relancer
        if config.last_run_at and config.last_run_at.date() == now.date():
            return False

        # Pas encore l'heure de déclenchement
        if now.hour < h or (now.hour == h and now.minute < m):
            return False

        if config.schedule_type == 'daily':
            return True
        elif config.schedule_type == 'weekly':
            return now.weekday() == (config.schedule_day - 1)
        elif config.schedule_type == 'monthly':
            return now.day == config.schedule_day

        return False

    async def run_scheduled_analyses(self):
        db = SessionLocal()
        try:
            configs = db.query(MetaAnalysisConfig).filter(MetaAnalysisConfig.enabled == True).all()
            now = datetime.utcnow()

            for config in configs:
                if self._should_run_schedule(config, now):
                    # Vérifier qu'une exécution manuelle n'est pas déjà en cours (éviter double résultat)
                    from app.routers.meta_analysis import _running_configs
                    if config.id in _running_configs:
                        logger.info('MetaAnalysisService', f'Analyse {config.name} déjà en cours (manuel), scheduleur ignoré')
                        continue
                    logger.info('MetaAnalysisService', f'Déclenchement calendaire de la méta-analyse {config.name}')
                    await self.execute_meta_analysis(config.id, now)

        except Exception as e:
            logger.error('MetaAnalysisService', f'Erreur planificateur: {str(e)}')
        finally:
            db.close()


    async def get_pending_context(self, config_id: int,
                                   forced_period_start: datetime = None,
                                   forced_period_end: datetime = None):
        db = SessionLocal()
        try:
            config = db.query(MetaAnalysisConfig).filter(MetaAnalysisConfig.id == config_id).first()
            if not config: return {'status': 'error', 'message': 'Config introuvable'}

            now = datetime.utcnow()

            if forced_period_start and forced_period_end:
                period_start = forced_period_start
                period_end = forced_period_end
            else:
                period_end = now
                if config.last_run_at is not None:
                    period_start = config.last_run_at
                else:
                    # Jamais exécuté : period_start = dernier slot planifié écoulé
                    # (identique à la logique du run planifié)
                    try:
                        h, m = map(int, (config.schedule_time or '00:00').split(':'))
                        # Dernier slot UTC passé
                        slot = now.replace(hour=h, minute=m, second=0, microsecond=0)
                        if slot > now:
                            slot -= timedelta(days=1)
                        period_start = slot
                    except Exception:
                        # Fallback sécurisé
                        if config.schedule_type == 'weekly':
                            period_start = now - timedelta(weeks=1)
                        elif config.schedule_type == 'monthly':
                            period_start = now - timedelta(days=30)
                        else:
                            period_start = now - timedelta(days=1)

            rule_ids = []
            if config.rule_ids_json:
                try: rule_ids = json.loads(config.rule_ids_json)
                except: pass

            query = db.query(Analysis, Rule).outerjoin(Rule, Analysis.rule_id == Rule.id).filter(
                Analysis.analyzed_at >= period_start,
                Analysis.analyzed_at <= period_end
            )
            if rule_ids:
                query = query.filter(Analysis.rule_id.in_(rule_ids))

            results = query.order_by(desc(Analysis.analyzed_at)).limit(config.max_analyses).all()
            if not results:
                return {
                    'status': 'ok',
                    'rules_context': [],
                    'analyses_count': 0,
                    'matched_keywords': [],
                    'period_start': period_start.isoformat(),
                    'period_end': period_end.isoformat()
                }

            # Grouper par règle
            by_rule = {}
            all_kws = set()
            for analysis, rule in results:
                rule_name = rule.name if rule else 'Inconnue'
                rule_id = rule.id if rule else 0
                if rule_id not in by_rule:
                    by_rule[rule_id] = {'rule_name': rule_name, 'entries': []}
                if analysis.matched_keywords_json:
                    try: all_kws.update(json.loads(analysis.matched_keywords_json))
                    except: pass
                date_str = analysis.analyzed_at.strftime('%Y-%m-%dT%H:%M:%SZ')
                short_ia = _last_paragraph(analysis.ollama_response) if analysis.ollama_response else 'N/A'
                by_rule[rule_id]['entries'].append({
                    'date': date_str,
                    'severity': analysis.severity.upper(),
                    'detection_id': analysis.detection_id,
                    'triggered_line': analysis.triggered_line[:500],
                    'short_ia': short_ia,
                    'keywords': json.loads(analysis.matched_keywords_json) if analysis.matched_keywords_json else []
                })

            return {
                'status': 'ok',
                'rules_context': list(by_rule.values()),
                'analyses_count': len(results),
                'matched_keywords': list(all_kws),
                'period_start': period_start.isoformat() + 'Z',
                'period_end': period_end.isoformat() + 'Z'
            }
        finally:
            db.close()

    async def execute_meta_analysis(self, config_id: int, trigger_time: datetime = None,
                                     custom_context: str = None,
                                     forced_period_start: datetime = None,
                                     forced_period_end: datetime = None):

        """
        Exécute la méta-analyse : récupération, compression, appel LLM, sauvegarde et notification.
        """
        if trigger_time is None:
            trigger_time = datetime.utcnow()

        db = SessionLocal()
        try:
            config = db.query(MetaAnalysisConfig).filter(MetaAnalysisConfig.id == config_id).first()
            if not config:
                return {"status": "error", "message": "Config introuvable"}

            global_cfg = db.query(GlobalConfig).first()
            ollama = self.orchestrator.ollama if self.orchestrator else OllamaService()
            ollama_url = (global_cfg.ollama_url or "http://ollama:11434") if global_cfg else "http://ollama:11434"
            ollama_model = (global_cfg.ollama_model or "gemma4:e4b") if global_cfg else "gemma4:e4b"
            lang = (global_cfg.ollama_prompt_lang or 'en') if global_cfg else 'en'

            is_manual = custom_context is not None

            # Période: priorité aux valeurs explicites (envoyées depuis le frontend lors d'un déclenchement manuel)
            if forced_period_start and forced_period_end:
                period_start = forced_period_start
                period_end = forced_period_end
            elif is_manual:
                # Déclenchement manuel sans période explicite : fenêtre jusqu'à maintenant
                period_end = trigger_time
                if config.last_run_at:
                    period_start = config.last_run_at
                else:
                    if config.schedule_type == 'weekly':
                        period_start = period_end - timedelta(weeks=1)
                    elif config.schedule_type == 'monthly':
                        period_start = period_end - timedelta(days=30)
                    else:
                        period_start = period_end - timedelta(days=1)
            else:
                # Déclenchement planifié : la fenêtre se termine à l'heure de schedule
                try:
                    h, m = config.schedule_time.split(':') if config.schedule_time else ('0', '0')
                    period_end = trigger_time.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
                    if period_end > trigger_time:
                        period_end = period_end - timedelta(days=1)
                except Exception:
                    period_end = trigger_time

                if config.last_run_at:
                    period_start = config.last_run_at
                else:
                    if config.schedule_type == 'weekly':
                        period_start = period_end - timedelta(weeks=1)
                    elif config.schedule_type == 'monthly':
                        period_start = period_end - timedelta(days=30)
                    else:
                        period_start = period_end - timedelta(days=1)
            
            prompt = ""
            analyses_count = 0
            detection_ids = []
            all_matched_keywords = set()

            if custom_context:
                prompt = custom_context
                # Compter les blocs d'événements dans le contexte personnalisé
                # Chaque événement est séparé par une ligne vide ("\n\n") dans le format attendu
                blocks = [b.strip() for b in custom_context.split('\n\n') if b.strip() and '[SEVERITY:' in b]
                analyses_count = len(blocks)
                # Extraire les IDs de détection présents dans le contexte
                import re
                detection_ids = re.findall(r'\[ID: ([a-f0-9]+)\]', custom_context)
                logger.info("MetaAnalysisService", f"Exécution avec contexte édité manuellement pour {config.name} ({analyses_count} événements)")
            else:
                rule_ids = []
                if config.rule_ids_json:
                    try: rule_ids = json.loads(config.rule_ids_json)
                    except: pass

                query = db.query(Analysis, Rule).outerjoin(Rule, Analysis.rule_id == Rule.id).filter(
                    Analysis.analyzed_at >= period_start, Analysis.analyzed_at <= period_end
                )
                if rule_ids: query = query.filter(Analysis.rule_id.in_(rule_ids))
                results = query.order_by(desc(Analysis.analyzed_at)).limit(config.max_analyses).all()

                if not results:
                    logger.info("MetaAnalysisService", f"Aucune analyse à traiter pour '{config.name}'")
                    config.last_run_at = trigger_time
                    db.commit()
                    return {"status": "skipped", "message": "Aucune donnée"}

                analyses_count = len(results)
                results.reverse()
                compressed_data = []
                for analysis, rule in results:
                    if analysis.detection_id: detection_ids.append(analysis.detection_id)
                    if analysis.matched_keywords_json:
                        try: all_matched_keywords.update(json.loads(analysis.matched_keywords_json))
                        except: pass
                    rule_name = rule.name if rule else ("Unknown" if lang == 'en' else "Inconnue")
                    date_str = analysis.analyzed_at.strftime("%Y-%m-%d %H:%M:%S")
                    short_ia = _last_paragraph(analysis.ollama_response) if analysis.ollama_response else "N/A"
                    rule_label, line_label, ai_label = ("Rule", "Line", "Individual analysis") if lang == 'en' else ("Règle", "Ligne", "IA unitaire")
                    block = f"[{date_str}] [SEVERITY: {analysis.severity.upper()}] [{rule_label}: {rule_name}] [ID: {analysis.detection_id}]\n{line_label}: {analysis.triggered_line[:500]}\n{ai_label}: {short_ia}"
                    compressed_data.append(block)

                events_text = "\n\n".join(compressed_data)
                if lang == 'en':
                    meta_instruction = (
                        "Perform an expert synthesis of this global situation. "
                        "Cross-reference information if multiple services are affected "
                        "and propose general recommendations."
                    )
                    events_header = (
                        f"Here are the {analyses_count} events (limited to most recent) "
                        f"between {period_start.strftime('%Y-%m-%d %H:%M')} "
                        f"and {period_end.strftime('%Y-%m-%d %H:%M')}:"
                    )
                else:
                    meta_instruction = (
                        "R\u00e9alise une synth\u00e8se experte de cette situation globale, "
                        "croise les informations si plusieurs services sont touch\u00e9s, "
                        "et propose des recommandations g\u00e9n\u00e9rales."
                    )
                    events_header = (
                        f"Voici les {analyses_count} \u00e9v\u00e9nements (limit\u00e9 aux plus r\u00e9cents) "
                        f"survenus entre {period_start.strftime('%Y-%m-%d %H:%M')} "
                        f"et {period_end.strftime('%Y-%m-%d %H:%M')} :"
                    )
                prompt = (
                    f"{config.system_prompt}\n\n{events_header}\n"
                    f"{'--' * 20}\n{events_text}\n{'--' * 20}\n"
                    f"{meta_instruction}"
                )

            if lang == 'en':
                prompt += "\n\nWrite your analysis and recommendations in English, even if the supplied context is in another language."

            logger.debug("MetaAnalysisService", f"Envoi prompt méta-analyse (Taille: {len(prompt)} car., Contexte: {config.context_size})")

            if self.orchestrator:
                async with self.orchestrator._ollama_semaphore:
                    response_text = await ollama.analyze_async(
                        prompt=prompt,
                        url=ollama_url,
                        model=ollama_model,
                        options={"temperature": 0.2, "num_ctx": config.context_size},
                        think=True
                    )
            else:
                response_text = await ollama.analyze_async(
                    prompt=prompt,
                    url=ollama_url,
                    model=ollama_model,
                    options={"temperature": 0.2, "num_ctx": config.context_size},
                    think=True
                )

            # Sauvegarder le résultat
            meta_result = MetaAnalysisResult(
                config_id=config.id,
                period_start=period_start,
                period_end=period_end,
                analyses_count=analyses_count,
                detection_ids_json=json.dumps(detection_ids),
                matched_keywords_json=json.dumps(list(all_matched_keywords)),
                context_sent=prompt,
                ollama_response=response_text
            )
            db.add(meta_result)

            # On enregistre period_end (et non trigger_time) comme dernière exécution :
            # la prochaine fenêtre démarrera exactement là où celle-ci s'est terminée (borne du schedule),
            # pas à l'heure réelle d'exécution — garantit des fenêtres contiguës et alignées sur le schedule.
            config.last_run_at = period_end
            db.commit()

            # Notification
            if config.notify_enabled:
                await self._send_notification(meta_result, config, global_cfg)

            return {"status": "ok", "analyses_count": analyses_count}

        except Exception as e:
            logger.error("MetaAnalysisService", f"Erreur lors de l'exécution de la méta-analyse {config_id} : {str(e)}")
            return {"status": "error", "message": str(e)}
        finally:
            db.close()

    async def _send_notification(self, result: MetaAnalysisResult, config: MetaAnalysisConfig, global_cfg: GlobalConfig):
        """
        Envoie une notification globale pour la méta-analyse.
        """
        notifier = NotificationService()
        lang = global_cfg.site_lang or 'fr'
        instance_prefix = f"[{global_cfg.instance_name}] " if global_cfg.instance_name else ""
        
        subject = instance_prefix + nt('meta_subject', lang).format(config_name=config.name)
        
        body_html = f"""
        <h2>📊 {nt('meta_title', lang)} : {config.name}</h2>
        <p><strong>{nt('period', lang)}:</strong> du {result.period_start.strftime('%Y-%m-%d %H:%M')} au {result.period_end.strftime('%Y-%m-%d %H:%M')}</p>
        <p><strong>{nt('events_analyzed', lang)}:</strong> {result.analyses_count}</p>
        <hr/>
        <h3>{nt('ia_synthesis', lang)} :</h3>
        <blockquote>{result.ollama_response}</blockquote>
        """
        
        notify_body = body_html

        if global_cfg.notification_method in ("apprise", "discord"):
            notify_body = f"""### 📊 {nt('meta_title', lang)} : {config.name}
**{nt('period', lang)}:** {result.period_start.strftime('%Y-%m-%d %H:%M')} - {result.period_end.strftime('%Y-%m-%d %H:%M')}
**{nt('events_analyzed', lang)}:** {result.analyses_count}

**{nt('ia_synthesis', lang)}:**
{result.ollama_response}
"""
            max_chars = global_cfg.apprise_max_chars or 1900
            if len(notify_body) > max_chars:
                logger.debug("MetaAnalysisService", f"Synthèse trop longue ({len(notify_body)} chars), demande de résumé simplifié à Ollama...")
                summary_prompt = nt('meta_summary_prompt', lang).format(max_chars=max_chars - 500, response=result.ollama_response)
                
                ollama = OllamaService()
                try:
                    summary = await asyncio.wait_for(
                        ollama.analyze_async(
                            prompt=summary_prompt,
                            url=global_cfg.ollama_url,
                            model=global_cfg.ollama_model,
                            think=False,
                            options={
                                "temperature": 0.1,
                                "num_ctx": 2048,
                            }
                        ),
                        timeout=60.0
                    )
                except asyncio.TimeoutError:
                    summary = "[Erreur Ollama] Délai d'attente dépassé pour le résumé (60s)"
                except Exception as e:
                    summary = f"[Erreur Ollama] {str(e)}"
                    
                if not (isinstance(summary, str) and summary.startswith("[Erreur Ollama]")):
                    notify_body = f"""### 📊 {nt('meta_title', lang)} ({nt('summary_of_synthesis', lang)}) : {config.name}
**{nt('period', lang)}:** {result.period_start.strftime('%Y-%m-%d %H:%M')} - {result.period_end.strftime('%Y-%m-%d %H:%M')}
**{nt('events_analyzed', lang)}:** {result.analyses_count}

**{nt('summary_of_synthesis', lang)}:**
{summary}

{nt('full_synthesis_available', lang)}
"""
                else:
                    notify_body = notify_body[:max_chars-100] + "\n\n... [TRONQUÉ]"

        # Send via thread to not block asyncio
        await asyncio.to_thread(notifier.send, subject, notify_body, {
            "smtp_host": global_cfg.smtp_host,
            "smtp_port": global_cfg.smtp_port,
            "smtp_user": global_cfg.smtp_user,
            "smtp_password": global_cfg.smtp_password,
            "smtp_recipient": global_cfg.smtp_recipient,
            "smtp_tls": global_cfg.smtp_tls,
            "smtp_ssl_mode": global_cfg.smtp_ssl_mode,
            "notification_method": global_cfg.notification_method,
            "apprise_url": global_cfg.apprise_url,
            "apprise_tags": global_cfg.apprise_tags,
            "apprise_max_chars": global_cfg.apprise_max_chars or 1900,
            "discord_webhook_url": global_cfg.discord_webhook_url or "",
        })
