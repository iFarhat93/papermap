/* PaperMap dashboard: plain JS, no build step. Talks to papermap/ui/server.py. */
(() => {
  'use strict';

  // ------------------------------------------------------------------ helpers
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else if (k === 'class') el.className = v;
      else if (k === 'value') el.value = v;
      else if (k === 'checked') el.checked = !!v;
      else el.setAttribute(k, v === true ? '' : v);
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid == null || kid === false) continue;
      el.append(kid instanceof Node ? kid : String(kid));
    }
    return el;
  }

  // replaceChildren() that skips null/false (conditional parts of a screen)
  function fill(el, ...kids) {
    el.replaceChildren(...kids.flat(Infinity).filter((k) => k != null && k !== false));
    return el;
  }

  const OFFLINE = 'The dashboard is not running. Start it again with papermap ui in your terminal, then reload this page.';
  async function api(path, opts = {}) {
    let r;
    try {
      r = await fetch(path, { cache: 'no-store', ...opts });
    } catch {
      throw new Error(OFFLINE); // the server is gone: fetch rejects without a response
    }
    const data = await r.json().catch(() => ({}));
    if (r.status === 401) throw new Error('This tab lost access. Open the link printed by papermap ui.');
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    return data;
  }
  const post = (path, body) => api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });

  let toastTimer = null;
  function toast(msg, bad = false) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.className = bad ? 'toast bad' : 'toast';
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, bad ? 7000 : 3500);
  }
  const fail = (e) => toast(e.message || String(e), true);

  const store = {
    get(k, d) { try { const v = localStorage.getItem(`papermap-ui:${k}`); return v == null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem(`papermap-ui:${k}`, JSON.stringify(v)); } catch { /* storage off */ } },
  };

  function fmtDur(s) {
    if (s == null) return '';
    s = Math.round(s);
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ${String(s % 60).padStart(2, '0')}s`;
    return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`;
  }
  function fmtAgo(ts) {
    if (!ts) return '';
    const d = Date.now() / 1000 - ts;
    if (d < 60) return 'just now';
    if (d < 3600) return `${Math.round(d / 60)} min ago`;
    if (d < 86400) return `${Math.round(d / 3600)} h ago`;
    return new Date(ts * 1000).toLocaleDateString();
  }
  function fmtBytes(n) {
    if (!n) return '0 B';
    const u = ['B', 'KB', 'MB', 'GB'];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i += 1; }
    return `${n.toFixed(i && n < 10 ? 1 : 0)} ${u[i]}`;
  }
  const fmtCost = (c) => (c == null ? '' : c === 0 ? 'free' : c < 0.01 ? '<$0.01' : `$${c.toFixed(2)}`);
  const fmtTok = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(n || 0));
  const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;
  const providerLabel = (p) => (S.settings && S.settings.providers[p] ? S.settings.providers[p].label : p);

  // inline "Are you sure?" instead of a dialog
  function confirmButton(label, question, action, cls = 'sm ghost danger') {
    const wrap = h('span', {});
    const show = () => fill(wrap, btn);
    const btn = h('button', { class: cls, onclick: () => fill(wrap, 
      h('span', { class: 'confirm' }, question,
        h('button', { class: 'sm danger', onclick: async () => { await action(); show(); } }, 'Yes'),
        h('button', { class: 'sm ghost', onclick: show }, 'No')),
    ) }, label);
    show();
    return wrap;
  }

  // ------------------------------------------------------------------- state
  const S = {
    screen: 'library',
    projects: [],
    runs: [],
    settings: null,
    state: null,
    models: {}, // "provider|base_url" -> {models, error}
    prevRunStates: null,
    openLogs: new Set(),
    query: '',
    hits: null,
  };
  const main = document.getElementById('main');
  let current = null; // the mounted screen

  // ------------------------------------------------------------------ polling
  async function refresh() {
    try {
      const [projects, runs] = await Promise.all([api('/api/projects'), api('/api/runs')]);
      S.projects = projects;
      noticeTransitions(runs);
      S.runs = runs;
      updateNav();
      if (current && current.update) current.update();
    } catch (e) {
      // say it once, not on every poll
      if (e.message !== S.lastPollError) fail(e);
      S.lastPollError = e.message;
      setTimeout(refresh, 5000);
      return;
    }
    if (S.lastPollError) toast('Connected to the dashboard again.');
    S.lastPollError = null;
    const busy = S.runs.some((r) => r.state === 'running' || r.state === 'queued');
    setTimeout(refresh, busy ? 2000 : 6000);
  }

  function updateNav() {
    document.getElementById('n-library').textContent = S.projects.length || '';
    const active = S.runs.filter((r) => r.state === 'running' || r.state === 'queued').length;
    const b = document.getElementById('n-runs');
    b.textContent = active;
    b.hidden = !active;
  }

  function noticeTransitions(runs) {
    const prev = S.prevRunStates;
    S.prevRunStates = Object.fromEntries(runs.map((r) => [r.id, r.state]));
    if (!prev) return;
    for (const r of runs) {
      if (prev[r.id] !== 'running' || (r.state !== 'done' && r.state !== 'failed')) continue;
      const ok = r.state === 'done';
      const title = ok ? `Ready: ${r.label}` : `Failed: ${r.label}`;
      const body = ok ? `Generated with ${r.model} in ${fmtDur(r.elapsed)}.` : (r.error || 'The run stopped.');
      toast(`${title}. ${body}`, !ok);
      if (store.get('notify', false)) {
        try {
          if ('Notification' in window && Notification.permission === 'granted') new Notification(title, { body });
        } catch { /* not supported */ }
        chime(ok);
      }
    }
  }

  function chime(ok) {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const notes = ok ? [660, 880] : [440, 330];
      notes.forEach((f, i) => {
        const o = ctx.createOscillator();
        const g = ctx.createGain();
        o.frequency.value = f;
        g.gain.setValueAtTime(0.0001, ctx.currentTime + i * 0.18);
        g.gain.exponentialRampToValueAtTime(0.15, ctx.currentTime + i * 0.18 + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + i * 0.18 + 0.25);
        o.connect(g).connect(ctx.destination);
        o.start(ctx.currentTime + i * 0.18);
        o.stop(ctx.currentTime + i * 0.18 + 0.3);
      });
    } catch { /* no audio */ }
  }

  async function loadSettings() {
    S.settings = await api('/api/settings');
    renderFoot();
    return S.settings;
  }

  function renderFoot() {
    const st = S.settings;
    if (!st) return;
    const d = st.default;
    const info = st.providers[d.provider] || {};
    const key = st.keys[d.provider] || {};
    const keyLine = !info.key ? (info.local ? 'Runs on this computer' : '')
      : key.set ? `Key from ${key.source === 'environment' ? key.env : key.source === 'keychain' ? 'keychain' : 'this session'}`
        : 'No API key yet';
    fill(document.getElementById('side-foot'), ...[
      h('div', {}, h('span', { class: `dot ${!info.key || key.set ? 'ok' : 'bad'}` }), h('b', {}, providerLabel(d.provider)), ' · ', d.model),
      keyLine ? h('div', {}, keyLine) : null,
      S.state ? h('div', { title: S.state.workspace }, `Workspace ${S.state.workspace.replace(/^\/Users\/[^/]+/, '~')}`) : null,
    ].filter(Boolean));
  }

  async function models(provider, baseUrl, force = false) {
    const k = `${provider}|${baseUrl || ''}`;
    if (!S.models[k] || force) {
      const q = new URLSearchParams({ provider, base_url: baseUrl || '' });
      S.models[k] = await api(`/api/models?${q}`).catch((e) => ({ models: [], error: e.message, tested: [] }));
    }
    return S.models[k];
  }

  function datalist(id, list, tested = []) {
    return h('datalist', { id }, list.map((m) => h('option', { value: m }, tested.includes(m) ? 'tested' : null)));
  }

  // ------------------------------------------------------------------ router
  const screens = {};
  function route() {
    const name = (location.hash || '#library').slice(1).split('?')[0];
    S.screen = screens[name] ? name : 'library';
    document.querySelectorAll('.nav').forEach((a) => a.setAttribute('aria-current', a.dataset.screen === S.screen ? 'page' : 'false'));
    current = screens[S.screen]();
    fill(main, current.root);
    if (current.update) current.update();
    main.focus({ preventScroll: true });
    window.scrollTo(0, 0);
  }

  function runFor(name) {
    return S.runs.find((r) => r.out === name && (r.state === 'running' || r.state === 'queued'));
  }

  // ----------------------------------------------------------------- library
  screens.library = () => {
    const cards = h('div', {});
    const summary = h('div', { class: 'summary' });
    let timer = null;
    const search = h('input', {
      type: 'search', class: 'search', placeholder: 'Search titles, authors, methods, concepts', value: S.query, 'aria-label': 'Search the library',
      oninput: () => {
        S.query = search.value;
        clearTimeout(timer);
        timer = setTimeout(async () => {
          S.hits = S.query.trim() ? await api(`/api/search?q=${encodeURIComponent(S.query)}`).catch(() => null) : null;
          update();
        }, 220);
      },
    });
    const root = h('div', {},
      h('div', { class: 'head' }, h('h1', {}, 'Library'),
        h('div', { class: 'actions' },
          h('button', { onclick: rerenderAll, title: 'Rebuild every page with the current page design. No model calls.' }, 'Re-render all'),
          h('a', { class: 'btn primary', href: '#new' }, '+ New paper'))),
      summary, search, cards);

    async function rerenderAll(e) {
      e.target.disabled = true;
      try {
        const r = await post('/api/rerender-all');
        toast(`Re-rendered ${plural(r.done.length, 'page')}${r.failed.length ? `, ${r.failed.length} failed` : ''}.`, r.failed.length > 0);
        refresh();
      } catch (err) { fail(err); }
      e.target.disabled = false;
    }

    function card(p) {
      const run = runFor(p.name);
      const hit = S.hits && S.hits.find((x) => x.name === p.name);
      let chip;
      if (run) chip = h('span', { class: `chip ${run.state === 'running' ? 'run' : 'wait'}` }, run.state === 'running' ? `Generating · ${run.current || 'starting'}` : 'Queued');
      else if (p.ready) chip = h('span', { class: 'chip good' }, 'Ready');
      else if (p.status && p.status.state === 'failed') chip = h('span', { class: 'chip bad' }, `Failed${p.status.failed_stage ? ` · ${p.status.failed_stage}` : ''}`);
      else if (p.status && p.status.state === 'running') chip = h('span', { class: 'chip wait' }, 'Stopped');
      else chip = h('span', { class: 'chip' }, 'Not generated');
      const q = p.quiz || {};
      const reading = p.reading;
      const menu = h('div', { class: 'menu-pop', hidden: true },
        h('a', { href: `/api/projects/${encodeURIComponent(p.name)}/export.html`, download: `${p.name}.html` }, 'One HTML file', h('small', {}, 'Figures and audio inside. Opens anywhere, no Q&A.')),
        h('a', { href: `/api/projects/${encodeURIComponent(p.name)}/export.zip`, download: `${p.name}.zip` }, 'Folder as .zip', h('small', {}, 'Q&A works with papermap serve.')));
      const share = h('div', { class: 'menu' },
        h('button', { class: 'sm', 'aria-haspopup': 'true', onclick: (e) => { e.stopPropagation(); menu.hidden = !menu.hidden; } }, 'Share'), menu);

      return h('article', { class: 'card' },
        h('div', { class: 'row' }, chip, p.reader ? h('span', { class: 'chip' }, `for ${p.reader}`) : null),
        h('h3', {}, p.title),
        h('div', { class: 'meta' },
          p.method ? h('span', {}, p.method) : null,
          p.pages ? h('span', {}, `${p.pages} pages`) : null,
          p.sections ? h('span', {}, `${p.sections} sections`) : null,
          p.quiz_questions ? h('span', {}, `${p.quiz_questions} quiz questions`) : null),
        p.model ? h('div', { class: 'meta' }, h('span', { class: 'mono' }, `${p.provider} · ${p.model}`), h('span', {}, fmtAgo(p.updated))) : null,
        (reading || q.last) ? h('div', { class: 'meta' },
          reading ? h('span', {}, `Read ${reading.read}/${reading.total}`) : null,
          q.last ? h('span', {}, `Quiz ${q.last.correct}/${q.last.total}${q.best && q.best.correct > q.last.correct ? ` · best ${q.best.correct}` : ''}`) : null) : null,
        hit && hit.concepts.length ? h('div', { class: 'matched' }, hit.concepts.map((c) => h('span', { class: 'chip run' }, c))) : null,
        p.status && p.status.state === 'failed' && !p.ready && p.status.error ? h('div', { class: 'err' }, p.status.error) : null,
        h('div', { class: 'acts' },
          p.ready ? h('a', { class: 'btn sm primary', href: `/p/${encodeURIComponent(p.name)}/`, target: '_blank', rel: 'noopener' }, 'Open') : null,
          !p.ready && !run ? h('a', { class: 'btn sm', href: '#runs' }, 'See runs') : null,
          p.ready ? share : null,
          p.ready ? h('button', { class: 'sm', onclick: async (e) => {
            e.target.disabled = true;
            try { await post(`/api/projects/${encodeURIComponent(p.name)}/rerender`); toast('Page rebuilt with the current design.'); } catch (err) { fail(err); }
            e.target.disabled = false;
          } }, 'Re-render') : null,
          h('button', { class: 'sm ghost', onclick: () => post(`/api/projects/${encodeURIComponent(p.name)}/reveal`).catch(fail) }, 'Folder'),
          run ? null : confirmButton('Delete', 'Delete this folder?', async () => {
            try { await post(`/api/projects/${encodeURIComponent(p.name)}/delete`); toast('Deleted.'); refresh(); } catch (err) { fail(err); }
          })));
    }

    let painted = null;
    function update() {
      // repaint only when something changed, and never under an open "Delete?" question
      const sig = JSON.stringify([S.projects, S.hits, S.query, S.runs.map((r) => [r.out, r.state, r.current])]);
      if (sig === painted || root.querySelector('.confirm')) return;
      painted = sig;
      const ps = S.projects;
      const read = ps.filter((p) => p.reading && p.reading.total && p.reading.read >= p.reading.total).length;
      const quizzes = ps.filter((p) => p.quiz && p.quiz.last);
      const avg = quizzes.length ? Math.round(100 * quizzes.reduce((a, p) => a + p.quiz.last.correct / Math.max(1, p.quiz.last.total), 0) / quizzes.length) : null;
      fill(summary, ...[
        h('span', {}, h('b', {}, ps.filter((p) => p.ready).length), 'pages ready'),
        h('span', {}, h('b', {}, read), 'read to the end'),
        h('span', {}, h('b', {}, quizzes.length), 'quizzes taken'),
        avg != null ? h('span', {}, h('b', {}, `${avg}%`), 'average last score') : null].filter(Boolean));
      if (!ps.length) {
        fill(cards, h('div', { class: 'empty' },
          h('h2', {}, 'No papers yet'),
          h('p', {}, 'Paste an arXiv link or drop a PDF, pick who will read it, and PaperMap builds the page in the background.'),
          h('a', { class: 'btn primary', href: '#new' }, '+ New paper')));
        return;
      }
      let shown = ps;
      if (S.query.trim()) {
        const names = new Set((S.hits || []).map((x) => x.name));
        const ql = S.query.toLowerCase();
        shown = ps.filter((p) => names.has(p.name) || `${p.title} ${p.name}`.toLowerCase().includes(ql));
      }
      fill(cards, shown.length ? h('div', { class: 'cards' }, shown.map(card))
        : h('p', { class: 'muted' }, `Nothing matches “${S.query}”.`));
    }
    return { root, update };
  };

  // -------------------------------------------------------------------- runs
  screens.runs = () => {
    const list = h('div', { class: 'jobs' });
    const sub = h('p', { class: 'lead' });
    const notifyBtn = h('button', { onclick: toggleNotify });
    const root = h('div', {},
      h('div', { class: 'head' }, h('h1', {}, 'Runs'),
        h('div', { class: 'actions' }, notifyBtn,
          h('button', { onclick: async () => { try { const r = await post('/api/runs/clear'); toast(`Removed ${plural(r.removed, 'finished run')} from the list.`); refresh(); } catch (e) { fail(e); } } }, 'Clear finished'),
          h('a', { class: 'btn primary', href: '#new' }, '+ New paper'))),
      sub, list);

    function paintNotify() {
      const on = store.get('notify', false);
      notifyBtn.textContent = on ? 'Notifications on' : 'Notify me when done';
      notifyBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
    }
    async function toggleNotify() {
      const on = !store.get('notify', false);
      if (on && 'Notification' in window && Notification.permission === 'default') {
        try { await Notification.requestPermission(); } catch { /* ignore */ }
      }
      store.set('notify', on);
      if (on) chime(true);
      paintNotify();
      toast(on ? 'You will get a notification and a sound when a run finishes. Keep this tab open.' : 'Notifications off.');
    }
    paintNotify();

    const logBoxes = {};
    async function loadLog(id) {
      if (!logBoxes[id]) return;
      try {
        const r = await api(`/api/runs/${id}/log`);
        const box = logBoxes[id];
        const atEnd = box.scrollTop + box.clientHeight >= box.scrollHeight - 8;
        box.textContent = r.lines.join('\n') || 'Nothing logged yet.';
        if (atEnd) box.scrollTop = box.scrollHeight;
      } catch { /* keep the old text */ }
    }

    function job(r) {
      const stages = r.stages.length ? r.stages : ['parse', 'understand', 'profile', 'explain', 'review', 'diagrams', 'graph', 'quiz', 'narrate', 'render'];
      const bars = stages.map((s) => {
        let c = '';
        if (r.cached.includes(s)) c = 'k';
        else if (r.done.includes(s)) c = 'd';
        if (r.current === s) c = 'c';
        if (r.state === 'failed' && r.failed_stage === s) c = 'x';
        return h('i', { class: c, title: s });
      });
      const chip = {
        running: h('span', { class: 'chip run' }, r.current || 'starting'),
        queued: h('span', { class: 'chip wait' }, r.position ? `queued · #${r.position}` : 'queued'),
        done: h('span', { class: 'chip good' }, 'done'),
        failed: h('span', { class: 'chip bad' }, r.failed_stage ? `failed at ${r.failed_stage}` : 'failed'),
        cancelled: h('span', { class: 'chip' }, 'cancelled'),
        interrupted: h('span', { class: 'chip wait' }, 'interrupted'),
      }[r.state];
      const facts = [];
      if (r.elapsed != null) facts.push(fmtDur(r.elapsed));
      if (r.tokens.input || r.tokens.output) facts.push(`${fmtTok(r.tokens.input + r.tokens.output)} tokens`);
      if (r.state === 'queued' && r.estimate) facts.push(`about ${Math.ceil(r.estimate.minutes)} min${r.estimate.cost ? ` · ~${fmtCost(r.estimate.cost)}` : ''}`);
      else if (r.cost != null && r.tokens.input) facts.push(fmtCost(r.cost));
      facts.push(`${providerLabel(r.provider)} · ${r.model}`);
      if (r.profile) facts.push(`for ${r.profile}`);

      const actions = [];
      if (r.state === 'running' || r.state === 'queued') actions.push(confirmButton('Cancel', 'Stop this run?', () => post(`/api/runs/${r.id}/cancel`).then(refresh, fail), 'sm'));
      if (r.state === 'failed' || r.state === 'cancelled' || r.state === 'interrupted') actions.push(h('button', { class: 'sm primary', onclick: () => post(`/api/runs/${r.id}/resume`).then(() => { toast('Resuming: finished steps come from the cache.'); refresh(); }, fail) }, 'Resume'));
      if (r.state === 'done') actions.push(h('a', { class: 'btn sm primary', href: `/p/${encodeURIComponent(r.out)}/`, target: '_blank', rel: 'noopener' }, 'Open'));
      if (r.state !== 'running') actions.push(h('button', { class: 'sm ghost', onclick: () => post(`/api/runs/${r.id}/remove`).then(refresh, fail) }, 'Remove'));

      const box = h('div', { class: 'log' }, 'Loading…');
      logBoxes[r.id] = box;
      const det = h('details', { open: S.openLogs.has(r.id), ontoggle: () => {
        if (det.open) { S.openLogs.add(r.id); loadLog(r.id); } else S.openLogs.delete(r.id);
      } }, h('summary', { class: 'job-sub' }, 'Log'), box);
      if (det.open) loadLog(r.id);

      return h('div', { class: 'job' },
        h('div', { class: 'job-top' },
          h('div', { class: 'job-title' }, r.label, chip),
          h('div', { class: 'row' }, h('span', { class: 'job-sub' }, facts.join(' · ')), actions)),
        r.state !== 'queued' ? h('div', { class: 'stages', role: 'img', 'aria-label': `${r.done.length} of ${stages.length} steps done` }, bars) : null,
        r.state === 'running' ? h('div', { class: 'stage-labels' }, stages.map((s) => h('span', {}, s))) : null,
        r.error && r.state !== 'done' ? h('div', { class: 'err' }, r.error) : null,
        r.state !== 'queued' ? det : null);
    }

    function update() {
      if (root.querySelector('.confirm')) return; // a "Stop this run?" question is open
      const running = S.runs.filter((r) => r.state === 'running').length;
      const queued = S.runs.filter((r) => r.state === 'queued').length;
      const lim = S.settings ? S.settings.limits : { local: 1, cloud: 3 };
      fill(sub, `${running} running, ${queued} queued. Local models run ${plural(lim.local, 'paper')} at a time, cloud models ${lim.cloud}. `,
        'You can close this tab: runs keep going, even if you stop the dashboard. ', h('a', { href: '#settings' }, 'Change limits'));
      Object.keys(logBoxes).forEach((k) => delete logBoxes[k]);
      fill(list, ...(S.runs.length ? S.runs.map(job) : [h('div', { class: 'empty' }, h('p', {}, 'No runs yet.'), h('a', { class: 'btn primary', href: '#new' }, '+ New paper'))]));
    }
    return { root, update };
  };

  // --------------------------------------------------------------- new paper
  screens.new = () => {
    const st = S.settings;
    const cfg = st.config;
    const form = store.get('newForm', {});
    const uploads = [];
    const sources = h('textarea', { id: 'sources', rows: 4, placeholder: 'https://arxiv.org/abs/1706.03762\n2505.23705', oninput: estimate }, form.sources || '');
    const fileInput = h('input', { type: 'file', accept: 'application/pdf,.pdf', multiple: true, hidden: true, onchange: () => { addFiles(fileInput.files); fileInput.value = ''; } });
    const fileList = h('div', { class: 'files' });
    const drop = h('div', {
      class: 'drop', tabindex: 0, role: 'button', onclick: () => fileInput.click(),
      onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); } },
      ondragover: (e) => { e.preventDefault(); drop.classList.add('over'); },
      ondragleave: () => drop.classList.remove('over'),
      ondrop: (e) => { e.preventDefault(); drop.classList.remove('over'); addFiles(e.dataTransfer.files); },
    }, 'Drop PDF files here, or click to choose');

    async function addFiles(files) {
      for (const f of files) {
        if (!/\.pdf$/i.test(f.name)) { toast(`${f.name} is not a PDF.`, true); continue; }
        const item = { name: f.name, pending: true };
        uploads.push(item);
        paintFiles();
        try {
          const r = await api('/api/upload', { method: 'POST', headers: { 'Content-Type': 'application/pdf', 'X-Filename': f.name }, body: f });
          Object.assign(item, r, { pending: false });
        } catch (e) {
          uploads.splice(uploads.indexOf(item), 1);
          fail(e);
        }
        paintFiles();
        estimate();
      }
    }
    function paintFiles() {
      fill(fileList, ...uploads.map((u) => h('div', { class: 'file' },
        h('span', {}, u.name, ' ', h('span', { class: 'muted' }, u.pending ? 'uploading…' : u.pages ? `${u.pages} pages` : '')),
        h('button', { class: 'sm ghost', onclick: () => { uploads.splice(uploads.indexOf(u), 1); paintFiles(); estimate(); } }, 'Remove'))));
    }

    const profileSel = h('select', { id: 'profile' }, h('option', { value: '' }, 'Generic curious reader'));
    api('/api/profiles').then((r) => {
      r.profiles.forEach((p) => profileSel.append(h('option', { value: p.name }, p.name)));
      profileSel.value = form.profile && r.profiles.some((p) => p.name === form.profile) ? form.profile : (r.profiles[0] ? r.profiles[0].name : '');
    }).catch(fail);

    const modelRows = h('div', { class: 'form' });
    const rows = [];
    function addModelRow(provider, model, baseUrl) {
      const row = { provider: provider || st.default.provider, model: model || '', base_url: baseUrl || '' };
      const listId = `models-${rows.length}-${Math.random().toString(36).slice(2, 6)}`;
      const dl = h('datalist', { id: listId });
      const prov = h('select', { 'aria-label': 'Provider', onchange: () => { row.provider = prov.value; row.base_url = ''; mi.value = ''; row.model = ''; loadList(); estimate(); } },
        Object.entries(st.providers).map(([k, v]) => h('option', { value: k }, v.label)));
      prov.value = row.provider;
      const mi = h('input', { type: 'text', class: 'mono', list: listId, value: row.model, placeholder: 'model name', 'aria-label': 'Model', oninput: () => { row.model = mi.value.trim(); estimate(); } });
      async function loadList() {
        const base = row.base_url || (row.provider === st.default.provider ? st.default.base_url : '') || '';
        const r = await models(row.provider, base);
        fill(dl, ...r.models.map((m) => h('option', { value: m }, (r.tested || []).includes(m) ? 'tested' : null)));
        if (!mi.value && r.models.length) { mi.value = r.models[0]; row.model = mi.value; estimate(); }
      }
      const removeBtn = rows.length ? h('button', { class: 'sm ghost', onclick: () => { rows.splice(rows.indexOf(row), 1); el.remove(); estimate(); } }, 'Remove') : h('span', {});
      const el = h('div', { class: 'model-row' }, prov, h('div', {}, mi, dl), removeBtn);
      rows.push(row);
      modelRows.append(el);
      loadList();
    }
    addModelRow(st.default.provider, st.default.model, st.default.base_url);

    const segState = { style: cfg.pipeline.narration_style, tts: cfg.tts.provider };
    function seg(key, options) {
      const wrap = h('div', { class: 'seg' });
      const paint = () => fill(wrap, ...options.map(([v, label]) => h('button', {
        type: 'button', 'aria-pressed': segState[key] === v ? 'true' : 'false', onclick: () => { segState[key] = v; paint(); },
      }, label)));
      paint();
      return wrap;
    }
    const quiz = h('input', { type: 'checkbox', checked: cfg.pipeline.quiz });
    const review = h('input', { type: 'checkbox', checked: cfg.pipeline.review });
    const latex = h('input', { type: 'checkbox', checked: cfg.pipeline.use_latex });
    const est = h('div', { class: 'estimate' }, 'Add a paper to see the estimate.');
    const submit = h('button', { class: 'primary', onclick: send }, 'Queue');

    function items() {
      const lines = sources.value.split('\n').map((s) => s.trim()).filter(Boolean);
      return [...lines.map((s) => ({ pages: null })), ...uploads.filter((u) => !u.pending).map((u) => ({ pages: u.pages }))];
    }
    let estTimer = null;
    function estimate() {
      clearTimeout(estTimer);
      estTimer = setTimeout(async () => {
        const its = items();
        const ms = rows.filter((r) => r.model);
        const n = its.length * ms.length;
        submit.textContent = n > 1 ? `Queue ${n} runs` : 'Queue';
        if (!n) { est.textContent = its.length ? 'Choose a model.' : 'Add a paper to see the estimate.'; return; }
        let minutes = 0;
        let cost = 0;
        let unknown = false;
        let guessed = false;
        for (const it of its) {
          for (const m of ms) {
            const q = new URLSearchParams({ provider: m.provider, model: m.model, base_url: m.base_url || '', pages: it.pages || '' });
            const e = await api(`/api/estimate?${q}`).catch(() => null);
            if (!e) continue;
            minutes += e.minutes;
            if (e.cost == null) unknown = true; else cost += e.cost;
            if (!e.pages_known) guessed = true;
          }
        }
        fill(est, `${plural(n, 'run')} · about ${Math.max(1, Math.round(minutes))} min of generation · `,
          unknown ? 'cost unknown for this model' : cost === 0 ? 'free' : `about ${fmtCost(cost)}`,
          guessed ? h('span', { class: 'muted' }, ' · linked papers counted as 15 pages') : null);
      }, 250);
    }

    async function send() {
      const lines = sources.value.split('\n').map((s) => s.trim()).filter(Boolean);
      const body = {
        sources: lines,
        uploads: uploads.filter((u) => !u.pending).map((u) => ({ file: u.file, name: u.name, pages: u.pages })),
        profile: profileSel.value,
        models: rows.filter((r) => r.model).map((r) => ({ provider: r.provider, model: r.model, base_url: r.base_url || null })),
        options: { style: segState.style, tts: segState.tts, quiz: quiz.checked, review: review.checked, use_latex: latex.checked },
      };
      submit.disabled = true;
      try {
        const r = await post('/api/runs', body);
        store.set('newForm', { profile: profileSel.value });
        toast(`Queued ${plural(r.queued.length, 'run')}.`);
        location.hash = '#runs';
        refresh();
      } catch (e) { fail(e); }
      submit.disabled = false;
    }

    const root = h('div', {},
      h('div', { class: 'head' }, h('h1', {}, 'New paper')),
      h('div', { class: 'form' },
        h('div', { class: 'field' }, h('label', { for: 'sources' }, 'arXiv links or IDs, or PDF links, one per line'), sources,
          h('span', { class: 'hint' }, 'Each line becomes its own run with the settings below.')),
        h('div', { class: 'field' }, drop, fileInput, fileList),
        h('div', { class: 'field' }, h('label', { for: 'profile' }, 'Who will read it'), profileSel,
          h('span', { class: 'hint' }, h('a', { href: '#profiles' }, 'Manage reader profiles'))),
        h('div', { class: 'field' }, h('span', { class: 'lbl' }, 'Model'), modelRows,
          h('div', {}, h('button', { class: 'sm', onclick: () => addModelRow(st.default.provider, '', '') }, '+ Compare with another model'),
            ' ', h('span', { class: 'hint' }, 'Each paper runs once per model, so you can open the results side by side.'))),
        h('div', { class: 'grid2' },
          h('div', { class: 'field' }, h('span', { class: 'lbl' }, 'Narration'), seg('style', [['narrator', 'One narrator'], ['duo', 'Host + expert']])),
          h('div', { class: 'field' }, h('span', { class: 'lbl' }, 'Voice'), seg('tts', [['browser', 'Browser'], ['edge', 'Edge voices'], ['none', 'None']]))),
        h('div', { class: 'row' },
          h('label', { class: 'check' }, quiz, 'Quiz'),
          h('label', { class: 'check' }, review, 'Fact-check pass'),
          h('label', { class: 'check' }, latex, 'Use the arXiv LaTeX source')),
        est,
        h('div', { class: 'row' }, submit, h('span', { class: 'hint' }, 'Runs continue in the background.'))));
    estimate();
    return { root };
  };

  // ---------------------------------------------------------------- progress
  screens.progress = () => {
    const body = h('div', {});
    const root = h('div', {},
      h('div', { class: 'head' }, h('h1', {}, 'Reading progress')),
      h('p', { class: 'lead' }, 'What you read and how you did on each quiz. Scores are recorded when you open a page from this dashboard or with papermap serve. Pages built before this version need a re-render first.'),
      body);
    function update() {
      const ps = S.projects.filter((p) => p.ready);
      if (!ps.length) { fill(body, h('p', { class: 'muted' }, 'No pages yet.')); return; }
      fill(body, h('div', { class: 'table-wrap' }, h('table', {},
        h('thead', {}, h('tr', {}, h('th', {}, 'Paper'), h('th', {}, 'Read'), h('th', {}, 'Last quiz'), h('th', {}, 'Best'), h('th', {}, 'Attempts'), h('th', {}, 'To revisit'))),
        h('tbody', {}, ps.map((p) => {
          const r = p.reading;
          const q = p.quiz || {};
          const pct = r && r.total ? Math.round((100 * r.read) / r.total) : 0;
          return h('tr', {},
            h('td', {}, h('a', { href: `/p/${encodeURIComponent(p.name)}/`, target: '_blank', rel: 'noopener' }, p.title),
              h('div', { class: 'small muted' }, [p.model, p.reader ? `for ${p.reader}` : ''].filter(Boolean).join(' · '))),
            h('td', {}, r ? h('div', { title: `${r.read} of ${r.total} sections` }, h('div', { class: 'bar' }, h('i', { style: `width:${pct}%` })), h('span', { class: 'small muted num' }, `${r.read}/${r.total}`)) : h('span', { class: 'muted' }, 'not started')),
            h('td', { class: 'n' }, q.last ? `${q.last.correct}/${q.last.total}` : '–'),
            h('td', { class: 'n' }, q.best ? `${q.best.correct}/${q.best.total}` : '–'),
            h('td', { class: 'n' }, q.attempts || 0),
            h('td', {}, q.missed && q.missed.length ? h('details', {}, h('summary', {}, plural(q.missed.length, 'missed question')),
              h('ul', { class: 'small' }, q.missed.map((m) => h('li', {}, m.question))),
              h('a', { class: 'small', href: `/p/${encodeURIComponent(p.name)}/#mode=quiz`, target: '_blank', rel: 'noopener' }, 'Open the quiz results'))
              : h('span', { class: 'muted' }, q.last ? 'nothing' : '')));
        })))));
    }
    return { root, update };
  };

  // ---------------------------------------------------------------- concepts
  screens.concepts = () => {
    const list = h('div', { class: 'concepts' });
    const results = h('div', {});
    let timer = null;
    const input = h('input', { type: 'search', class: 'search', placeholder: 'Find a concept, method or dataset across all papers', 'aria-label': 'Search concepts',
      oninput: () => {
        clearTimeout(timer);
        timer = setTimeout(async () => {
          const q = input.value.trim();
          if (!q) { fill(results); return; }
          const hits = await api(`/api/search?q=${encodeURIComponent(q)}`).catch(() => []);
          fill(results, hits.length ? h('div', { class: 'concepts' }, hits.map((x) => h('div', { class: 'concept' },
            h('a', { href: `/p/${encodeURIComponent(x.name)}/`, target: '_blank', rel: 'noopener' }, x.title),
            h('div', { class: 'papers' }, x.concepts.length ? x.concepts.map((c) => h('span', { class: 'chip run' }, c)) : h('span', { class: 'muted small' }, 'matches the title or authors')))))
            : h('p', { class: 'muted' }, 'No paper mentions that.'));
        }, 220);
      } });
    const root = h('div', {},
      h('div', { class: 'head' }, h('h1', {}, 'Concepts')),
      h('p', { class: 'lead' }, 'Every page has a knowledge graph. Search all of them at once, and see the ideas that connect your papers.'),
      input, results,
      h('h2', { style: 'margin:22px 0 10px' }, 'Shared by several papers'), list);
    api('/api/connections').then((rows) => {
      fill(list, ...(rows.length ? rows.map((c) => h('div', { class: 'concept' },
        h('div', {}, h('b', {}, c.label), ' ', h('span', { class: 'chip' }, c.type)),
        h('div', { class: 'papers' }, c.papers.map((p) => h('a', { class: 'chip', href: `/p/${encodeURIComponent(p.name)}/`, target: '_blank', rel: 'noopener' }, p.title)))))
        : [h('p', { class: 'muted' }, 'Concepts shared by two or more papers show up here once your library has a few.')]));
    }).catch(fail);
    return { root };
  };

  // ----------------------------------------------------------------- compare
  screens.compare = () => {
    const ready = S.projects.filter((p) => p.ready);
    const pick = store.get('compare', {});
    const label = (p) => `${p.title}${p.model ? ` · ${p.model}` : ''}${p.reader ? ` · for ${p.reader}` : ''}`;
    const base = (n) => n.split('--')[0];
    let a = pick.a && ready.some((p) => p.name === pick.a) ? pick.a : null;
    let b = pick.b && ready.some((p) => p.name === pick.b) ? pick.b : null;
    if (!a || !b) { // default: two versions of the same paper, if there are any
      const pair = ready.find((p) => ready.some((q) => q !== p && base(q.name) === base(p.name)));
      a = pair ? pair.name : (ready[0] || {}).name;
      b = pair ? ready.find((q) => q.name !== pair.name && base(q.name) === base(pair.name)).name : (ready[1] || {}).name;
    }
    const frames = h('div', { class: 'compare' });
    const sel = (v, set) => {
      const s = h('select', { onchange: () => { set(s.value); paint(); } }, ready.map((p) => h('option', { value: p.name }, label(p))));
      if (v) s.value = v;
      return s;
    };
    function paint() {
      store.set('compare', { a, b });
      fill(frames, ...[a, b].map((n) => (n ? h('iframe', { src: `/p/${encodeURIComponent(n)}/`, title: n }) : h('div', { class: 'empty' }, 'Pick a page'))));
    }
    const root = h('div', {},
      h('div', { class: 'head' }, h('h1', {}, 'Compare')),
      ready.length < 2 ? h('div', { class: 'empty' }, h('p', {}, 'You need two pages to compare. Queue the same paper with two models from New paper.'), h('a', { class: 'btn primary', href: '#new' }, '+ New paper'))
        : h('div', {}, h('div', { class: 'grid2', style: 'margin-bottom:12px' }, sel(a, (v) => { a = v; }), sel(b, (v) => { b = v; })), frames));
    if (ready.length >= 2) paint();
    return { root };
  };

  // ---------------------------------------------------------------- profiles
  screens.profiles = () => {
    const listEl = h('div', { class: 'plist' });
    const editor = h('div', {});
    let profiles = [];
    let template = '';
    let selected = null;
    async function load(keep) {
      const r = await api('/api/profiles');
      profiles = r.profiles;
      template = r.template;
      selected = keep && profiles.some((p) => p.name === keep) ? keep : (profiles[0] ? profiles[0].name : null);
      paint();
    }
    function paint() {
      fill(listEl, ...profiles.map((p) => h('button', { 'aria-current': p.name === selected ? 'true' : 'false', onclick: () => { selected = p.name; paint(); } },
        p.name, h('span', { class: 'muted small' }, p.papers ? plural(p.papers, 'paper') : ''))),
      h('button', { class: 'ghost', onclick: () => { selected = null; paint(); } }, '+ New profile'));
      const p = profiles.find((x) => x.name === selected);
      const name = h('input', { type: 'text', id: 'pname', value: p ? p.name : '', placeholder: 'e.g. robotics-phd', disabled: !!p });
      const text = h('textarea', { id: 'ptext', rows: 16 }, p ? p.text : template);
      fill(editor, h('div', { class: 'form' },
        h('div', { class: 'field' }, h('label', { for: 'pname' }, 'Name'), name, p ? null : h('span', { class: 'hint' }, 'Used in folder names, so keep it short.')),
        h('div', { class: 'field' }, h('label', { for: 'ptext' }, 'Who is reading'), text,
          h('span', { class: 'hint' }, 'Write freely. This is the same Markdown file the command line reads with --profile.')),
        h('div', { class: 'row' },
          h('button', { class: 'primary', onclick: async () => {
            const n = (p ? p.name : name.value.trim()).replace(/\.md$/i, '');
            if (!n) { toast('Give the profile a name.', true); return; }
            try { await post(`/api/profiles/${encodeURIComponent(n)}`, { text: text.value }); toast('Profile saved.'); load(n); } catch (e) { fail(e); }
          } }, 'Save'),
          p ? confirmButton('Delete', 'Delete this profile?', async () => {
            try { await post(`/api/profiles/${encodeURIComponent(p.name)}/delete`); toast('Profile deleted.'); load(); } catch (e) { fail(e); }
          }, 'ghost danger') : null)));
    }
    const root = h('div', {},
      h('div', { class: 'head' }, h('h1', {}, 'Reader profiles')),
      h('p', { class: 'lead' }, 'One profile per reader. The same paper becomes a different page for a student, a reviewer or a colleague from another field.'),
      h('div', { class: 'split' }, listEl, editor));
    load().catch(fail);
    return { root };
  };

  // ---------------------------------------------------------------- settings
  screens.settings = () => {
    const st = S.settings;
    const cfg = st.config;
    const v = {
      provider: cfg.llm.provider, model: cfg.llm.model, base_url: cfg.llm.base_url || '', effort: cfg.llm.effort || 'medium',
      diagrams: (cfg.stages.diagrams || {}).model || '', concurrency: cfg.pipeline.concurrency,
      style: cfg.pipeline.narration_style, tts: cfg.tts.provider, quiz: cfg.pipeline.quiz, review: cfg.pipeline.review, latex: cfg.pipeline.use_latex,
      local: st.limits.local, cloud: st.limits.cloud,
    };
    const provSeg = h('div', { class: 'seg', role: 'group', 'aria-label': 'Provider' });
    const keyBox = h('div', { class: 'field' });
    const urlBox = h('div', { class: 'field' });
    const dl = h('datalist', { id: 'model-list' });
    const modelErr = h('span', { class: 'hint' });
    const model = h('input', { type: 'text', id: 'model', class: 'mono', list: 'model-list', value: v.model, oninput: () => { v.model = model.value.trim(); } });
    const checkLine = h('span', { class: 'small' });
    const effortBox = h('div', { class: 'field' });

    function info() { return st.providers[v.provider] || {}; }
    function paintProviders() {
      fill(provSeg, ...Object.entries(st.providers).map(([k, p]) => h('button', {
        type: 'button', 'aria-pressed': v.provider === k ? 'true' : 'false',
        onclick: () => {
          if (v.provider === k) return;
          v.provider = k;
          v.base_url = '';
          v.model = '';
          model.value = '';
          paintProviders(); paintKey(); paintUrl(); paintEffort(); loadModels();
        },
      }, p.label)));
    }
    function paintKey() {
      const ks = st.keys[v.provider] || {};
      const needs = info().key;
      if (!ks.env) { fill(keyBox); keyBox.hidden = true; return; }
      keyBox.hidden = false;
      const input = h('input', { type: 'password', id: 'key', class: 'mono', autocomplete: 'off', placeholder: ks.set ? `saved (${ks.hint})` : (needs ? 'paste your API key' : 'only if your server needs one') });
      const remember = h('input', { type: 'checkbox', id: 'remember', disabled: !st.keyring });
      const where = ks.set ? (ks.source === 'environment' ? `Using ${ks.env} from your environment. A key entered here replaces it while the dashboard runs.`
        : ks.source === 'keychain' ? 'Saved in your system keychain.' : 'Kept in memory until you stop the dashboard.') : (needs ? 'No key yet.' : '');
      fill(keyBox, 
        h('label', { for: 'key' }, needs ? 'API key' : 'API key (optional)'),
        h('div', { class: 'row' }, input,
          h('button', { onclick: async () => {
            if (!input.value.trim()) { toast('Paste a key first.', true); return; }
            try {
              const r = await post('/api/keys', { env: ks.env, value: input.value, remember: remember.checked });
              st.keys = r.keys; S.models = {};
              toast(r.warning || 'Key saved.', !!r.warning);
              paintKey(); loadModels(); renderFoot();
            } catch (e) { fail(e); }
          } }, 'Use key'),
          ks.set && ks.source !== 'environment' ? confirmButton('Forget', 'Forget this key?', async () => {
            try { const r = await post('/api/keys/forget', { env: ks.env }); st.keys = r.keys; toast('Key forgotten.'); paintKey(); renderFoot(); } catch (e) { fail(e); }
          }, 'ghost danger') : null),
        h('span', { class: `hint ${ks.set ? '' : needs ? 'warn' : ''}` }, where),
        h('label', { class: 'check small' }, remember, st.keyring ? 'Remember on this computer (system keychain)' : 'Remember on this computer: install keyring first (pip install "papermap[ui]")'),
        h('span', { class: 'hint' }, `The key never goes into a config file. Runs receive it as ${ks.env}.`),
        h('span', { class: 'hint' }, 'Going through a proxy that does not check keys? Enter any text, for example "proxy".'));
    }
    function paintUrl() {
      const def = info().base_url;
      const needed = v.provider === 'openai_compatible';
      const anthropic = v.provider === 'anthropic';
      if (v.provider === 'mock') { fill(urlBox); return; }
      const input = h('input', { type: 'text', id: 'base', class: 'mono', value: v.base_url,
        placeholder: anthropic ? 'https://api.anthropic.com' : def || 'http://localhost:8000/v1',
        onchange: () => { v.base_url = input.value.trim(); loadModels(true); } });
      fill(urlBox, h('label', { for: 'base' }, anthropic ? 'Proxy or gateway URL (optional)' : needed ? 'Server URL' : 'Server URL (optional)'), input,
        h('span', { class: 'hint' }, anthropic
          ? 'Leave empty for the Anthropic API. For a local proxy, give its address without a path, e.g. http://127.0.0.1:8901.'
          : `The base URL, ending in /v1 for most servers${def ? ` (default ${def})` : ''}. A full endpoint like .../v1/chat/completions is trimmed for you.`));
    }
    function paintEffort() {
      if (v.provider !== 'anthropic') { fill(effortBox); return; }
      const s = h('select', { id: 'effort', onchange: () => { v.effort = s.value; } }, ['low', 'medium', 'high', 'xhigh', 'max'].map((x) => h('option', { value: x }, x)));
      s.value = v.effort;
      fill(effortBox, h('label', { for: 'effort' }, 'Effort'), s, h('span', { class: 'hint' }, 'Higher effort thinks longer and costs more.'));
    }
    async function loadModels(force) {
      modelErr.textContent = 'Loading models…';
      const r = await models(v.provider, v.base_url, force);
      fill(dl, ...r.models.map((m) => h('option', { value: m }, (r.tested || []).includes(m) ? 'tested' : null)));
      modelErr.textContent = r.error ? `Could not list models: ${r.error}` : `${plural(r.models.length, 'model')} available. Tested models are marked.`;
      modelErr.className = r.error ? 'hint warn' : 'hint';
      if (!model.value && r.models.length) { model.value = r.models[0]; v.model = model.value; }
    }

    const num = (id, val, min, max, set) => {
      const i = h('input', { type: 'number', id, min, max, value: val, oninput: () => set(parseInt(i.value, 10) || min) });
      return i;
    };
    const segs = {};
    const seg = (key, options) => {
      const wrap = h('div', { class: 'seg' });
      const paint = () => fill(wrap, ...options.map(([x, label]) => h('button', { type: 'button', 'aria-pressed': v[key] === x ? 'true' : 'false', onclick: () => { v[key] = x; paint(); } }, label)));
      paint();
      segs[key] = paint;
      return wrap;
    };
    const cb = (key, label) => h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: v[key], onchange: (e) => { v[key] = e.target.checked; } }), label);

    async function save() {
      if (!v.model) { toast('Choose a model.', true); return; }
      try {
        S.settings = await post('/api/settings', {
          llm: { provider: v.provider, model: v.model, base_url: v.base_url, effort: v.provider === 'anthropic' ? v.effort : undefined },
          diagrams_model: v.diagrams,
          pipeline: { concurrency: v.concurrency, narration_style: v.style, quiz: v.quiz, review: v.review, use_latex: v.latex },
          tts: { provider: v.tts },
          limits: { local: v.local, cloud: v.cloud },
        });
        renderFoot();
        toast(`Saved to ${S.settings.config_path}.`);
      } catch (e) { fail(e); }
    }

    const root = h('div', {},
      h('div', { class: 'head' }, h('h1', {}, 'Model settings')),
      h('p', { class: 'lead' }, 'Defaults for new runs and for answering questions on your pages. You can still pick another model for each run.'),
      h('div', { class: 'form' },
        h('div', { class: 'field' }, h('span', { class: 'lbl' }, 'Provider'), provSeg),
        keyBox, urlBox,
        h('div', { class: 'field' }, h('label', { for: 'model' }, 'Model'),
          h('div', { class: 'row' }, model, dl, h('button', { onclick: () => loadModels(true) }, 'Refresh list')), modelErr),
        h('div', { class: 'row' },
          h('button', { onclick: async (e) => {
            e.target.disabled = true;
            checkLine.textContent = 'Asking the model to reply…';
            checkLine.className = 'small muted';
            try {
              const r = await post('/api/check', { provider: v.provider, model: v.model, base_url: v.base_url });
              checkLine.textContent = r.ok ? `✓ Replied in ${r.seconds}s` : `✗ ${r.error}`;
              checkLine.className = `small ${r.ok ? 'ok' : 'bad'}`;
            } catch (err) { checkLine.textContent = `✗ ${err.message}`; checkLine.className = 'small bad'; }
            e.target.disabled = false;
          } }, 'Test connection'), checkLine),
        effortBox,
        h('details', {}, h('summary', { class: 'lbl' }, 'Advanced'),
          h('div', { class: 'form', style: 'margin-top:12px' },
            h('div', { class: 'field' }, h('label', { for: 'diag' }, 'Different model for diagrams'),
              h('input', { type: 'text', id: 'diag', class: 'mono', list: 'model-list', value: v.diagrams, placeholder: 'same as above', oninput: (e) => { v.diagrams = e.target.value.trim(); } }),
              h('span', { class: 'hint' }, 'Diagrams are the hardest step. A stronger model here helps most.')),
            h('div', { class: 'grid2' },
              h('div', { class: 'field' }, h('label', { for: 'conc' }, 'Parallel model calls per run'), num('conc', v.concurrency, 1, 32, (x) => { v.concurrency = x; })),
              h('div', {})),
            h('div', { class: 'grid2' },
              h('div', { class: 'field' }, h('label', { for: 'lim-local' }, 'Local runs at a time'), num('lim-local', v.local, 1, 16, (x) => { v.local = x; }),
                h('span', { class: 'hint' }, 'One GPU is usually fastest with 1.')),
              h('div', { class: 'field' }, h('label', { for: 'lim-cloud' }, 'Cloud runs at a time'), num('lim-cloud', v.cloud, 1, 16, (x) => { v.cloud = x; }))))),
        h('div', { class: 'field' }, h('span', { class: 'lbl' }, 'Default narration'), seg('style', [['narrator', 'One narrator'], ['duo', 'Host + expert']])),
        h('div', { class: 'field' }, h('span', { class: 'lbl' }, 'Default voice'), seg('tts', [['browser', 'Browser'], ['edge', 'Edge voices'], ['none', 'None']])),
        h('div', { class: 'row' }, cb('quiz', 'Quiz'), cb('review', 'Fact-check pass'), cb('latex', 'Use the arXiv LaTeX source')),
        h('div', { class: 'row' }, h('button', { class: 'primary', onclick: save }, 'Save as default'),
          h('span', { class: 'hint' }, `Writes ${st.config_path}`))));
    paintProviders(); paintKey(); paintUrl(); paintEffort(); loadModels();
    return { root };
  };

  // -------------------------------------------------------------------- cache
  screens.cache = () => {
    const body = h('div', {}, h('p', { class: 'muted' }, 'Measuring…'));
    async function load() {
      const c = await api('/api/cache');
      const busy = S.runs.some((r) => r.state === 'running');
      const clear = (payload, what) => confirmButton('Clear', `Clear ${what}?`, async () => {
        try { const r = await post('/api/cache/clear', payload); toast(`Freed ${fmtBytes(r.freed)}.`); load(); } catch (e) { fail(e); }
      }, 'sm');
      const title = (n) => {
        const p = S.projects.find((x) => x.name === n) || {};
        return [p.title || n, h('div', { class: 'small muted' }, [p.model, p.reader ? `for ${p.reader}` : ''].filter(Boolean).join(' · '))];
      };
      fill(body, 
        h('div', { class: 'summary' }, h('span', {}, h('b', {}, fmtBytes(c.total)), 'in ', h('code', {}, c.root))),
        busy ? h('p', { class: 'warn', style: 'margin-bottom:12px' }, 'A run is in progress. Clearing is possible once it finishes.') : null,
        h('h2', { style: 'margin-bottom:8px' }, 'By paper'),
        h('p', { class: 'lead', style: 'margin:0 0 10px' }, 'Clearing a paper removes only what no other paper uses. Its page stays; re-running it calls the model again.'),
        h('div', { class: 'table-wrap' }, h('table', {},
          h('thead', {}, h('tr', {}, h('th', {}, 'Paper'), h('th', {}, 'Cache used'), h('th', {}, 'Only this paper'), h('th', {}, ''))),
          h('tbody', {}, c.projects.length ? c.projects.map((p) => h('tr', {},
            h('td', {}, title(p.name), p.tracked ? null : h('div', { class: 'small muted' }, 'made before cache tracking: re-run it once to measure')),
            h('td', { class: 'n' }, fmtBytes(p.bytes)), h('td', { class: 'n' }, fmtBytes(p.own_bytes)),
            h('td', { class: 'n' }, p.own_bytes && !busy ? clear({ project: p.name }, 'this paper\'s cache') : null)))
            : h('tr', {}, h('td', { colspan: 4, class: 'muted' }, 'No papers yet.'))))),
        h('h2', { style: 'margin:22px 0 8px' }, 'By kind'),
        h('div', { class: 'table-wrap' }, h('table', {},
          h('thead', {}, h('tr', {}, h('th', {}, 'Kind'), h('th', {}, 'Files'), h('th', {}, 'Size'), h('th', {}, ''))),
          h('tbody', {}, c.kinds.map((k) => h('tr', {},
            h('td', {}, { llm: 'Model answers', stages: 'Step results', downloads: 'Downloaded papers', audio: 'Narration audio', figures: 'Figures', latex: 'LaTeX sources' }[k.kind] || k.kind, ' ', h('code', {}, k.kind)),
            h('td', { class: 'n' }, k.files), h('td', { class: 'n' }, fmtBytes(k.bytes)),
            h('td', { class: 'n' }, k.bytes && !busy ? clear({ kind: k.kind }, `all ${k.kind}`) : null)))))));
    }
    const root = h('div', {},
      h('div', { class: 'head' }, h('h1', {}, 'Cache')),
      h('p', { class: 'lead' }, 'PaperMap keeps every model answer, download and audio clip, so re-runs and resumes are instant and free. Clear what you no longer need.'),
      body);
    load().catch(fail);
    return { root };
  };

  // ---------------------------------------------------------------------- go
  document.addEventListener('click', () => document.querySelectorAll('.menu-pop').forEach((m) => { m.hidden = true; }));

  async function start() {
    try {
      [S.state] = await Promise.all([api('/api/state'), loadSettings()]);
      renderFoot();
      const [projects, runs] = await Promise.all([api('/api/projects'), api('/api/runs')]);
      S.projects = projects;
      S.runs = runs;
      S.prevRunStates = Object.fromEntries(runs.map((r) => [r.id, r.state]));
      updateNav();
    } catch (e) {
      fill(main, h('div', { class: 'empty' }, h('h2', {}, 'Cannot reach the dashboard server'), h('p', {}, e.message)));
      return;
    }
    window.addEventListener('hashchange', route);
    route();
    setTimeout(refresh, 2000);
  }
  start();
})();
