# English AI analysis fix for Unraid

This package patches the fork at https://github.com/smith844/log-to-llm-sentinel-anglais.

## What changed

The automatic analysis worker omitted the stored `ollama_prompt_lang` and `site_lang` settings, which caused French preambles and prompts even when English had been saved. Those settings now reach the worker. Rule tests, retries, manual analysis, follow-up questions and meta-analysis also use the saved analysis language. English is the default for new analysis settings, and English prompts explicitly request English explanations.

French support remains available. The interface language selector and the Ollama analysis language are separate settings. For an existing installation, select **English** in **Settings → Ollama analysis language**, then save. Selecting English only in the page header does not change the stored analysis language.

Existing saved analyses retain their original text. Generate a new analysis to verify the corrected preamble. Retrying an old analysis updates its AI response but retains its original stored log preamble. No database migration rewrites existing history or changes your saved language choice.

## Install using your existing Unraid container template

1. Download and extract this ZIP into `/mnt/user/appdata/sentinel-anglais-source`. The source files must be directly inside that folder, including `Dockerfile` and `requirements.txt`.
2. Record the current container's **Repository** value and existing port, path and environment mappings. Export Sentinel's configuration from its Settings page. Stop the container and copy its mapped application data folder to a separate backup location while it is stopped.
3. In the Unraid terminal, build the local image:

   ```bash
   cd /mnt/user/appdata/sentinel-anglais-source
   docker build -t sentinel-anglais:english-analysis-1 .
   ```

4. Edit the existing Unraid container template. Set **Repository** to `sentinel-anglais:english-analysis-1`, retain your existing mappings, and click Apply. If Unraid offers to pull the local image from a registry, use the image already built on this host rather than substituting the upstream image. Do not run the supplied docker-compose stack alongside the existing container; it would compete for ports and may use a different data folder.
5. Open Sentinel, set **Ollama analysis language** to **English** in Settings, and save. Check any custom system prompt for instructions explicitly requesting French and adjust them if necessary.
6. Trigger a new matching event or use the rule-test action. For two buffered events, the log block should begin:

   > These 2 matching events appeared in the last 60 seconds. Here are the most recent:

   The model prompt requests an English explanation with the usual `SEVERITY:` line. Model behaviour still depends on the model and supplied custom instructions.

The fork has not been published as a Docker Hub image. Updating to the upstream container image would replace this patch; rebuild this local image when you intentionally update the source.

## Rollback

Stop the patched container, restore the original Repository value in the same Unraid template, and Apply. Retain the same application data mapping. Restore the stopped-container data backup only if needed, with the container stopped.

## Validation

- Eleven regression tests passed using a temporary SQLite database and mocked Ollama responses. They cover the automatic worker, saved language choices, defaults, truncation notices, notification language forwarding, meta-analysis prompts and translation key consistency.
- The English worker regression test failed against the upstream source and passed after the fix.
- Python compilation, JavaScript syntax and Git whitespace checks passed.
- A Docker build and real Ollama/Unraid integration test were not available in the development environment.

Run the included tests locally with:

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

## GitHub changes

Local branch: `fix/english-ai-analysis`.

`english-ai-analysis.patch` contains the code fix and tests as a mail-formatted Git patch. To apply it to a clean checkout of the fork at the original revision:

```bash
git switch -c fix/english-ai-analysis
git am /path/to/english-ai-analysis.patch
```

The GitHub push was blocked by automatic approval review pending explicit authorization. This ZIP already includes the patched source; installation does not require the remote branch.
