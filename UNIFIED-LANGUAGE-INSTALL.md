# Unified language selector update

The header selector is now the shared English/French setting for the interface and new AI output: automatic/manual/retried analyses, chat, chat titles and summaries, meta-analysis, resolution explanations, and notifications. It is saved on the server, so other browsers and future page loads use that setting. Another already-open browser picks up changes when refreshed.

Old analysis/chat language overrides are ignored. The independent analysis selector is removed; the chat language field is read-only. Existing header selections are retained. New installations default to English.

Existing responses, saved conversation titles, custom prompts and raw logs keep their text. Match keywords and technical identifiers are preserved. Language instructions guide the model but model output must still be checked. In-progress generations finish with the language captured when they started.

## Update GitHub using VS Code

This ZIP contains only changed/new repository files, with their folder structure intact. Extract it to a temporary folder first.

In your repository's VS Code terminal:

```powershell
git switch main
git pull --ff-only origin main
git switch -c fix/unified-language-selector
```

Copy the ZIP contents into the repository, allowing replacement of matching files. Then inspect and publish:

```powershell
git status --short
git diff --stat
git add app static templates tests CHANGELOG.md ENGLISH-FORK-INSTALL.md UNIFIED-LANGUAGE-INSTALL.md
git diff --cached --stat
git commit -m "Use the header language selector across Sentinel"
git push -u origin fix/unified-language-selector
```

Create and merge the pull request into your fork's main branch, as before.

## Build on Betty after merging

In the Unraid terminal, run only these commands:

```bash
cd /mnt/user/appdata/sentinel-anglais-source
curl -fL https://github.com/smith844/log-to-llm-sentinel-anglais/archive/refs/heads/main.tar.gz -o /tmp/sentinel-anglais-source.tar.gz
tar -xzf /tmp/sentinel-anglais-source.tar.gz --strip-components=1
docker build -t sentinel-anglais:unified-language-1 .
```

Edit the existing **Log-to-LLM-Sentinel** Unraid template. Change **Repository** to `sentinel-anglais:unified-language-1`, and apply. Keep your existing data/log mappings and other settings. The previous image `sentinel-anglais:english-analysis-1` remains available for rollback.

Refresh the app with Ctrl+F5. Choose English in the header, generate a new analysis and send a new chat message. Select Français and repeat, then return to English. Existing history should be unchanged. Reopen the page to verify the saved choice persists.

## Development validation

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
node --test tests/test_i18n.js
```

16 Python tests cover the worker, conflicting legacy values, saving both language choices, chat prompts/history, summaries, meta-analysis and notification configuration. 3 JavaScript tests cover server-backed initialization, rapid switching and failed saves. Compilation, JS syntax, rendered chat-script syntax and whitespace checks also pass. Docker/Ollama integration must be verified on Betty.
