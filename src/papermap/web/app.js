/* PaperMap player - vanilla JS, no dependencies.
 * Reads the experience JSON embedded in the page and renders three synchronized
 * views (high-level, deep-dive, knowledge graph), a narrated beat player, typed
 * diagrams with per-beat focus, and grounded Q&A (when served by `papermap serve`).
 */
(() => {
  'use strict';

  const DATA = JSON.parse(document.getElementById('papermap-data').textContent);
  const VIEW_LABEL = { high: 'High-level', deep: 'Deep dive', graph: 'Knowledge graph' };
  const ROLE_LABEL = {
    overview: 'Overview', motivation: 'Motivation', background: 'Background', related_work: 'Related work',
    method: 'Method', theory: 'Theory', experiments: 'Experiments', results: 'Results', analysis: 'Analysis',
    discussion: 'Discussion', limitations: 'Limitations', conclusion: 'Conclusion', appendix: 'Appendix', other: 'Section',
  };
  const SECTIONS = DATA.sections;
  const SEC_INDEX = Object.fromEntries(SECTIONS.map((s, i) => [s.id, i]));
  const NODES = DATA.graph.nodes || [];
  const EDGES = DATA.graph.edges || [];
  const NODE_BY_ID = Object.fromEntries(NODES.map((n) => [n.id, n]));
  // Diagram node kinds and graph node types -> categorical slot (0 = neutral). Text never wears these colors.
  const KIND_SLOT = { input: 3, process: 1, model: 7, data: 4, output: 6, metric: 2, concept: 5, decision: 8 };
  const TYPE_SLOT = { this_work: 1, method: 7, model: 2, dataset: 4, task: 3, metric: 8, concept: 5, prior_work: 6, tool: 0 };
  const TYPE_LABEL = {
    this_work: 'This work', method: 'Method', model: 'Model', dataset: 'Dataset', task: 'Task',
    metric: 'Metric', concept: 'Concept', prior_work: 'Prior work', tool: 'Tool',
  };
  const slotColor = (slot) => (slot ? `var(--series-${slot})` : 'var(--text-muted)');
  const REL_LABEL = (r) => r.replace(/_/g, ' ');

  // ------------------------------------------------------------------ utils
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'html') el.innerHTML = v;
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? '' : v);
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid == null || kid === false) continue;
      el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }
  const SVGNS = 'http://www.w3.org/2000/svg';
  function s(tag, attrs, ...kids) {
    const el = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v);
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid == null || kid === false) continue;
      el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }
  const esc = (t) => String(t).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
  const SUP = { '-': '⁻', 0: '⁰', 1: '¹', 2: '²', 3: '³', 4: '⁴', 5: '⁵', 6: '⁶', 7: '⁷', 8: '⁸', 9: '⁹' };
  const fmtNum = (v) => {
    if (v == null || Number.isNaN(v)) return '–';
    const a = Math.abs(v);
    if (a >= 1e6 || (a > 0 && a < 1e-3)) {
      const [m, e] = v.toExponential(2).split('e');
      const mant = String(+m);
      const exp = String(+e).split('').map((c) => SUP[c] ?? c).join('');
      return mant === '1' ? `10${exp}` : `${mant}×10${exp}`;
    }
    const d = a >= 100 ? 1 : a >= 10 ? 2 : 3;
    return String(+v.toFixed(d));
  };
  const ICONS = {
    play: '<path d="M7.5 4.8v14.4c0 .8.9 1.3 1.6.9l11.3-7.2c.6-.4.6-1.4 0-1.8L9.1 3.9c-.7-.4-1.6.1-1.6.9z" fill="currentColor"/>',
    pause: '<rect x="6" y="4.5" width="4.2" height="15" rx="1.2" fill="currentColor"/><rect x="13.8" y="4.5" width="4.2" height="15" rx="1.2" fill="currentColor"/>',
    prev: '<path d="M18.5 5.6v12.8c0 .7-.8 1.1-1.4.7L8 12.7a.8.8 0 0 1 0-1.4l9.1-6.4c.6-.4 1.4 0 1.4.7z" fill="currentColor"/><rect x="5" y="5" width="2.6" height="14" rx="1" fill="currentColor"/>',
    next: '<path d="M5.5 5.6v12.8c0 .7.8 1.1 1.4.7l9.1-6.4a.8.8 0 0 0 0-1.4L6.9 4.9c-.6-.4-1.4 0-1.4.7z" fill="currentColor"/><rect x="16.4" y="5" width="2.6" height="14" rx="1" fill="currentColor"/>',
    volume: '<path d="M4 9.2v5.6c0 .4.3.7.7.7H8l4.4 3.6c.5.4 1.1 0 1.1-.6V5.5c0-.6-.7-1-1.1-.6L8 8.5H4.7c-.4 0-.7.3-.7.7z" fill="currentColor"/><path d="M16.5 8.5a5 5 0 0 1 0 7M19 6a8.5 8.5 0 0 1 0 12" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round"/>',
    mute: '<path d="M4 9.2v5.6c0 .4.3.7.7.7H8l4.4 3.6c.5.4 1.1 0 1.1-.6V5.5c0-.6-.7-1-1.1-.6L8 8.5H4.7c-.4 0-.7.3-.7.7z" fill="currentColor"/><path d="M17 9.5l5 5M22 9.5l-5 5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
    moon: '<path d="M20 14.6A8.2 8.2 0 0 1 9.4 4a8.2 8.2 0 1 0 10.6 10.6z" fill="currentColor"/>',
    sun: '<circle cx="12" cy="12" r="4.2" fill="currentColor"/><path d="M12 2.5v2.2M12 19.3v2.2M4.6 4.6l1.6 1.6M17.8 17.8l1.6 1.6M2.5 12h2.2M19.3 12h2.2M4.6 19.4l1.6-1.6M17.8 6.2l1.6-1.6" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
    sliders: '<path d="M4 7h9M19 7h1M4 17h3M13 17h7" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><circle cx="16" cy="7" r="2.4" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="10" cy="17" r="2.4" fill="none" stroke="currentColor" stroke-width="2"/>',
    help: '<circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/><path d="M9.6 9.4a2.5 2.5 0 1 1 3.4 2.4c-.6.3-1 .8-1 1.5v.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="17" r="1.2" fill="currentColor"/>',
    menu: '<path d="M4 7h16M4 12h16M4 17h16" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
    panel: '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5" fill="none" stroke="currentColor" stroke-width="2"/><path d="M14.5 4.5v15" stroke="currentColor" stroke-width="2"/>',
    lock: '<rect x="5" y="10.5" width="14" height="10" rx="2.5" fill="currentColor"/><path d="M8 10.5V8a4 4 0 0 1 8 0v2.5" fill="none" stroke="currentColor" stroke-width="2"/>',
    chat: '<path d="M5 5h14a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-8l-5 4v-4H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>',
    wave: '<path d="M4 12h1.5M8 8.5v7M12 5v14M16 8.5v7M19.5 11v2" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/>',
    close: '<path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
    send: '<path d="M4.5 11.4L19 4.6c.6-.3 1.2.3.9.9l-6.8 14.5c-.3.6-1.2.6-1.4-.1l-1.6-5-5-1.6c-.7-.2-.7-1.1-.1-1.4z" fill="currentColor"/>',
    fit: '<path d="M4 9V5h4M20 9V5h-4M4 15v4h4M20 15v4h-4" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round"/>',
    graph: '<circle cx="6" cy="7" r="2.5" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="18" cy="6" r="2.5" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="13" cy="17.5" r="2.5" fill="none" stroke="currentColor" stroke-width="2"/><path d="M8.4 7.6l7.2-1M7.3 9.2l4.5 6.2M17 8.3l-3 6.8" stroke="currentColor" stroke-width="1.8"/>',
  };
  function icon(name) {
    const t = document.createElement('template');
    t.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">${ICONS[name]}</svg>`;
    return t.content.firstChild;
  }

  const store = (() => {
    const prefix = `papermap:${DATA.fingerprint}:`;
    return {
      get(k, d) {
        try {
          const v = localStorage.getItem(prefix + k);
          return v == null ? d : JSON.parse(v);
        } catch { return d; }
      },
      set(k, v) {
        try { localStorage.setItem(prefix + k, JSON.stringify(v)); } catch { /* storage unavailable */ }
      },
    };
  })();

  const measure = (() => {
    const c = document.createElement('canvas').getContext('2d');
    return (text, font) => { c.font = font; return c.measureText(text).width; };
  })();
  const FONT = () => getComputedStyle(document.body).fontFamily;

  function wrapText(text, maxW, font, maxLines) {
    const words = String(text).split(/\s+/).filter(Boolean);
    const lines = [];
    let cur = '';
    for (const w of words) {
      const t = cur ? `${cur} ${w}` : w;
      if (!cur || measure(t, font) <= maxW) cur = t;
      else { lines.push(cur); cur = w; }
    }
    if (cur) lines.push(cur);
    if (lines.length > maxLines) {
      const kept = lines.slice(0, maxLines);
      kept[maxLines - 1] = `${kept[maxLines - 1].replace(/\s*\S*$/, '')}…`;
      return kept;
    }
    return lines;
  }

  const pagesLabel = (pages) => {
    if (!pages || !pages.length) return '';
    const a = Math.min(...pages);
    const b = Math.max(...pages);
    return a === b ? `p. ${a}` : `pp. ${a}–${b}`;
  };
  const secNum = (i) => String(i).padStart(2, '0');

  // ------------------------------------------------------------------ state
  const initialView = store.get('view', 'high');
  const state = {
    view: ['high', 'deep', 'graph'].includes(initialView) ? initialView : 'high',
    readView: ['high', 'deep'].includes(initialView) ? initialView : 'high',
    sIdx: clamp(store.get('sIdx', 0), 0, SECTIONS.length - 1),
    bIdx: 0,
    playing: false,
    mode: 'read',
    visited: new Set(store.get('visited', [])),
    completed: store.get('completed', false),
    forceUnlock: store.get('forceUnlock', false),
    voiceOn: store.get('voiceOn', true),
    rate: store.get('rate', DATA.narration.rate || 1),
    autoAdvance: store.get('autoAdvance', true),
    qaAvailable: false,
    qaInfo: null,
    chat: [],
    askMsgs: [],
    askContext: null,
    sideTab: 'narration',
    selectedNode: null,
  };

  const sectionView = (view = state.readView, i = state.sIdx) => {
    const sv = DATA.views[view].sections[i];
    if (sv.beats && sv.beats.length) return sv;
    const meta = SECTIONS[i];
    return { ...sv, beats: [{ id: `${view}.${meta.id}.b1`, speaker: 'narrator', narration: meta.summary || meta.title, subtitle: meta.title, focus: [], refs: [meta.id] }] };
  };
  const currentBeat = () => sectionView().beats[state.bIdx];
  const unlocked = () => state.completed || state.forceUnlock || state.visited.size >= SECTIONS.length;

  function persist() {
    store.set('view', state.view);
    store.set('sIdx', state.sIdx);
    store.set('visited', [...state.visited]);
    store.set('completed', state.completed);
    // shareable position: #view=deep&s=3&b=1
    try { history.replaceState(null, '', `#view=${state.view}&s=${state.sIdx}&b=${state.bIdx}`); } catch { /* sandboxed */ }
  }

  function applyHash() {
    const p = new URLSearchParams(location.hash.slice(1));
    const v = p.get('view');
    if (['high', 'deep', 'graph'].includes(v)) { state.view = v; if (v !== 'graph') state.readView = v; }
    if (p.has('s')) state.sIdx = clamp(parseInt(p.get('s'), 10) || 0, 0, SECTIONS.length - 1);
    if (p.has('b')) state.bIdx = parseInt(p.get('b'), 10) || 0;
    if (p.get('mode') === 'qa') state.mode = 'qa';
    if (p.has('node') && NODE_BY_ID[p.get('node')]) state.selectedNode = p.get('node');
    if (p.get('unlock') === '1') state.forceUnlock = true;
    if (p.get('theme') === 'dark' || p.get('theme') === 'light') applyTheme(p.get('theme'));
  }

  // ---------------------------------------------------------------- shell
  const app = document.getElementById('app');
  const ui = {};

  function buildShell() {
    ui.viewBtns = {};
    const viewSeg = h('div', { class: 'seg', role: 'group', 'aria-label': 'View' },
      ['high', 'deep', 'graph'].map((v) => (ui.viewBtns[v] = h('button', { 'aria-pressed': 'false', title: `${VIEW_LABEL[v]} (${['high', 'deep', 'graph'].indexOf(v) + 1})`, onclick: () => setView(v) },
        h('span', { class: 'long' }, VIEW_LABEL[v]), h('span', { class: 'short' }, { high: 'High', deep: 'Deep', graph: 'Graph' }[v])))));
    ui.modeBtns = {
      read: h('button', { 'aria-pressed': 'true', onclick: () => setMode('read') }, 'Read'),
      qa: h('button', { 'aria-pressed': 'false', onclick: () => setMode('qa'), title: 'Q&A (q)' }, (ui.qaLock = h('span', { class: 'lockwrap' }, icon('lock'))), 'Q&A'),
    };
    ui.qaLock.firstChild.classList.add('lock');
    const modeSeg = h('div', { class: 'seg', role: 'group', 'aria-label': 'Mode' }, ui.modeBtns.read, ui.modeBtns.qa);
    ui.themeBtn = h('button', { class: 'icon-btn', 'aria-label': 'Toggle dark mode', title: 'Theme', onclick: toggleTheme });
    ui.settingsBtn = h('button', { class: 'icon-btn', 'aria-label': 'Narration settings', title: 'Narration settings', onclick: toggleSettings }, icon('sliders'));
    ui.helpBtn = h('button', { class: 'icon-btn', 'aria-label': 'Keyboard shortcuts', title: 'Shortcuts (?)', onclick: showHelp }, icon('help'));
    ui.panelBtn = h('button', { class: 'icon-btn only-narrow', 'aria-label': 'Narration and questions panel', onclick: () => ui.side.classList.toggle('open') }, icon('panel'));
    const menuBtn = h('button', { class: 'icon-btn only-narrow', 'aria-label': 'Sections', onclick: () => ui.rail.classList.toggle('open') }, icon('menu'));
    const authors = (DATA.paper.authors || []).join(', ');
    const topbar = h('header', { class: 'topbar' },
      menuBtn,
      h('div', { class: 'brand' }, h('span', { class: 'brand-mark' }, icon('wave')), h('span', { class: 'brand-name' }, 'PaperMap')),
      h('div', { class: 'paper-title', title: authors ? `${DATA.paper.title} — ${authors}` : DATA.paper.title }, DATA.paper.title),
      viewSeg, modeSeg, ui.panelBtn, ui.settingsBtn, ui.themeBtn, ui.helpBtn);

    ui.rail = h('nav', { class: 'rail', 'aria-label': 'Sections' });
    ui.center = h('main', { class: 'center' });
    ui.side = h('aside', { class: 'side', 'aria-label': 'Narration and questions' });
    ui.main = h('div', { class: 'main' }, ui.rail, ui.center, ui.side);
    ui.player = buildPlayer();
    ui.tooltip = h('div', { class: 'tooltip', role: 'tooltip' });
    app.append(topbar, ui.main, ui.player, ui.tooltip);

    buildRail();
    buildReadView();
    buildGraphView();
    buildQAView();
    buildSide();
    applyTheme(store.get('theme', null));
  }

  // ---------------------------------------------------------------- rail
  function buildRail() {
    ui.secBtns = [];
    const list = h('ol', { class: 'sec-list' });
    SECTIONS.forEach((sec, i) => {
      const btn = h('button', { class: 'sec-btn', onclick: () => { ui.rail.classList.remove('open'); if (state.mode !== 'read') setMode('read'); if (state.view === 'graph') setView(state.readView); goTo(i, 0); } },
        h('span', { class: 'sec-num' }, i === 0 ? '★' : String(i)),
        h('span', {}, h('div', { class: 'sec-title' }, sec.title), h('div', { class: 'sec-meta' }, [ROLE_LABEL[sec.role] || 'Section', pagesLabel(sec.pages)].filter(Boolean).join(' · '))));
      ui.secBtns.push(btn);
      list.append(h('li', {}, btn));
    });
    ui.progressText = h('span', {});
    ui.progressFill = h('div', { class: 'bar-fill' });
    const p = DATA.profile;
    ui.rail.append(
      h('h2', {}, 'Sections'), list,
      h('div', { class: 'rail-progress' }, ui.progressText, h('div', { class: 'bar-track' }, ui.progressFill)),
      h('div', { class: 'rail-about' },
        DATA.paper.one_line ? h('p', {}, h('strong', {}, 'In one line. '), DATA.paper.one_line) : null,
        h('p', {}, h('strong', {}, 'Explained for '), `${p.name}`, p.summary ? ` — ${p.summary}` : '')));
  }

  function updateRail() {
    ui.secBtns.forEach((b, i) => {
      b.setAttribute('aria-current', i === state.sIdx && state.mode === 'read' && state.view !== 'graph' ? 'true' : 'false');
      b.classList.toggle('visited', state.visited.has(SECTIONS[i].id));
    });
    const n = SECTIONS.filter((x) => state.visited.has(x.id)).length;
    ui.progressText.textContent = state.completed ? 'Finished — Q&A unlocked' : `${n} of ${SECTIONS.length} sections explored`;
    ui.progressFill.style.width = `${(state.completed ? 1 : n / SECTIONS.length) * 100}%`;
  }

  // ------------------------------------------------------------ read view
  function buildReadView() {
    ui.kicker = h('div', { class: 'kicker' });
    ui.stageTitle = h('h1', { class: 'stage-title' });
    ui.askDiagramBtn = h('button', { class: 'chip-btn', onclick: () => openAsk(readContext(true)) }, icon('chat'), h('span', { class: 'lbl' }, 'Ask about this'));
    ui.toGraphBtn = h('button', { class: 'chip-btn', onclick: () => setView('graph'), title: 'See these concepts in the knowledge graph' }, icon('graph'), h('span', { class: 'lbl' }, 'In the graph'));
    ui.stageBody = h('div', { class: 'stage-body' });
    ui.stageCaption = h('div', { class: 'stage-caption' });
    ui.speaker = h('span', { class: 'speaker' });
    ui.subtitle = h('p', { class: 'subtitle' });
    ui.quote = h('blockquote', { class: 'quote' });
    ui.readView = h('div', { class: 'stage-wrap' },
      h('div', { class: 'stage-head' }, h('div', { class: 'titles' }, ui.kicker, ui.stageTitle), h('div', { class: 'stage-tools' }, ui.askDiagramBtn, ui.toGraphBtn)),
      h('div', { class: 'stage' }, ui.stageBody, ui.stageCaption),
      h('div', { class: 'caption', 'aria-live': 'polite' }, h('div', { class: 'subtitle-row' }, ui.speaker, ui.subtitle), ui.quote));
    ui.center.append(ui.readView);
  }

  let diagramCtl = null;

  function renderSection() {
    const sv = sectionView();
    const meta = SECTIONS[state.sIdx];
    ui.kicker.textContent = `${state.sIdx === 0 ? 'Overview' : `${secNum(state.sIdx)} · ${ROLE_LABEL[meta.role] || 'Section'}`} · ${VIEW_LABEL[state.readView]}`;
    ui.stageTitle.textContent = sv.title;
    ui.stageBody.replaceChildren();
    ui.stageCaption.replaceChildren();
    ui.readView.classList.toggle('no-diagram', !sv.diagram);
    hidePopover();
    if (sv.diagram) {
      diagramCtl = renderDiagram(sv.diagram, ui.stageBody);
      ui.stageCaption.append(h('span', { class: 'dtitle' }, sv.diagram.title), sv.diagram.caption ? h('span', {}, sv.diagram.caption) : null);
    } else {
      diagramCtl = renderIdea(sv, ui.stageBody);
    }
    const hasNodes = NODES.some((n) => (n.sections || []).includes(meta.id));
    ui.toGraphBtn.style.display = hasNodes ? '' : 'none';
    ui.askDiagramBtn.lastChild.textContent = sv.diagram ? 'Ask about this diagram' : 'Ask about this section';
    renderTranscript();
    renderTimeline();
    updateRail();
  }

  function renderBeat(animate = true) {
    const sv = sectionView();
    const beat = currentBeat();
    const meta = SECTIONS[state.sIdx];
    ui.subtitle.textContent = beat.subtitle || '';
    if (animate) { ui.subtitle.classList.remove('swap'); void ui.subtitle.offsetWidth; ui.subtitle.classList.add('swap'); }
    const duo = beat.speaker === 'host' || beat.speaker === 'expert';
    ui.speaker.style.display = duo ? '' : 'none';
    ui.speaker.className = `speaker ${beat.speaker}`;
    ui.speaker.textContent = beat.speaker === 'host' ? 'Host' : 'Expert';
    if (beat.quote) {
      ui.quote.replaceChildren(document.createTextNode(`“${beat.quote}”`), h('cite', {}, `From the paper · ${meta.title}${pagesLabel(meta.pages) ? ` · ${pagesLabel(meta.pages)}` : ''}`));
      ui.quote.style.display = '';
    } else {
      ui.quote.style.display = 'none';
    }
    if (diagramCtl) diagramCtl.focus(beat.focus || [], beat);
    ui.tbeatEls?.forEach((el, i) => el.classList.toggle('current', i === state.bIdx));
    const cur = ui.tbeatEls?.[state.bIdx];
    if (cur && state.sideTab === 'narration') cur.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    updateTimeline(0);
    ui.nowLabel.textContent = `${state.sIdx === 0 ? 'Overview' : `Section ${state.sIdx} of ${SECTIONS.length - 1}`} · ${sv.title}`;
    ui.beatLabel.textContent = `beat ${state.bIdx + 1}/${sv.beats.length}`;
    setMediaSession(sv);
  }

  function renderIdea(sv, host) {
    const text = h('p', { class: 'idea-text' });
    const dots = h('div', { class: 'idea-dots' }, sv.beats.map(() => h('span')));
    host.append(h('div', { class: 'idea diagram-enter' }, text, dots));
    return {
      focus(_, beat) {
        text.textContent = beat.subtitle || sv.title;
        text.classList.remove('swap'); void text.offsetWidth; text.classList.add('swap');
        [...dots.children].forEach((d, i) => d.classList.toggle('on', i === state.bIdx));
      },
    };
  }

  // ------------------------------------------------------------- diagrams
  let markerSeq = 0;

  function renderDiagram(d, host) {
    try {
      if (d.type === 'bar') return renderBar(d, host);
      if (d.type === 'table') return renderTable(d, host);
      if (d.type === 'equation') return renderEquation(d, host);
      return renderFlow(d, host);
    } catch (err) {
      console.error('diagram failed', err);
      host.append(h('p', { class: 'chart-note' }, 'This diagram could not be drawn.'));
      return { focus() {} };
    }
  }

  function layoutFlow(d, aspect) {
    const font = `600 13px ${FONT()}`;
    const nodes = d.nodes.map((n, i) => {
      const lines = wrapText(n.label, 148, font, 3);
      const tw = Math.max(...lines.map((l) => measure(l, font)), 40);
      return { ...n, i, lines, w: clamp(tw + 34, 104, 200), h: 34 + lines.length * 16 };
    });
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const edges = d.edges.filter((e) => byId.has(e.source) && byId.has(e.target) && e.source !== e.target);
    // break cycles (DFS from sources first)
    const out = new Map(nodes.map((n) => [n.id, []]));
    const indeg0 = new Map(nodes.map((n) => [n.id, 0]));
    edges.forEach((e) => { out.get(e.source).push(e); indeg0.set(e.target, indeg0.get(e.target) + 1); });
    const mark = new Map();
    const back = new Set();
    const dfs = (id) => {
      mark.set(id, 1);
      for (const e of out.get(id)) {
        const m = mark.get(e.target);
        if (m === 1) back.add(e);
        else if (!m) dfs(e.target);
      }
      mark.set(id, 2);
    };
    nodes.filter((n) => indeg0.get(n.id) === 0).forEach((n) => { if (!mark.get(n.id)) dfs(n.id); });
    nodes.forEach((n) => { if (!mark.get(n.id)) dfs(n.id); });
    const dag = edges.filter((e) => !back.has(e));
    // longest-path layering
    const indeg = new Map(nodes.map((n) => [n.id, 0]));
    const succ = new Map(nodes.map((n) => [n.id, []]));
    const pred = new Map(nodes.map((n) => [n.id, []]));
    dag.forEach((e) => { indeg.set(e.target, indeg.get(e.target) + 1); succ.get(e.source).push(e.target); pred.get(e.target).push(e.source); });
    const layer = new Map(nodes.map((n) => [n.id, 0]));
    const queue = nodes.filter((n) => indeg.get(n.id) === 0).map((n) => n.id);
    while (queue.length) {
      const id = queue.shift();
      for (const t of succ.get(id)) {
        layer.set(t, Math.max(layer.get(t), layer.get(id) + 1));
        indeg.set(t, indeg.get(t) - 1);
        if (indeg.get(t) === 0) queue.push(t);
      }
    }
    // pull sinks-only-sources (nodes feeding a far layer) closer to their target
    nodes.forEach((n) => {
      const ts = succ.get(n.id);
      if (!pred.get(n.id).length && ts.length) layer.set(n.id, Math.max(0, Math.min(...ts.map((t) => layer.get(t))) - 1));
    });
    const nLayers = Math.max(0, ...layer.values()) + 1;
    const layers = Array.from({ length: nLayers }, () => []);
    nodes.forEach((n) => layers[layer.get(n.id)].push(n));
    // crossing reduction: barycenter sweeps
    const pos = new Map();
    layers.forEach((L) => L.forEach((n, i) => pos.set(n.id, i)));
    for (let it = 0; it < 8; it++) {
      const down = it % 2 === 0;
      const seq = down ? layers.slice(1) : layers.slice(0, -1).reverse();
      for (const L of seq) {
        for (const n of L) {
          const nb = (down ? pred : succ).get(n.id);
          n.bc = nb.length ? nb.reduce((a, id) => a + pos.get(id), 0) / nb.length : pos.get(n.id);
        }
        L.sort((a, b) => a.bc - b.bc || a.i - b.i);
        L.forEach((n, i) => pos.set(n.id, i));
      }
    }
    const hasGroups = (d.groups || []).length > 0;
    const GAP_CROSS = hasGroups ? 40 : 26;
    const BAND_GAP = hasGroups ? 76 : 62;
    // Candidate placements: left-to-right (optionally wrapped into bands) or top-to-bottom.
    // Pick the one whose shape best fits the stage, with a small bias to the model's choice.
    const placeLR = (bands) => {
      const per = Math.ceil(layers.length / bands);
      const P = new Map();
      let yOff = 0;
      let width = 0;
      for (let b = 0; b < bands; b++) {
        const Ls = layers.slice(b * per, (b + 1) * per);
        if (!Ls.length) break;
        const colH = Ls.map((L) => L.reduce((a, n) => a + n.h, 0) + GAP_CROSS * (L.length - 1));
        const bandH = Math.max(...colH);
        let x = 0;
        Ls.forEach((L, li) => {
          const colW = Math.max(...L.map((n) => n.w));
          let y = yOff + (bandH - colH[li]) / 2;
          L.forEach((n) => { P.set(n.id, { x: x + colW / 2, y: y + n.h / 2, band: b }); y += n.h + GAP_CROSS; });
          x += colW + 84;
        });
        width = Math.max(width, x - 84);
        yOff += bandH + BAND_GAP;
      }
      return { P, w: width, h: yOff - BAND_GAP, LR: true };
    };
    const placeTB = () => {
      const P = new Map();
      const rowW = layers.map((L) => L.reduce((a, n) => a + n.w, 0) + GAP_CROSS * (L.length - 1));
      const maxW = Math.max(...rowW);
      let y = 0;
      layers.forEach((L, li) => {
        const rowH = Math.max(...L.map((n) => n.h));
        let x = (maxW - rowW[li]) / 2;
        L.forEach((n) => { P.set(n.id, { x: x + n.w / 2, y: y + rowH / 2, band: 0 }); x += n.w + GAP_CROSS; });
        y += rowH + 64;
      });
      return { P, w: maxW, h: y - 64, LR: false };
    };
    // Swimlanes: when the diagram has groups (e.g. encoder / decoder), each group gets
    // its own lane across all layers, so group boxes can never overlap.
    const groupIds = (d.groups || []).map((g) => g.id);
    const laneOf = (n) => (n.group && groupIds.includes(n.group) ? n.group : '__none__');
    const placeLanes = (LRdir) => {
      const lanes = [];
      const ordered = [...nodes].sort((x, y) => layer.get(x.id) - layer.get(y.id) || x.i - y.i);
      ordered.forEach((n) => { const l = laneOf(n); if (!lanes.includes(l)) lanes.push(l); });
      // compact ranks: each lane stacks its own nodes consecutively (like the encoder and
      // decoder columns of an architecture figure) instead of inheriting global layers
      const rank = new Map();
      const seen = {};
      ordered.forEach((n) => {
        const l = laneOf(n);
        const prevLayer = seen[l]?.layer;
        const r = seen[l] ? (layer.get(n.id) === prevLayer ? seen[l].rank : seen[l].rank + 1) : 0;
        seen[l] = { rank: r, layer: layer.get(n.id) };
        rank.set(n.id, r);
      });
      // ungrouped nodes keep their global position relative to the grouped ones
      ordered.filter((n) => laneOf(n) === '__none__').forEach((n) => {
        const before = ordered.filter((m) => laneOf(m) !== '__none__' && layer.get(m.id) < layer.get(n.id));
        rank.set(n.id, Math.max(rank.get(n.id), before.length ? Math.max(...before.map((m) => rank.get(m.id))) + 1 : 0));
      });
      const nRanks = Math.max(...nodes.map((n) => rank.get(n.id))) + 1;
      const rows = Array.from({ length: nRanks }, (_, r) => nodes.filter((n) => rank.get(n.id) === r));
      const cross = (n) => (LRdir ? n.h : n.w);
      const along = (n) => (LRdir ? n.w : n.h);
      const ext = Object.fromEntries(lanes.map((l) => [l, 0]));
      rows.forEach((L) => lanes.forEach((l) => {
        const ns = L.filter((n) => laneOf(n) === l);
        ext[l] = Math.max(ext[l], ns.reduce((acc, n) => acc + cross(n), 0) + GAP_CROSS * Math.max(0, ns.length - 1));
      }));
      const LANE_GAP = 48;
      const start = {};
      let off = 0;
      lanes.forEach((l) => { start[l] = off; off += ext[l] + LANE_GAP; });
      const P = new Map();
      const STEP = LRdir ? 84 : 56;
      let main = 0;
      rows.forEach((L) => {
        if (!L.length) return;
        const colMain = Math.max(...L.map(along));
        lanes.forEach((l) => {
          const ns = L.filter((n) => laneOf(n) === l);
          const used = ns.reduce((acc, n) => acc + cross(n), 0) + GAP_CROSS * Math.max(0, ns.length - 1);
          let c = start[l] + (ext[l] - used) / 2;
          ns.forEach((n) => {
            const cs = cross(n);
            P.set(n.id, LRdir ? { x: main + colMain / 2, y: c + cs / 2, band: 0 } : { x: c + cs / 2, y: main + colMain / 2, band: 0 });
            c += cs + GAP_CROSS;
          });
        });
        main += colMain + STEP;
      });
      const mainTotal = main - STEP;
      const crossTotal = off - LANE_GAP;
      return LRdir ? { P, w: mainTotal, h: crossTotal, LR: true, lanes: true } : { P, w: crossTotal, h: mainTotal, LR: false, lanes: true };
    };
    const target = Math.log(aspect || 1.5);
    const options = [placeLR(1), placeTB()];
    if (layers.length >= 4) options.push(placeLR(2));
    if (layers.length >= 7) options.push(placeLR(3));
    if (groupIds.length && nodes.some((n) => n.group)) options.push(placeLanes(true), placeLanes(false));
    const preferLR = d.direction !== 'TB';
    const groupIntrusions = (o) => {
      let bad = 0;
      for (const g of d.groups || []) {
        const mem = nodes.filter((n) => n.group === g.id);
        if (!mem.length) continue;
        const ps = mem.map((n) => ({ p: o.P.get(n.id), n }));
        const x0 = Math.min(...ps.map(({ p, n }) => p.x - n.w / 2));
        const x1 = Math.max(...ps.map(({ p, n }) => p.x + n.w / 2));
        const y0 = Math.min(...ps.map(({ p, n }) => p.y - n.h / 2));
        const y1 = Math.max(...ps.map(({ p, n }) => p.y + n.h / 2));
        nodes.forEach((n) => {
          if (n.group === g.id) return;
          const p = o.P.get(n.id);
          if (p.x > x0 && p.x < x1 && p.y > y0 && p.y < y1) bad += 1;
        });
      }
      return bad;
    };
    const groupOverlaps = (o) => {
      const boxes = (d.groups || []).map((g) => {
        const mem = nodes.filter((n) => n.group === g.id);
        if (!mem.length) return null;
        const ps = mem.map((n) => ({ p: o.P.get(n.id), n }));
        return {
          x0: Math.min(...ps.map(({ p, n }) => p.x - n.w / 2)) - 14, x1: Math.max(...ps.map(({ p, n }) => p.x + n.w / 2)) + 14,
          y0: Math.min(...ps.map(({ p, n }) => p.y - n.h / 2)) - 26, y1: Math.max(...ps.map(({ p, n }) => p.y + n.h / 2)) + 14,
        };
      }).filter(Boolean);
      let bad = 0;
      for (let i = 0; i < boxes.length; i++) {
        for (let j = i + 1; j < boxes.length; j++) {
          const A = boxes[i]; const B = boxes[j];
          if (A.x0 < B.x1 && B.x0 < A.x1 && A.y0 < B.y1 && B.y0 < A.y1) bad += 1;
        }
      }
      return bad;
    };
    const score = (o, i) => Math.abs(Math.log(Math.max(o.w, 1) / Math.max(o.h, 1)) - target)
      + (o.LR !== preferLR ? 0.25 : 0) + (!o.lanes && i >= 2 ? 0.2 * (i - 1) : 0)
      + 0.6 * groupIntrusions(o) + 0.8 * groupOverlaps(o) - (o.lanes ? 0.15 : 0);
    let best = 0;
    options.forEach((o, i) => { if (score(o, i) < score(options[best], best)) best = i; });
    const chosen = options[best];
    nodes.forEach((n) => { const p = chosen.P.get(n.id); n.x = p.x; n.y = p.y; n.band = p.band; });
    return { nodes, byId, edges, back, LR: chosen.LR, layer };
  }

  function bezierMid(p0, p1, p2, p3) {
    return { x: (p0.x + 3 * p1.x + 3 * p2.x + p3.x) / 8, y: (p0.y + 3 * p1.y + 3 * p2.y + p3.y) / 8 };
  }

  function edgeGeometry(a, b, LR, isBack) {
    let p0, p1, p2, p3;
    if (LR && !isBack && b.band > a.band) { // wrap to the next band: leave downwards, arrive from above
      p0 = { x: a.x, y: a.y + a.h / 2 }; p3 = { x: b.x, y: b.y - b.h / 2 - 3 };
      const dy = Math.max(30, (p3.y - p0.y) * 0.5);
      p1 = { x: p0.x, y: p0.y + dy }; p2 = { x: p3.x, y: p3.y - dy };
      return { d: `M${p0.x},${p0.y} C${p1.x},${p1.y} ${p2.x},${p2.y} ${p3.x},${p3.y}`, mid: bezierMid(p0, p1, p2, p3), ext: [p1, p2] };
    }
    const forward = LR ? b.band === a.band && b.x - b.w / 2 > a.x + a.w / 2 : b.y - b.h / 2 > a.y + a.h / 2;
    if (!isBack && forward) {
      if (LR) {
        p0 = { x: a.x + a.w / 2, y: a.y }; p3 = { x: b.x - b.w / 2 - 3, y: b.y };
        const dx = Math.max(28, (p3.x - p0.x) * 0.5);
        p1 = { x: p0.x + dx, y: p0.y }; p2 = { x: p3.x - dx, y: p3.y };
      } else {
        p0 = { x: a.x, y: a.y + a.h / 2 }; p3 = { x: b.x, y: b.y - b.h / 2 - 3 };
        const dy = Math.max(24, (p3.y - p0.y) * 0.5);
        p1 = { x: p0.x, y: p0.y + dy }; p2 = { x: p3.x, y: p3.y - dy };
      }
    } else if (LR) { // loop under the nodes
      p0 = { x: a.x, y: a.y + a.h / 2 }; p3 = { x: b.x, y: b.y + b.h / 2 + 3 };
      const dy = 40 + Math.abs(p3.x - p0.x) * 0.12;
      p1 = { x: p0.x, y: Math.max(p0.y, p3.y) + dy }; p2 = { x: p3.x, y: Math.max(p0.y, p3.y) + dy };
    } else { // loop beside the nodes
      p0 = { x: a.x + a.w / 2, y: a.y }; p3 = { x: b.x + b.w / 2 + 3, y: b.y };
      const dx = 40 + Math.abs(p3.y - p0.y) * 0.12;
      p1 = { x: Math.max(p0.x, p3.x) + dx, y: p0.y }; p2 = { x: Math.max(p0.x, p3.x) + dx, y: p3.y };
    }
    return { d: `M${p0.x},${p0.y} C${p1.x},${p1.y} ${p2.x},${p2.y} ${p3.x},${p3.y}`, mid: bezierMid(p0, p1, p2, p3), ext: [p1, p2] };
  }

  function renderFlow(d, host) {
    const hr = host.getBoundingClientRect();
    const aspect = hr.width > 50 && hr.height > 50 ? (hr.width - 36) / (hr.height - 36) : 1.5;
    const L = layoutFlow(d, aspect);
    const font = FONT();
    const id = ++markerSeq;
    const mk = `pm-arrow-${id}`;
    const mkA = `pm-arrow-a-${id}`;
    const svg = s('svg', { class: 'diagram-svg diagram-enter', role: 'img', 'aria-label': `${d.title}. ${d.caption || ''}`, preserveAspectRatio: 'xMidYMid meet' });
    svg.append(s('defs', {},
      s('marker', { id: mk, viewBox: '0 0 10 10', refX: 8, refY: 5, markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse' }, s('path', { d: 'M0,0 L10,5 L0,10 z', class: 'arrowhead' })),
      s('marker', { id: mkA, viewBox: '0 0 10 10', refX: 8, refY: 5, markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse' }, s('path', { d: 'M0,0 L10,5 L0,10 z', class: 'arrowhead-active' }))));
    let minX = Infinity; let minY = Infinity; let maxX = -Infinity; let maxY = -Infinity;
    const grow = (x, y) => { minX = Math.min(minX, x); minY = Math.min(minY, y); maxX = Math.max(maxX, x); maxY = Math.max(maxY, y); };
    L.nodes.forEach((n) => { grow(n.x - n.w / 2, n.y - n.h / 2); grow(n.x + n.w / 2, n.y + n.h / 2); });

    // groups (behind everything)
    const groupEls = new Map();
    const gGroups = s('g', {});
    (d.groups || []).forEach((g) => {
      const members = L.nodes.filter((n) => n.group === g.id);
      if (!members.length) return;
      const x0 = Math.min(...members.map((n) => n.x - n.w / 2)) - 14;
      const y0 = Math.min(...members.map((n) => n.y - n.h / 2)) - 26;
      const x1 = Math.max(...members.map((n) => n.x + n.w / 2)) + 14;
      const y1 = Math.max(...members.map((n) => n.y + n.h / 2)) + 14;
      grow(x0, y0); grow(x1, y1);
      const el = s('g', { class: 'fgroup' }, s('rect', { x: x0, y: y0, width: x1 - x0, height: y1 - y0, rx: 16 }), s('text', { x: x0 + 12, y: y0 + 16 }, g.label));
      groupEls.set(g.id, { el, members: members.map((m) => m.id) });
      gGroups.append(el);
    });

    // edges
    const gEdges = s('g', {});
    const edgeEls = [];
    L.edges.forEach((e) => {
      const a = L.byId.get(e.source);
      const b = L.byId.get(e.target);
      const geo = edgeGeometry(a, b, L.LR, L.back.has(e));
      geo.ext.forEach((p) => grow(p.x, p.y));
      const path = s('path', { d: geo.d, 'marker-end': `url(#${mk})` });
      const el = s('g', { class: 'fedge' }, path);
      if (e.label) {
        const tw = measure(e.label, `11px ${font}`) + 10;
        el.append(s('rect', { class: 'elabel-bg', x: geo.mid.x - tw / 2, y: geo.mid.y - 9, width: tw, height: 18, rx: 6 }),
          s('text', { x: geo.mid.x, y: geo.mid.y + 4, 'text-anchor': 'middle' }, e.label));
      }
      edgeEls.push({ el, path, e });
      gEdges.append(el);
    });

    // nodes
    const gNodes = s('g', {});
    const nodeEls = new Map();
    L.nodes.forEach((n, i) => {
      const slot = KIND_SLOT[n.kind] || 1;
      const top = -n.h / 2;
      const label = s('text', { class: 'flabel', 'text-anchor': 'middle' },
        n.lines.map((line, k) => s('tspan', { x: 0, y: top + 34 + k * 16 }, line)));
      const kindW = measure(n.kind.toUpperCase(), `700 9.5px ${font}`);
      const el = s('g', { class: 'fnode', transform: `translate(${n.x},${n.y})`, tabindex: 0, role: 'button', 'aria-label': `${n.label}${n.detail ? `: ${n.detail}` : ''}` },
        s('g', { class: REDUCED ? '' : 'anim-in', style: `animation-delay:${(L.layer.get(n.id) * 90 + i * 15)}ms` },
          s('rect', { class: 'box', x: -n.w / 2, y: top, width: n.w, height: n.h, rx: 12 }),
          s('circle', { class: 'kind-dot', cx: -kindW / 2 - 7, cy: top + 13, r: 3.2, fill: slotColor(slot) }),
          s('text', { class: 'fkind', 'text-anchor': 'middle', x: 3, y: top + 16.5 }, n.kind.toUpperCase()),
          label));
      el.addEventListener('mouseenter', (ev) => n.detail && showTooltip(ev, `<b>${esc(n.label)}</b><br>${esc(n.detail)}`));
      el.addEventListener('mousemove', moveTooltip);
      el.addEventListener('mouseleave', hideTooltip);
      el.addEventListener('click', (ev) => { ev.stopPropagation(); showNodePopover(el, n, d); });
      el.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); showNodePopover(el, n, d); } });
      nodeEls.set(n.id, el);
      gNodes.append(el);
    });
    const pad = 18;
    let vb = { x: minX - pad, y: minY - pad, w: maxX - minX + 2 * pad, h: maxY - minY + 2 * pad };
    if (hr.width > 50 && hr.height > 50) {
      const maxScale = 1.35;
      const sc = Math.min((hr.width - 36) / vb.w, (hr.height - 36) / vb.h);
      if (sc > maxScale) {
        const nw = (hr.width - 36) / maxScale;
        const nh = (hr.height - 36) / maxScale;
        vb = { x: vb.x - (nw - vb.w) / 2, y: vb.y - (nh - vb.h) / 2, w: nw, h: nh };
      }
    }
    svg.setAttribute('viewBox', `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
    svg.append(gGroups, gEdges, gNodes);
    host.append(svg);

    return {
      focus(ids) {
        const set = new Set(ids || []);
        for (const [gid, g] of groupEls) if (set.has(gid)) g.members.forEach((m) => set.add(m));
        svg.classList.toggle('has-focus', set.size > 0);
        for (const [nid, el] of nodeEls) el.classList.toggle('is-focus', set.has(nid));
        for (const [gid, g] of groupEls) g.el.classList.toggle('is-focus', set.has(gid));
        const single = set.size === 1;
        edgeEls.forEach(({ el, path, e }) => {
          const on = (set.has(e.source) && set.has(e.target)) || (single && (set.has(e.source) || set.has(e.target)));
          el.classList.toggle('is-active', on);
          path.setAttribute('marker-end', `url(#${on ? mkA : mk})`);
        });
      },
    };
  }

  function niceTicks(lo, hi, count = 5) {
    const span = hi - lo || Math.abs(hi) || 1;
    const raw = span / count;
    const mag = 10 ** Math.floor(Math.log10(raw));
    const norm = raw / mag;
    const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
    const a = Math.floor(lo / step) * step;
    const b = Math.ceil(hi / step) * step;
    const ticks = [];
    for (let v = a; v <= b + step / 2; v += step) ticks.push(+v.toFixed(10));
    return ticks;
  }

  function renderBar(d, host) {
    const cats = d.categories;
    const series = d.series.slice(0, 3); // >3 series would need facets; the slot palette validates 3 all-pairs
    const values = series.flatMap((sr) => sr.values.filter((v) => v != null));
    const positive = values.length > 1 && values.every((v) => v > 0);
    const logScale = positive && Math.max(...values) / Math.min(...values) >= 100;
    let ticks;
    if (logScale) {
      const e0 = Math.floor(Math.log10(Math.min(...values)));
      const e1 = Math.ceil(Math.log10(Math.max(...values)));
      const stepE = Math.max(1, Math.ceil((e1 - e0) / 5));
      ticks = [];
      for (let e = e0; e <= e1; e += stepE) ticks.push(10 ** e);
      if (Math.log10(ticks[ticks.length - 1]) < e1) ticks.push(10 ** e1);
    } else {
      ticks = niceTicks(Math.min(0, ...values), Math.max(0, ...values), 4);
    }
    const y0 = ticks[0];
    const y1 = ticks[ticks.length - 1];
    const W = 760;
    const H = 360;
    const m = { t: 30, r: 12, b: 58, l: logScale ? 64 : 50 };
    const iw = W - m.l - m.r;
    const ih = H - m.t - m.b;
    const y = logScale
      ? (v) => m.t + ih - ((Math.log10(Math.max(v, y0)) - Math.log10(y0)) / (Math.log10(y1) - Math.log10(y0) || 1)) * ih
      : (v) => m.t + ih - ((v - y0) / (y1 - y0 || 1)) * ih;
    const base = logScale ? y0 : 0;
    const band = iw / cats.length;
    const groupPad = band * (series.length > 1 ? 0.24 : 0.36);
    const bw = Math.min(64, (band - groupPad) / series.length);
    const font = FONT();
    const svg = s('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': `${d.title}. ${d.caption || ''}`, preserveAspectRatio: 'xMidYMid meet', class: REDUCED ? '' : 'bars-enter' });
    const grid = s('g', { class: 'tick' });
    ticks.forEach((t) => {
      grid.append(s('line', { class: t === base ? 'baseline' : 'gridline', x1: m.l, x2: W - m.r, y1: y(t), y2: y(t) }),
        s('text', { x: m.l - 8, y: y(t) + 4, 'text-anchor': 'end' }, fmtNum(t)));
    });
    svg.append(grid);
    if (d.unit) svg.append(s('text', { x: 4, y: 12, 'text-anchor': 'start', fill: 'var(--text-muted)', 'font-size': 11.5, 'font-family': font }, d.unit));
    const bars = [];
    const labels = [];
    const catEls = [];
    cats.forEach((c, ci) => {
      const gx = m.l + ci * band + groupPad / 2 + (band - groupPad - bw * series.length) / 2;
      series.forEach((sr, si) => {
        const v = sr.values[ci];
        if (v == null) return;
        const x = gx + si * bw + 1;
        const w = Math.max(2, bw - 2);
        const top = y(logScale ? v : Math.max(v, 0));
        const bottom = y(logScale ? base : Math.min(v, 0));
        const hgt = Math.max(1, bottom - top);
        const r = Math.min(4, w / 2, hgt);
        const dPath = v >= 0
          ? `M${x},${bottom} V${top + r} Q${x},${top} ${x + r},${top} H${x + w - r} Q${x + w},${top} ${x + w},${top + r} V${bottom} Z`
          : `M${x},${top} V${bottom - r} Q${x},${bottom} ${x + r},${bottom} H${x + w - r} Q${x + w},${bottom} ${x + w},${bottom - r} V${top} Z`;
        const bar = s('path', { class: 'bar', d: dPath, fill: `var(--series-${si + 1})`, style: `animation-delay:${ci * 60 + si * 30}ms` });
        bar.dataset.cat = c;
        bar.addEventListener('mouseenter', (ev) => showTooltip(ev, `<b>${esc(c)}</b><br>${esc(sr.name)}: ${esc(fmtNum(v))}${d.unit ? ` ${esc(d.unit)}` : ''}`));
        bar.addEventListener('mousemove', moveTooltip);
        bar.addEventListener('mouseleave', hideTooltip);
        const lab = s('text', { class: 'vlabel', x: x + w / 2, y: v >= 0 ? top - 6 : bottom + 14, 'text-anchor': 'middle' }, fmtNum(v));
        lab.dataset.cat = c;
        lab.dataset.series = String(si);
        lab.dataset.value = String(v);
        bars.push(bar);
        labels.push(lab);
        svg.append(bar);
      });
      const lines = wrapText(c, band - 8, `12px ${font}`, 2);
      const t = s('text', { class: 'cat-label', 'text-anchor': 'middle' }, lines.map((ln, k) => s('tspan', { x: m.l + ci * band + band / 2, y: H - m.b + 20 + k * 14 }, ln)));
      t.dataset.cat = c;
      catEls.push(t);
      svg.append(t);
    });
    labels.forEach((l) => svg.append(l));
    const legend = series.length > 1
      ? h('div', { class: 'chart-legend' }, series.map((sr, i) => h('span', {}, h('i', { style: `background:var(--series-${i + 1})` }), sr.name)))
      : h('div', { class: 'chart-legend' }, h('span', {}, series[0]?.name || ''));
    const note = [logScale ? 'log scale' : '', d.higher_is_better == null ? '' : d.higher_is_better ? '↑ higher is better' : '↓ lower is better'].filter(Boolean).join(' · ');
    const wrap = h('div', { class: 'bar-chart diagram-enter' }, h('div', { style: 'display:flex;justify-content:space-between;gap:12px;align-items:baseline' }, legend, h('span', { class: 'chart-note' }, note)), svg);
    host.append(wrap);
    // default labels: the best value of each series (never a number on every bar)
    const best = new Set();
    series.forEach((sr, si) => {
      const vals = sr.values.map((v, i) => [v, i]).filter(([v]) => v != null);
      if (!vals.length) return;
      const pick = vals.reduce((a, b) => ((d.higher_is_better === false ? b[0] < a[0] : b[0] > a[0]) ? b : a));
      best.add(`${si}:${cats[pick[1]]}`);
    });
    return {
      focus(ids) {
        const set = new Set((ids || []).filter((x) => cats.includes(x)));
        svg.classList.toggle('has-focus', set.size > 0);
        bars.forEach((b) => b.classList.toggle('is-focus', set.has(b.dataset.cat)));
        catEls.forEach((t) => t.classList.toggle('is-focus', set.has(t.dataset.cat)));
        labels.forEach((l) => l.classList.toggle('show', set.size ? set.has(l.dataset.cat) : best.has(`${l.dataset.series}:${l.dataset.cat}`)));
      },
    };
  }

  function renderTable(d, host) {
    const isNum = (c) => /^[\s\d.,%±+\-–×x()~<>]+$/.test(c) && /\d/.test(c);
    const rows = d.rows.map((r) => h('tr', { 'data-key': r[0] }, r.map((c, i) => h('td', { class: i > 0 && isNum(c) ? 'num' : '' }, c))));
    const table = h('table', { class: 'dtable' },
      h('thead', {}, h('tr', {}, d.columns.map((c) => h('th', {}, c)))),
      h('tbody', {}, rows));
    host.append(h('div', { class: 'dtable-wrap diagram-enter' }, table));
    return {
      focus(ids) {
        const set = new Set(ids || []);
        const hit = rows.filter((r) => set.has(r.dataset.key));
        table.classList.toggle('has-focus', hit.length > 0);
        rows.forEach((r) => r.classList.toggle('is-focus', set.has(r.dataset.key)));
      },
    };
  }

  function renderEquation(d, host) {
    const main = h('div', { class: 'eq-main' });
    const terms = d.terms.map((t) => {
      const sym = h('span', { class: 'sym' });
      const el = h('div', { class: 'term', 'data-key': t.symbol }, sym, h('span', { class: 'meaning' }, t.meaning));
      return { el, sym, t };
    });
    const paint = () => {
      renderMath(main, d.latex, true);
      terms.forEach(({ sym, t }) => renderMath(sym, t.symbol, false));
    };
    paint();
    if (!window.katex) ensureKatex().then((k) => k && paint());
    const box = h('div', { class: 'equation diagram-enter' }, main, h('div', { class: 'terms' }, terms.map((x) => x.el)));
    host.append(box);
    return {
      focus(ids) {
        const set = new Set(ids || []);
        box.classList.toggle('has-focus', terms.some((x) => set.has(x.t.symbol)));
        terms.forEach((x) => x.el.classList.toggle('is-focus', set.has(x.t.symbol)));
      },
    };
  }

  let katexPromise = null;
  function ensureKatex() {
    if (window.katex) return Promise.resolve(window.katex);
    if (!katexPromise) {
      katexPromise = new Promise((resolve) => {
        if (navigator.onLine === false) { resolve(null); return; }
        const base = 'https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/';
        const css = h('link', { rel: 'stylesheet', href: `${base}katex.min.css` });
        const js = h('script', { src: `${base}katex.min.js`, async: true });
        js.onload = () => resolve(window.katex || null);
        js.onerror = () => resolve(null);
        document.head.append(css, js);
        setTimeout(() => resolve(window.katex || null), 8000);
      });
    }
    return katexPromise;
  }
  function renderMath(el, tex, display) {
    if (window.katex) {
      try {
        window.katex.render(tex, el, { displayMode: display, throwOnError: false });
        el.classList.remove('raw');
        return;
      } catch { /* fall through to raw */ }
    }
    el.textContent = tex;
    el.classList.add('raw');
  }

  // popover / tooltip
  function showTooltip(ev, html) {
    ui.tooltip.innerHTML = html;
    ui.tooltip.classList.add('show');
    moveTooltip(ev);
  }
  function moveTooltip(ev) {
    const pad = 14;
    const r = ui.tooltip.getBoundingClientRect();
    let x = ev.clientX + pad;
    let y = ev.clientY + pad;
    if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - pad;
    if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - pad;
    ui.tooltip.style.left = `${x}px`;
    ui.tooltip.style.top = `${y}px`;
  }
  function hideTooltip() { ui.tooltip.classList.remove('show'); }

  let popover = null;
  function hidePopover() { if (popover) { popover.remove(); popover = null; } }
  function showNodePopover(el, n, d) {
    hidePopover();
    hideTooltip();
    const stage = ui.stageBody.parentElement;
    const sr = stage.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    popover = h('div', { class: 'popover', role: 'dialog' },
      h('h4', {}, n.label),
      h('p', {}, n.detail || `${n.kind} in “${d.title}”`),
      h('div', { class: 'row', style: 'display:flex;gap:8px' },
        h('button', { class: 'btn', onclick: () => { hidePopover(); openAsk({ ...readContext(true), focusLabel: n.label, label: `“${n.label}” in ${d.title}` }); } }, icon('chat'), 'Ask about this'),
        h('button', { class: 'btn ghost', onclick: hidePopover }, 'Close')));
    stage.append(popover);
    const pw = 300;
    let left = r.left - sr.left + r.width / 2 - pw / 2;
    left = clamp(left, 8, sr.width - pw - 8);
    let top = r.bottom - sr.top + 8;
    if (top + 150 > sr.height) top = Math.max(8, r.top - sr.top - 150);
    popover.style.left = `${left}px`;
    popover.style.top = `${top}px`;
  }
  document.addEventListener('click', (ev) => { if (popover && !popover.contains(ev.target)) hidePopover(); });

  // ------------------------------------------------------------ narration
  const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;

  function splitSentences(text) {
    const parts = String(text).match(/[^.!?]+[.!?]+["”’)]*\s*|[^.!?]+$/g) || [text];
    return parts.map((p) => p.trim()).filter(Boolean);
  }
  const estimateSeconds = (beat) => Math.max(2.4, beat.narration.split(/\s+/).length / (2.6 * state.rate) + 0.5);

  const narrator = {
    audio: new Audio(),
    token: 0,
    timer: null,
    watchdog: null,
    mode: 'idle',
    pausedBeat: null,
    progress: 0,
    t0: 0,
    dur: 0,
    voices: [],
    play(beat, onEnd) {
      this.stop();
      const token = ++this.token;
      const done = () => { if (token === this.token) { this.mode = 'idle'; this.progress = 1; onEnd(); } };
      this.progress = 0;
      if (state.voiceOn && beat.audio) {
        this.mode = 'file';
        this.audio.src = beat.audio;
        this.audio.playbackRate = state.rate;
        this.audio.onended = done;
        this.audio.onerror = () => { if (token === this.token) this.silent(beat, done); };
        this.audio.play().catch(() => { if (token === this.token) this.silent(beat, done); });
        this.pausedBeat = beat.id;
        return;
      }
      if (state.voiceOn && DATA.narration.provider === 'browser' && 'speechSynthesis' in window) {
        this.speak(beat, token, done);
        return;
      }
      this.silent(beat, done);
    },
    speak(beat, token, done) {
      this.mode = 'speech';
      const sentences = splitSentences(beat.narration);
      const total = beat.narration.length || 1;
      let i = 0;
      let before = 0;
      const next = () => {
        clearTimeout(this.watchdog);
        if (token !== this.token) return;
        if (i >= sentences.length) { done(); return; }
        const text = sentences[i++];
        const u = new SpeechSynthesisUtterance(text);
        const v = voiceFor(beat.speaker);
        if (v) { u.voice = v; u.lang = v.lang; } else u.lang = DATA.narration.language_code || 'en-US';
        u.rate = state.rate;
        const start = before;
        u.onboundary = (e) => { if (token === this.token) this.progress = (start + (e.charIndex || 0)) / total; };
        u.onend = () => { before = start + text.length + 1; this.progress = before / total; next(); };
        u.onerror = (e) => { if (e.error === 'interrupted' || e.error === 'canceled') return; before = start + text.length + 1; next(); };
        // some engines never fire onend: move on after a generous estimate
        this.watchdog = setTimeout(() => { if (token === this.token) { speechSynthesis.cancel(); before = start + text.length + 1; next(); } }, (text.split(/\s+/).length / (1.6 * state.rate) + 4) * 1000);
        speechSynthesis.speak(u);
      };
      speechSynthesis.cancel();
      setTimeout(next, 60); // Chrome drops a speak() issued in the same tick as cancel()
    },
    silent(beat, done) {
      this.mode = 'silent';
      this.dur = estimateSeconds(beat) * 1000;
      this.t0 = performance.now();
      this.timer = setTimeout(done, this.dur);
    },
    currentProgress() {
      if (this.mode === 'file' && this.audio.duration) return this.audio.currentTime / this.audio.duration;
      if (this.mode === 'silent') return clamp((performance.now() - this.t0) / this.dur, 0, 1);
      return this.progress;
    },
    pause() {
      if (this.mode === 'file' && !this.audio.paused) {
        this.audio.pause();
        this.mode = 'file-paused';
        return;
      }
      this.stop();
    },
    resume(beat, onEnd) {
      if (this.mode === 'file-paused' && this.pausedBeat === beat.id) {
        this.mode = 'file';
        this.audio.playbackRate = state.rate;
        this.audio.play().catch(() => this.play(beat, onEnd));
        return;
      }
      this.play(beat, onEnd);
    },
    stop() {
      this.token++;
      clearTimeout(this.timer);
      clearTimeout(this.watchdog);
      try { this.audio.pause(); } catch { /* ignore */ }
      if ('speechSynthesis' in window) speechSynthesis.cancel();
      this.mode = 'idle';
    },
  };

  function rankedVoices() {
    if (!('speechSynthesis' in window)) return [];
    const lang = (DATA.narration.language_code || 'en-US').toLowerCase();
    const base = lang.split('-')[0];
    const score = (v) => (v.lang.toLowerCase() === lang ? 4 : 0) + (/natural|neural|online|premium|enhanced|google/i.test(v.name) ? 2 : 0) + (v.default ? 0.5 : 0);
    return speechSynthesis.getVoices().filter((v) => v.lang.toLowerCase().startsWith(base)).sort((a, b) => score(b) - score(a));
  }
  function voiceFor(speaker) {
    const voices = narrator.voices;
    if (!voices.length) return null;
    const role = speaker === 'host' ? 'host' : speaker === 'expert' ? 'expert' : 'narrator';
    const saved = store.get(`voice:${role}`, null);
    const found = saved && voices.find((v) => v.name === saved);
    if (found) return found;
    if (role === 'host') return voices.find((v) => v !== voiceFor('expert')) || voices[0];
    return voices[0];
  }
  if ('speechSynthesis' in window) {
    const load = () => { narrator.voices = rankedVoices(); };
    load();
    speechSynthesis.addEventListener?.('voiceschanged', load);
  }

  // --------------------------------------------------------------- player
  function buildPlayer() {
    ui.playBtn = h('button', { class: 'play-btn', 'aria-label': 'Play', title: 'Play / pause (space)', onclick: togglePlay }, icon('play'));
    ui.prevBtn = h('button', { class: 'icon-btn', 'aria-label': 'Previous beat', title: 'Previous (←)', onclick: () => step(-1) }, icon('prev'));
    ui.nextBtn = h('button', { class: 'icon-btn', 'aria-label': 'Next beat', title: 'Next (→)', onclick: () => step(1) }, icon('next'));
    ui.nowLabel = h('span', { class: 'now' });
    ui.beatLabel = h('span', {});
    ui.timeline = h('div', { class: 'timeline', role: 'slider', 'aria-label': 'Position in the paper', tabindex: 0 });
    ui.speedBtn = h('button', { class: 'speed', title: 'Narration speed', onclick: cycleSpeed }, `${state.rate}×`);
    ui.voiceBtn = h('button', { class: 'icon-btn', 'aria-label': 'Voice on/off', title: 'Voice on/off (m)', onclick: toggleVoice });
    const bar = h('footer', { class: 'player' },
      h('div', { class: 'transport' }, ui.prevBtn, ui.playBtn, ui.nextBtn),
      h('div', { class: 'timeline-wrap' }, h('div', { class: 'timeline-meta' }, ui.nowLabel, ui.beatLabel), ui.timeline),
      h('div', { class: 'extras' }, ui.speedBtn, ui.voiceBtn));
    updateVoiceBtn();
    return bar;
  }

  function renderTimeline() {
    const view = state.readView;
    ui.tlSegs = SECTIONS.map((sec, i) => {
      const n = Math.max(1, DATA.views[view].sections[i].beats.length);
      const fill = h('div', { class: 'tl-fill' });
      const seg = h('div', { class: 'tl-seg', style: `flex:${n} 1 0`, title: `${i === 0 ? 'Overview' : `${i}.`} ${sec.title}` }, fill);
      seg.addEventListener('click', (ev) => {
        const r = seg.getBoundingClientRect();
        const frac = clamp((ev.clientX - r.left) / r.width, 0, 0.999);
        if (state.view === 'graph') setView(state.readView);
        goTo(i, Math.floor(frac * n));
      });
      return { seg, fill, n };
    });
    ui.timeline.replaceChildren(...ui.tlSegs.map((x) => x.seg));
    updateTimeline(0);
  }

  function updateTimeline(beatProgress) {
    if (!ui.tlSegs) return;
    ui.tlSegs.forEach((x, i) => {
      x.seg.classList.toggle('current', i === state.sIdx);
      x.seg.classList.toggle('done', i < state.sIdx);
      if (i === state.sIdx) x.fill.style.width = `${((state.bIdx + clamp(beatProgress, 0, 1)) / x.n) * 100}%`;
      else if (i > state.sIdx) x.fill.style.width = '0';
      else x.fill.style.width = '';
    });
    ui.timeline.setAttribute('aria-valuetext', `${SECTIONS[state.sIdx].title}, beat ${state.bIdx + 1}`);
  }

  function tick() {
    if (state.playing) updateTimeline(narrator.currentProgress());
    requestAnimationFrame(tick);
  }

  function updatePlayBtn() {
    ui.playBtn.replaceChildren(icon(state.playing ? 'pause' : 'play'));
    ui.playBtn.setAttribute('aria-label', state.playing ? 'Pause' : 'Play');
    if ('mediaSession' in navigator) navigator.mediaSession.playbackState = state.playing ? 'playing' : 'paused';
  }
  function updateVoiceBtn() {
    ui.voiceBtn.replaceChildren(icon(state.voiceOn ? 'volume' : 'mute'));
    ui.voiceBtn.setAttribute('aria-pressed', state.voiceOn ? 'true' : 'false');
  }

  function togglePlay() { if (state.playing) pause(); else play(); }
  function play() {
    if (state.mode !== 'read') setMode('read');
    if (state.view === 'graph') setView(state.readView);
    state.playing = true;
    updatePlayBtn();
    narrator.resume(currentBeat(), onBeatEnd);
  }
  function pause() {
    state.playing = false;
    narrator.pause();
    updatePlayBtn();
  }
  function onBeatEnd() {
    if (!state.playing) return;
    const sv = sectionView();
    if (state.bIdx + 1 < sv.beats.length) { goTo(state.sIdx, state.bIdx + 1, { auto: true }); return; }
    if (state.sIdx + 1 < SECTIONS.length) {
      if (!state.autoAdvance) { pause(); return; }
      goTo(state.sIdx + 1, 0, { auto: true });
      return;
    }
    finish();
  }
  function step(dir) {
    const sv = sectionView();
    const b = state.bIdx + dir;
    if (b >= sv.beats.length) {
      if (state.sIdx + 1 < SECTIONS.length) goTo(state.sIdx + 1, 0);
      else finish();
    } else if (b < 0) {
      if (state.sIdx > 0) goTo(state.sIdx - 1, sectionView(state.readView, state.sIdx - 1).beats.length - 1);
    } else goTo(state.sIdx, b);
  }
  function stepSection(dir) {
    const i = clamp(state.sIdx + dir, 0, SECTIONS.length - 1);
    if (i !== state.sIdx) goTo(i, 0);
  }

  function goTo(sIdx, bIdx, { auto = false } = {}) {
    const changed = sIdx !== state.sIdx;
    state.sIdx = sIdx;
    state.bIdx = clamp(bIdx, 0, sectionView().beats.length - 1);
    state.visited.add(SECTIONS[sIdx].id);
    if (changed) {
      state.askContext = null; // questions follow what is on screen
      if (state.sideTab === 'ask') ui.askPane.refresh();
    }
    if (changed || !diagramCtl) renderSection();
    renderBeat();
    if (state.playing) {
      narrator.stop();
      if (auto && changed) setTimeout(() => { if (state.playing) narrator.play(currentBeat(), onBeatEnd); }, 650);
      else narrator.play(currentBeat(), onBeatEnd);
    }
    updateRail();
    if (state.visited.size >= SECTIONS.length) refreshLock();
    persist();
  }

  function finish() {
    const first = !state.completed;
    state.playing = false;
    state.completed = true;
    narrator.stop();
    updatePlayBtn();
    persist();
    updateRail();
    refreshLock();
    if (first) showCompletion();
  }

  function cycleSpeed() {
    const speeds = [0.75, 1, 1.25, 1.5, 1.75, 2];
    const i = speeds.indexOf(state.rate);
    state.rate = speeds[(i + 1) % speeds.length] || 1;
    store.set('rate', state.rate);
    ui.speedBtn.textContent = `${state.rate}×`;
    if (narrator.mode === 'file') narrator.audio.playbackRate = state.rate;
    else if (state.playing) narrator.play(currentBeat(), onBeatEnd);
  }
  function toggleVoice() {
    state.voiceOn = !state.voiceOn;
    store.set('voiceOn', state.voiceOn);
    updateVoiceBtn();
    if (state.playing) narrator.play(currentBeat(), onBeatEnd);
  }

  function setMediaSession(sv) {
    if (!('mediaSession' in navigator) || !window.MediaMetadata) return;
    try {
      navigator.mediaSession.metadata = new MediaMetadata({ title: sv.title, artist: DATA.paper.title, album: 'PaperMap' });
      navigator.mediaSession.setActionHandler('play', play);
      navigator.mediaSession.setActionHandler('pause', pause);
      navigator.mediaSession.setActionHandler('previoustrack', () => step(-1));
      navigator.mediaSession.setActionHandler('nexttrack', () => step(1));
    } catch { /* unsupported action */ }
  }

  // ------------------------------------------------------------ side panel
  function buildSide() {
    ui.tabBtns = {
      narration: h('button', { class: 'tab', role: 'tab', onclick: () => selectTab('narration') }, 'Narration'),
      ask: h('button', { class: 'tab', role: 'tab', onclick: () => selectTab('ask') }, 'Ask'),
    };
    ui.tabs = h('div', { class: 'tabs', role: 'tablist' }, ui.tabBtns.narration, ui.tabBtns.ask);
    ui.sideBody = h('div', { class: 'side-body' });
    ui.transcript = h('div', {});
    ui.askPane = buildAskPane();
    ui.nodePane = h('div', { class: 'node-card' });
    ui.side.append(ui.tabs, ui.sideBody);
    selectTab('narration');
  }

  function selectTab(tab) {
    state.sideTab = tab;
    Object.entries(ui.tabBtns).forEach(([k, b]) => b.setAttribute('aria-selected', k === tab ? 'true' : 'false'));
    ui.sideBody.replaceChildren(tab === 'ask' ? ui.askPane.root : ui.transcript);
    if (tab === 'ask') ui.askPane.refresh();
  }

  function renderTranscript() {
    const sv = sectionView();
    ui.tbeatEls = sv.beats.map((b, i) => h('button', { class: `tbeat${i === state.bIdx ? ' current' : ''}`, onclick: () => goTo(state.sIdx, i) },
      b.speaker === 'host' || b.speaker === 'expert' ? h('span', { class: 'who' }, b.speaker) : null, b.narration));
    ui.transcript.replaceChildren(
      h('h2', {}, `${VIEW_LABEL[state.readView]} narration`),
      h('div', { class: 'tbeats' }, ui.tbeatEls),
      h('p', { class: 'side-note' }, 'Click a line to jump there. Switch views any time — your place is kept.'));
  }

  // ---------------------------------------------------------------- Q&A
  function readContext(withDiagram) {
    const sv = sectionView();
    const ctx = { view: state.readView, section_id: sv.section_id, label: sv.diagram && withDiagram ? `diagram “${sv.diagram.title}”` : `section “${sv.title}”` };
    if (withDiagram && sv.diagram) ctx.diagram_id = sv.diagram.id;
    return ctx;
  }

  function buildAskPane() {
    const ctxLine = h('div', { class: 'ask-context' });
    const msgs = h('div', { class: 'msgs', 'aria-live': 'polite' });
    const ta = h('textarea', { rows: 1, placeholder: 'Ask about this part of the paper…', 'aria-label': 'Your question' });
    const send = h('button', { class: 'btn', 'aria-label': 'Send' }, icon('send'));
    const form = h('form', { class: 'ask-form' }, ta, send);
    const offline = h('div', { class: 'offline' });
    const root = h('div', { class: 'ask' }, h('h2', {}, 'Ask about what you see'), ctxLine, offline, msgs, form);
    autoGrow(ta);
    ta.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); form.requestSubmit(); } });
    form.addEventListener('submit', (ev) => {
      ev.preventDefault();
      const q = ta.value.trim();
      if (!q) return;
      ta.value = '';
      ta.style.height = '';
      const ctx = state.askContext || readContext(true);
      const full = ctx.focusLabel ? `${q}\n(About: ${ctx.focusLabel})` : q;
      sendQuestion(full, ctx, state.askMsgs, msgs);
    });
    const refresh = () => {
      const ctx = state.askContext || readContext(true);
      ctxLine.replaceChildren('About:', h('span', { class: 'ctx-chip' }, ctx.label || 'this section'));
      offline.style.display = state.qaAvailable ? 'none' : '';
      offline.replaceChildren(...offlineMessage());
      form.style.display = state.qaAvailable ? '' : 'none';
    };
    return { root, refresh, ta, msgs };
  }

  function offlineMessage() {
    const viaFile = !/^https?:$/.test(location.protocol);
    return [
      h('strong', {}, 'Questions need the local server. '),
      viaFile ? 'This page was opened as a file. ' : 'The Q&A endpoint is not reachable. ',
      'Run ', h('code', {}, 'papermap serve <output folder>'), ' and open the page it prints.',
    ];
  }

  function openAsk(ctx) {
    state.askContext = ctx;
    selectTab('ask');
    ui.side.classList.add('open');
    setTimeout(() => ui.askPane.ta.focus(), 50);
  }

  function autoGrow(ta) {
    ta.addEventListener('input', () => { ta.style.height = 'auto'; ta.style.height = `${Math.min(140, ta.scrollHeight)}px`; });
  }

  async function checkServer() {
    if (!/^https?:$/.test(location.protocol)) { state.qaAvailable = false; return; }
    try {
      const r = await fetch('api/health', { cache: 'no-store' });
      const j = await r.json();
      state.qaAvailable = !!j.ok;
      state.qaInfo = j;
    } catch { state.qaAvailable = false; }
    ui.askPane.refresh();
    if (state.mode === 'qa') renderQA();
  }

  async function sendQuestion(question, ctx, history, container) {
    const userMsg = { role: 'user', content: question };
    history.push(userMsg);
    container.append(h('div', { class: 'msg user' }, question));
    const thinking = h('div', { class: 'msg thinking' }, 'Reading the paper…');
    container.append(thinking);
    container.scrollTop = container.scrollHeight;
    try {
      const r = await fetch('api/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question, context: { view: ctx.view, section_id: ctx.section_id, diagram_id: ctx.diagram_id, node_id: ctx.node_id }, history: history.slice(0, -1).slice(-6) }),
      });
      const j = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
      if (!r.ok || j.error) throw new Error(j.error || `HTTP ${r.status}`);
      history.push({ role: 'assistant', content: j.answer });
      thinking.replaceWith(h('div', { class: 'msg assistant' }, renderAnswer(j.answer)));
    } catch (err) {
      history.pop();
      thinking.replaceWith(h('div', { class: 'msg error' }, `Could not get an answer: ${err.message}`));
    }
    container.scrollTop = container.scrollHeight;
  }

  function secChip(id) {
    const i = SEC_INDEX[id];
    const sec = SECTIONS[i];
    return `<button class="ref" data-sec="${id}" title="${esc(sec.title)}">${i === 0 ? 'Overview' : `§${i}`}</button>`;
  }

  function renderAnswer(text) {
    const maths = [];
    const keep = (m) => { maths.push(m); return `\u0000${maths.length - 1}\u0000`; };
    const raw = String(text)
      .replace(/\\\[([\s\S]{1,400}?)\\\]/g, (_, m) => keep(m))
      .replace(/\\\(([\s\S]{1,240}?)\\\)/g, (_, m) => keep(m))
      .replace(/\$\$([^$]{1,400})\$\$/g, (_, m) => keep(m))
      .replace(/\$([^$\n]{1,240})\$/g, (_, m) => keep(m));
    let html = esc(raw)
      .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
      .replace(/`([^`\n]+)`/g, '<code>$1</code>')
      .replace(/\[((?:s\d+)(?:\s*[,;]\s*s\d+)*)\]/g, (_, g) => g.split(/\s*[,;]\s*/).map((id) => (id in SEC_INDEX ? secChip(id) : id)).join(''));
    html = html.split(/\n{2,}/).map((block) => {
      const lines = block.split('\n').filter((l) => l.trim());
      if (lines.length && lines.every((l) => /^\s*[-*•]\s+/.test(l))) return `<ul>${lines.map((l) => `<li>${l.replace(/^\s*[-*•]\s+/, '')}</li>`).join('')}</ul>`;
      return `<p>${lines.join('<br>')}</p>`;
    }).join('');
    html = html.replace(/\u0000(\d+)\u0000/g, (_, i) => `<span class="math" data-i="${i}"></span>`);
    const el = h('div', { html });
    const paintMath = () => el.querySelectorAll('.math').forEach((m) => renderMath(m, maths[+m.dataset.i], false));
    paintMath();
    if (maths.length && !window.katex) ensureKatex().then((k) => k && paintMath());
    el.querySelectorAll('.ref').forEach((b) => b.addEventListener('click', () => jumpToSection(b.dataset.sec)));
    return el;
  }

  function jumpToSection(id) {
    const i = SEC_INDEX[id];
    if (i == null) return;
    if (state.mode !== 'read') setMode('read');
    if (state.view === 'graph') setView(state.readView);
    goTo(i, 0);
  }

  function buildQAView() {
    ui.qaView = h('div', { class: 'qa' });
    ui.qaMsgs = h('div', { class: 'msgs', 'aria-live': 'polite' });
    ui.center.append(ui.qaView);
  }

  function renderQA() {
    ui.qaView.replaceChildren();
    if (!unlocked()) {
      const n = SECTIONS.filter((x) => state.visited.has(x.id)).length;
      ui.qaView.append(h('div', { class: 'lockscreen' },
        h('div', { class: 'big-lock' }, icon('lock')),
        h('h2', {}, 'Q&A opens after the paper'),
        h('p', {}, `Finish the guided read first — it makes the questions better. You have explored ${n} of ${SECTIONS.length} sections.`),
        h('button', { class: 'btn', onclick: () => { setMode('read'); play(); } }, icon('play'), 'Continue reading'),
        h('div', {}, h('button', { class: 'linklike', onclick: () => { state.forceUnlock = true; store.set('forceUnlock', true); refreshLock(); renderQA(); } }, 'Unlock anyway'))));
      return;
    }
    const ta = h('textarea', { rows: 1, placeholder: 'Ask anything about the paper, its methods, results or related work…', 'aria-label': 'Your question' });
    const form = h('form', { class: 'ask-form' }, ta, h('button', { class: 'btn' }, icon('send'), 'Ask'));
    autoGrow(ta);
    ta.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); form.requestSubmit(); } });
    const submit = (q) => {
      if (!q.trim()) return;
      suggest.remove();
      sendQuestion(q.trim(), { view: 'qa' }, state.chat, ui.qaMsgs);
    };
    form.addEventListener('submit', (ev) => { ev.preventDefault(); const q = ta.value; ta.value = ''; ta.style.height = ''; submit(q); });
    const suggest = h('div', { class: 'suggest' }, (DATA.qa.suggested_questions || []).map((q) => h('button', { onclick: () => submit(q) }, q)));
    ui.qaView.append(...[
      h('h2', {}, 'Ask the paper'),
      h('p', { class: 'lede' }, 'Answers stay grounded in the paper and its knowledge graph, with links back to the sections they come from.'),
      state.qaAvailable ? null : h('div', { class: 'offline' }, offlineMessage()),
      ui.qaMsgs,
      state.chat.length || !state.qaAvailable ? null : suggest,
      state.qaAvailable ? form : null].filter(Boolean));
    if (state.qaAvailable) setTimeout(() => ta.focus(), 50);
  }

  function refreshLock() {
    ui.qaLock.style.display = unlocked() ? 'none' : '';
    ui.modeBtns.qa.title = unlocked() ? 'Q&A (q)' : 'Q&A unlocks after you finish the paper';
  }

  // --------------------------------------------------------- graph view
  const graph = { built: false };

  function buildGraphView() {
    ui.graphView = h('div', { class: 'stage-wrap' },
      h('div', { class: 'stage-head' }, h('div', { class: 'titles' }, h('div', { class: 'kicker' }, 'State of the art'), h('h1', { class: 'stage-title' }, 'How this paper connects'))),
      (ui.graphStage = h('div', { class: 'stage' })));
    ui.center.append(ui.graphView);
  }

  function layoutGraph() {
    const N = NODES.length;
    const deg = Object.fromEntries(NODES.map((n) => [n.id, 0]));
    EDGES.forEach((e) => { deg[e.source]++; deg[e.target]++; });
    const radius = (n) => (n.id === DATA.graph.center ? 24 : 8 + Math.min(12, Math.sqrt(deg[n.id] * 2 + (n.mentions || 1)) * 2.4));
    const types = Object.keys(TYPE_SLOT);
    const others = NODES.filter((n) => n.id !== DATA.graph.center)
      .sort((a, b) => types.indexOf(a.type) - types.indexOf(b.type) || a.label.localeCompare(b.label));
    const P = {};
    P[DATA.graph.center] = { x: 0, y: 0 };
    others.forEach((n, i) => {
      const a = (i / Math.max(1, others.length)) * Math.PI * 2;
      const R = 200 + (i % 3) * 45;
      P[n.id] = { x: Math.cos(a) * R, y: Math.sin(a) * R };
    });
    const ids = NODES.map((n) => n.id);
    const r = Object.fromEntries(NODES.map((n) => [n.id, radius(n)]));
    const k = Math.sqrt((1000 * 760) / Math.max(N, 1)) * 0.85;
    const iters = 420;
    for (let it = 0; it < iters; it++) {
      const temp = 70 * (1 - it / iters) + 0.5;
      const disp = Object.fromEntries(ids.map((id) => [id, { x: 0, y: 0 }]));
      for (let i = 0; i < N; i++) {
        for (let j = i + 1; j < N; j++) {
          const a = ids[i];
          const b = ids[j];
          let dx = P[a].x - P[b].x;
          let dy = P[a].y - P[b].y;
          let d = Math.hypot(dx, dy);
          if (d < 0.01) { dx = 0.1 * (i - j); dy = 0.1; d = Math.hypot(dx, dy); }
          let f = (k * k) / d;
          const minD = r[a] + r[b] + 34;
          if (d < minD) f += (minD - d) * 3;
          disp[a].x += (dx / d) * f; disp[a].y += (dy / d) * f;
          disp[b].x -= (dx / d) * f; disp[b].y -= (dy / d) * f;
        }
      }
      EDGES.forEach((e) => {
        const dx = P[e.source].x - P[e.target].x;
        const dy = P[e.source].y - P[e.target].y;
        const d = Math.max(0.01, Math.hypot(dx, dy));
        const f = ((d * d) / k) * 0.9;
        disp[e.source].x -= (dx / d) * f; disp[e.source].y -= (dy / d) * f;
        disp[e.target].x += (dx / d) * f; disp[e.target].y += (dy / d) * f;
      });
      ids.forEach((id) => {
        if (id === DATA.graph.center) return;
        const g = deg[id] ? 0.015 : 0.05;
        disp[id].x -= P[id].x * g * k * 0.1;
        disp[id].y -= P[id].y * g * k * 0.1;
        const len = Math.hypot(disp[id].x, disp[id].y) || 1;
        const mv = Math.min(len, temp);
        P[id].x += (disp[id].x / len) * mv;
        P[id].y += (disp[id].y / len) * mv * 0.85; // slightly wider than tall
      });
    }
    return { P, r, deg };
  }

  function buildGraph() {
    graph.built = true;
    const host = h('div', { class: 'graph-wrap' });
    ui.graphStage.append(host);
    graph.host = host;
    if (!NODES.length) {
      host.append(h('div', { class: 'stage-body' }, h('p', { class: 'chart-note' }, 'No knowledge graph was extracted for this paper.')));
      return;
    }
    const { P, r, deg } = layoutGraph();
    graph.P = P;
    graph.r = r;
    graph.deg = deg;
    const svg = s('svg', { role: 'img', 'aria-label': 'Knowledge graph of the paper and related work' });
    const vp = s('g', {});
    svg.append(s('defs', {}, s('marker', { id: 'kg-arrow', viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 6, markerHeight: 6, orient: 'auto' }, s('path', { d: 'M0,0 L10,5 L0,10 z', class: 'arrowhead' }))), vp);
    const gE = s('g', {});
    const gN = s('g', {});
    vp.append(gE, gN);
    graph.edgeEls = EDGES.map((e) => {
      const line = s('line', { 'marker-end': 'url(#kg-arrow)' });
      const text = s('text', { 'text-anchor': 'middle' }, REL_LABEL(e.relation));
      const g = s('g', { class: 'gedge' }, line, text);
      g.append(s('title', {}, `${NODE_BY_ID[e.source].label} — ${REL_LABEL(e.relation)} → ${NODE_BY_ID[e.target].label}${e.description ? `: ${e.description}` : ''}`));
      gE.append(g);
      return { g, line, text, e };
    });
    graph.nodeEls = new Map();
    NODES.forEach((n) => {
      const slot = TYPE_SLOT[n.type] ?? 0;
      const lbl = n.label.length > 28 ? `${n.label.slice(0, 26)}…` : n.label;
      const g = s('g', { class: `gnode${n.id === DATA.graph.center ? ' center' : ''}`, tabindex: 0, role: 'button', 'aria-label': `${n.label} (${TYPE_LABEL[n.type] || n.type})` },
        s('circle', { class: 'ring', r: r[n.id] + 5 }),
        s('circle', { r: r[n.id], fill: slotColor(slot) }),
        s('text', { y: r[n.id] + 15, 'text-anchor': 'middle' }, lbl));
      g.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); selectNode(n.id); } });
      graph.nodeEls.set(n.id, g);
      gN.append(g);
    });
    host.append(svg);
    graph.svg = svg;
    positionGraph();
    fitGraph();
    enableGraphInteractions(svg);

    // toolbar: search + type legend + fit
    const types = [...new Set(NODES.map((n) => n.type))].sort((a, b) => Object.keys(TYPE_SLOT).indexOf(a) - Object.keys(TYPE_SLOT).indexOf(b));
    graph.hidden = new Set();
    const search = h('input', { class: 'graph-search', type: 'search', placeholder: 'Find a concept…', 'aria-label': 'Find a node' });
    search.addEventListener('input', () => highlightSearch(search.value));
    const chips = types.map((t) => {
      const count = NODES.filter((n) => n.type === t).length;
      const b = h('button', { class: 'legend-chip', 'aria-pressed': 'true', title: 'Show / hide' }, h('i', { style: `background:${slotColor(TYPE_SLOT[t] ?? 0)}` }), `${TYPE_LABEL[t] || t} ${count}`);
      b.addEventListener('click', () => {
        if (graph.hidden.has(t)) graph.hidden.delete(t); else graph.hidden.add(t);
        b.setAttribute('aria-pressed', graph.hidden.has(t) ? 'false' : 'true');
        applyHidden();
      });
      return b;
    });
    const fit = h('button', { class: 'legend-chip', title: 'Fit to screen', onclick: fitGraph }, icon('fit'));
    fit.firstChild.style.width = '14px';
    fit.firstChild.style.height = '14px';
    host.append(h('div', { class: 'graph-toolbar' }, search, chips, fit),
      h('div', { class: 'graph-hint' }, 'Ringed nodes appear in the section you are on · drag to pan · scroll to zoom · click a node'));
  }

  function positionGraph() {
    const { P, r } = graph;
    graph.nodeEls.forEach((g, id) => g.setAttribute('transform', `translate(${P[id].x},${P[id].y})`));
    graph.edgeEls.forEach(({ line, text, e }) => {
      const a = P[e.source];
      const b = P[e.target];
      const d = Math.max(1, Math.hypot(b.x - a.x, b.y - a.y));
      const ux = (b.x - a.x) / d;
      const uy = (b.y - a.y) / d;
      const x1 = a.x + ux * r[e.source];
      const y1 = a.y + uy * r[e.source];
      const x2 = b.x - ux * (r[e.target] + 3);
      const y2 = b.y - uy * (r[e.target] + 3);
      line.setAttribute('x1', x1); line.setAttribute('y1', y1); line.setAttribute('x2', x2); line.setAttribute('y2', y2);
      text.setAttribute('x', (x1 + x2) / 2); text.setAttribute('y', (y1 + y2) / 2 - 4);
    });
  }

  function fitGraph() {
    const { P, r } = graph;
    if (!P) return;
    let minX = Infinity; let minY = Infinity; let maxX = -Infinity; let maxY = -Infinity;
    NODES.forEach((n) => {
      if (graph.hidden?.has(n.type)) return;
      const p = P[n.id];
      const lw = Math.min(28, n.label.length) * 3.6;
      minX = Math.min(minX, p.x - Math.max(r[n.id], lw)); maxX = Math.max(maxX, p.x + Math.max(r[n.id], lw));
      minY = Math.min(minY, p.y - r[n.id]); maxY = Math.max(maxY, p.y + r[n.id] + 22);
    });
    const pad = 40;
    graph.vb = { x: minX - pad, y: minY - pad - 40, w: maxX - minX + 2 * pad, h: maxY - minY + 2 * pad + 40 };
    applyViewBox();
  }
  function applyViewBox() {
    const { x, y, w, h: hh } = graph.vb;
    graph.svg.setAttribute('viewBox', `${x} ${y} ${w} ${hh}`);
    // keep labels readable at any zoom: font size in screen pixels, not graph units
    const rect = graph.svg.getBoundingClientRect();
    if (rect.width > 0) {
      const scale = Math.max(w / rect.width, hh / rect.height);
      graph.svg.style.setProperty('--kg-font', `${clamp(12 * scale, 6, 60)}px`);
      graph.svg.style.setProperty('--kg-font-center', `${clamp(14 * scale, 7, 70)}px`);
    }
  }

  function enableGraphInteractions(svg) {
    const toSvg = (cx, cy) => {
      const rect = svg.getBoundingClientRect();
      const { vb } = graph;
      const scale = Math.max(vb.w / rect.width, vb.h / rect.height);
      const ox = vb.x - (rect.width * scale - vb.w) / 2;
      const oy = vb.y - (rect.height * scale - vb.h) / 2;
      return { x: ox + (cx - rect.left) * scale, y: oy + (cy - rect.top) * scale, scale };
    };
    svg.addEventListener('wheel', (ev) => {
      ev.preventDefault();
      const p = toSvg(ev.clientX, ev.clientY);
      const f = ev.deltaY > 0 ? 1.12 : 1 / 1.12;
      const { vb } = graph;
      const nw = clamp(vb.w * f, 120, 8000);
      const ratio = nw / vb.w;
      graph.vb = { x: p.x - (p.x - vb.x) * ratio, y: p.y - (p.y - vb.y) * ratio, w: nw, h: vb.h * ratio };
      applyViewBox();
    }, { passive: false });
    let drag = null;
    svg.addEventListener('pointerdown', (ev) => {
      const nodeEl = ev.target.closest('.gnode');
      const id = nodeEl ? [...graph.nodeEls].find(([, g]) => g === nodeEl)?.[0] : null;
      const p = toSvg(ev.clientX, ev.clientY);
      drag = { id, start: { x: ev.clientX, y: ev.clientY }, p, vb: { ...graph.vb }, moved: false };
      svg.setPointerCapture(ev.pointerId);
      svg.classList.add('dragging');
    });
    svg.addEventListener('pointermove', (ev) => {
      if (!drag) return;
      const dx = ev.clientX - drag.start.x;
      const dy = ev.clientY - drag.start.y;
      if (Math.hypot(dx, dy) > 4) drag.moved = true;
      if (!drag.moved) return;
      if (drag.id) {
        const p = toSvg(ev.clientX, ev.clientY);
        graph.P[drag.id] = { x: p.x, y: p.y };
        positionGraph();
      } else {
        const rect = svg.getBoundingClientRect();
        const scale = Math.max(drag.vb.w / rect.width, drag.vb.h / rect.height);
        graph.vb = { ...drag.vb, x: drag.vb.x - dx * scale, y: drag.vb.y - dy * scale };
        applyViewBox();
      }
    });
    const end = () => {
      if (!drag) return;
      if (!drag.moved) { if (drag.id) selectNode(drag.id); else selectNode(null); }
      drag = null;
      svg.classList.remove('dragging');
    };
    svg.addEventListener('pointerup', end);
    svg.addEventListener('pointercancel', end);
  }

  function applyHidden() {
    graph.nodeEls.forEach((g, id) => g.classList.toggle('hidden', graph.hidden.has(NODE_BY_ID[id].type)));
    graph.edgeEls.forEach(({ g, e }) => g.classList.toggle('hidden', graph.hidden.has(NODE_BY_ID[e.source].type) || graph.hidden.has(NODE_BY_ID[e.target].type)));
  }

  function highlightSearch(q) {
    const term = q.trim().toLowerCase();
    if (!term) { applyGraphHighlight(); return; }
    const hits = new Set(NODES.filter((n) => [n.label, ...(n.aliases || [])].some((x) => x.toLowerCase().includes(term))).map((n) => n.id));
    graph.host.classList.toggle('dim', true);
    graph.nodeEls.forEach((g, id) => g.classList.toggle('hl', hits.has(id)));
    graph.edgeEls.forEach(({ g }) => g.classList.remove('hl'));
  }

  function applyGraphHighlight() {
    if (!graph.built || !graph.nodeEls) return;
    const secId = SECTIONS[state.sIdx].id;
    graph.nodeEls.forEach((g, id) => g.classList.toggle('in-section', (NODE_BY_ID[id].sections || []).includes(secId)));
    const sel = state.selectedNode;
    if (!sel) {
      graph.host.classList.remove('dim');
      graph.nodeEls.forEach((g) => g.classList.remove('hl', 'sel'));
      graph.edgeEls.forEach(({ g }) => g.classList.remove('hl'));
      return;
    }
    const nb = new Set([sel]);
    graph.edgeEls.forEach(({ g, e }) => {
      const on = e.source === sel || e.target === sel;
      g.classList.toggle('hl', on);
      if (on) { nb.add(e.source); nb.add(e.target); }
    });
    graph.host.classList.add('dim');
    graph.host.classList.toggle('many', nb.size > 9);
    graph.nodeEls.forEach((g, id) => { g.classList.toggle('hl', nb.has(id)); g.classList.toggle('sel', id === sel); });
  }

  function selectNode(id) {
    state.selectedNode = id;
    applyGraphHighlight();
    renderNodePane();
    if (state.view === 'graph' && ui.sideBody.firstChild !== ui.nodePane) { // back from an open Ask pane
      ui.tabs.style.display = 'none';
      ui.sideBody.replaceChildren(ui.nodePane);
    }
    if (id && innerWidth <= 1180) ui.side.classList.add('open');
  }

  function renderNodePane() {
    const pane = ui.nodePane;
    pane.replaceChildren();
    const id = state.selectedNode;
    if (!id) {
      const counts = {};
      NODES.forEach((n) => { counts[n.type] = (counts[n.type] || 0) + 1; });
      const top = [...NODES].filter((n) => n.id !== DATA.graph.center).sort((a, b) => (graph.deg?.[b.id] || 0) - (graph.deg?.[a.id] || 0)).slice(0, 8);
      pane.append(
        h('h2', {}, 'Knowledge graph'),
        h('p', {}, `${NODES.length} entities and ${EDGES.length} relations extracted from the paper: the prior work, models, datasets, tasks and concepts it builds on or compares with.`),
        h('h4', {}, 'Most connected'),
        h('ul', { class: 'rel-list' }, top.map((n) => h('li', {}, h('button', { onclick: () => selectNode(n.id) }, h('span', { class: 'rel' }, TYPE_LABEL[n.type] || n.type), n.label)))),
        h('p', { class: 'side-note' }, 'Select a node to see how it relates to the paper and jump to the sections that discuss it.'));
      return;
    }
    const n = NODE_BY_ID[id];
    const rels = EDGES.filter((e) => e.source === id || e.target === id);
    pane.append(...[
      h('span', { class: 'badge' }, h('i', { style: `background:${slotColor(TYPE_SLOT[n.type] ?? 0)}` }), TYPE_LABEL[n.type] || n.type),
      h('h3', { style: 'margin-top:8px' }, n.label),
      n.aliases?.length ? h('div', { class: 'side-note', style: 'margin:0 0 6px' }, `Also: ${n.aliases.join(', ')}`) : null,
      n.description ? h('p', {}, n.description) : null,
      n.evidence ? h('blockquote', { class: 'quote' }, `“${n.evidence}”`, h('cite', {}, 'From the paper')) : null,
      rels.length ? h('h4', {}, 'Relations') : null,
      rels.length ? h('ul', { class: 'rel-list' }, rels.map((e) => {
        const out = e.source === id;
        const other = NODE_BY_ID[out ? e.target : e.source];
        const phrase = `${NODE_BY_ID[e.source].label} ${REL_LABEL(e.relation)} ${NODE_BY_ID[e.target].label}`;
        return h('li', { class: 'rel-row' },
          h('button', { onclick: () => selectNode(other.id), title: e.description || '' },
            h('span', { class: 'rel' }, out ? `${REL_LABEL(e.relation)} →` : `← ${REL_LABEL(e.relation)}`), other.label),
          h('button', { class: 'rel-ask', 'aria-label': `Ask about: ${phrase}`, title: 'Ask about this relationship', onclick: () => openAskRelation(n, e, phrase) }, icon('chat')));
      })) : null,
      n.sections?.length ? h('h4', {}, 'Discussed in') : null,
      n.sections?.length ? h('div', { class: 'sec-chips' }, n.sections.filter((sid) => sid in SEC_INDEX).map((sid) => h('button', { class: 'chip-btn', onclick: () => jumpToSection(sid) }, SEC_INDEX[sid] === 0 ? 'Overview' : `§${SEC_INDEX[sid]} ${SECTIONS[SEC_INDEX[sid]].title}`))) : null,
      h('div', { style: 'margin-top:16px' }, h('button', { class: 'btn', onclick: () => openAskNode(n) }, icon('chat'), 'Ask about this'))].filter(Boolean));
  }

  function openAskRelation(n, e, phrase) {
    openAskNode(n, { section_id: e.sections?.[0] || n.sections?.[0], label: `relation “${phrase}”`, focusLabel: `the relation “${phrase}”` });
  }

  function openAskNode(n, extra = {}) {
    state.askContext = { view: 'graph', section_id: n.sections?.[0], node_id: n.id, label: `“${n.label}”`, focusLabel: n.label, ...extra };
    if (state.view === 'graph') {
      ui.tabs.style.display = '';
      selectTab('ask');
      ui.side.classList.add('open');
      setTimeout(() => ui.askPane.ta.focus(), 50);
    } else openAsk(state.askContext);
  }

  // ------------------------------------------------------- view / mode
  function setView(v) {
    if (v === state.view && state.mode === 'read') return;
    if (state.mode !== 'read') setMode('read', { silent: true });
    if (v === 'graph') {
      state.view = 'graph';
      if (state.playing) pause();
    } else {
      // keep the position: same section, proportional beat
      const oldN = sectionView(state.readView).beats.length;
      const frac = oldN > 1 ? state.bIdx / (oldN - 1) : 0;
      state.view = v;
      state.readView = v;
      const newN = sectionView(v).beats.length;
      state.bIdx = Math.round(frac * (newN - 1));
    }
    applyLayout();
    if (state.view !== 'graph') {
      renderSection();
      renderBeat(false);
      if (state.playing) narrator.play(currentBeat(), onBeatEnd);
    } else {
      enterGraph();
    }
    persist();
  }

  function enterGraph() {
    if (!graph.built) buildGraph();
    applyGraphHighlight();
    renderNodePane();
    if (!ui.tlSegs) renderTimeline();
    ui.nowLabel.textContent = `Knowledge graph · ringed: concepts from “${SECTIONS[state.sIdx].title}”`;
    ui.beatLabel.textContent = '';
  }

  function setMode(mode, { silent = false } = {}) {
    state.mode = mode;
    if (mode === 'qa' && state.playing) pause();
    applyLayout();
    if (mode === 'qa') renderQA();
    else if (!silent && state.view !== 'graph') { renderSection(); renderBeat(false); }
  }

  function applyLayout() {
    const qa = state.mode === 'qa';
    const g = state.view === 'graph' && !qa;
    ui.readView.style.display = !qa && !g ? '' : 'none';
    ui.graphView.style.display = g ? '' : 'none';
    ui.qaView.style.display = qa ? '' : 'none';
    ui.main.classList.toggle('qa-mode', qa);
    Object.entries(ui.viewBtns).forEach(([k, b]) => b.setAttribute('aria-pressed', !qa && state.view === k ? 'true' : 'false'));
    Object.entries(ui.modeBtns).forEach(([k, b]) => b.setAttribute('aria-pressed', state.mode === k ? 'true' : 'false'));
    // side panel content
    if (g) {
      ui.tabs.style.display = 'none';
      ui.sideBody.replaceChildren(ui.nodePane);
    } else if (!qa) {
      ui.tabs.style.display = '';
      selectTab(state.sideTab);
    }
    updateRail();
  }

  // ------------------------------------------------------------- extras
  function applyTheme(theme) {
    if (theme) document.documentElement.setAttribute('data-theme', theme);
    else document.documentElement.removeAttribute('data-theme');
    const dark = theme ? theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
    ui.themeBtn.replaceChildren(icon(dark ? 'sun' : 'moon'));
  }
  function toggleTheme() {
    const cur = document.documentElement.getAttribute('data-theme');
    const dark = cur ? cur === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
    const next = dark ? 'light' : 'dark';
    store.set('theme', next);
    applyTheme(next);
  }

  let settingsEl = null;
  function toggleSettings() {
    if (settingsEl) { settingsEl.remove(); settingsEl = null; return; }
    const voices = narrator.voices;
    const browser = DATA.narration.provider === 'browser';
    const roles = Object.values(DATA.views).some((v) => v.sections.some((sv) => sv.beats.some((b) => b.speaker === 'host'))) ? ['host', 'expert'] : ['narrator'];
    const voiceSelect = (role) => {
      const sel = h('select', {}, voices.map((v) => h('option', { value: v.name }, `${v.name} (${v.lang})`)));
      const cur = voiceFor(role);
      if (cur) sel.value = cur.name;
      sel.addEventListener('change', () => { store.set(`voice:${role}`, sel.value); if (state.playing) narrator.play(currentBeat(), onBeatEnd); });
      return h('label', {}, `${role === 'narrator' ? 'Voice' : `${role[0].toUpperCase()}${role.slice(1)} voice`}`, sel);
    };
    const auto = h('input', { type: 'checkbox' });
    auto.checked = state.autoAdvance;
    auto.addEventListener('change', () => { state.autoAdvance = auto.checked; store.set('autoAdvance', auto.checked); });
    const voiceToggle = h('input', { type: 'checkbox' });
    voiceToggle.checked = state.voiceOn;
    voiceToggle.addEventListener('change', () => { if (voiceToggle.checked !== state.voiceOn) toggleVoice(); });
    settingsEl = h('div', { class: 'popover settings', role: 'dialog', 'aria-label': 'Narration settings' },
      h('h4', {}, 'Narration'),
      h('label', { class: 'toggle' }, 'Voice', voiceToggle),
      h('label', { class: 'toggle' }, 'Continue to the next section', auto),
      browser && voices.length ? roles.map(voiceSelect) : null,
      browser && !voices.length ? h('p', {}, 'No speech voices are available in this browser; captions will advance on their own.') : null,
      !browser && DATA.narration.provider !== 'none' ? h('p', {}, `Narration audio was generated with ${DATA.narration.provider}.`) : null,
      h('p', { style: 'margin:6px 0 0;font-size:12px;color:var(--text-muted)' }, `Generated by ${DATA.generator} · ${DATA.fingerprint}`));
    app.append(settingsEl);
  }
  document.addEventListener('click', (ev) => {
    if (settingsEl && !settingsEl.contains(ev.target) && !ui.settingsBtn.contains(ev.target)) { settingsEl.remove(); settingsEl = null; }
  });

  function showOverlay(content) {
    const ov = h('div', { class: 'overlay', role: 'dialog', 'aria-modal': 'true' }, content);
    ov.addEventListener('click', (ev) => { if (ev.target === ov) ov.remove(); });
    document.body.append(ov);
    const close = () => ov.remove();
    content.querySelector('button')?.focus();
    return close;
  }
  function showHelp() {
    const keys = [['Space', 'Play / pause'], ['← →', 'Previous / next beat'], ['[ ]', 'Previous / next section'], ['1 2 3', 'High-level · Deep dive · Graph'], ['q', 'Q&A mode'], ['m', 'Voice on / off'], ['Esc', 'Close panels']];
    let close;
    const dlg = h('div', { class: 'dialog' }, h('h2', {}, 'Shortcuts'),
      h('div', { class: 'kbd-list' }, keys.map(([k, v]) => [h('span', {}, k.split(' ').map((x) => h('kbd', {}, x)).reduce((acc, el) => (acc.length ? [...acc, ' ', el] : [el]), [])), h('span', {}, v)])),
      h('div', { class: 'row' }, h('button', { class: 'btn', onclick: () => close() }, 'Got it')));
    close = showOverlay(dlg);
  }
  function showCompletion() {
    let close;
    const dlg = h('div', { class: 'dialog' },
      h('h2', {}, 'That’s the paper.'),
      h('p', {}, 'Q&A mode is now unlocked: ask about methods, results, related work or any diagram — answers link back to the sections they come from.'),
      h('div', { class: 'row' },
        h('button', { class: 'btn', onclick: () => { close(); setMode('qa'); } }, icon('chat'), 'Open Q&A'),
        h('button', { class: 'btn ghost', onclick: () => { close(); setView('graph'); } }, 'Explore the graph'),
        h('button', { class: 'btn ghost', onclick: () => { close(); setView(state.readView === 'high' ? 'deep' : 'high'); goTo(0, 0); } }, state.readView === 'high' ? 'Watch the deep dive' : 'Watch the high-level')));
    close = showOverlay(dlg);
  }

  document.addEventListener('keydown', (ev) => {
    const tag = (ev.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select' || ev.target.isContentEditable) {
      if (ev.key === 'Escape') ev.target.blur();
      return;
    }
    if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
    switch (ev.key) {
      case ' ': ev.preventDefault(); togglePlay(); break;
      case 'ArrowRight': if (state.view !== 'graph' && state.mode === 'read') { ev.preventDefault(); step(1); } break;
      case 'ArrowLeft': if (state.view !== 'graph' && state.mode === 'read') { ev.preventDefault(); step(-1); } break;
      case ']': stepSection(1); break;
      case '[': stepSection(-1); break;
      case '1': setView('high'); break;
      case '2': setView('deep'); break;
      case '3': setView('graph'); break;
      case 'q': setMode(state.mode === 'qa' ? 'read' : 'qa'); break;
      case 'm': toggleVoice(); break;
      case '?': showHelp(); break;
      case 'Escape':
        document.querySelector('.overlay')?.remove();
        hidePopover();
        ui.side.classList.remove('open');
        ui.rail.classList.remove('open');
        if (settingsEl) { settingsEl.remove(); settingsEl = null; }
        break;
      default:
    }
  });

  // diagrams are laid out for the stage's shape: redo it when the window changes
  let resizeTimer = null;
  addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (state.mode !== 'read') return;
      if (state.view === 'graph') { if (graph.svg) applyViewBox(); return; }
      renderSection();
      renderBeat(false);
    }, 250);
  });

  // ---------------------------------------------------------------- init
  buildShell();
  applyHash();
  state.bIdx = clamp(state.bIdx, 0, sectionView().beats.length - 1);
  state.visited.add(SECTIONS[state.sIdx].id);
  applyLayout();
  if (state.view === 'graph') {
    enterGraph();
  } else {
    renderSection();
    renderBeat(false);
  }
  if (state.mode === 'qa') renderQA();
  refreshLock();
  updatePlayBtn();
  requestAnimationFrame(tick);
  checkServer();
  if (Object.values(DATA.views).some((v) => v.sections.some((sv) => sv.diagram && sv.diagram.type === 'equation'))) ensureKatex();
})();
