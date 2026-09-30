import json
import unittest
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Analysis, GlobalConfig, Rule, MetaAnalysisConfig
from app.routers.config import _get_config_dict
from app.services.orchestrator import Orchestrator
from app.services.meta_service import MetaAnalysisService


class AnalysisLanguageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', poolclass=StaticPool,
                                    connect_args={'check_same_thread': False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.orchestrator = Orchestrator()
        self.orchestrator.ollama.analyze_async = AsyncMock(
            return_value='SEVERITY: warning\nThe requested file is missing.')
        with self.sessions() as db:
            rule = Rule(name='System', log_file_path='/logs/syslog',
                        keywords_json='["error"]', anti_spam_delay=60,
                        application_context='Nginx on Unraid', notify_on_match=False)
            db.add(rule)
            db.commit()
            self.rule_id = rule.id

    def tearDown(self):
        self.engine.dispose()

    async def flush(self, lines, lang='en', max_chars=5000, create_config=True):
        if create_config:
            with self.sessions() as db:
                db.add(GlobalConfig(ollama_prompt_lang=('fr' if lang == 'en' else 'en'), site_lang=lang,
                                    max_log_chars=max_chars))
                db.commit()
        self.orchestrator._buffers[self.rule_id] = {
            'lines': lines, 'task': None, 'detection_id': 'abc12345',
            'matched_keywords': {'error'}}
        with patch('app.services.orchestrator.SessionLocal', self.sessions), \
             patch('app.services.orchestrator.asyncio.sleep', new=AsyncMock()):
            await self.orchestrator._flush_buffer(self.rule_id)
        with self.sessions() as db:
            analysis = db.query(Analysis).one()
            self.assertEqual(analysis.severity, 'warning')
            return analysis.triggered_line

    async def test_saved_english_reaches_background_worker(self):
        line = await self.flush(['nginx error: missing icon', 'nginx error: missing icon'])
        self.assertTrue(line.startswith('These 2 matching events appeared in the last 60 seconds.'))
        prompt = self.orchestrator.ollama.analyze_async.call_args.kwargs['prompt']
        self.assertIn('Analyze the following log line', prompt)
        self.assertIn('Write your explanation in English', prompt)
        self.assertNotIn('Ces 2', prompt)

    async def test_french_choice_is_respected(self):
        line = await self.flush(['nginx error one', 'nginx error two'], lang='fr')
        self.assertTrue(line.startswith('Ces 2 événements'))
        prompt = self.orchestrator.ollama.analyze_async.call_args.kwargs['prompt']
        self.assertIn('Analyse la ligne de log', prompt)

    async def test_missing_global_config_defaults_to_english(self):
        line = await self.flush(['nginx error one', 'nginx error two'], create_config=False)
        self.assertTrue(line.startswith('These 2 matching events'))

    async def test_single_line_truncation_is_english(self):
        line = await self.flush(['error ' + 'x' * 1000], max_chars=500)
        self.assertTrue(line.endswith('[TRUNCATED]'))
        self.assertNotIn('TRONQUÉ', line)

    async def test_bundled_truncation_is_english(self):
        line = await self.flush(['error ' + 'x' * 1000] * 2, max_chars=500)
        self.assertIn('[truncated]', line)
        self.assertIn('Truncated: 500 character limit reached (2 events detected)', line)
        self.assertNotIn('tronqué', line.lower())

    async def test_site_language_is_forwarded_for_notifications(self):
        self.orchestrator._process_match = AsyncMock()
        with self.sessions() as db:
            db.add(GlobalConfig(ollama_prompt_lang='en', site_lang='en'))
            db.commit()
        self.orchestrator._buffers[self.rule_id] = {
            'lines': ['error'], 'task': None, 'detection_id': 'abc12345',
            'matched_keywords': {'error'}}
        with patch('app.services.orchestrator.SessionLocal', self.sessions), \
             patch('app.services.orchestrator.asyncio.sleep', new=AsyncMock()):
            await self.orchestrator._flush_buffer(self.rule_id)
        config = self.orchestrator._process_match.call_args.args[2]
        self.assertEqual(config['site_lang'], 'en')
        self.assertEqual(config['ollama_prompt_lang'], 'en')

    async def test_meta_analysis_uses_english_labels_and_instruction(self):
        await self.flush(['nginx error'])
        with self.sessions() as db:
            meta = MetaAnalysisConfig(name='Daily review', notify_enabled=False)
            db.add(meta)
            db.commit()
            meta_id = meta.id
        now = datetime.utcnow()
        with patch('app.services.meta_service.SessionLocal', self.sessions):
            result = await MetaAnalysisService(self.orchestrator).execute_meta_analysis(
                meta_id, forced_period_start=now - timedelta(hours=1),
                forced_period_end=now + timedelta(minutes=1))
        self.assertEqual(result['status'], 'ok')
        prompt = self.orchestrator.ollama.analyze_async.call_args.kwargs['prompt']
        self.assertIn('[Rule: System]', prompt)
        self.assertIn('Individual analysis:', prompt)
        self.assertIn('Write your analysis and recommendations in English', prompt)
        self.assertNotIn('[Règle:', prompt)

    async def test_manual_meta_context_still_requests_english(self):
        with self.sessions() as db:
            db.add(GlobalConfig(ollama_prompt_lang='en'))
            meta = MetaAnalysisConfig(name='Manual review', notify_enabled=False)
            db.add(meta)
            db.commit()
            meta_id = meta.id
        with patch('app.services.meta_service.SessionLocal', self.sessions):
            result = await MetaAnalysisService(self.orchestrator).execute_meta_analysis(
                meta_id, custom_context='Contexte fourni en français')
        self.assertEqual(result['status'], 'ok')
        prompt = self.orchestrator.ollama.analyze_async.call_args.kwargs['prompt']
        self.assertIn('Contexte fourni en français', prompt)
        self.assertIn('Write your analysis and recommendations in English', prompt)

    def test_new_config_and_api_defaults_are_english(self):
        with self.sessions() as db:
            config = GlobalConfig()
            db.add(config)
            db.commit()
            self.assertEqual(config.ollama_prompt_lang, 'en')
        self.assertEqual(_get_config_dict(None)['ollama_prompt_lang'], 'en')

    def test_default_prompt_and_context_are_english(self):
        rule = Rule(application_context='Nginx')
        prompt = self.orchestrator._build_prompt(rule, 'error', 'Custom instructions',
                                                context_lines=['previous log'])
        self.assertTrue(prompt.startswith('Custom instructions'))
        self.assertIn('Previous context', prompt)
        self.assertIn('Write your explanation in English', prompt)

    def test_translation_key_structures_match(self):
        def keys(value, prefix=''):
            result = set()
            for key, child in value.items():
                path = prefix + key
                result.add(path)
                if isinstance(child, dict):
                    result.update(keys(child, path + '.'))
            return result
        en = json.loads(Path('static/i18n/en.json').read_text(encoding='utf-8-sig'))
        fr = json.loads(Path('static/i18n/fr.json').read_text(encoding='utf-8-sig'))
        self.assertEqual(keys(en), keys(fr))


if __name__ == '__main__':
    unittest.main()
