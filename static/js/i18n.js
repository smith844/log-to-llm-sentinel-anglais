/**
 * i18n.js — Module d'internationalisation pour Log to LLM Sentinel
 *
 * Usage:
 *   - Les éléments HTML statiques utilisent l'attribut data-i18n="section.key"
 *     ou data-i18n-placeholder="section.key" pour les placeholders.
 *   - Le JS dynamique utilise window.t('section.key') pour obtenir la traduction.
 *   - Pour changer de langue: window.switchLanguage('en')
 *
 * Pour ajouter une nouvelle langue:
 *   1. Copiez static/i18n/fr.json vers static/i18n/xx.json (xx = code ISO de la langue)
 *   2. Modifiez le bloc "_meta" avec le nom et le drapeau de votre langue
 *   3. Traduisez toutes les valeurs
 *   L'application détectera automatiquement le nouveau fichier au redémarrage.
 */

(function () {
    'use strict';

    let _translations = {};
    let _currentLang = 'en';
    let _availableLangs = [];
    // Callbacks à appeler après chaque changement de langue (pour re-rendre le contenu dynamique)
    const _reRenderCallbacks = [];

    // Fallback SVG pour Chrome sur Windows qui ne gère pas les emojis drapeaux
    const FLAG_SVGS = {
        '🇫🇷': '<svg viewBox="0 0 3 2" style="width:18px;height:14px;border-radius:2px;display:inline-block;vertical-align:middle;box-shadow:0 0 2px rgba(0,0,0,0.2)"><path fill="#002654" d="M0 0h1v2H0z"/><path fill="#fff" d="M1 0h1v2H1z"/><path fill="#ed2939" d="M2 0h1v2H2z"/></svg>',
        '🇬🇧': '<svg viewBox="0 0 60 30" style="width:18px;height:14px;border-radius:2px;display:inline-block;vertical-align:middle;box-shadow:0 0 2px rgba(0,0,0,0.2)"><clipPath id="uk-a"><path d="M0 0h60v30H0z"/></clipPath><clipPath id="uk-b"><path d="M30 15h30v15zv15H0zH0V0zV0h30z"/></clipPath><g clip-path="url(#uk-a)"><path fill="#012169" d="M0 0h60v30H0z"/><path stroke="#fff" stroke-width="6" d="M0 0l60 30m0-30L0 30"/><path stroke="#C8102E" stroke-width="4" clip-path="url(#uk-b)" d="M0 0l60 30m0-30L0 30"/><path stroke="#fff" stroke-width="10" d="M30 0v30M0 15h60"/><path stroke="#C8102E" stroke-width="6" d="M30 0v30M0 15h60"/></g></svg>'
    };

    function getFlagHTML(flagEmoji) {
        return FLAG_SVGS[flagEmoji] || flagEmoji;
    }

    /**
     * Résout une clé en chemin pointé ("section.key") dans l'objet de traductions.
     * Retourne la clé elle-même si non trouvée (fail-safe visible).
     */
    function t(key) {
        const parts = key.split('.');
        let current = _translations;
        for (const part of parts) {
            if (current == null || typeof current !== 'object') return key;
            current = current[part];
        }
        return (current != null && typeof current === 'string') ? current : key;
    }

    /**
     * Applique les traductions à tous les éléments DOM avec data-i18n.
     */
    function applyTranslations() {
        // Texte des éléments
        document.querySelectorAll('[data-i18n]').forEach(el => {
            const key = el.getAttribute('data-i18n');
            const translated = t(key);
            if (translated !== key) el.textContent = translated;
        });

        // Placeholders des inputs/textareas
        document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
            const key = el.getAttribute('data-i18n-placeholder');
            const translated = t(key);
            if (translated !== key) el.setAttribute('placeholder', translated);
        });

        // Titles (tooltips)
        document.querySelectorAll('[data-i18n-title]').forEach(el => {
            const key = el.getAttribute('data-i18n-title');
            const translated = t(key);
            if (translated !== key) el.setAttribute('title', translated);
        });

        // HTML (pour les éléments avec balisage interne)
        document.querySelectorAll('[data-i18n-html]').forEach(el => {
            const key = el.getAttribute('data-i18n-html');
            const translated = t(key);
            if (translated !== key) el.innerHTML = translated;
        });

        // Mettre à jour l'attribut lang du document
        document.documentElement.setAttribute('lang', _currentLang);
    }

    /**
     * Charge un fichier de langue et l'applique.
     */
    async function loadLanguage(lang, persist = false) {
        try {
            const res = await fetch(`/static/i18n/${lang}.json?v=${Date.now()}`);
            if (!res.ok) throw new Error(`Fichier de langue introuvable: ${lang}`);
            const translations = await res.json();
            if (persist) {
                const saved = await fetch('/api/config/site-lang', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ lang })
                });
                if (!saved.ok) throw new Error('Language could not be saved');
            }
            _translations = translations;
            _currentLang = lang;
            localStorage.setItem('sentinel_lang', lang);
            applyTranslations();
            // Déclencher les callbacks de re-rendu (ex: re-render les listes dynamiques)
            _reRenderCallbacks.forEach(cb => { try { cb(lang); } catch (e) { console.warn('i18n reRender callback error:', e); } });
            updateLangSwitcherUI();
        } catch (e) {
            console.error('[i18n] Erreur chargement langue:', e);
            if (persist) alert(t('common.language_sync_error'));
        }
    }

    /**
     * Retourne la langue active.
     */
    async function detectLanguage() {
        // Opening a page must never overwrite the shared server setting.
        const res = await fetch('/api/config/site-lang');
        if (!res.ok) throw new Error('Language could not be loaded');
        return (await res.json()).site_lang || 'en';
    }

    /**
     * Charge les langues disponibles depuis le backend et construit le switcher.
     */
    async function initLangSwitcher() {
        try {
            _availableLangs = await (await fetch('/api/i18n/languages')).json();
        } catch (e) {
            // Fallback si le backend n'est pas disponible
            _availableLangs = [
                { code: 'fr', name: 'Français', flag: '🇫🇷' },
                { code: 'en', name: 'English', flag: '🇬🇧' },
            ];
        }
        buildSwitcherUI();
    }

    /**
     * Construit le DOM du sélecteur de langue dans la navbar.
     */
    function buildSwitcherUI() {
        const container = document.getElementById('lang-switcher');
        if (!container || _availableLangs.length < 2) return;

        const current = _availableLangs.find(l => l.code === _currentLang) || _availableLangs[0];
        container.innerHTML = `
            <div class="lang-switcher-wrapper">
                <button class="lang-btn" id="lang-toggle-btn" aria-haspopup="true" aria-expanded="false">
                    <span>${getFlagHTML(current.flag)}</span>
                    <span class="lang-code">${current.code.toUpperCase()}</span>
                    <svg width="10" height="10" viewBox="0 0 10 10"><path fill="currentColor" d="M5 7L1 3h8z"/></svg>
                </button>
                <div class="lang-dropdown" id="lang-dropdown" role="menu">
                    ${_availableLangs.map(l => `
                        <button class="lang-option ${l.code === _currentLang ? 'active' : ''}"
                                onclick="window.switchLanguage('${l.code}')"
                                role="menuitem">
                            <span>${getFlagHTML(l.flag)}</span>
                            <span>${l.name}</span>
                        </button>
                    `).join('')}
                </div>
            </div>
        `;

        // Toggle dropdown
        const btn = document.getElementById('lang-toggle-btn');
        const dropdown = document.getElementById('lang-dropdown');
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const open = dropdown.classList.toggle('open');
            btn.setAttribute('aria-expanded', open);
        });
        document.addEventListener('click', () => {
            dropdown.classList.remove('open');
            btn.setAttribute('aria-expanded', false);
        });
    }

    /**
     * Met à jour l'UI du switcher sans le reconstruire entièrement.
     */
    function updateLangSwitcherUI() {
        // Mettre à jour le bouton principal
        const current = _availableLangs.find(l => l.code === _currentLang);
        const btn = document.getElementById('lang-toggle-btn');
        if (btn && current) {
            btn.innerHTML = `
                <span>${getFlagHTML(current.flag)}</span>
                <span class="lang-code">${current.code.toUpperCase()}</span>
                <svg width="10" height="10" viewBox="0 0 10 10"><path fill="currentColor" d="M5 7L1 3h8z"/></svg>
            `;
        }
        // Mettre à jour les options actives
        document.querySelectorAll('.lang-option').forEach(opt => {
            const code = opt.getAttribute('onclick')?.match(/'([^']+)'/)?.[1];
            opt.classList.toggle('active', code === _currentLang);
        });
    }

    /**
     * Change la langue et re-applique toutes les traductions + re-rendu dynamique.
     */
    let languageChange = Promise.resolve();

    function switchLanguage(lang) {
        languageChange = languageChange.then(() => changeLanguage(lang));
        return languageChange;
    }

    async function changeLanguage(lang) {
        if (lang === _currentLang) return;
        // Fermer le dropdown
        document.getElementById('lang-dropdown')?.classList.remove('open');
        await loadLanguage(lang, true);
    }

    /**
     * Enregistre un callback à appeler après chaque changement de langue.
     * Utilisé par les pages pour re-rendre leur contenu dynamique.
     */
    function onLanguageChange(callback) {
        _reRenderCallbacks.push(callback);
    }

    // ── API publique ──
    window.t = t;
    window.switchLanguage = switchLanguage;
    window.i18n = {
        t,
        switchLanguage,
        onLanguageChange,
        applyTranslations,
        getCurrentLang: () => _currentLang,
        getFlagHTML: getFlagHTML,
    };

    // ── Initialisation ──
    document.addEventListener('DOMContentLoaded', async () => {
        await initLangSwitcher();
        try {
            await loadLanguage(await detectLanguage());
        } catch (e) {
            console.warn('[i18n] Could not read shared language:', e);
            await loadLanguage(localStorage.getItem('sentinel_lang') || 'en');
        }
    });
})();
