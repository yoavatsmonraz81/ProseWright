/**
 * ProseWright — SillyTavern extension (Phase 6 face, slice 3)
 *
 * Talks to the local engine API: python3 -m story_editor serve
 */

import { extension_settings, getContext, renderExtensionTemplateAsync } from '../../../extensions.js';
import { saveChatConditional, saveSettingsDebounced, showMoreMessages, getFirstDisplayedMessageId } from '../../../../script.js';
import { flashHighlight, delay } from '../../../utils.js';
import { debounce_timeout } from '../../../constants.js';
import { SlashCommandParser } from '../../../slash-commands/SlashCommandParser.js';
import { SlashCommand } from '../../../slash-commands/SlashCommand.js';
import { ARGUMENT_TYPE, SlashCommandArgument, SlashCommandNamedArgument } from '../../../slash-commands/SlashCommandArgument.js';

const EXT_ID = 'story_editor';
const EXT_VERSION = '0.4.3';

/** Cards in the open chat: the character card first, then the player's persona. */
function chatCards() {
    const { name1, name2 } = getContext();
    return [name2, name1].filter((n, i, a) => n && a.indexOf(n) === i);
}

function cardOptions() {
    return chatCards().map(c => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join('');
}

const TAB_RESULTS_LABEL = {
    edit: 'Proposal (before → after)',
    label: 'Review queue',
    audit: 'Audit report',
    history: 'History',
};
const EXT_FOLDER = 'third-party/StoryEditor';
const DEFAULT_API = 'http://127.0.0.1:8765';
const LLM_TIMEOUT_MS = 600000; // match server llm.chat timeout (10 min)

const defaultSettings = {
    api_url: DEFAULT_API,
    log_path: '',
};

function settings() {
    extension_settings[EXT_ID] = extension_settings[EXT_ID] || { ...defaultSettings };
    return extension_settings[EXT_ID];
}

function apiBase() {
    return (settings().api_url || DEFAULT_API).replace(/\/$/, '');
}

function apiBody(extra = {}) {
    const body = { ...extra };
    const logPath = (settings().log_path || '').trim();
    if (logPath) {
        body.log = logPath;
    }
    return body;
}

async function api(path, options = {}) {
    const url = `${apiBase()}${path}`;
    const init = {
        headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
        ...options,
    };
    if (options.timeoutMs) {
        init.signal = AbortSignal.timeout(options.timeoutMs);
    }
    const res = await fetch(url, init);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
        throw new Error(data.error || `HTTP ${res.status}`);
    }
    return data;
}

let streamTimer = null;
const streamMeta = { start: 0, phase: '', thinking: '', output: '' };

function beginStreamPreview(tabId, label) {
    setResultsPlaceholder(tabId, `
        <div class="se-stream-box">
            <div class="se-stream-meta" id="se-stream-meta">${escapeHtml(label)}</div>
            <div class="se-stream-thinking-wrap" id="se-stream-thinking-wrap" hidden>
                <div class="se-stream-channel-label">Planning…</div>
                <div class="se-stream-thinking" id="se-stream-thinking"></div>
            </div>
            <div class="se-stream-output-wrap">
                <div class="se-stream-channel-label">Draft</div>
                <div class="se-stream-output" id="se-stream-output"></div>
            </div>
        </div>`);
    streamMeta.start = Date.now();
    streamMeta.phase = label;
    streamMeta.thinking = '';
    streamMeta.output = '';
    clearInterval(streamTimer);
    streamTimer = setInterval(updateStreamMeta, 1000);
}

function updateStreamMeta() {
    const el = document.getElementById('se-stream-meta');
    if (!el) return;
    const secs = Math.floor((Date.now() - streamMeta.start) / 1000);
    const m = Math.floor(secs / 60);
    const s = secs % 60;
    const elapsed = m ? `${m}m ${s}s` : `${s}s`;
    const bits = [streamMeta.phase, elapsed];
    if (streamMeta.thinking.length) bits.push(`${streamMeta.thinking.length} plan chars`);
    if (streamMeta.output.length) bits.push(`${streamMeta.output.length} draft chars`);
    el.textContent = bits.join(' · ');
}

function onStreamEvent(ev) {
    if (!ev?.kind) return;
    if (ev.kind === 'phase' && ev.text) {
        streamMeta.phase = ev.text;
        streamMeta.output = '';
        streamMeta.thinking = '';
        const out = document.getElementById('se-stream-output');
        const think = document.getElementById('se-stream-thinking');
        const wrap = document.getElementById('se-stream-thinking-wrap');
        if (out) out.textContent = '';
        if (think) think.textContent = '';
        if (wrap) wrap.hidden = true;
        updateStreamMeta();
        return;
    }
    if (ev.kind === 'reasoning' && ev.text) {
        streamMeta.thinking += ev.text;
        const wrap = document.getElementById('se-stream-thinking-wrap');
        const el = document.getElementById('se-stream-thinking');
        if (wrap) wrap.hidden = false;
        if (el) {
            el.textContent = streamMeta.thinking.slice(-800);
            el.scrollTop = el.scrollHeight;
        }
        updateStreamMeta();
        return;
    }
    if (ev.kind === 'content' && ev.text) {
        streamMeta.output += ev.text;
        const el = document.getElementById('se-stream-output');
        if (el) {
            el.textContent = streamMeta.output;
            el.scrollTop = el.scrollHeight;
        }
        updateStreamMeta();
    }
}

function endStreamPreview() {
    clearInterval(streamTimer);
    streamTimer = null;
}

async function apiStream(path, body) {
    const url = `${apiBase()}${path}`;
    const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...body, stream: true }),
        signal: AbortSignal.timeout(LLM_TIMEOUT_MS),
    });
    if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || `HTTP ${res.status}`);
    }
    if (!res.body) {
        throw new Error('No response body for stream');
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let finalPayload = null;

    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split('\n\n');
        buffer = parts.pop() || '';
        for (const chunk of parts) {
            for (const line of chunk.split('\n')) {
                if (!line.startsWith('data:')) continue;
                const json = line.slice(5).trim();
                if (!json) continue;
                const ev = JSON.parse(json);
                if (ev.kind === 'done') {
                    finalPayload = ev;
                    continue;
                }
                if (ev.kind === 'error') {
                    throw new Error(ev.error || 'Stream error');
                }
                onStreamEvent(ev);
            }
        }
    }

    if (!finalPayload) {
        throw new Error('Stream ended without a result');
    }
    return finalPayload;
}

async function proposeWithStream(path, payload, previewLabel) {
    beginStreamPreview('edit', previewLabel);
    try {
        return await apiStream(path, apiBody(payload));
    } finally {
        endStreamPreview();
    }
}

const TAB_IDS = ['edit', 'label', 'audit', 'history'];

const TAB_META = {
    edit: {
        label: 'Edit',
        blurb: 'Lock a target message by msg # or beat + position, then Propose. Single-msg lock edits one turn; widen From–to for a span. Commit saves to the log.',
    },
    label: {
        label: 'Label',
        blurb: 'Tag who was really voiced (card ≠ voice). Accept saves immediately to the sidecar — no Commit. Use Go to msg for context.',
    },
    audit: {
        label: 'Audit',
        blurb: 'Check quality before or after edits. Sweep needs a prior commit; Proofread and Canon check run on pending proposals.',
    },
    history: {
        label: 'History',
        blurb: 'Past commits and undos on the engine log. Click Show diff to reload an edit in the panel below.',
    },
};

function resultsBody(tabId) {
    return document.getElementById(`se-body-${tabId}`);
}

function tabResultsHtml(tabId) {
    const label = TAB_RESULTS_LABEL[tabId] || 'Results';
    return `
        <div class="se-tab-results">
            <div class="se-results-label">${label}</div>
            <div class="se-body" id="se-body-${tabId}" data-tab="${tabId}"></div>
        </div>`;
}

function setResultsPlaceholder(tabId, html) {
    const body = resultsBody(tabId);
    if (body) body.innerHTML = html;
}

function switchTab(tabId) {
    const drawer = document.getElementById('story-editor-drawer');
    if (!drawer || !TAB_IDS.includes(tabId)) return;
    drawer.dataset.activeTab = tabId;
    drawer.querySelectorAll('.se-tab').forEach((btn) => {
        btn.classList.toggle('active', btn.dataset.tab === tabId);
    });
    drawer.querySelectorAll('.se-tab-panel').forEach((panel) => {
        panel.classList.toggle('active', panel.dataset.tab === tabId);
    });
    const latch = document.getElementById('story-editor-latch');
    if (latch) latch.setAttribute('aria-expanded', 'true');
    if (tabId === 'edit') {
        void refreshEditTarget().then(async () => {
            if (!targetLock) {
                const { chat } = getContext();
                const lastIdx = Math.max(0, chat.length - 1);
                await locateTarget({ msg_id: lastIdx });
            }
            await refreshEditTabProposal();
        });
    }
}

function openDrawer(tabId = 'edit') {
    const drawer = ensureDrawer();
    drawer.classList.add('open');
    switchTab(tabId);
    const latch = document.getElementById('story-editor-latch');
    if (latch) latch.classList.add('hidden');
}

function closeDrawer() {
    const drawer = document.getElementById('story-editor-drawer');
    if (drawer) drawer.classList.remove('open');
    const latch = document.getElementById('story-editor-latch');
    if (latch) latch.classList.remove('hidden');
}

function ensureLatch() {
    if (document.getElementById('story-editor-latch')) return;
    const latch = document.createElement('button');
    latch.type = 'button';
    latch.id = 'story-editor-latch';
    latch.className = 'se-drawer-latch menu_button';
    latch.title = 'ProseWright';
    latch.textContent = 'ProseWright';
    latch.addEventListener('click', () => openDrawer('edit'));
    document.body.appendChild(latch);
}

function ensureDrawer() {
    let drawer = document.getElementById('story-editor-drawer');
    if (drawer && !drawer.querySelector('#se-body-edit')) {
        drawer.remove();
        drawer = null;
    }
    if (drawer && !drawer.querySelector('#se-inject-panel')) {
        drawer.remove();
        drawer = null;
    }
    if (drawer) return drawer;

    const tabButtons = TAB_IDS.map((id, i) =>
        `<button type="button" class="se-tab menu_button${i === 0 ? ' active' : ''}" data-tab="${id}">${TAB_META[id].label}</button>`,
    ).join('');

    const tabPanels = TAB_IDS.map((id) => {
        const blurb = TAB_META[id].blurb;
        const active = id === 'edit' ? ' active' : '';
        if (id === 'edit') {
            return `
        <div class="se-tab-panel${active}" data-tab="edit">
            <p class="se-tab-blurb">${blurb}</p>
            <div class="se-edit-target">
                <div class="se-target-row">
                    <label>Msg #
                        <input id="se-target-msg" class="text_pole" type="number" min="0"
                            title="0-based index (mesid in chat)" />
                    </label>
                    <button type="button" class="menu_button" id="se-target-set-msg">Set</button>
                </div>
                <div class="se-target-row">
                    <label>Beat
                        <input id="se-target-beat" class="text_pole" type="number" min="0" />
                    </label>
                    <label># in beat
                        <input id="se-target-beat-pos" class="text_pole" type="number" min="1"
                            title="1-based: 1 = first message in beat" />
                    </label>
                    <label>Card
                        <select id="se-target-speaker" class="text_pole">
                            <option value="">any</option>
                            ${cardOptions()}
                        </select>
                    </label>
                    <button type="button" class="menu_button" id="se-target-set-beat">Set</button>
                </div>
                <div class="se-target-row se-target-nav">
                    <button type="button" class="menu_button" id="se-target-prev" title="Previous in beat">◀</button>
                    <button type="button" class="menu_button" id="se-target-next" title="Next in beat">▶</button>
                    <button type="button" class="menu_button" id="se-target-last">Last msg</button>
                    <button type="button" class="menu_button" id="se-target-goto">Go to</button>
                    <label class="se-target-span-label">Span from
                        <input id="se-target-from" class="text_pole" type="number" min="0" />
                    </label>
                    <label>to
                        <input id="se-target-to" class="text_pole" type="number" min="0" />
                    </label>
                </div>
                <p id="se-target-banner" class="se-target-banner" aria-live="polite"></p>
            </div>
            <div class="se-inject-panel" id="se-inject-panel" hidden>
                <div class="se-target-row">
                    <label>Card
                        <select id="se-inject-speaker" class="text_pole" title="ST card slot for the new line">
                            ${cardOptions()}
                        </select>
                    </label>
                    <label>Voice <span class="se-optional">(optional)</span>
                        <select id="se-inject-voice" class="text_pole" title="Sidecar voice — saved on Commit">
                            <option value="">— label later —</option>
                            ${attributionVoices().map(v => `<option value="${v}">${v}</option>`).join('')}
                        </select>
                    </label>
                </div>
                <label class="se-inject-gist-label">Gist
                    <input id="se-inject-gist" class="text_pole" type="text"
                        placeholder="e.g. talks about the weather, a quiet interior beat…" />
                </label>
            </div>
            <div class="se-toolbar">
                <select id="se-operator" class="text_pole" title="Transform operator">
                    <option value="restyle">Restyle</option>
                    <option value="retune-tighten">Retune · tighten</option>
                    <option value="retune-expand">Retune · expand</option>
                    <option value="inject">Inject</option>
                </select>
                <input id="se-note-input" class="text_pole" type="text"
                    placeholder="Direction (plain language)…" />
                <button type="button" class="menu_button" id="se-run-propose">Propose</button>
            </div>
            <div class="se-tab-actions">
                <button type="button" class="menu_button" id="se-discard">Discard</button>
                <button type="button" class="menu_button se-primary" id="se-commit">Commit</button>
                <button type="button" class="menu_button" id="se-pull" title="Replace this chat with the engine log. Use this after a prepend committed in the web editor.">Load from engine</button>
                <button type="button" class="menu_button" id="se-undo">Undo last</button>
            </div>
            ${tabResultsHtml('edit')}
        </div>`;
        }
        if (id === 'label') {
            return `
        <div class="se-tab-panel${active}" data-tab="label">
            <p class="se-tab-blurb">${blurb}</p>
            <details class="se-mode-guide">
                <summary>Voice &amp; mode cheat sheet</summary>
                <ul>
                    <li><strong>ensemble</strong> — multiple characters, no single POV (use with scene_narrator)</li>
                    <li><strong>voice</strong> — who was played for single-voice posts (a character's key)</li>
                    <li><strong>pov</strong> — single character, card matches voice</li>
                    <li><strong>wrong_card</strong> — card used to play someone else</li>
                    <li><strong>scene_narrator</strong> — multi-voice scene block (common on narrator cards)</li>
                    <li><strong>mixed</strong> — main POV plus embedded dialogue</li>
                </ul>
            </details>
            <div class="se-attr-filters">
                <label>Preset
                    <select id="se-attr-preset" class="text_pole">
                        <option value="focus" selected>Focus (hide scene blocks)</option>
                        <option value="player_turns">Player turns · mixed / wrong card</option>
                        <option value="scene_blocks">Scene blocks only</option>
                        <option value="all">All need review</option>
                    </select>
                </label>
                <label>Card
                    <select id="se-attr-card" class="text_pole">
                        <option value="">Any</option>
                        ${cardOptions()}
                    </select>
                </label>
                <label>Conf ≥
                    <input id="se-attr-conf-min" class="text_pole" type="number"
                        min="0" max="1" step="0.05" placeholder="0" />
                </label>
                <label>Conf ≤
                    <input id="se-attr-conf-max" class="text_pole" type="number"
                        min="0" max="1" step="0.05" placeholder="1" />
                </label>
            </div>
            <div class="se-tab-actions">
                <button type="button" class="menu_button se-primary" id="se-attribution">Review queue</button>
                <button type="button" class="menu_button" id="se-attr-reconcile">Reconcile</button>
            </div>
            ${tabResultsHtml('label')}
        </div>`;
        }
        if (id === 'audit') {
            return `
        <div class="se-tab-panel" data-tab="audit">
            <p class="se-tab-blurb">${blurb}</p>
            <div class="se-tab-actions">
                <button type="button" class="menu_button" id="se-sweep">Sweep last edit</button>
                <button type="button" class="menu_button" id="se-proofread">Proofread pending</button>
                <button type="button" class="menu_button" id="se-canon">Canon check</button>
            </div>
            ${tabResultsHtml('audit')}
        </div>`;
        }
        return `
        <div class="se-tab-panel" data-tab="history">
            <p class="se-tab-blurb">${blurb}</p>
            <div class="se-tab-actions">
                <button type="button" class="menu_button se-primary" id="se-history">Refresh history</button>
            </div>
            ${tabResultsHtml('history')}
        </div>`;
    }).join('');

    drawer = document.createElement('div');
    drawer.id = 'story-editor-drawer';
    drawer.dataset.activeTab = 'edit';
    drawer.innerHTML = `
        <div class="se-header">
            <h3>ProseWright <span class="se-ext-version">v${EXT_VERSION}</span></h3>
            <button type="button" class="se-close menu_button" title="Close">✕</button>
        </div>
        <nav class="se-tabs" aria-label="ProseWright sections">${tabButtons}</nav>
        <div class="se-tab-panels">${tabPanels}</div>
        <div class="se-status" id="se-status"></div>
    `;
    document.body.appendChild(drawer);

    setResultsPlaceholder('edit', '<p class="se-empty-hint">Propose a change to see before/after here.</p>');
    setResultsPlaceholder('label', '<p class="se-empty-hint">Click Review queue to load labels needing attention.</p>');
    setResultsPlaceholder('audit', '<p class="se-empty-hint">Run sweep, proofread, or canon check to see results.</p>');
    setResultsPlaceholder('history', '<p class="se-empty-hint">Click Refresh history for recent commits.</p>');

    drawer.querySelector('.se-close').addEventListener('click', closeDrawer);
    drawer.querySelector('#se-discard').addEventListener('click', onDiscard);
    drawer.querySelector('#se-commit').addEventListener('click', onCommit);
    drawer.querySelector('#se-pull').addEventListener('click', onPull);
    drawer.querySelector('#se-undo').addEventListener('click', onUndo);
    drawer.querySelector('#se-history').addEventListener('click', onHistory);
    drawer.querySelector('#se-sweep').addEventListener('click', onSweep);
    drawer.querySelector('#se-proofread').addEventListener('click', onProofread);
    drawer.querySelector('#se-canon').addEventListener('click', onCanonCheck);
    drawer.querySelector('#se-attribution').addEventListener('click', onAttribution);
    drawer.querySelector('#se-attr-reconcile').addEventListener('click', onAttributionReconcile);
    drawer.querySelector('#se-run-propose').addEventListener('click', onToolbarPropose);
    drawer.querySelector('#se-operator')?.addEventListener('change', updateOperatorUI);
    drawer.querySelector('#se-inject-speaker')?.addEventListener('change', () => {
        suggestInjectVoice();
    });
    drawer.querySelector('#se-inject-voice')?.addEventListener('change', (ev) => {
        ev.target.dataset.touched = ev.target.value ? '1' : '';
    });
    drawer.querySelector('#se-target-set-msg')?.addEventListener('click', () => {
        const raw = document.getElementById('se-target-msg')?.value;
        if (raw === '' || raw == null) return;
        locateTarget({ msg_id: Number(raw) });
    });
    drawer.querySelector('#se-target-set-beat')?.addEventListener('click', () => {
        const beat = document.getElementById('se-target-beat')?.value;
        const pos = document.getElementById('se-target-beat-pos')?.value;
        if (beat === '' || pos === '') return;
        locateTarget({ beat: Number(beat), pos: Number(pos) });
    });
    drawer.querySelector('#se-target-last')?.addEventListener('click', () => {
        refreshEditTarget({ resetToLast: true });
    });
    drawer.querySelector('#se-target-goto')?.addEventListener('click', () => {
        scrollChatToMsg(getEditTargetSpan().from);
    });
    drawer.querySelector('#se-target-prev')?.addEventListener('click', () => {
        stepTargetInBeat(-1);
    });
    drawer.querySelector('#se-target-next')?.addEventListener('click', () => {
        stepTargetInBeat(1);
    });
    document.getElementById('se-target-msg')?.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter') {
            ev.preventDefault();
            drawer.querySelector('#se-target-set-msg')?.click();
        }
    });
    for (const id of ['se-target-beat', 'se-target-beat-pos']) {
        document.getElementById(id)?.addEventListener('keydown', (ev) => {
            if (ev.key === 'Enter') {
                ev.preventDefault();
                drawer.querySelector('#se-target-set-beat')?.click();
            }
        });
    }
    drawer.querySelectorAll('.se-tab').forEach((btn) => {
        btn.addEventListener('click', () => switchTab(btn.dataset.tab));
    });

    ensureLatch();
    updateOperatorUI();
    return drawer;
}

function setStatus(text) {
    const el = document.getElementById('se-status');
    if (el) el.textContent = text;
}

function escapeHtml(s) {
    return String(s ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

function defaultSpanOpts() {
    const { from, to } = getEditTargetSpan();
    return { from, to };
}

function updateOperatorUI() {
    const op = document.getElementById('se-operator')?.value || 'restyle';
    const injectPanel = document.getElementById('se-inject-panel');
    const noteInput = document.getElementById('se-note-input');
    if (injectPanel) {
        injectPanel.hidden = op !== 'inject';
    }
    if (noteInput) {
        noteInput.hidden = op === 'inject';
    }
    if (op === 'inject') {
        syncInjectSpeakerFromTarget();
    }
}

function syncInjectSpeakerFromTarget() {
    const { from } = getEditTargetSpan();
    const { chat } = getContext();
    const card = chat[from]?.name || chat[from]?.ch_name;
    const speakerEl = document.getElementById('se-inject-speaker');
    if (speakerEl && card && chatCards().includes(card)) {
        speakerEl.value = card;
        suggestInjectVoice();
    }
}

function suggestInjectVoice() {
    const card = document.getElementById('se-inject-speaker')?.value || '';
    const voiceEl = document.getElementById('se-inject-voice');
    if (!voiceEl || voiceEl.dataset.touched) return;
    const suggested = card ? card.toLowerCase() : '';
    if (suggested) voiceEl.value = suggested;
}

function getInjectParams() {
    const speaker = document.getElementById('se-inject-speaker')?.value || chatCards()[0] || 'Narrator';
    const voice = (document.getElementById('se-inject-voice')?.value || '').trim();
    const note = (document.getElementById('se-inject-gist')?.value || '').trim();
    return { speaker, voice: voice || undefined, note };
}

/** @type {object | null} */
let targetLock = null;

function speakerFilterValue() {
    return (document.getElementById('se-target-speaker')?.value || '').trim() || null;
}

function getEditTargetSpan() {
    const { chat } = getContext();
    const lastIdx = Math.max(0, chat.length - 1);
    const fromEl = document.getElementById('se-target-from');
    const toEl = document.getElementById('se-target-to');
    const fromRaw = fromEl?.value;
    const toRaw = toEl?.value;
    const from = fromRaw !== '' && fromRaw != null ? Number(fromRaw) : lastIdx;
    const to = toRaw !== '' && toRaw != null ? Number(toRaw) : from;
    if (!Number.isFinite(from) || from < 0) {
        return { from: lastIdx, to: lastIdx };
    }
    const lo = Math.min(from, Number.isFinite(to) ? to : from);
    const hi = Math.max(from, Number.isFinite(to) ? to : from);
    return { from: lo, to: hi };
}

function applyTargetLocation(loc) {
    targetLock = loc;
    const msg = loc.msg_id;
    const msgEl = document.getElementById('se-target-msg');
    const fromEl = document.getElementById('se-target-from');
    const toEl = document.getElementById('se-target-to');
    const beatEl = document.getElementById('se-target-beat');
    const posEl = document.getElementById('se-target-beat-pos');
    if (msgEl) msgEl.value = msg;
    if (fromEl) fromEl.value = msg;
    if (toEl) toEl.value = msg;
    if (beatEl && loc.beat_id != null) beatEl.value = loc.beat_id;
    if (posEl && loc.position_in_beat != null) {
        posEl.value = loc.speaker_filter && loc.position_filtered
            ? loc.position_filtered
            : loc.position_in_beat;
    }
    renderTargetBanner(loc);
    if (document.getElementById('se-operator')?.value === 'inject') {
        syncInjectSpeakerFromTarget();
    }
}

function renderTargetBanner(loc) {
    const banner = document.getElementById('se-target-banner');
    if (!banner || !loc) return;
    const { from, to } = getEditTargetSpan();
    const spanMode = from !== to;
    let text = spanMode
        ? `Propose span msgs ${from}–${to}`
        : `Locked on msg ${loc.msg_id} · ${loc.speaker}`;
    if (!spanMode && loc.beat_id != null && loc.position_in_beat != null) {
        text += ` · B${loc.beat_id} "${loc.beat_title}" · ${loc.position_in_beat}/${loc.total_in_beat} in beat`;
        if (loc.speaker_filter && loc.position_filtered != null) {
            text += ` (${loc.position_filtered}/${loc.total_filtered} ${loc.speaker_filter})`;
        }
    }
    if (loc.preview) {
        text += ` — ${loc.preview}`;
    }
    banner.textContent = text;
}

function renderTargetBannerLocal() {
    const banner = document.getElementById('se-target-banner');
    if (!banner) return;
    if (targetLock) {
        renderTargetBanner(targetLock);
        return;
    }
    const { chat } = getContext();
    const { from, to } = getEditTargetSpan();
    const lo = Math.min(from, to);
    const hi = Math.max(from, to);
    const msg = chat[lo];
    const speaker = msg?.name || msg?.ch_name || '?';
    const lastIdx = Math.max(0, chat.length - 1);
    banner.textContent = lo === hi
        ? `Target msg ${lo} · ${speaker}${lo === lastIdx ? ' (last in chat)' : ''} — press Set to resolve beat`
        : `Propose span msgs ${lo}–${hi}`;
}

async function locateTarget({ msg_id, beat, pos }) {
    if (!await ensureEngine()) return;
    setStatus('Resolving target…');
    try {
        await pushChat();
        const params = new URLSearchParams();
        const speaker = speakerFilterValue();
        if (speaker) params.set('speaker', speaker);
        if (msg_id !== undefined) {
            params.set('msg_id', String(msg_id));
        } else {
            params.set('beat', String(beat));
            params.set('pos', String(pos));
        }
        const logPath = (settings().log_path || '').trim();
        if (logPath) params.set('log', logPath);
        const result = await api(`/structure/locate?${params}`);
        applyTargetLocation(result.location);
        setStatus(renderTargetBannerText(result.location));
    } catch (err) {
        setStatus(`Target failed: ${err.message}`);
        const banner = document.getElementById('se-target-banner');
        if (banner) banner.textContent = err.message;
    }
}

function renderTargetBannerText(loc) {
    if (!loc) return '';
    if (loc.beat_id != null) {
        return `Locked msg ${loc.msg_id} · B${loc.beat_id} #${loc.position_in_beat}/${loc.total_in_beat}`;
    }
    return `Locked msg ${loc.msg_id}`;
}

async function stepTargetInBeat(delta) {
    if (!targetLock?.beat_id) {
        setStatus('Set a beat target first (beat + # in beat → Set).');
        return;
    }
    const speaker = speakerFilterValue();
    const useFiltered = Boolean(speaker && targetLock.position_filtered != null);
    const current = useFiltered ? targetLock.position_filtered : targetLock.position_in_beat;
    const total = useFiltered ? targetLock.total_filtered : targetLock.total_in_beat;
    if (!current || !total) return;
    const next = current + delta;
    if (next < 1 || next > total) {
        setStatus(`Already at ${delta > 0 ? 'end' : 'start'} of beat.`);
        return;
    }
    await locateTarget({ beat: targetLock.beat_id, pos: next });
}

async function refreshEditTarget({ resetToLast = false } = {}) {
    const { chat } = getContext();
    const lastIdx = Math.max(0, chat.length - 1);
    if (resetToLast) {
        targetLock = null;
        const msgEl = document.getElementById('se-target-msg');
        const fromEl = document.getElementById('se-target-from');
        const toEl = document.getElementById('se-target-to');
        if (msgEl) msgEl.value = lastIdx;
        if (fromEl) fromEl.value = lastIdx;
        if (toEl) toEl.value = lastIdx;
        await locateTarget({ msg_id: lastIdx });
        return;
    }
    if (!targetLock) {
        const fromEl = document.getElementById('se-target-from');
        const toEl = document.getElementById('se-target-to');
        const msgEl = document.getElementById('se-target-msg');
        if (fromEl && fromEl.value === '') fromEl.value = lastIdx;
        if (toEl && toEl.value === '') toEl.value = lastIdx;
        if (msgEl && msgEl.value === '') msgEl.value = lastIdx;
        renderTargetBannerLocal();
    }
}

const ATTRIBUTION_MODES = ['pov', 'scene_narrator', 'wrong_card', 'mixed', 'unknown'];

/** Voice keys offered in label editors: this chat's cards plus the ensemble. */
function attributionVoices(current = '') {
    const voices = ['ensemble', ...chatCards().map(c => c.toLowerCase())];
    if (current && !voices.includes(current)) voices.push(current);
    return voices;
}

function attributionFilterParams() {
    const params = new URLSearchParams();
    const preset = document.getElementById('se-attr-preset')?.value || 'focus';
    const card = document.getElementById('se-attr-card')?.value || '';
    const confMin = document.getElementById('se-attr-conf-min')?.value;
    const confMax = document.getElementById('se-attr-conf-max')?.value;
    if (preset) params.set('preset', preset);
    if (card) params.set('card', card);
    if (confMin !== '' && confMin != null) params.set('label_confidence_min', confMin);
    if (confMax !== '' && confMax != null) params.set('label_confidence_max', confMax);
    return params;
}

function attributionFilterBody() {
    const params = attributionFilterParams();
    const body = {};
    for (const [key, val] of params.entries()) {
        if (key.startsWith('label_confidence_')) {
            body[key] = parseFloat(val);
        } else {
            body[key] = val;
        }
    }
    return body;
}

function renderAttributionReview(review) {
    const body = resultsBody('label');
    if (!body) return;
    body.innerHTML = '';

    const total = review.total_needs_human ?? review.count;
    const shown = review.count ?? 0;
    const filterHint = review.filters?.preset
        ? ` · preset ${review.filters.preset}`
        : '';

    const header = document.createElement('div');
    header.className = 'se-attr-header';
    header.innerHTML = `
        <h4>Voice attribution · ${shown} shown / ${total} need review${filterHint}</h4>
        <div class="se-attr-bulk">
            <span>Bulk (shown rows):</span>
            <label>voice
                <select id="se-attr-bulk-voice" class="text_pole">
                    ${attributionVoices().map(v => `<option value="${v}">${v}</option>`).join('')}
                </select>
            </label>
            <label>mode
                <select id="se-attr-bulk-mode" class="text_pole">
                    ${ATTRIBUTION_MODES.map(m => `<option value="${m}">${m}</option>`).join('')}
                </select>
            </label>
            <button type="button" class="menu_button se-primary" id="se-attr-bulk-accept">Accept all shown</button>
        </div>
    `;
    body.appendChild(header);
    header.querySelector('#se-attr-bulk-accept')?.addEventListener('click', () => {
        bulkAcceptAttribution(review);
    });
    if (!review.items || review.items.length === 0) {
        const done = document.createElement('p');
        done.textContent = 'All labels reviewed — nothing needs human attention.';
        body.appendChild(done);
        return;
    }

    for (const item of review.items) {
        const lab = item.label || {};
        const voice = lab.voice || (chatCards()[0] || 'ensemble').toLowerCase();
        const mode = lab.mode || 'pov';
        const block = document.createElement('div');
        block.className = 'se-edit se-attr-item';
        block.dataset.msgId = String(item.msg_id);

        const modeOpts = ATTRIBUTION_MODES.map(m =>
            `<option value="${m}"${m === mode ? ' selected' : ''}>${m}</option>`,
        ).join('');
        const voiceOpts = attributionVoices(voice).map(v =>
            `<option value="${v}"${v === voice ? ' selected' : ''}>${v}</option>`,
        ).join('');

        block.innerHTML = `
            <h4>msg ${item.msg_id} · card <code>${escapeHtml(item.card)}</code>
                ${lab.source ? `· ${escapeHtml(lab.source)} @ ${(lab.confidence ?? 0).toFixed(2)}` : ''}</h4>
            <div class="se-attr-controls">
                <label>voice <select class="text_pole se-attr-voice">${voiceOpts}</select></label>
                <label>mode <select class="text_pole se-attr-mode">${modeOpts}</select></label>
                <button type="button" class="menu_button se-attr-goto">Go to msg</button>
                <button type="button" class="menu_button se-attr-save">Accept</button>
            </div>
            <div class="se-before se-attr-message">${escapeHtml(item.message || '')}</div>
        `;

        block.querySelector('.se-attr-goto').addEventListener('click', () => {
            scrollChatToMsg(item.msg_id);
        });
        block.querySelector('.se-attr-save').addEventListener('click', async () => {
            await saveAttributionLabel(block, item.msg_id);
        });
        body.appendChild(block);
    }
}

function scrollChatToMsg(msgId) {
    const idx = Number(msgId);
    const { chat } = getContext();
    if (!Number.isFinite(idx) || idx < 0 || (chat && idx >= chat.length)) {
        setStatus(`Invalid message index: ${msgId}`);
        return;
    }

    (async () => {
        const firstDisplayed = getFirstDisplayedMessageId();
        if (Number.isFinite(firstDisplayed) && idx < firstDisplayed) {
            await showMoreMessages(firstDisplayed - idx);
            await delay(debounce_timeout.quick);
        }

        const chatContainer = document.getElementById('chat');
        const messageElement = document.querySelector(`#chat .mes[mesid="${idx}"]`);

        if (messageElement instanceof HTMLElement && chatContainer instanceof HTMLElement) {
            const elementRect = messageElement.getBoundingClientRect();
            const containerRect = chatContainer.getBoundingClientRect();
            const scrollPosition = elementRect.top - containerRect.top + chatContainer.scrollTop;
            chatContainer.scrollTo({ top: scrollPosition, behavior: 'smooth' });
            flashHighlight($(messageElement), 2000);
            return;
        }

        const ctx = getContext();
        if (typeof ctx.goToMessage === 'function') {
            ctx.goToMessage(idx);
            return;
        }
        if (typeof ctx.scrollChatToMessage === 'function') {
            ctx.scrollChatToMessage(idx);
            return;
        }
        setStatus(`Message ${idx} not visible — try loading more chat history.`);
    })();
}

async function bulkAcceptAttribution(review) {
    const voice = document.getElementById('se-attr-bulk-voice')?.value;
    const mode = document.getElementById('se-attr-bulk-mode')?.value;
    if (!voice || !mode) return;
    const count = review?.items?.length ?? 0;
    if (!count) {
        setStatus('Nothing to accept in the current filter.');
        return;
    }
    if (!confirm(`Accept ${count} label(s) as ${voice}/${mode}?`)) return;
    setStatus(`Accepting ${count} labels…`);
    try {
        const result = await api('/attribution/bulk-set', {
            method: 'POST',
            body: JSON.stringify(apiBody({
                voice,
                mode,
                reviewed: true,
                ...attributionFilterBody(),
            })),
        });
        await loadAttributionReview();
        setStatus(
            `Bulk accept: ${result.applied} saved, ${result.skipped} skipped `
            + `(${result.needs_human_remaining ?? '?'} need review)`,
        );
    } catch (err) {
        setStatus(`Bulk accept failed: ${err.message}`);
    }
}

async function saveAttributionLabel(block, msgId) {
    const voice = block.querySelector('.se-attr-voice')?.value;
    const mode = block.querySelector('.se-attr-mode')?.value;
    setStatus(`Saving label for msg ${msgId}…`);
    try {
        const result = await api('/attribution/set', {
            method: 'POST',
            body: JSON.stringify(apiBody({
                msg_id: msgId,
                voice,
                mode,
                reviewed: true,
            })),
        });
        block.classList.add('se-attr-saved');
        setStatus(`Saved msg ${msgId} → ${voice}/${mode} (${result.needs_human_remaining} left)`);
        if (result.needs_human_remaining === 0) {
            await loadAttributionReview();
        } else {
            block.remove();
        }
    } catch (err) {
        setStatus(`Save failed: ${err.message}`);
    }
}

async function loadAttributionReview() {
    if (!await ensureEngine()) return false;
    setStatus('Syncing chat → engine log…');
    setResultsPlaceholder('label', '<p class="se-loading">Loading attribution queue…</p>');
    try {
        await pushChat();
        setStatus('Loading attribution queue…');
        const qs = attributionFilterParams().toString();
        const review = await api(`/attribution/review${qs ? `?${qs}` : ''}`);
        renderAttributionReview(review);
        const total = review.total_needs_human ?? review.count;
        setStatus(`${review.count} shown / ${total} need review`);
        return true;
    } catch (err) {
        setStatus(`Attribution load failed: ${err.message}`);
        return false;
    }
}

async function onAttribution() {
    openDrawer('label');
    await loadAttributionReview();
}

async function onAttributionReconcile() {
    if (!await ensureEngine()) return;
    setStatus('Reconciling labels (auto → propose → auto-accept)…');
    try {
        await pushChat();
        const result = await api('/attribution/reconcile', {
            method: 'POST',
            body: JSON.stringify(apiBody({
                skip_propose: false,
                ...attributionFilterBody(),
            })),
            timeoutMs: LLM_TIMEOUT_MS,
        });
        renderAttributionReview(result.review);
        const s = result.stats || {};
        const rev = result.review || {};
        setStatus(
            `Reconcile done — accepted ${s.auto_accepted || 0}, `
            + `${rev.count ?? result.needs_human} shown in queue`,
        );
    } catch (err) {
        setStatus(`Reconcile failed: ${err.message}`);
    }
}

function renderEdits(editSet) {
    const body = resultsBody('edit');
    if (!body) return;
    body.innerHTML = '';

    if (!editSet || !editSet.edits || editSet.edits.length === 0) {
        body.innerHTML = '<p class="se-empty-hint">No pending proposal. The model may have judged the target already correct, or you need to Propose again.</p>';
        return;
    }

    const header = document.createElement('div');
    header.className = 'se-proposal-header';
    header.innerHTML = `<h4>${editSet.edits.length} pending edit(s) — review below, then Commit or Discard</h4>`;
    body.appendChild(header);

    for (const e of editSet.edits) {
        const block = document.createElement('div');
        block.className = 'se-edit se-proposal';
        const flags = (e.flags || []).map(f => `<div class="se-flag">⚠ ${escapeHtml(f)}</div>`).join('');
        const kind = e.kind === 'inject' ? `inject after ${e.msg_id}` : `msg ${e.msg_id}`;
        const attrHint = (e.kind === 'inject' && e.attribution_voice)
            ? ` · sidecar ${escapeHtml(e.attribution_voice)}/${escapeHtml(e.attribution_mode || 'pov')}`
            : '';
        block.innerHTML = `
            <h4>${kind} · ${escapeHtml(e.speaker)}${attrHint}</h4>
            ${flags}
            ${e.before ? `<div class="se-diff-label">Before</div><div class="se-before">${escapeHtml(e.before)}</div>` : ''}
            <div class="se-diff-label se-diff-label-after">After (proposed)</div>
            <div class="se-after">${escapeHtml(e.after)}</div>
        `;
        body.appendChild(block);
    }
    body.scrollTop = 0;
}

function renderProofreadReport(summary) {
    const body = resultsBody('audit');
    if (!body) return;
    body.innerHTML = '';
    const header = document.createElement('div');
    header.className = 'se-sweep-summary';
    header.innerHTML = `
        <h4>Proofread · pending edits</h4>
        <p>${summary.clean ?? 0} clean,
           ${summary.supported ?? 0} supported claim(s),
           ${summary.plausible ?? 0} plausibly-extend,
           ${summary.contradicts ?? 0} contradiction(s)</p>
    `;
    body.appendChild(header);

    const reports = summary.reports || [];
    if (!reports.length) {
        const p = document.createElement('p');
        p.textContent = 'No pending edits to proofread.';
        body.appendChild(p);
        return;
    }

    for (const r of reports) {
        const block = document.createElement('div');
        const verdict = (r.verdict || 'unknown').toLowerCase();
        block.className = `se-edit se-proofread-${verdict}`;
        const claimList = (items, label) => {
            if (!items?.length) return '';
            const rows = items.map(c =>
                `<li><strong>${escapeHtml(c.claim || c)}</strong>`
                + (c.reason ? ` — ${escapeHtml(c.reason)}` : '')
                + '</li>',
            ).join('');
            return `<p>${label}</p><ul>${rows}</ul>`;
        };
        block.innerHTML = `
            <h4>${escapeHtml(r.edit_kind || 'edit')} · msg ${r.edit_msg_id}
                · ${escapeHtml(r.edit_speaker)} · ${escapeHtml(r.verdict || '')}</h4>
            ${claimList(r.contradicts, 'Contradicts')}
            ${claimList(r.plausible, 'Plausibly extends')}
            ${claimList(r.supported, 'Supported')}
        `;
        body.appendChild(block);
    }
    body.scrollTop = 0;
}

function renderCanonCheckReport(result) {
    const body = resultsBody('audit');
    if (!body) return;
    body.innerHTML = '';

    if (result.empty) {
        body.innerHTML = '<p>No pending edits — propose a change first, then run canon check.</p>';
        return;
    }

    const results = result.results || [];
    const bad = results.filter(x =>
        x.verdict === 'out_of_character' || x.verdict === 'concerns',
    );
    const header = document.createElement('div');
    header.className = 'se-sweep-summary';
    header.innerHTML = `
        <h4>Canon check · in-character audit</h4>
        <p>${results.length} edit(s) checked —
           ${bad.length} concern(s),
           ${results.length - bad.length} clear or skipped</p>
        <p class="se-tab-blurb">Compares each pending rewrite against the character bible
            (voice, constraints, register). Run before Commit when you changed dialogue or POV.</p>
    `;
    body.appendChild(header);

    for (const r of results) {
        const block = document.createElement('div');
        const verdict = (r.verdict || 'unknown').toLowerCase();
        const icon = verdict === 'in_character' || verdict === 'clear' ? '✓'
            : verdict === 'skipped' ? '·' : '⚠';
        block.className = `se-edit se-canon-${verdict}`;
        const issues = (r.specific_issues || []).map(i =>
            `<li>${escapeHtml(typeof i === 'string' ? i : JSON.stringify(i))}</li>`,
        ).join('');
        block.innerHTML = `
            <h4>${icon} msg ${r.msg_id} · ${escapeHtml(r.speaker)} · ${escapeHtml(r.verdict)}</h4>
            ${r.reason ? `<div class="se-reason">${escapeHtml(r.reason)}</div>` : ''}
            ${issues ? `<ul>${issues}</ul>` : ''}
        `;
        body.appendChild(block);
    }
    body.scrollTop = 0;
}

function renderSweepReport(report) {
    const body = resultsBody('audit');
    if (!body) return;
    body.innerHTML = '';
    const header = document.createElement('div');
    header.className = 'se-sweep-summary';
    header.innerHTML = `
        <h4>Sweep · ${escapeHtml(report.verdict)}</h4>
        <p>${report.clean_count} clean,
           ${(report.hits || []).filter(h => h.verdict === 'REVIEW').length} review,
           ${(report.hits || []).filter(h => h.verdict === 'BREAK').length} break
           (from msg ${report.sweep_from})</p>
    `;
    body.appendChild(header);

    const hits = report.hits || [];
    if (!hits.length) {
        const p = document.createElement('p');
        p.textContent = 'All downstream candidates read cleanly.';
        body.appendChild(p);
        return;
    }

    for (const h of hits) {
        const block = document.createElement('div');
        block.className = `se-edit se-sweep-${(h.verdict || 'review').toLowerCase()}`;
        const icon = h.verdict === 'BREAK' ? '✗' : '⚠';
        block.innerHTML = `
            <h4>${icon} msg ${h.msg_id} · ${escapeHtml(h.speaker)} · ${h.verdict}</h4>
            <div class="se-before">${escapeHtml(h.excerpt)}</div>
            <div class="se-reason">${escapeHtml(h.reason)}</div>
        `;
        body.appendChild(block);
    }
    body.scrollTop = 0;
}

async function refreshEditTabProposal() {
    try {
        const r = await api('/edits');
        if (r.edit_set?.edits?.length) {
            renderEdits(r.edit_set);
        }
    } catch {
        /* quiet — engine may be offline */
    }
}

async function ensureEngine() {
    try {
        await api('/health');
        return true;
    } catch {
        setStatus(`Engine not reachable at ${apiBase()} — run: python3 -m story_editor serve`);
        return false;
    }
}

async function pushChat() {
    const { chat } = getContext();
    return api('/sync/import', {
        method: 'POST',
        body: JSON.stringify(apiBody({ messages: chat })),
    });
}

async function pullChat(options = {}) {
    const exported = await api('/sync/export');
    const { chat, reloadCurrentChat } = getContext();
    const incoming = exported.messages || [];

    // Full replace so injects and prepends land at the correct index.
    chat.splice(0, chat.length, ...incoming);
    await saveChatConditional();
    await reloadCurrentChat();

    const focus = options.focusMsgId;
    if (focus != null && Number.isFinite(Number(focus))) {
        await delay(debounce_timeout.quick);
        scrollChatToMsg(Number(focus));
    }
    return { message_count: incoming.length };
}

async function onPull() {
    if (!await ensureEngine()) return;
    setStatus('Loading engine log into this chat…');
    try {
        const result = await pullChat();
        setStatus(`Loaded ${result.message_count} turns from the engine.`);
        toastr.success('ProseWright: chat loaded from engine');
    } catch (exc) {
        setStatus(String(exc.message || exc));
    }
}

function focusMsgIdFromPending(st) {
    const edits = st.edit_set?.edits;
    if (!edits?.length) {
        return null;
    }
    const inject = edits.find((e) => e.kind === 'inject');
    if (inject) {
        return inject.msg_id + 1;
    }
    return edits[0].msg_id;
}

async function runOperator(operator, note, opts = {}) {
    openDrawer('edit');

    if (!await ensureEngine()) {
        return 'Engine offline. Start the ProseWright API first.';
    }

    await pushChat();

    const span = {
        speaker: opts.speaker,
        from: opts.from,
        to: opts.to,
        beat: opts.beat,
        scene: opts.scene,
    };
    if (span.from === undefined && span.to === undefined && !span.beat && span.scene === undefined) {
        Object.assign(span, defaultSpanOpts());
    }

    if (operator === 'inject') {
        if (!opts.speaker) {
            setStatus('Inject requires a card (speaker).');
            return 'Pick a card in the inject panel.';
        }
        setStatus('Generating injection…');
        const payload = {
            note,
            speaker: opts.speaker,
            after: opts.after ?? span.from,
        };
        if (opts.voice) payload.voice = opts.voice;
        if (opts.attribution_mode) payload.mode = opts.attribution_mode;
        const result = await proposeWithStream('/inject', payload, 'Generating injection…');
        renderEdits(result.edit_set);
        setStatus('Injection proposed — review and commit or discard.');
        return 'Injection proposed.';
    }

    if (operator.startsWith('retune-')) {
        const mode = operator === 'retune-expand' ? 'expand' : 'tighten';
        setStatus(`Retuning (${mode})…`);
        const result = await proposeWithStream(
            '/retune',
            { note, mode, ...span },
            `Retuning (${mode})…`,
        );
        renderEdits(result.edit_set);
        const n = result.edit_set?.edits?.length ?? 0;
        setStatus(result.advisory
            ? 'Advisory: model made no retune changes.'
            : `Proposed ${n} retune edit(s)`);
        return n ? `Proposed ${n} retune edit(s).` : 'No retune changes proposed.';
    }

    setStatus('Proposing restyle…');
    const result = await proposeWithStream('/restyle', { note, ...span }, 'Proposing restyle…');
    renderEdits(result.edit_set);
    const n = result.edit_set?.edits?.length ?? 0;
    setStatus(result.advisory
        ? 'Advisory: model made no changes.'
        : `Proposed ${n} edit(s)`);
    return n ? `Proposed ${n} edit(s). Review in the ProseWright drawer.` : 'No changes proposed.';
}

async function onToolbarPropose() {
    const operator = document.getElementById('se-operator')?.value || 'restyle';
    openDrawer('edit');
    const { from, to } = getEditTargetSpan();

    if (operator === 'inject') {
        const inj = getInjectParams();
        if (!inj.note) {
            setStatus('Enter a gist for the injection.');
            return;
        }
        await runOperator('inject', inj.note, {
            speaker: inj.speaker,
            voice: inj.voice,
            from,
            to,
            after: from,
        });
        return;
    }

    const note = (document.getElementById('se-note-input')?.value || '').trim();
    if (!note) {
        setStatus('Enter a direction in the toolbar field.');
        return;
    }
    const { chat } = getContext();
    const targetMsg = chat[from];
    const lastSpeaker = targetMsg?.name || targetMsg?.ch_name
        || chat[chat.length - 1]?.name || chat[chat.length - 1]?.ch_name;
    await runOperator(operator, note, {
        speaker: undefined,
        from,
        to,
        after: from,
    });
}

async function onDiscard() {
    const st = await api('/status');
    await api('/edits/discard', { method: 'POST', body: '{}' });
    renderEdits(null);
    setStatus('Discarded pending proposal.');
}

async function onCommit() {
    setStatus('Committing…');
    if (!await ensureEngine()) return;
    const st = await api('/status');
    let focusMsgId = focusMsgIdFromPending(st);
    if (st.pending_kind === 'edits') {
        await api('/edits/commit', {
            method: 'POST',
            body: JSON.stringify(apiBody({})),
        });
    } else {
        setStatus('No pending proposal — loading the engine log into this chat…');
        try {
            const result = await pullChat();
            setStatus(`Loaded ${result.message_count} turns from the engine.`);
            toastr.success('ProseWright: chat loaded from engine');
        } catch (exc) {
            setStatus(String(exc.message || exc));
        }
        return;
    }
    await pullChat({ focusMsgId });
    renderEdits(null);
    setStatus('Committed. Chat updated.');
    toastr.success('ProseWright: committed');
}

async function onUndo() {
    setStatus('Restoring last backup…');
    await api('/edits/undo', { method: 'POST', body: '{}' });
    await pullChat();
    setStatus('Undo complete — log restored from backup.');
    toastr.info('ProseWright: undo applied');
}

async function onSweep() {
    openDrawer('audit');
    if (!await ensureEngine()) return;
    setStatus('Running propagation sweep…');
    try {
        const result = await api('/sweep/last', {
            method: 'POST',
            body: JSON.stringify(apiBody({})),
            timeoutMs: LLM_TIMEOUT_MS,
        });
        renderSweepReport(result.report);
        setStatus(`Sweep: ${result.report.verdict}`);
    } catch (exc) {
        setStatus(String(exc.message || exc));
    }
}

async function onProofread() {
    openDrawer('audit');
    if (!await ensureEngine()) return;
    setStatus('Running proofread…');
    const r = await api('/proofread', {
        method: 'POST',
        body: JSON.stringify(apiBody({})),
        timeoutMs: LLM_TIMEOUT_MS,
    });
    renderProofreadReport(r);
    setStatus(`Proofread: ${r.clean} clean, ${r.contradicts} contradiction(s), ${r.plausible} plausibly-extend`);
}

async function onCanonCheck() {
    openDrawer('audit');
    if (!await ensureEngine()) return;
    setStatus('Running canon check…');
    const r = await api('/canon/check', {
        method: 'POST',
        body: JSON.stringify(apiBody({})),
        timeoutMs: LLM_TIMEOUT_MS,
    });
    renderCanonCheckReport(r);
    if (r.empty) {
        setStatus('Canon check: nothing to audit (empty edit set).');
        return;
    }
    const bad = (r.results || []).filter(x => x.verdict === 'out_of_character' || x.verdict === 'concerns');
    setStatus(bad.length
        ? `Canon check: ${bad.length} concern(s) — review before commit`
        : 'Canon check: all clear');
}

function renderHistoryDiff(entry) {
    const body = resultsBody('history');
    if (!body) return;
    body.innerHTML = '';
    const back = document.createElement('button');
    back.type = 'button';
    back.className = 'menu_button se-history-back';
    back.textContent = '← Back to list';
    back.addEventListener('click', () => onHistory());
    body.appendChild(back);

    const edits = (entry.edits || []).map(ed => ({
        msg_id: ed.msg_id,
        speaker: ed.speaker,
        kind: ed.kind,
        before: ed.before,
        after: ed.after,
        flags: ed.flags || [],
    }));
    const header = document.createElement('div');
    header.className = 'se-proposal-header';
    header.innerHTML = `<h4>History #${entry.id} · ${escapeHtml(entry.operator)} · ${escapeHtml(entry.note || '')}</h4>`;
    body.appendChild(header);
    for (const e of edits) {
        const block = document.createElement('div');
        block.className = 'se-edit se-proposal';
        const kind = e.kind === 'inject' ? `inject after ${e.msg_id}` : `msg ${e.msg_id}`;
        block.innerHTML = `
            <h4>${kind} · ${escapeHtml(e.speaker)}</h4>
            ${e.before ? `<div class="se-diff-label">Before</div><div class="se-before">${escapeHtml(e.before)}</div>` : ''}
            <div class="se-diff-label se-diff-label-after">After (committed)</div>
            <div class="se-after">${escapeHtml(e.after)}</div>
        `;
        body.appendChild(block);
    }
    body.scrollTop = 0;
}

async function onHistory() {
    openDrawer('history');
    if (!await ensureEngine()) return;
    setStatus('Loading history…');
    const r = await api('/history?limit=10');
    const body = resultsBody('history');
    if (!body) return;
    body.innerHTML = '';
    const entries = r.entries || [];
    if (!entries.length) {
        body.innerHTML = '<p>No edit history yet.</p>';
        setStatus('History empty.');
        return;
    }
    for (const e of entries) {
        const row = document.createElement('div');
        row.className = 'se-edit';
        const span = e.changed_from != null ? `msgs ${e.changed_from}–${e.changed_to}` : '';
        row.innerHTML = `<h4>#${e.id} · ${e.event} · ${e.operator} · ${span}</h4>
            <div class="se-before">${escapeHtml(e.note || '')}</div>
            <button type="button" class="menu_button se-show-entry" data-id="${e.id}">Show diff</button>`;
        body.appendChild(row);
    }
    body.querySelectorAll('.se-show-entry').forEach(btn => {
        btn.addEventListener('click', async () => {
            const entry = await api(`/history/${btn.dataset.id}`);
            if (entry.event === 'commit' && entry.edits?.length) {
                renderHistoryDiff(entry);
            }
            setStatus(`History #${entry.id} — ${entry.operator}: ${entry.note || ''}`);
        });
    });
    setStatus(`${entries.length} recent history entries.`);
}

function registerSlashCommands() {
    const spanArgs = [
        SlashCommandNamedArgument.fromProps({
            name: 'speaker',
            description: 'Limit to this speaker',
            typeList: [ARGUMENT_TYPE.STRING],
            isRequired: false,
        }),
        SlashCommandNamedArgument.fromProps({
            name: 'from',
            description: 'Start msg index',
            typeList: [ARGUMENT_TYPE.NUMBER],
            isRequired: false,
        }),
        SlashCommandNamedArgument.fromProps({
            name: 'to',
            description: 'End msg index',
            typeList: [ARGUMENT_TYPE.NUMBER],
            isRequired: false,
        }),
    ];

    SlashCommandParser.addCommandObject(SlashCommand.fromProps({
        name: 'editnote',
        callback: async (args, value) => {
            const note = (value || '').trim();
            if (!note) return 'Usage: /editnote your direction';
            return runOperator('restyle', note, {
                speaker: args?.speaker,
                from: args?.from !== undefined ? Number(args.from) : undefined,
                to: args?.to !== undefined ? Number(args.to) : undefined,
            });
        },
        returns: 'confirmation',
        helpString: 'ProseWright restyle from a plain-language note.',
        unnamedArgumentList: [
            SlashCommandArgument.fromProps({
                description: 'Editing direction',
                typeList: [ARGUMENT_TYPE.STRING],
                isRequired: true,
            }),
        ],
        namedArgumentList: spanArgs,
    }));

    SlashCommandParser.addCommandObject(SlashCommand.fromProps({
        name: 'retunenote',
        callback: async (args, value) => {
            const note = (value || '').trim();
            const mode = (args?.mode || 'tighten').toLowerCase();
            const op = mode === 'expand' ? 'retune-expand' : 'retune-tighten';
            return runOperator(op, note || undefined, {
                speaker: args?.speaker,
                from: args?.from !== undefined ? Number(args.from) : undefined,
                to: args?.to !== undefined ? Number(args.to) : undefined,
            });
        },
        returns: 'confirmation',
        helpString: 'Retune length/density (tighten or expand) on a message span.',
        unnamedArgumentList: [
            SlashCommandArgument.fromProps({
                description: 'Optional note',
                typeList: [ARGUMENT_TYPE.STRING],
                isRequired: false,
            }),
        ],
        namedArgumentList: [
            SlashCommandNamedArgument.fromProps({
                name: 'mode',
                description: 'tighten or expand',
                typeList: [ARGUMENT_TYPE.STRING],
                isRequired: false,
            }),
            ...spanArgs,
        ],
    }));

    SlashCommandParser.addCommandObject(SlashCommand.fromProps({
        name: 'injectnote',
        callback: async (args, value) => {
            const note = (value || '').trim();
            if (!note) return 'Usage: /injectnote speaker=Name your direction';
            if (!args?.speaker) return 'injectnote requires speaker=Name';
            return runOperator('inject', note, {
                speaker: args.speaker,
                after: args?.after !== undefined ? Number(args.after) : undefined,
                from: args?.from !== undefined ? Number(args.from) : undefined,
            });
        },
        returns: 'confirmation',
        helpString: 'Insert new prose after a message (kind=inject).',
        unnamedArgumentList: [
            SlashCommandArgument.fromProps({
                description: 'What to insert',
                typeList: [ARGUMENT_TYPE.STRING],
                isRequired: true,
            }),
        ],
        namedArgumentList: [
            SlashCommandNamedArgument.fromProps({
                name: 'speaker',
                description: 'Speaker name for the new passage',
                typeList: [ARGUMENT_TYPE.STRING],
                isRequired: true,
            }),
            SlashCommandNamedArgument.fromProps({
                name: 'after',
                description: 'Insert after this msg id',
                typeList: [ARGUMENT_TYPE.NUMBER],
                isRequired: false,
            }),
            ...spanArgs,
        ],
    }));

    SlashCommandParser.addCommandObject(SlashCommand.fromProps({
        name: 'attribution',
        callback: async () => {
            await onAttribution();
            return 'Voice attribution queue opened in ProseWright drawer.';
        },
        returns: 'confirmation',
        helpString: 'Open voice-label review queue (card ≠ character voiced).',
        unnamedArgumentList: [],
        namedArgumentList: [],
    }));
}

async function initSettings() {
    let container = document.getElementById('story_editor_settings_container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'story_editor_settings_container';
        container.className = 'extension_container';
        $('#extensions_settings').append(container);
    }

    let html;
    try {
        html = await renderExtensionTemplateAsync(EXT_FOLDER, 'settings');
    } catch {
        html = await renderExtensionTemplateAsync('sillytavern', 'settings');
    }
    container.innerHTML = html;

    const s = settings();
    $('#se_settings_api_url').val(s.api_url || DEFAULT_API);
    $('#se_settings_log_path').val(s.log_path || '');

    $('#se_settings_api_url').on('input', function () {
        s.api_url = String($(this).val()).trim() || DEFAULT_API;
        saveSettingsDebounced();
    });
    $('#se_settings_log_path').on('input', function () {
        s.log_path = String($(this).val()).trim();
        saveSettingsDebounced();
    });
    $('#se_settings_test').on('click', async () => {
        const status = $('#se_settings_status');
        status.text('');
        try {
            const h = await api('/health');
            status.text(`Extension ${EXT_VERSION} · API ${h.version}`);
        } catch (exc) {
            status.text(String(exc.message || exc));
        }
    });
    $('#se_settings_open').on('click', () => openDrawer('edit'));
}

jQuery(async () => {
    settings();
    ensureDrawer();
    registerSlashCommands();
    await initSettings();
    console.info('[ProseWright] extension loaded — API:', apiBase());
});
