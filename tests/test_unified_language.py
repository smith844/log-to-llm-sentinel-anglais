import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import GlobalConfig, ChatConversation, ChatMessage
from app.routers.config import get_site_lang, update_site_lang, _get_config_dict
from app.utils.language import get_language
from app.utils.compression import run_summary, run_compaction

# The module imports main's template globals. Avoid startup update checks in tests.
with patch('httpx.Client'):
    from app.routers.chat import build_chat_prompt, get_chat_settings, save_chat_settings


class UnifiedLanguageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', poolclass=StaticPool,
                                    connect_args={'check_same_thread': False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        with self.sessions() as db:
            db.add(GlobalConfig(site_lang='en', ollama_prompt_lang='fr', chat_lang='fr',
                                chat_system_prompt='Réponds toujours en français.'))
            conv = ChatConversation(title='Saved title')
            db.add(conv)
            db.commit()
            self.conv_id = conv.id
            db.add(ChatMessage(conversation_id=conv.id, role='assistant', content='Ancienne réponse.'))
            db.commit()

    def tearDown(self):
        self.engine.dispose()

    async def test_header_switch_drives_chat_and_analysis_and_preserves_history(self):
        with patch('app.routers.config.SessionLocal', self.sessions):
            for lang in ['fr', 'en', 'fr', 'en']:
                update_site_lang({'lang': lang})
                self.assertEqual(get_site_lang()['site_lang'], lang)
                with self.sessions() as db:
                    cfg = db.query(GlobalConfig).one()
                    self.assertEqual((cfg.site_lang, cfg.chat_lang, cfg.ollama_prompt_lang), (lang, lang, lang))
                    self.assertEqual(_get_config_dict(cfg)['ollama_prompt_lang'], lang)
                    settings = await get_chat_settings(db)
                    self.assertEqual(settings['chat_lang'], lang)
                    prompt, _ = build_chat_prompt(self.conv_id, db)
                    self.assertIn('Respond in English' if lang == 'en' else 'Réponds en français', prompt.splitlines()[-1])
                    self.assertEqual(db.query(ChatMessage).one().content, 'Ancienne réponse.')
                    self.assertEqual(cfg.chat_system_prompt, 'Réponds toujours en français.')

    async def test_stale_chat_form_cannot_override_header(self):
        with self.sessions() as db:
            await save_chat_settings({'chat_lang': 'fr', 'chat_system_prompt': 'Custom instructions'}, db)
            self.assertEqual(get_language(db.query(GlobalConfig).one()), 'en')
            prompt, _ = build_chat_prompt(self.conv_id, db)
            self.assertIn('Respond in English', prompt)
            self.assertIn('Custom instructions', prompt)

    def test_invalid_header_value_does_not_change_saved_setting(self):
        with patch('app.routers.config.SessionLocal', self.sessions):
            for value in ['de', '../fr', None, ['en']]:
                with self.assertRaises(HTTPException) as caught:
                    update_site_lang({'lang': value})
                self.assertEqual(caught.exception.status_code, 422)
            self.assertEqual(get_site_lang()['site_lang'], 'en')

    def test_legacy_settings_do_not_override_shared_setting(self):
        self.assertEqual(get_language({'site_lang': 'en', 'ollama_prompt_lang': 'fr'}), 'en')
        self.assertEqual(get_language({'site_lang': 'fr', 'ollama_prompt_lang': 'en'}), 'fr')
        self.assertEqual(get_language(None), 'en')

    async def test_summary_and_compaction_follow_selected_language(self):
        ollama = type('Ollama', (), {'analyze_async': AsyncMock(return_value='Summary')})()
        for lang in ['en', 'fr']:
            for function in [run_summary, run_compaction]:
                await function('Ancienne conversation française', ollama, 'url', 'model', lang=lang)
                prompt = ollama.analyze_async.call_args.args[0]
                self.assertIn('Respond in English' if lang == 'en' else 'Réponds en français', prompt)
                self.assertNotIn('SAME LANGUAGE', prompt)
                self.assertNotIn('SAME language', prompt)
