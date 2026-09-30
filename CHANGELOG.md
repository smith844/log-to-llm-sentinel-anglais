# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased] - Unified language selector

### Changed
- The header language selector now controls the interface, automatic/manual/retried analyses, follow-up chat, chat summaries and titles, meta-analysis, resolution explanations and notifications.
- Remove the independent analysis language control; show chat language as a read-only reflection of the header choice. Old language fields no longer override the shared setting.
- Load the saved language on page navigation. Await successful saving before changing the interface; report failures and serialize rapid switches.
- Preserve existing analyses, chat history, custom prompts, log text and match patterns. New installations default to English; existing header choices are respected.

### Validation
- 16 Python regression tests and 3 JavaScript selector tests pass. Ollama is mocked; deployment and model output still require verification on the host.

## English analysis fork (previous fix)

### Fixed
- Pass the saved analysis and notification languages to the background worker. Previously, the missing settings forced French log preambles and prompts regardless of the saved selection.
- Apply the analysis language to rule tests, retries, manual analysis and follow-up questions.
- Translate log truncation notices and meta-analysis event labels when English is selected, and explicitly request English explanations from the model.

### Changed
- Default new analysis settings and missing analysis-language values to English. Existing saved language choices and analysis history are preserved; the interface language and analysis language remain separate settings.

## [1.2.297] - 2026-06-30

### Fixed
- **Syslog Keyword Match & Detached Instance**: Fixed an issue where keyword detection would fail for syslog logs because only the message body was analyzed instead of the complete log line with the timestamp and hostname. Resolved a `DetachedInstanceError` caused by using an expired session object within an asynchronous orchestrator task.

## [1.2.296] - 2026-06-27

### Added
- **Complete Internationalization (i18n) & English Fallbacks**: Conducted an exhaustive scan of the codebase to translate all remaining user-facing French text hidden in templates, modales, tooltips, and default prompts. Added 15 new translation keys to `fr.json` and `en.json`. Overhauled fallback values in JavaScript (`meta.js`, `keyword_wizard.js`, `config.js`) to display English universally if the selected language pack fails to load, securing global application accessibility.

### Fixed
- **Light Theme Readability in Meta-Analysis Results**: Fixed a styling bug where inline `<code>`, `<pre>` code blocks, and `<blockquote>` quotes rendered with a hardcoded dark background (`#2d2d2d`) in light mode, making text completely unreadable. Swapped hardcoded background colors for theme-aware CSS variables and added explicit light theme overrides (`#f1f5f9` backgrounds with `#1e293b` text).

## [1.2.294] - 2026-06-27

### Added
- **Resolution Monitoring Redesign**: Completely overhauled the resolution monitoring section in the rule configuration modal. The old dropdown + disconnected toggle switches have been replaced by three explicit clickable mode cards (⏱ Inactivity Timeout, 🔍 Keyword in Logs, ⚡ Both), each with a title, a short description of the ideal use case, and inline configuration where relevant. The AI Validation and notification options are now grouped in a unified card list with icon, title, and description per row, making parent/child relationships (AI search notification indented under AI Validation) immediately clear. Fully responsive, keyboard-navigable, and i18n-compliant (fr + en).

### Fixed
- **Syslog/Webhook Pipeline — Missing AI Analysis**: Fixed a critical bug where log lines matched via syslog triggered the yellow highlight in the real-time monitor but never proceeded to AI analysis. Root cause: `asyncio.run_coroutine_threadsafe` was called from within the asyncio loop, causing a deadlock. Replaced with `asyncio.ensure_future` for correct coroutine scheduling from a synchronous threaded context.
- **Resolution Service — Lock Held During AI Call (BUG-RES-01)**: Refactored `_try_resolve` in `resolution_service.py` into three distinct phases. The `_state_lock` is now only held during short state mutation phases (transition to `resolving`, then final result application), never during the LLM call (which can take up to 180s). This eliminates a deadlock that froze all concurrent match processing during AI resolution validation.
- **Resolution Service — Stuck 'resolving' State (BUG-RES-02)**: Each phase of `_try_resolve` now has its own `except` block that resets both the in-memory state and the database `alert_status` back to `alert` on failure, preventing rules from being permanently stuck in `resolving`.
- **Resolution Service — Double Resolution Trigger (BUG-RES-EXTRA)**: `check_timeout_resolutions` was incorrectly re-triggering resolution for rules already in `resolving` state. Fixed by changing the status filter from `not in ("alert", "resolving")` to `!= "alert"`.
- **Resolution Service — Syslog/Webhook Paths in AI Audit (BUG-RES-04)**: `audit_patterns_with_ai` was attempting to open `[SYSLOG]:hostname` as a literal file path. Virtual paths are now resolved to their actual disk file (`/app/data/syslog/<hostname>.log` or `/app/data/webhooks/<token>.log`) before reading log context.
- **Orchestrator — Double SQLAlchemy Session Close (BUG-ORCH-01)**: Restructured `_flush_buffer` into two clean, independent `try/finally` blocks. The first opens a short-lived session to read `anti_spam_delay` and closes it before `await asyncio.sleep(...)`. The second opens a fresh session for actual processing. Eliminates the manual `db.close()` mid-try that caused a double-close on the `finally` block.
- **Syslog/Webhook Buffer Limit (BUG-SYS-01)**: Raised `_BUFFER_MAX` from 500 to 2000 lines in both `syslog_receiver.py` and `webhook.py`, matching the actual needs of production log volumes and eliminating the premature tail truncation reported by users.

## [1.2.285] - 2026-06-14

### Added
- **Configuration Export/Import**: Implemented a compressed ZIP-based configuration backup and restore system. Users can export their entire global settings and alert rules, with or without complete operational history (analyses, chat sessions, meta-analyses, and auto-learning history) and restore it seamlessly on another server instance.

## [1.2.284] - 2026-06-14

### Fixed
- **Meta-Analysis Variable Initialization**: Resolved a `NameError` crash (`lang` referenced before assignment) by initializing configuration variables (`lang`, `ollama`, `ollama_url`, `ollama_model`) early in the execution sequence.
- **Deepen with AI Button**: Fixed the "Approfondir avec l'IA" button on the meta-analysis result interface by directing requests to the correct router prefix (`/chat/api/create`), formatting the context fields as required by the backend schema, and pointing the redirect parameter to `id` instead of `conv`.

## [1.2.283] - 2026-06-13

### Fixed
- **Inactivity Notification Formatting**: Cleaned up HTML tags and formatted the inactivity warnings using Markdown for Discord and Apprise notification endpoints, preventing raw HTML code from appearing in alerts.
- **Syslog Path Resolution in Services**: Resolved `[SYSLOG]:hostname` formatted rule paths to actual server log files (`/app/data/syslog/<hostname>.log`) in both the keyword auto-learning service and the manual resolution service, preventing "File not found" errors when processing syslog sources.
- **Keyword Auto-Learning Syslog Source**: Fixed a bug where the auto-learning wizard failed to activate or launch when the Syslog source card was selected.
- **Config Update Endpoint**: Resolved config update endpoint failure causing UI "unknown error" warnings on settings page.
- **Browser Cache Busting**: Appended static query parameters in script references to ensure latest Javascript fixes load immediately without requiring manual cache refresh (CTRL+F5).

## [1.1.0] - 2026-06-11

### Added
- **Syslog Ingestion**: Turn Sentinel into a syslog UDP receiver (listening on port 10514 UDP on host / 514 UDP in container).
- **Syslog Relay**: Forward raw incoming syslog packets to an optional secondary backup server (e.g., secondary Unraid server).
- **Multi-Source Rule Routing**: Stream logs based on hostname prefix (`[SYSLOG]:hostname` or catch-all `[SYSLOG]:*`).
- **Interactive Configuration Helpers**: Settings page and Rule modal now display the host's actual LAN IP address (e.g., `192.168.22.167`) and port (`10514`) for easy copy-pasting into remote syslog configurations.
- **Global Syslog Buffer**: Added support for global catch-all buffer (`*`) tracking all syslog events simultaneously.

### Fixed
- **FastAPI Event Loop Sync-Reload Issue**: Fixed a bug where saving settings triggered a `no running event loop` error when reloading the UDP listener protocol.
- **I18n Accented Character Corruptions**: Protected JSON files using proper `utf-8-sig` encoding throughout updates.
