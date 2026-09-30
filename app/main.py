from app.utils.language import get_language
import asyncio
from contextlib import asynccontextmanager
import logging

class WebhookAccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Suppress access logs for the webhook receiver endpoint to avoid spamming docker logs
        if record.args and len(record.args) >= 3:
            path = record.args[2]
            if isinstance(path, str) and "/api/webhook/logs/" in path:
                return False
        msg = record.getMessage()
        if "/api/webhook/logs/" in msg:
            return False
        return True

# Apply the filter to the uvicorn.access logger
logging.getLogger("uvicorn.access").addFilter(WebhookAccessLogFilter())

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import os
import subprocess
import httpx
import re

# ── Update cache (persists across requests / page navigations) ──
_update_cache: dict = {"is_available": False, "is_latest": False, "error": True, "checked": False}


def check_for_updates(current_version):
    """Checks GitHub for new commits on main branch and updates the in-memory cache."""
    global _update_cache
    try:
        parts = current_version.split('.')
        if len(parts) < 3:
            result = {"is_available": False, "is_latest": False, "error": True, "checked": True}
            _update_cache = result
            return result
        local_commits = int(parts[-1])

        url = "https://api.github.com/repos/Aschefr/log-to-llm-sentinel/commits?per_page=1"
        headers = {"User-Agent": "Log-to-LLM-Sentinel-App"}
        with httpx.Client(headers=headers, timeout=5.0) as client:
            response = client.get(url)
            if response.status_code == 200:
                link_header = response.headers.get("Link", "")
                if 'rel="last"' in link_header:
                    match = re.search(r'page=(\d+)>; rel="last"', link_header)
                    if match:
                        remote_commits = int(match.group(1))
                        result = {
                            "is_available": remote_commits > local_commits,
                            "is_latest": remote_commits <= local_commits,
                            "error": False,
                            "checked": True
                        }
                        _update_cache = result
                        return result
                else:
                    result = {"is_available": False, "is_latest": True, "error": False, "checked": True}
                    _update_cache = result
                    return result
    except Exception as e:
        print(f"[Main] Update check failed: {e}")
    result = {"is_available": False, "is_latest": False, "error": True, "checked": True}
    _update_cache = result
    return result


def get_app_version():
    try:
        if os.path.exists(".git"):
            commits = subprocess.check_output(["git", "rev-list", "--count", "HEAD"]).decode("utf-8").strip()
            merges = int(subprocess.check_output(["git", "rev-list", "--merges", "--count", "HEAD"]).decode("utf-8").strip())
            merges += 2  # Offset pour branches mergées avant versionning (souvent fast-forward)
            version = f"1.{merges}.{commits}"
            with open("version.txt", "w", encoding="utf-8") as f:
                f.write(version)
            return version
    except Exception:
        pass
    try:
        if os.path.exists("version.txt"):
            with open("version.txt", "r", encoding="utf-8") as f:
                return f.read().strip()
    except Exception:
        pass
    return "1.0.0"

APP_VERSION = get_app_version()

from app.database import init_db
from app.routers import rules, config, dashboard
from app.routers import files as files_router
from app.routers import monitor as monitor_router
from app.routers import chat as chat_router
from app.routers import i18n as i18n_router
from app.services.log_watcher import LogWatcher
from app.services.orchestrator import Orchestrator
from app.services.task_manager import task_manager
from app.services.meta_service import MetaAnalysisService

# ── Instances globales ──
orchestrator = Orchestrator()
meta_service = MetaAnalysisService(orchestrator=orchestrator)
from app.services.resolution_service import ResolutionService
resolution_service = ResolutionService(orchestrator=orchestrator)
orchestrator.resolution_service = resolution_service

log_watcher = LogWatcher(on_new_lines=orchestrator.handle_new_lines)
watcher_task = None


# ── Lifespan ──
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gère le démarrage et l'arrêt propre du watcher."""
    global watcher_task

    # Startup
    init_db()
    os.makedirs("/logs", exist_ok=True)
    os.makedirs("./data", exist_ok=True)

    # Partager l'orchestrateur avec les routers (pour accès aux buffers et Ollama)
    monitor_router.set_orchestrator(orchestrator)
    monitor_router.set_resolution_service(resolution_service)
    chat_router.set_orchestrator(orchestrator)
    config.set_orchestrator(orchestrator)
    rules.set_orchestrator(orchestrator)
    webhook_router.set_orchestrator(orchestrator)

    # Initialiser et démarrer le récepteur Syslog (UDP 514)
    from app.services.syslog_receiver import syslog_receiver, set_orchestrator as set_syslog_orchestrator
    set_syslog_orchestrator(orchestrator)
    asyncio.create_task(syslog_receiver.start())
    print("[Main] SyslogReceiver démarré en background")

    # Restaurer les états d'alerte pour la résolution
    resolution_service.restore_states_from_db()

    # Démarre le watcher en background
    watcher_task = asyncio.create_task(log_watcher.start())
    print("[Main] LogWatcher démarré en background")

    # Reprendre les sessions d'auto-apprentissage interrompues par un redémarrage
    from app.services.keyword_learning_service import resume_stuck_sessions
    asyncio.create_task(resume_stuck_sessions())

    # Nettoyage périodique des tâches d'arrière-plan (toutes les 30 min)
    async def _cleanup_tasks():
        while True:
            await asyncio.sleep(1800)
            removed = task_manager.cleanup_old_tasks(max_age_hours=2)
            if removed:
                print(f"[Main] Nettoyage tâches : {removed} entrée(s) supprimée(s)")

    async def _run_meta_analyses_loop():
        while True:
            await asyncio.sleep(600)  # Vérifie toutes les 10 minutes
            await meta_service.run_scheduled_analyses()

    async def _daily_update_check():
        import datetime
        # Check immédiat au démarrage
        print("[Main] Vérification des mises à jour au démarrage...")
        check_for_updates(APP_VERSION)
        print(f"[Main] Résultat mise à jour : {_update_cache}")
        while True:
            now = datetime.datetime.utcnow()
            # Prochaine occurrence de 00:00 UTC
            next_midnight = (now + datetime.timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            wait_seconds = (next_midnight - now).total_seconds()
            await asyncio.sleep(wait_seconds)
            print("[Main] Vérification quotidienne des mises à jour (00:00 UTC)...")
            check_for_updates(APP_VERSION)
            print(f"[Main] Résultat mise à jour : {_update_cache}")

    async def _inactivity_checker():
        from app.services.notification_service import NotificationService
        from datetime import datetime, timedelta
        from app.database import SessionLocal
        from app.models import Rule, GlobalConfig
        while True:
            await asyncio.sleep(300)  # Check every 5 minutes
            db = SessionLocal()
            try:
                now = datetime.utcnow()
                rules = db.query(Rule).filter(Rule.enabled == True, Rule.inactivity_warning_enabled == True, Rule.inactivity_notified == False).all()
                for rule in rules:
                    if rule.last_line_received_at:
                        delta = now - rule.last_line_received_at
                        if delta > timedelta(hours=rule.inactivity_period_hours):
                            if rule.inactivity_notify:
                                config = db.query(GlobalConfig).first()
                                if config:
                                    notifier = NotificationService()
                                    from app.utils.notification_i18n import nt
                                    lang = get_language(config)
                                    instance_prefix = f"[{config.instance_name}] " if config.instance_name else ""
                                    subject = instance_prefix + nt('inactivity_subject', lang).format(rule_name=rule.name)
                                    body = nt('inactivity_body', lang).format(rule_name=rule.name, hours=rule.inactivity_period_hours, last_received=rule.last_line_received_at.strftime('%Y-%m-%d %H:%M:%S'))
                                    if config.notification_method in ("apprise", "discord"):
                                        body = body.replace("<p>", "").replace("</p>", "\n").replace("<b>", "**").replace("</b>", "**").strip()
                                    config_dict = {
                                        "smtp_host": config.smtp_host, "smtp_port": config.smtp_port,
                                        "smtp_user": config.smtp_user, "smtp_password": config.smtp_password,
                                        "smtp_recipient": config.smtp_recipient, "smtp_tls": config.smtp_tls,
                                        "smtp_ssl_mode": config.smtp_ssl_mode, "notification_method": config.notification_method,
                                        "apprise_url": config.apprise_url, "apprise_tags": config.apprise_tags,
                                        "apprise_max_chars": config.apprise_max_chars if config.apprise_max_chars is not None else 1900,
                                        "discord_webhook_url": config.discord_webhook_url if config.discord_webhook_url is not None else "",
                                        "debug_mode": config.debug_mode,
                                    }
                                    try:
                                        notifier.send(subject, body, config_dict)
                                    except Exception:
                                        pass
                            rule.inactivity_notified = True
                db.commit()
            except Exception as e:
                print(f"[Main] Erreur inactivity checker: {e}")
            finally:
                db.close()

    async def _resolution_checker():
        while True:
            await asyncio.sleep(60)  # Check every 60 seconds
            try:
                await resolution_service.check_timeout_resolutions()
            except Exception as e:
                print(f"[Main] Erreur resolution checker: {e}")

    async def _pattern_decay_checker():
        from app.services.resolution_service import PATTERN_DECAY_INTERVAL_HOURS
        interval = PATTERN_DECAY_INTERVAL_HOURS * 3600
        await asyncio.sleep(300)  # Attendre 5 min apres le demarrage avant le premier check
        while True:
            try:
                await resolution_service.decay_stale_patterns()
            except Exception as e:
                print(f"[Main] Erreur pattern decay: {e}")
            await asyncio.sleep(interval)

    asyncio.create_task(_cleanup_tasks())
    asyncio.create_task(_run_meta_analyses_loop())
    asyncio.create_task(_daily_update_check())
    asyncio.create_task(_inactivity_checker())
    asyncio.create_task(_resolution_checker())
    asyncio.create_task(_pattern_decay_checker())

    yield

    # Shutdown
    log_watcher.stop()
    from app.services.syslog_receiver import syslog_receiver
    asyncio.create_task(syslog_receiver.stop())
    if watcher_task:
        watcher_task.cancel()
        try:
            await watcher_task
        except asyncio.CancelledError:
            pass
    print("[Main] Arrêt propre terminé")


app = FastAPI(title="Log-to-LLM-Sentinel", lifespan=lifespan)


@app.get("/api/system/update-check")
async def api_update_check():
    """Manual check for updates from the UI. Result is cached server-side."""
    return check_for_updates(APP_VERSION)


@app.get("/api/system/update-status")
async def api_update_status():
    """Returns the last cached update check result without hitting GitHub."""
    return _update_cache

# ── Templates & Static ──
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
templates.env.globals['APP_VERSION'] = APP_VERSION
templates.env.globals['UPDATE_STATUS'] = check_for_updates(APP_VERSION)
static_dir = os.path.join(BASE_DIR, "static")

app.mount("/static", StaticFiles(directory=static_dir), name="static")

# ── Routers ──
from app.routers import chat as chat_router
from app.routers import i18n as i18n_router
from app.routers import meta_analysis as meta_router
from app.routers import keyword_learning as kw_learning_router
from app.routers import webhook as webhook_router

app.include_router(rules.router)
app.include_router(config.router)
app.include_router(dashboard.router)
app.include_router(files_router.router)
app.include_router(monitor_router.router)
app.include_router(chat_router.router)
app.include_router(i18n_router.router)
app.include_router(meta_router.router)
app.include_router(kw_learning_router.router)
app.include_router(webhook_router.router)


# ── Pages ──
@app.get("/")
def dashboard_page(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/rules")
def rules_page(request: Request):
    return templates.TemplateResponse("rules.html", {"request": request})


@app.get("/config")
def config_page(request: Request):
    return templates.TemplateResponse("config.html", {"request": request})


@app.get("/monitor")
def monitor_page(request: Request):
    return templates.TemplateResponse("monitor.html", {"request": request})

@app.get("/meta-analysis")
def meta_analysis_page(request: Request):
    return templates.TemplateResponse("meta.html", {"request": request})
