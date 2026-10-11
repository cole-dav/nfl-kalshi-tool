// ========================================================================
// LAB module: Big Data Bowl-inspired visualizations.
//   ALL-22 REPLAY  -- tracking sample (2017 KC @ NE), dots + trails, space
//                     control, pocket pressure, synced separation charts
//   NGS LEADERS    -- this season's tracking-derived player metrics
//   PRE-SNAP TELLS -- look -> pass/blitz/man rate vs league
//   ROUTE TREES    -- last season's targeted routes per receiver
//   BUILD STATUS   -- what from the BDB report is built / approximated / blocked
// Loaded after index.html's main script; uses its globals (state, render,
// escapeHtml, TEAM_COLORS, teamColor). Every attribute hook is data-lab-* so
// the app's global click handler never claims a Lab click (except
// data-open-player, which deliberately opens the research ticket).
// ========================================================================
(function () {
'use strict';

// Validated (dataviz validate_palette.js, dark, surface #101318): band, chroma,
// CVD and contrast all pass. Slot order is fixed; never cycled.
const PAL = ['#3D8DE0', '#E0602B', '#24A084', '#A070C8', '#A08A10'];
const OFF = PAL[0], DEF = PAL[1], BALL = '#C98A3A', OTHER = '#5A6470';
// Receiver lines: magenta instead of orange so no line reads as "defense" (also validated, adjacent pairs).
const RECV = ['#3D8DE0', '#D0508F', '#A08A10', '#A070C8', '#24A084'];
const FPS = 10;

const css = `
#labView{display:none;flex-direction:column;flex-grow:1;min-height:0}
.lab-tabs{height:32px;flex-shrink:0;display:flex;align-items:stretch;gap:2px;padding-left:10px;border-bottom:1px solid var(--border);background:var(--chrome)}
.lab-tabs .ttl{display:flex;align-items:center;font-size:10px;letter-spacing:.12em;color:var(--amber);font-weight:600;margin-right:12px}
.lab-tabs button{padding:0 10px;font-size:12px;color:var(--dim);border-bottom:2px solid transparent;border-top:2px solid transparent}
.lab-tabs button.on{color:var(--on);border-bottom-color:var(--amber)}
.lab-tabs .src{margin-left:auto;display:flex;align-items:center;padding:0 12px;font-size:10px;color:var(--dim)}
.lab-body{flex-grow:1;min-height:0;overflow:auto;padding:8px}
.lab-note{font-size:10.5px;color:var(--muted);line-height:1.45}
.lab-note b{color:var(--label);font-weight:600}
.lab-chip{font-size:11px;padding:2px 7px;border:1px solid var(--head-rule);color:var(--muted)}
.lab-chip.on{color:var(--bg);background:var(--amber);border-color:var(--amber);font-weight:600}
.lab-teams{display:flex;flex-wrap:wrap;gap:3px;margin-bottom:8px}
.lab-teams button{font-size:10.5px;padding:2px 0;width:38px;text-align:center;border:1px solid var(--head-rule);color:var(--muted);border-left:3px solid var(--tc)}
.lab-teams button.on{color:var(--on);background:#161B22;border-color:var(--amber);border-left-color:var(--tc)}
.lab-tip{position:fixed;z-index:200;pointer-events:none;background:#0B0E12;border:1px solid var(--head-rule);padding:6px 8px;font-size:11px;color:var(--on);min-width:120px;display:none;box-shadow:0 4px 14px rgba(0,0,0,.5)}
.lab-tip .k{color:var(--dim);font-size:10px;letter-spacing:.06em}
.lab-tip .r{display:flex;justify-content:space-between;gap:12px}

/* replay */
.rp{display:grid;grid-template-columns:300px minmax(0,1fr);gap:8px;height:100%;min-height:560px}
.rp-list{display:flex;flex-direction:column;min-height:0}
.rp-filt{display:flex;gap:4px;padding:6px 8px;border-bottom:1px solid var(--border);flex-wrap:wrap;align-items:center}
.rp-filt input{flex:1 1 100%;margin-top:4px;background:var(--bg);border:1px solid var(--head-rule);color:var(--on);font:inherit;font-size:11.5px;padding:3px 6px;outline:none}
.rp-plays{overflow:auto;flex-grow:1;min-height:0}
.rp-game{width:100%;background:var(--bg);border:1px solid var(--head-rule);color:var(--on);font:inherit;font-size:11.5px;padding:3px 4px}
.dr-table{width:100%;border-collapse:collapse;font-size:11px}
.dr-table th{position:sticky;top:0;background:var(--panel);text-align:right;font-size:9.5px;letter-spacing:.06em;color:var(--dim);font-weight:500;border-bottom:1px solid var(--head-rule);padding:4px 6px;white-space:nowrap;cursor:pointer}
.dr-table th.l,.dr-table td.l{text-align:left}
.dr-table th.on{color:var(--amber)}
.dr-table td{text-align:right;padding:3px 6px;border-bottom:1px solid var(--row-rule);white-space:nowrap}
.dr-table tr:hover td{background:var(--amber-soft)}
.dr-table td.hi{color:var(--blue)}
.rp-play{display:grid;grid-template-columns:34px 1fr;gap:6px;padding:5px 8px;border-bottom:1px solid var(--row-rule);cursor:pointer;font-size:11px;color:var(--label)}
.rp-play:hover{background:var(--amber-soft)}
.rp-play.on{background:#161B22;box-shadow:inset 2px 0 0 var(--amber)}
.rp-play .q{font-size:10px;color:var(--dim);line-height:1.3}
.rp-play .d{overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;line-height:1.35}
.rp-play .k{font-size:9px;font-weight:600;letter-spacing:.06em;padding:0 3px;margin-right:4px}
.rp-main{display:flex;flex-direction:column;gap:8px;min-width:0}
.rp-head{display:flex;gap:16px;align-items:flex-start;padding:8px 10px}
.rp-head .dd{font-size:15px;font-weight:600;white-space:nowrap}
.rp-head .desc{font-size:11.5px;color:var(--label);line-height:1.4;flex:1}
.rp-head .meta{font-size:10px;color:var(--dim);margin-top:3px}
.rp-field{position:relative;background:#0C1610;border:1px solid var(--border)}
.rp-field canvas{display:block;width:100%}
.rp-ctl{display:flex;align-items:center;gap:10px;padding:6px 10px;flex-wrap:wrap}
.rp-ctl .play{width:30px;height:24px;border:1px solid var(--amber-ring);color:var(--amber);display:flex;align-items:center;justify-content:center}
.rp-ctl .clock{width:64px;font-size:11px;color:var(--label)}
.rp-scrub{position:relative;flex:1 1 260px;height:26px}
.rp-scrub input{position:absolute;left:0;right:0;top:3px;width:100%;margin:0;accent-color:var(--amber)}
.rp-scrub .ev{position:absolute;top:18px;font-size:8.5px;color:var(--dim);transform:translateX(-50%);white-space:nowrap;letter-spacing:.04em}
.rp-scrub .ev::before{content:'';position:absolute;left:50%;top:-6px;width:1px;height:5px;background:var(--muted)}
.rp-tog{display:flex;gap:4px;flex-wrap:wrap}
.rp-charts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
.rp-chart svg{display:block;width:100%;height:170px}
.rp-legend{display:flex;flex-wrap:wrap;gap:10px;padding:0 10px 6px;font-size:10.5px;color:var(--label)}
.rp-legend i{display:inline-block;width:10px;height:2px;vertical-align:middle;margin-right:4px}
.rp-key{display:flex;gap:12px;font-size:10px;color:var(--dim);padding:0 10px 6px;flex-wrap:wrap}
.rp-key span{display:inline-flex;align-items:center;gap:4px}
.rp-key i{width:9px;height:9px;border-radius:5px;display:inline-block}

/* leaders */
.ld-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:8px}
.ld-card .ld-note{padding:6px 10px 2px;font-size:10.5px;color:var(--muted);line-height:1.4;min-height:34px}
.ld-row{display:grid;grid-template-columns:20px minmax(0,150px) 34px minmax(0,1fr) 52px 36px;gap:6px;align-items:center;height:21px;padding:0 10px;font-size:11px;border-bottom:1px solid var(--row-rule)}
.ld-row:hover{background:var(--amber-soft)}
.ld-row .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer;color:var(--on)}
.ld-row .nm:hover{color:var(--amber);text-decoration:underline}
.ld-row .tm{font-size:10px;color:var(--muted);border-left:3px solid var(--tc);padding-left:4px}
.ld-row .rk{color:var(--dim);font-size:10px;text-align:right}
.ld-row .v{text-align:right}
.ld-row .vol{text-align:right;color:var(--dim);font-size:10px}
.ld-bar{position:relative;height:9px}
.ld-bar .avg{position:absolute;top:-4px;bottom:-4px;width:1px;background:var(--muted)}
.ld-bar .f{position:absolute;top:0;bottom:0;border-radius:0 2px 2px 0}
.ld-bar .f.neg{border-radius:2px 0 0 2px}
.ld-foot{display:flex;justify-content:space-between;padding:4px 10px 6px;font-size:10px;color:var(--dim)}

/* tells / routes */
.tl-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:8px}
.tl-row{display:grid;grid-template-columns:110px 70px minmax(0,1fr) 54px 50px;gap:8px;align-items:center;padding:4px 10px;font-size:11px;border-bottom:1px solid var(--row-rule)}
.tl-row.h{font-size:9.5px;color:var(--dim);letter-spacing:.06em;border-bottom:1px solid var(--head-rule)}
.tl-grp{padding:8px 10px 2px;font-size:10px;letter-spacing:.08em;color:var(--amber)}
.tl-bar{position:relative;height:10px;background:#161B22}
.tl-bar .f{position:absolute;left:0;top:0;bottom:0;background:var(--blue-bar);border-radius:0 2px 2px 0}
.tl-bar .lg{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--on)}
.tl-sum{padding:8px 10px;font-size:11px;color:var(--label);line-height:1.45;border-bottom:1px solid var(--border)}
.rt-row{display:grid;grid-template-columns:130px minmax(0,1fr) 40px 48px 48px 48px 44px;gap:8px;align-items:center;padding:3px 10px;font-size:11px;border-bottom:1px solid var(--row-rule)}
.rt-row.h{font-size:9.5px;color:var(--dim);letter-spacing:.06em;border-bottom:1px solid var(--head-rule)}
.rt-vs{display:flex;gap:14px;padding:6px 10px;font-size:10.5px;color:var(--muted)}
.pos{color:var(--blue)} .neg{color:var(--orange)}

/* status */
.st-table{width:100%;border-collapse:collapse;font-size:11.5px}
.st-table th{text-align:left;font-size:9.5px;letter-spacing:.06em;color:var(--dim);font-weight:500;border-bottom:1px solid var(--head-rule);padding:5px 8px}
.st-table td{padding:7px 8px;border-bottom:1px solid var(--row-rule);vertical-align:top;line-height:1.4;color:var(--label)}
.st-table td:first-child{color:var(--on);font-weight:600;white-space:nowrap}
.st-badge{font-size:9.5px;font-weight:600;letter-spacing:.05em;padding:1px 6px;white-space:nowrap;border:1px solid}
`;
const styleEl = document.createElement('style');
styleEl.textContent = css;
document.head.appendChild(styleEl);

const L = {
  tab: 'leaders',
  replay: { sources: null, src: null, games: null, gameId: null, plays: null, filter: 'all', q: '', playId: null,
            data: null, loading: false, error: null, t: 0, playing: false, speed: 1, trails: true, routes: false,
            space: false, pressure: true, chart2: 'catch' },
  draft: { group: 'receivers', season: 2025, sort: null, desc: true, data: {}, loading: false, error: null },
  leaders: { data: null, loading: false, error: null, dir: 'top' },
  tells: { team: 'KC', data: {}, loading: false, error: null },
  routes: { team: 'KC', data: {}, loading: false, error: null },
};

const esc = s => escapeHtml(s);
const fmt = (v, d = 1) => v == null || Number.isNaN(v) ? '—' : Number(v).toFixed(d);
const pct = (v, d = 0) => v == null ? '—' : (100 * v).toFixed(d) + '%';
const sgn = (v, d = 2) => v == null ? '—' : (v > 0 ? '+' : '') + Number(v).toFixed(d);
const kalshiCode = c => ({ JAX: 'JAC', LA: 'LAR' })[c] || c;

async function getJSON(url) {
  const r = await fetch(url);
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || 'request failed');
  return d;
}

function root() { return document.getElementById('labView'); }

let tip;
function showTip(html, ev) {
  if (!tip) { tip = document.createElement('div'); tip.className = 'lab-tip'; document.body.appendChild(tip); }
  tip.innerHTML = html;
  tip.style.display = 'block';
  const w = tip.offsetWidth, h = tip.offsetHeight;
  let x = ev.clientX + 14, y = ev.clientY + 14;
  if (x + w > innerWidth - 8) x = ev.clientX - w - 14;
  if (y + h > innerHeight - 8) y = ev.clientY - h - 14;
  tip.style.left = x + 'px'; tip.style.top = y + 'px';
}
function hideTip() { if (tip) tip.style.display = 'none'; }

// ---------------------------------------------------------------- shell
const TABS = [['leaders', 'NGS LEADERS'], ['replay', 'ALL-22 REPLAY'], ['draft', 'DRAFT CLASSES'], ['tells', 'PRE-SNAP TELLS'],
              ['routes', 'ROUTE TREES'], ['status', 'BUILD STATUS']];
// Only NGS LEADERS (and the this-season half of PRE-SNAP TELLS) is current data; everything else here is history.
const SRC = {
  get replay() { const s = L.replay.sources && L.replay.sources.find(x => x.id === L.replay.src); return s ? 'HISTORICAL · ' + s.note : 'NFL Big Data Bowl tracking · historical'; },
  draft: 'HISTORICAL · NFL Big Data Bowl 2027 · 2023-25 draft classes (510 players) · 2023-25 seasons',
  leaders: 'nflverse Next Gen Stats + PFR charting · this season',
  tells: 'nflverse FTN charting (this season) + participation (last season)',
  routes: 'nflverse participation + pbp · last season',
  status: 'NFL Big Data Bowl Viz Dev Report, Oct 2026',
};

window.renderLab = function () {
  const el = root();
  if (!el.dataset.built) {
    el.dataset.built = '1';
    el.innerHTML = `<div class="lab-tabs"></div><div class="lab-body"></div>`;
    el.addEventListener('click', onClick);
    el.addEventListener('input', onInput);
  }
  el.querySelector('.lab-tabs').innerHTML = `<span class="ttl">VIZ LAB</span>` +
    TABS.map(([k, n]) => `<button type="button" data-lab-tab="${k}" class="${L.tab === k ? 'on' : ''}">${n}</button>`).join('') +
    `<span class="src">${esc(SRC[L.tab])}</span>`;
  if (L.tab !== 'replay') stop();
  ({ replay: renderReplay, leaders: renderLeaders, draft: renderDraft, tells: renderTells, routes: renderRoutes, status: renderStatus })[L.tab]();
};

function body() { return root().querySelector('.lab-body'); }

function onClick(e) {
  const t = e.target.closest('[data-lab-tab],[data-lab-play],[data-lab-filter],[data-lab-tog],[data-lab-speed],[data-lab-toggle-play],[data-lab-team],[data-lab-dir],[data-lab-src],[data-lab-c2],[data-lab-group],[data-lab-season],[data-lab-sort]');
  if (!t) return;
  const d = t.dataset;
  if (d.labTab) { L.tab = d.labTab; renderLab(); return; }
  if (d.labPlay) { openPlay(Number(d.labPlay)); return; }
  if (d.labFilter) { L.replay.filter = d.labFilter; renderPlayList(); return; }
  if (d.labTog) { L.replay[d.labTog] = !L.replay[d.labTog]; t.classList.toggle('on', L.replay[d.labTog]); drawField(); return; }
  if (d.labSpeed) { L.replay.speed = Number(d.labSpeed); renderControls(); return; }
  if ('labTogglePlay' in d) { togglePlay(); return; }
  if (d.labTeam) {
    const tab = L.tab === 'routes' ? L.routes : L.tells;
    tab.team = d.labTeam; renderLab(); return;
  }
  if (d.labDir) { L.leaders.dir = d.labDir; renderLeaders(); return; }
  if (d.labSrc) { if (d.labSrc !== L.replay.src) switchSource(d.labSrc); return; }
  if (d.labC2) { L.replay.chart2 = d.labC2; drawCharts(); return; }
  if (d.labGroup) { L.draft.group = d.labGroup; L.draft.sort = null; renderDraft(); return; }
  if (d.labSeason) { L.draft.season = Number(d.labSeason); renderDraft(); return; }
  if (d.labSort) { const D = L.draft; D.desc = D.sort === d.labSort ? !D.desc : true; D.sort = d.labSort; renderDraft(); return; }
}

function onInput(e) {
  if (e.target.matches('[data-lab-q]')) { L.replay.q = e.target.value; renderPlayList(); }
  if (e.target.matches('[data-lab-scrub]')) { stop(); L.replay.t = Number(e.target.value); drawFrame(); }
  if (e.target.matches('[data-lab-game]')) switchGame(Number(e.target.value));
}

document.addEventListener('keydown', e => {
  if (state.module !== 'lab' || L.tab !== 'replay' || !L.replay.data) return;
  if (e.target.closest('input,textarea') && !e.target.matches('[data-lab-scrub]')) return;
  if (e.key === ' ') { e.preventDefault(); togglePlay(); }
  else if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
    e.preventDefault(); stop();
    L.replay.t = Math.max(0, Math.min(L.replay.data.frames - 1, Math.round(L.replay.t) + (e.key === 'ArrowRight' ? 1 : -1)));
    drawFrame();
  }
});

// ---------------------------------------------------------------- REPLAY
async function renderReplay() {
  const R = L.replay;
  const b = body();
  if (!R.plays) {
    b.innerHTML = `<div class="empty">Loading tracking…</div>`;
    try {
      if (!R.sources) { R.sources = (await getJSON('/api/lab/bdb/sources')).sources; R.src = R.sources[0].id; }
      await loadGames();
    } catch (e) { b.innerHTML = `<div class="empty" style="color:var(--orange)">${esc(e.message)}</div>`; return; }
    if (L.tab !== 'replay') return;
    renderLab();
    return;
  }
  const weeks = [...new Set(R.games.map(x => x.week))];
  const gameLabel = x => `Wk ${x.week} · ${x.away}${x.awayScore != null ? ' ' + x.awayScore : ''} @ ${x.home}${x.homeScore != null ? ' ' + x.homeScore : ''}`;
  b.innerHTML = `<div class="rp">
    <div class="panel rp-list">
      <div class="panel-head">${R.sources.map(x => `<button type="button" class="tab small ${x.id === R.src ? 'on' : ''}" data-lab-src="${x.id}" title="${esc(x.note)}">${esc(x.label)}</button>`).join('')}</div>
      <div style="padding:6px 8px 0"><select data-lab-game aria-label="Game" class="rp-game">${weeks.map(w => `<optgroup label="Week ${w}">${R.games.filter(x => x.week === w).map(x => `<option value="${x.gameId}" ${x.gameId === R.gameId ? 'selected' : ''}>${esc(gameLabel(x))}</option>`).join('')}</optgroup>`).join('')}</select></div>
      <div class="rp-filt">${(R.src === '2021' ? ['all'] : ['all', 'pass', 'run', 'st']).map(k => `<button type="button" class="lab-chip" data-lab-filter="${k}">${{ all: 'ALL', pass: 'PASS', run: 'RUN', st: 'SPECIAL' }[k]}</button>`).join('')}
        <input data-lab-q placeholder="Filter plays: player, TOUCHDOWN, sacked…" value="${esc(R.q)}"></div>
      <div class="rp-plays"></div>
    </div>
    <div class="rp-main">
      <div class="panel"><div class="rp-head" id="rpHead"></div>
        <div class="rp-field"><canvas id="rpCanvas"></canvas></div>
        <div class="rp-ctl" id="rpCtl"></div>
        <div class="rp-key"><span><i style="background:${OFF}"></i>Offense</span><span><i style="background:${DEF}"></i>Defense</span><span><i style="background:${BALL}"></i>Ball</span><span><i style="background:none;border:2px solid var(--amber)"></i>Target / ball carrier</span><span id="rpKeyPff" style="display:none"><i style="background:none;border:2px dashed #fff"></i>PFF hit / hurry / sack</span><span style="color:var(--dim)">— line of scrimmage &nbsp;<span style="color:var(--amber)">—</span> first down · space/←/→ to play &amp; step</span></div>
      </div>
      <div class="rp-charts">
        <div class="panel rp-chart"><div class="panel-head"><span class="panel-title" id="rpC1t">POCKET PRESSURE INDEX</span></div><svg id="rpC1"></svg><div class="lab-note" id="rpC1n" style="padding:0 10px 8px"></div></div>
        <div class="panel rp-chart"><div class="panel-head" id="rpC2h"></div><svg id="rpC2"></svg><div class="rp-legend" id="rpC2l"></div><div class="lab-note" id="rpC2n" style="padding:0 10px 8px"></div></div>
      </div>
    </div></div>`;
  renderPlayList();
  const cv = document.getElementById('rpCanvas');
  cv.addEventListener('mousemove', canvasHover);
  cv.addEventListener('mouseleave', hideTip);
  if (!fieldObserver) {
    fieldObserver = new ResizeObserver(() => { if (L.tab === 'replay' && R.data) { sizeCanvas(); drawFrame(); } });
  }
  fieldObserver.disconnect();
  fieldObserver.observe(cv.parentElement);
  if (R.data && R.data.playId === R.playId) { renderPlayMeta(); sizeCanvas(); drawFrame(); }
  else openPlay(R.playId);
}
let fieldObserver = null;

async function loadGames() {
  const R = L.replay;
  R.games = (await getJSON(`/api/lab/bdb/games?src=${R.src}`)).games;
  // 2017: open on the 2019 tutorial's game; 2021: the season opener.
  R.gameId = (R.games.find(x => x.gameId === 2017090700) || R.games[0]).gameId;
  await loadPlays();
}

async function loadPlays() {
  const R = L.replay;
  R.plays = (await getJSON(`/api/lab/bdb/plays?src=${R.src}&game=${R.gameId}`)).plays;
  // Default play: Tyreek Hill's 75-yd TD (2017 opener), else the game's longest completion.
  const hill = R.plays.find(p => /T\.Hill for 75 yards/.test(p.desc));
  const best = R.plays.filter(p => p.passResult === 'C').sort((a, b) => (b.yards || 0) - (a.yards || 0))[0];
  R.playId = (hill || best || R.plays[0]).playId;
  R.data = null; R.q = ''; R.filter = 'all';
}

async function switchSource(src) {
  const R = L.replay;
  stop(); R.src = src; R.plays = null;
  renderLab();
}

async function switchGame(gameId) {
  const R = L.replay;
  stop(); R.gameId = gameId;
  const box = root().querySelector('.rp-plays');
  if (box) box.innerHTML = `<div class="empty">Loading game${R.src === '2017' ? ' (first open fetches ~30 MB of tracking)' : ''}…</div>`;
  try { await loadPlays(); } catch (e) { if (box) box.innerHTML = `<div class="empty" style="color:var(--orange)">${esc(e.message)}</div>`; return; }
  renderLab();
}

const KIND = { pass: ['PASS', OFF], run: ['RUN', '#24A084'], st: ['ST', '#A070C8'] };

function renderPlayList() {
  const R = L.replay;
  const box = root().querySelector('.rp-plays');
  if (!box) return;
  root().querySelectorAll('[data-lab-filter]').forEach(b => b.classList.toggle('on', b.dataset.labFilter === R.filter));
  const q = R.q.trim().toLowerCase();
  const rows = R.plays.filter(p => (R.filter === 'all' || p.kind === R.filter) && (!q || p.desc.toLowerCase().includes(q)));
  box.innerHTML = rows.map(p => {
    const [k, c] = KIND[p.kind];
    const dd = p.down ? `${p.down}&${p.togo}` : (p.special || '').toUpperCase().slice(0, 7);
    return `<div class="rp-play ${p.playId === R.playId ? 'on' : ''}" data-lab-play="${p.playId}">
      <div class="q mono">Q${p.qtr}<br>${esc(p.clock)}</div>
      <div><div class="d"><span class="k" style="color:${c};border:1px solid ${c}">${k}</span><span class="mono" style="color:var(--dim)">${esc(p.offense)} ${dd}</span> ${esc(p.desc)}</div></div></div>`;
  }).join('') || `<div class="empty">No plays match.</div>`;
}

async function openPlay(playId) {
  const R = L.replay;
  stop();
  R.playId = playId;
  root().querySelectorAll('.rp-play').forEach(el => el.classList.toggle('on', Number(el.dataset.labPlay) === playId));
  const sel = root().querySelector('.rp-play.on');
  if (sel) sel.scrollIntoView({ block: 'nearest' });
  try {
    const d = await getJSON(`/api/lab/bdb/play?src=${R.src}&game=${R.gameId}&play=${playId}`);
    if (R.playId !== playId) return;
    prepPlay(d);
    R.data = d;
    R.t = Math.max(0, d.snapFrame - 6);
  } catch (e) { document.getElementById('rpHead').innerHTML = `<span style="color:var(--orange)">${esc(e.message)}</span>`; return; }
  renderPlayMeta();
  sizeCanvas();
  drawFrame();
  play();
}

// Precompute velocities (finite differences) and the receiver order once per play.
function prepPlay(d) {
  for (const p of d.players) {
    p.vx = p.x.map((v, i) => (v == null || p.x[i - 1] == null) ? 0 : (v - p.x[i - 1]) * FPS);
    p.vy = p.y.map((v, i) => (v == null || p.y[i - 1] == null) ? 0 : (v - p.y[i - 1]) * FPS);
  }
  const ids = Object.keys(d.separation || {}).length ? Object.keys(d.separation) : Object.keys(d.catchProb || {});
  const byId = Object.fromEntries(d.players.filter(p => p.id != null).map(p => [String(p.id), p]));
  ids.sort((a, b) => (a == d.focusId ? -1 : b == d.focusId ? 1 : 0) || (byId[a].jersey || 0) - (byId[b].jersey || 0));
  d.recv = ids.map((id, i) => ({ id, p: byId[id], color: i < RECV.length ? RECV[i] : OTHER, focus: String(d.focusId) === id }));
  d.evName = Object.fromEntries(d.events.map(e => [e.f, e.name]));
  d.throwF = (d.events.find(e => e.name === 'pass_forward' || e.name === 'pass_shovel') || {}).f;
  d.endF = (d.events.find(e => ['pass_forward', 'pass_shovel', 'qb_sack', 'handoff', 'run'].includes(e.name)) || {}).f;
  d.pffIds = new Set((d.pff || []).filter(x => x.events.length).map(x => x.id));
}

function renderPlayMeta() {
  const d = L.replay.data;
  const off = d.offense, def = d.offense === d.home ? d.away : d.home;
  const dd = d.down ? `${['', '1st', '2nd', '3rd', '4th'][d.down]} & ${d.togo}` : 'Special teams';
  const bits = [d.season + ' Wk ' + d.week, d.formation, d.personnelO && 'O: ' + d.personnelO, d.personnelD && 'D: ' + d.personnelD,
                d.box != null && d.box + ' in box', d.rushers != null && d.rushers + ' rushers', d.coverage && 'PFF: ' + d.coverage,
                d.endsAtThrow && 'tracking ends at the throw / sack'].filter(Boolean);
  const keyPff = document.getElementById('rpKeyPff');
  if (keyPff) keyPff.style.display = d.source === '2021' ? '' : 'none';
  document.getElementById('rpHead').innerHTML = `
    <div><div class="dd">${esc(off)} <span style="color:var(--dim);font-weight:400">vs</span> ${esc(def)}</div>
      <div class="meta mono">Q${d.qtr} ${esc(d.clock)} · ${dd}</div></div>
    <div class="desc">${esc(d.desc)}<div class="meta">${bits.map(esc).join(' · ')}</div></div>`;
  renderControls();
  drawCharts();
}

const EV_SHORT = { ball_snap: 'SNAP', pass_forward: 'THROW', pass_arrived: 'ARRIVE', pass_outcome_caught: 'CATCH',
  pass_outcome_incomplete: 'INC', handoff: 'HANDOFF', tackle: 'TACKLE', touchdown: 'TD', qb_sack: 'SACK',
  first_contact: 'CONTACT', out_of_bounds: 'OOB', punt: 'PUNT', kickoff: 'KICK', punt_received: 'REC',
  kick_received: 'REC', fumble: 'FUMBLE', pass_outcome_touchdown: 'TD', man_in_motion: 'MOTION', pass_shovel: 'SHOVEL' };

function renderControls() {
  const R = L.replay, d = R.data;
  if (!d) return;
  const n = d.frames - 1;
  const evs = d.events.filter(e => EV_SHORT[e.name]);
  // Drop labels that would collide with the previous one.
  let last = -99;
  const marks = evs.filter(e => { const ok = (e.f - last) / n > 0.06; if (ok) last = e.f; return ok; });
  document.getElementById('rpCtl').innerHTML = `
    <button type="button" class="play" data-lab-toggle-play aria-label="${R.playing ? 'Pause' : 'Play'}">${R.playing
      ? '<svg width="10" height="10"><rect x="1" y="0" width="3" height="10" fill="currentColor"/><rect x="6" y="0" width="3" height="10" fill="currentColor"/></svg>'
      : '<svg width="10" height="10"><path d="M1 0l9 5-9 5z" fill="currentColor"/></svg>'}</button>
    <span class="clock mono" id="rpClock"></span>
    <div class="rp-scrub"><input type="range" min="0" max="${n}" step="0.01" value="${R.t}" data-lab-scrub aria-label="Frame">
      ${marks.map(e => `<span class="ev" style="left:${(100 * (e.f - 1) / n).toFixed(2)}%">${EV_SHORT[e.name]}</span>`).join('')}</div>
    <div class="rp-tog">${[0.5, 1, 2].map(s => `<button type="button" class="lab-chip ${R.speed === s ? 'on' : ''}" data-lab-speed="${s}">${s}×</button>`).join('')}</div>
    <div class="rp-tog">${[['trails', 'Trails'], ['routes', 'Full routes'], ['space', 'Space control'], ['pressure', 'Pocket pressure']]
      .map(([k, n]) => `<button type="button" class="lab-chip ${R[k] ? 'on' : ''}" data-lab-tog="${k}">${n}</button>`).join('')}</div>`;
  updateClock();
}

function updateClock() {
  const R = L.replay, d = R.data;
  const c = document.getElementById('rpClock');
  if (!c || !d) return;
  const s = (R.t + 1 - d.snapFrame) / FPS;
  c.textContent = (s < 0 ? '−' : '+') + Math.abs(s).toFixed(1) + 's';
  const sc = root().querySelector('[data-lab-scrub]');
  if (sc && document.activeElement !== sc) sc.value = R.t;
}

// ---- animation loop
let raf = 0, lastTs = 0;
function play() {
  const R = L.replay;
  if (!R.data) return;
  if (R.t >= R.data.frames - 1) R.t = 0;
  R.playing = true; lastTs = 0;
  renderControls();
  cancelAnimationFrame(raf);
  raf = requestAnimationFrame(tick);
}
function stop() {
  cancelAnimationFrame(raf);
  if (L.replay.playing) { L.replay.playing = false; if (L.tab === 'replay') renderControls(); }
}
function togglePlay() { L.replay.playing ? stop() : play(); }
function tick(ts) {
  const R = L.replay;
  if (!R.playing || state.module !== 'lab' || L.tab !== 'replay') { stop(); return; }
  if (lastTs) R.t += (ts - lastTs) / 1000 * FPS * R.speed;
  lastTs = ts;
  if (R.t >= R.data.frames - 1) { R.t = R.data.frames - 1; drawFrame(); stop(); return; }
  drawFrame();
  raf = requestAnimationFrame(tick);
}

function drawFrame() { drawField(); updateClock(); drawPlayheads(); }

// ---- field
let geo = null;
function sizeCanvas() {
  const cv = document.getElementById('rpCanvas');
  if (!cv) return;
  const w = cv.parentElement.clientWidth;
  // Keep the whole field + the charts on screen: cap height by the viewport.
  const maxH = Math.max(240, innerHeight - 470);
  const W = Math.min(w, maxH * 120 / 53.3);
  const H = W * 53.3 / 120;
  const dpr = devicePixelRatio || 1;
  cv.width = W * dpr; cv.height = H * dpr;
  cv.style.width = W + 'px'; cv.style.height = H + 'px';
  cv.style.margin = '0 auto';
  geo = { W, H, s: W / 120, dpr };
}
const X = x => x * geo.s, Y = y => geo.H - y * geo.s;

function lerpAt(arr, t) {
  const i = Math.floor(t), f = t - i;
  const a = arr[i], b = arr[Math.min(i + 1, arr.length - 1)];
  if (a == null) return b;
  if (b == null) return a;
  return a + (b - a) * f;
}

function drawField() {
  const R = L.replay, d = R.data;
  const cv = document.getElementById('rpCanvas');
  if (!cv || !d || !geo) return;
  const c = cv.getContext('2d');
  c.setTransform(geo.dpr, 0, 0, geo.dpr, 0, 0);
  const { W, H, s } = geo;
  c.fillStyle = '#0C1610'; c.fillRect(0, 0, W, H);
  // end zones
  c.fillStyle = '#10201A'; c.fillRect(0, 0, X(10), H); c.fillRect(X(110), 0, X(10), H);
  c.strokeStyle = '#22382B'; c.lineWidth = 1;
  for (let yd = 10; yd <= 110; yd += 5) {
    c.globalAlpha = yd % 10 ? 0.5 : 1;
    c.beginPath(); c.moveTo(X(yd) + 0.5, 0); c.lineTo(X(yd) + 0.5, H); c.stroke();
  }
  c.globalAlpha = 1;
  // hashes (NFL: 70'9" from each sideline)
  c.beginPath();
  for (let yd = 11; yd < 110; yd++) for (const hy of [0.7, 23.36, 29.96, 52.6]) { c.moveTo(X(yd) + 0.5, Y(hy)); c.lineTo(X(yd) + 0.5, Y(hy) + (hy > 26 ? 4 : -4)); }
  c.stroke();
  c.fillStyle = '#2E4A39'; c.font = `600 ${Math.max(10, s * 2)}px "IBM Plex Sans Condensed"`; c.textAlign = 'center'; c.textBaseline = 'middle';
  for (let yd = 20; yd <= 100; yd += 10) {
    const lab = String(yd <= 60 ? yd - 10 : 110 - yd);
    c.fillText(lab, X(yd), Y(12)); c.fillText(lab, X(yd), Y(41.3));
  }
  const t = R.t;
  // space control: each 1-yd cell goes to whoever's projected spot (0.5 s ahead) is nearest
  if (R.space) drawSpace(c, d, t);
  // LOS + first down
  if (d.los != null) { c.strokeStyle = '#5AA9FF'; c.lineWidth = 2; c.globalAlpha = 0.8; c.beginPath(); c.moveTo(X(d.los), 0); c.lineTo(X(d.los), H); c.stroke(); }
  if (d.firstDown != null && d.firstDown > 0 && d.firstDown < 120) { c.strokeStyle = '#F2A541'; c.beginPath(); c.moveTo(X(d.firstDown), 0); c.lineTo(X(d.firstDown), H); c.stroke(); }
  c.globalAlpha = 1;
  const fi = Math.round(t);
  // pocket pressure: ring around the QB + spokes from each defender inside it
  const qb = d.qbId != null && d.players.find(p => p.id === d.qbId);
  const pressureOn = R.pressure && qb && d.pressure && fi >= d.snapFrame - 1 && (!d.endF || fi <= d.endF + 5);
  if (pressureOn) {
    const qx = lerpAt(qb.x, t), qy = lerpAt(qb.y, t);
    const pv = d.pressure[fi] || 0;
    const g = c.createRadialGradient(X(qx), Y(qy), 0, X(qx), Y(qy), 7 * s);
    g.addColorStop(0, `rgba(224,96,43,${Math.min(0.55, pv / 6)})`); g.addColorStop(1, 'rgba(224,96,43,0)');
    c.fillStyle = g; c.beginPath(); c.arc(X(qx), Y(qy), 7 * s, 0, 2 * Math.PI); c.fill();
    c.strokeStyle = 'rgba(224,96,43,0.45)'; c.setLineDash([3, 3]); c.lineWidth = 1; c.stroke(); c.setLineDash([]);
    for (const p of d.players) {
      if (p.side === d.offenseSide || p.side === 'ball') continue;
      const px = lerpAt(p.x, t), py = lerpAt(p.y, t);
      const dist = Math.hypot(px - qx, py - qy);
      if (dist >= 7) continue;
      c.strokeStyle = DEF; c.globalAlpha = 0.25 + 0.6 * (1 - dist / 7); c.lineWidth = 1 + 3 * (1 - dist / 7);
      c.beginPath(); c.moveTo(X(px), Y(py)); c.lineTo(X(qx), Y(qy)); c.stroke();
    }
    c.globalAlpha = 1;
  }
  // trails / routes
  for (const p of d.players) {
    if (p.side === 'ball') continue;
    const col = p.side === d.offenseSide ? OFF : DEF;
    const isRecv = d.recv.some(r => r.p === p);
    const from = R.routes && isRecv ? d.snapFrame - 1 : R.trails ? Math.max(0, fi - 12) : fi;
    if (from >= fi) continue;
    c.strokeStyle = R.routes && isRecv ? d.recv.find(r => r.p === p).color : col;
    c.lineWidth = R.routes && isRecv ? 2 : 1.5;
    c.globalAlpha = R.routes && isRecv ? 0.9 : 0.4;
    c.beginPath();
    let started = false;
    for (let i = from; i <= fi; i++) {
      if (p.x[i] == null) continue;
      started ? c.lineTo(X(p.x[i]), Y(p.y[i])) : c.moveTo(X(p.x[i]), Y(p.y[i]));
      started = true;
    }
    c.lineTo(X(lerpAt(p.x, t)), Y(lerpAt(p.y, t)));
    c.stroke();
  }
  c.globalAlpha = 1;
  // players
  const r = Math.max(6.5, 0.95 * s);
  c.font = `600 ${Math.max(8, r * 1.05)}px "IBM Plex Mono"`;
  geo.hits = [];
  for (const p of d.players) {
    const px = lerpAt(p.x, t), py = lerpAt(p.y, t);
    if (px == null) continue;
    if (p.side === 'ball') continue;
    const col = p.side === d.offenseSide ? OFF : DEF;
    c.fillStyle = col; c.strokeStyle = '#0C1610'; c.lineWidth = 2;
    c.beginPath(); c.arc(X(px), Y(py), r, 0, 2 * Math.PI); c.fill(); c.stroke();
    if (p.id === d.focusId) { c.strokeStyle = '#F2A541'; c.lineWidth = 2; c.beginPath(); c.arc(X(px), Y(py), r + 3, 0, 2 * Math.PI); c.stroke(); }
    if (d.pffIds.has(p.id)) { c.strokeStyle = '#fff'; c.lineWidth = 1.5; c.setLineDash([2, 2]); c.beginPath(); c.arc(X(px), Y(py), r + 3, 0, 2 * Math.PI); c.stroke(); c.setLineDash([]); }
    c.fillStyle = '#fff'; c.fillText(p.jersey != null ? String(p.jersey) : '', X(px), Y(py) + 0.5);
    geo.hits.push({ p, x: X(px), y: Y(py), r: r + 4 });
  }
  const ball = d.players.find(p => p.side === 'ball');
  if (ball) {
    const bx = lerpAt(ball.x, t), by = lerpAt(ball.y, t);
    if (bx != null) {
      c.fillStyle = BALL; c.strokeStyle = '#0C1610'; c.lineWidth = 1.5;
      c.beginPath(); c.ellipse(X(bx), Y(by), Math.max(4, 0.55 * s), Math.max(2.8, 0.36 * s), 0, 0, 2 * Math.PI); c.fill(); c.stroke();
    }
  }
}

let spaceGrid = null;
function drawSpace(c, d, t) {
  const pts = [];
  for (const p of d.players) {
    if (p.side === 'ball') continue;
    const px = lerpAt(p.x, t), py = lerpAt(p.y, t);
    if (px == null) continue;
    const i = Math.round(t);
    pts.push([px + 0.5 * (p.vx[i] || 0), py + 0.5 * (p.vy[i] || 0), p.side === d.offenseSide]);
  }
  const s = geo.s;
  for (let gx = 0; gx < 120; gx++) {
    for (let gy = 0; gy < 54; gy++) {
      const cx = gx + 0.5, cy = gy + 0.5;
      let best = Infinity, off = false;
      for (const q of pts) {
        const dd = (q[0] - cx) ** 2 + (q[1] - cy) ** 2;
        if (dd < best) { best = dd; off = q[2]; }
      }
      c.fillStyle = off ? 'rgba(61,141,224,0.16)' : 'rgba(224,96,43,0.13)';
      c.fillRect(X(gx), Y(gy + 1), s + 0.5, s + 0.5);
    }
  }
}

function canvasHover(ev) {
  if (!geo || !geo.hits) return;
  const rect = ev.target.getBoundingClientRect();
  const mx = ev.clientX - rect.left, my = ev.clientY - rect.top;
  const h = geo.hits.find(h => Math.hypot(h.x - mx, h.y - my) <= h.r);
  if (!h) { hideTip(); return; }
  const p = h.p, d = L.replay.data, i = Math.round(L.replay.t);
  const sep = d.separation[String(p.id)];
  const credit = (d.pff || []).find(x => x.id === p.id);
  showTip(`<div style="font-weight:600">${esc(p.name)}</div><div class="k">${esc(p.pos || '')} #${esc(p.jersey)} · ${p.side === d.offenseSide ? 'offense' : 'defense'}${p.role ? ' · PFF: ' + esc(p.role) : ''}</div>
    ${credit ? `<div class="r"><span class="k">PFF</span><span>${esc([...credit.events, ...credit.allowed.map(a => a + ' allowed')].join(', '))}</span></div>` : ''}
    <div class="r"><span class="k">SPEED</span><span class="mono">${fmt(p.s[i] != null ? p.s[i] * 2.045 : null)} mph</span></div>
    ${sep && sep[i] != null ? `<div class="r"><span class="k">SEPARATION</span><span class="mono">${fmt(sep[i])} yd</span></div>` : ''}`, ev);
}

// ---- synced charts
const CH = { l: 34, r: 10, t: 12, b: 22 };
function chartScale(svg, d, ymax) {
  const W = svg.clientWidth || 400, H = 170;
  const f0 = d.snapFrame - 1, f1 = d.frames - 1;
  return {
    W, H, f0, f1, ymax,
    x: f => CH.l + (W - CH.l - CH.r) * (f - f0) / Math.max(1, f1 - f0),
    y: v => H - CH.b - (H - CH.t - CH.b) * Math.min(v, ymax) / ymax,
    f: px => f0 + (px - CH.l) / (W - CH.l - CH.r) * (f1 - f0),
  };
}
function axes(sc, d, yTicks, unit) {
  let g = '';
  for (const v of yTicks) g += `<line x1="${CH.l}" x2="${sc.W - CH.r}" y1="${sc.y(v)}" y2="${sc.y(v)}" stroke="#1A2027"/><text x="${CH.l - 5}" y="${sc.y(v) + 3}" text-anchor="end" fill="#7A8591" font-size="9.5">${v}${unit}</text>`;
  for (let s = 0; (sc.f0 + s * FPS) <= sc.f1; s++) {
    const x = sc.x(sc.f0 + s * FPS);
    g += `<text x="${x}" y="${sc.H - 6}" text-anchor="middle" fill="#7A8591" font-size="9.5">${s}s</text>`;
  }
  for (const e of d.events) {
    if (!['pass_forward', 'pass_arrived', 'handoff', 'qb_sack', 'pass_shovel'].includes(e.name)) continue;
    const x = sc.x(e.f - 1);
    g += `<line x1="${x}" x2="${x}" y1="${CH.t}" y2="${sc.H - CH.b}" stroke="#3A434E" stroke-dasharray="3 3"/><text x="${x + 3}" y="${CH.t + 6}" fill="#9AA4AF" font-size="9">${EV_SHORT[e.name]}</text>`;
  }
  return g;
}
function path(arr, sc, from) {
  let s = '', pen = false;
  for (let i = from; i < arr.length; i++) {
    if (arr[i] == null) { pen = false; continue; }
    s += (pen ? 'L' : 'M') + sc.x(i).toFixed(1) + ' ' + sc.y(arr[i]).toFixed(1);
    pen = true;
  }
  return s;
}

function drawCharts() {
  const d = L.replay.data;
  const c1 = document.getElementById('rpC1'), c2 = document.getElementById('rpC2');
  if (!c1) return;
  // Chart 1: pressure on passes, ball-carrier speed otherwise.
  let series1, title, unit, note;
  const focus = d.players.find(p => p.id === d.focusId);
  if (d.pressure) {
    // Pressure only means something until the ball is out (or the sack).
    series1 = d.pressure.map((v, i) => d.endF && i > d.endF + 4 ? null : v); title = 'POCKET PRESSURE INDEX'; unit = '';
    note = (d.source === '2021' ? 'Each PFF-charted rusher' : 'Each defender') + ' within 7 yd of the QB adds 1 − d/7, up to doubled by closing speed. A CPP-style read after the 2023 winners (no trained model).';
    const pc = d.pressureCheck;
    if (pc) note += ` Checked against PFF across ${pc.dropbacks.toLocaleString()} 2021 dropbacks: AUC ${pc.auc.toFixed(2)}; plays in the top fifth of peaks drew charted pressure ${pct(pc.quintiles[4].pressure_rate)} of the time vs ${pct(pc.quintiles[0].pressure_rate)} in the bottom fifth.`;
    const cred = (d.pff || []).filter(x => x.events.length);
    if (d.source === '2021') note += cred.length ? ' This play — PFF credits: ' + cred.map(x => `${x.name} (${x.events.join('/')})`).join(', ') + '.' : ' This play — PFF charted no pressure.';
  } else if (focus) {
    series1 = focus.s.map((v, i) => i < d.snapFrame - 1 || v == null ? null : v * 2.045); title = `${focus.name.toUpperCase()} · SPEED (MPH)`; unit = '';
    note = 'Ball-carrier speed from the tracking feed.';
  }
  document.getElementById('rpC1t').textContent = title || 'NO QB / CARRIER ON THIS PLAY';
  document.getElementById('rpC1n').textContent = note || '';
  if (series1) {
    const mx = Math.max(1, ...series1.filter(v => v != null));
    const ymax = Math.ceil(mx / (d.pressure ? 1 : 5)) * (d.pressure ? 1 : 5);
    const sc = chartScale(c1, d, ymax);
    const ticks = [0, ymax / 2, ymax].map(v => +v.toFixed(1));
    c1._sc = sc; c1._series = [{ arr: series1, name: d.pressure ? 'Pressure' : 'Speed', color: d.pressure ? DEF : '#24A084', dec: 2 }];
    c1.setAttribute('viewBox', `0 0 ${sc.W} ${sc.H}`);
    c1.innerHTML = axes(sc, d, ticks, unit) +
      `<path d="${path(series1, sc, d.snapFrame - 1)}" fill="none" stroke="${c1._series[0].color}" stroke-width="2" stroke-linejoin="round"/>` +
      `<line class="ph" x1="0" x2="0" y1="${CH.t}" y2="${sc.H - CH.b}" stroke="#F2A541" stroke-width="1"/><g class="hv"></g>`;
  } else { c1.innerHTML = ''; c1._sc = null; }
  // Chart 2: per eligible receiver -- "if thrown now" catch probability, or separation.
  const recv = d.recv;
  const hasCatch = Object.keys(d.catchProb || {}).length > 0;
  const mode = hasCatch && L.replay.chart2 === 'catch' ? 'catch' : 'sep';
  document.getElementById('rpC2h').innerHTML = (hasCatch ? `<button type="button" class="tab small ${mode === 'catch' ? 'on' : ''}" data-lab-c2="catch">CATCH PROB · IF THROWN NOW</button>` : '') +
    `<button type="button" class="tab small ${mode === 'sep' ? 'on' : ''}" data-lab-c2="sep">SEPARATION · YD TO NEAREST COVER DEFENDER</button>`;
  const mc = d.model;
  document.getElementById('rpC2n').textContent = mode === 'catch' && mc
    ? `Logistic model trained on ${mc.source.replace('NFL Big Data Bowl 2026 Analytics (Kaggle), ', 'BDB 2026: ')}; holdout ${mc.holdout_weeks} AUC ${mc.auc.toFixed(2)}, log loss ${mc.log_loss.toFixed(3)} vs ${mc.baseline_log_loss.toFixed(3)} baseline. Learned only from passes actually thrown to that receiver, so it reads "if he'd been the target here", not who the QB should pick.`
    : 'Distance from each receiver to the nearest coverage defender (pass rushers excluded).';
  const seriesOf = id => mode === 'catch' ? (d.catchProb[id] || []).map(v => v == null ? null : 100 * v) : d.separation[id] || [];
  if (recv.length) {
    const all = recv.flatMap(r => seriesOf(r.id).filter(v => v != null));
    const ymax = mode === 'catch' ? 100 : Math.min(20, Math.ceil(Math.max(5, ...all) / 5) * 5);
    const sc = chartScale(c2, d, ymax);
    c2._sc = sc; c2._series = recv.map(r => ({ arr: seriesOf(r.id), name: r.p.name, color: r.color, dec: mode === 'catch' ? 0 : 1 }));
    c2.setAttribute('viewBox', `0 0 ${sc.W} ${sc.H}`);
    c2.innerHTML = axes(sc, d, mode === 'catch' ? [0, 50, 100] : [0, ymax / 2, ymax], mode === 'catch' ? '%' : '') +
      recv.slice().reverse().map(r => `<path d="${path(seriesOf(r.id), sc, d.snapFrame - 1)}" fill="none" stroke="${r.color}" stroke-width="${r.focus ? 2.5 : 1.5}" opacity="${r.focus || !d.focusId ? 1 : 0.75}" stroke-linejoin="round"/>`).join('') +
      `<line class="ph" x1="0" x2="0" y1="${CH.t}" y2="${sc.H - CH.b}" stroke="#F2A541" stroke-width="1"/><g class="hv"></g>`;
    document.getElementById('rpC2l').innerHTML = recv.map(r => `<span><i style="background:${r.color};height:${r.focus ? 3 : 2}px"></i>${esc(r.p.name)} <span style="color:var(--dim)">${esc(r.p.pos)} #${esc(r.p.jersey)}</span>${r.focus ? ' <b style="color:var(--amber);font-size:9.5px">TARGET</b>' : ''}</span>`).join('');
  } else { c2.innerHTML = ''; c2._sc = null; document.getElementById('rpC2l').innerHTML = '<span style="color:var(--dim)">No eligible receivers tracked.</span>'; }
  for (const svg of [c1, c2]) {
    if (svg._wired) continue;
    svg._wired = true;
    svg.addEventListener('mousemove', chartHover);
    svg.addEventListener('mouseleave', ev => { hideTip(); const g = ev.currentTarget.querySelector('.hv'); if (g) g.innerHTML = ''; });
    svg.addEventListener('click', ev => {
      const sc = ev.currentTarget._sc; if (!sc) return;
      const rect = ev.currentTarget.getBoundingClientRect();
      stop(); L.replay.t = Math.max(0, Math.min(L.replay.data.frames - 1, Math.round(sc.f((ev.clientX - rect.left) * sc.W / rect.width))));
      drawFrame();
    });
  }
  drawPlayheads();
}

function chartHover(ev) {
  const svg = ev.currentTarget, sc = svg._sc;
  if (!sc) return;
  const rect = svg.getBoundingClientRect();
  const f = Math.round(sc.f((ev.clientX - rect.left) * sc.W / rect.width));
  if (f < sc.f0 || f > sc.f1) { hideTip(); return; }
  const rows = svg._series.filter(s => s.arr[f] != null).sort((a, b) => b.arr[f] - a.arr[f]);
  svg.querySelector('.hv').innerHTML = `<line x1="${sc.x(f)}" x2="${sc.x(f)}" y1="${CH.t}" y2="${sc.H - CH.b}" stroke="#9AA4AF" stroke-width="1"/>` +
    rows.map(s => `<circle cx="${sc.x(f)}" cy="${sc.y(s.arr[f])}" r="4" fill="${s.color}" stroke="#101318" stroke-width="2"/>`).join('');
  showTip(`<div class="k">+${((f - sc.f0) / FPS).toFixed(1)}s AFTER SNAP · CLICK TO SEEK</div>` +
    rows.map(s => `<div class="r"><span><i style="display:inline-block;width:8px;height:2px;background:${s.color};vertical-align:middle;margin-right:5px"></i>${esc(s.name)}</span><span class="mono">${s.arr[f].toFixed(s.dec)}${svg.id === 'rpC2' && L.replay.chart2 === 'catch' && Object.keys(L.replay.data.catchProb || {}).length ? '%' : ''}</span></div>`).join(''), ev);
}

function drawPlayheads() {
  for (const id of ['rpC1', 'rpC2']) {
    const svg = document.getElementById(id);
    if (!svg || !svg._sc) continue;
    const ph = svg.querySelector('.ph');
    const t = Math.max(svg._sc.f0, L.replay.t);
    const x = svg._sc.x(t);
    ph.setAttribute('x1', x); ph.setAttribute('x2', x);
    ph.style.display = L.replay.t < svg._sc.f0 ? 'none' : '';
  }
}

// ---------------------------------------------------------------- LEADERS
async function renderLeaders() {
  const S = L.leaders, b = body();
  if (!S.data) {
    if (!S.loading) {
      S.loading = true;
      b.innerHTML = `<div class="empty">Loading Next Gen Stats…</div>`;
      try { S.data = await getJSON('/api/lab/leaders'); } catch (e) { S.error = e.message; }
      S.loading = false;
      if (L.tab === 'leaders') renderLeaders();
    }
    if (S.error) b.innerHTML = `<div class="empty" style="color:var(--orange)">${esc(S.error)}</div>`;
    return;
  }
  const top = S.dir === 'top';
  b.innerHTML = `<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
      <button type="button" class="lab-chip ${top ? 'on' : ''}" data-lab-dir="top">BEST 12</button>
      <button type="button" class="lab-chip ${top ? '' : 'on'}" data-lab-dir="bottom">WORST 12</button>
      <span class="lab-note" style="margin-left:6px">${S.data.season} regular season through Week ${S.data.throughWeek ?? '—'}. Bars run from the volume-weighted league average (gray tick);
      <span class="pos">blue</span> = better than average, <span class="neg">orange</span> = worse. Click a name to open their research ticket.
      The Big Data Bowl's own metrics (SLIME, RYAE, tackle conversion) need raw tracking; these are the NFL's published tracking metrics that cover the same ground.</span></div>
    <div class="ld-grid">${S.data.boards.map(board).join('')}</div>`;
  b.querySelectorAll('.ld-row').forEach(r => {
    r.addEventListener('mousemove', ev => showTip(r.dataset.tip, ev));
    r.addEventListener('mouseleave', hideTip);
  });
}

function board(bd) {
  const rows = L.leaders.dir === 'top' ? bd.rows.slice(0, 12) : bd.rows.slice(-12).reverse();
  const n = bd.rows.length;
  const vals = bd.rows.map(r => r.value);
  const lo = Math.min(...vals, bd.avg), hi = Math.max(...vals, bd.avg);
  const span = hi - lo || 1;
  const pos = v => 100 * (v - lo) / span;
  const unit = bd.unit === '%' ? '%' : bd.unit ? ' ' + bd.unit : '';
  return `<div class="panel ld-card"><div class="panel-head"><span class="panel-title">${esc(bd.title.toUpperCase())}</span><span class="legend">${n} qualified · ${esc(bd.volLabel)}</span></div>
    <div class="ld-note">${esc(bd.note)}</div>
    ${rows.map(r => {
      const rank = bd.rows.indexOf(r) + 1;
      const better = bd.neutral ? null : (bd.higherBetter ? r.value >= bd.avg : r.value <= bd.avg);
      const col = better == null ? '#5A6470' : better ? PAL[0] : PAL[1];
      const a = pos(bd.avg), v = pos(r.value);
      const left = Math.min(a, v), width = Math.max(1.5, Math.abs(v - a));
      const extras = Object.entries(r).filter(([k]) => !['name', 'team', 'value', 'vol', 'id'].includes(k));
      const tipHtml = `<div style="font-weight:600">${esc(r.name)} · ${esc(r.team)}</div><div class="r"><span class="k">${esc(bd.title.toUpperCase())}</span><span class="mono">${fmt(r.value, bd.dec)}${unit}</span></div><div class="r"><span class="k">LEAGUE AVG</span><span class="mono">${fmt(bd.avg, bd.dec)}${unit}</span></div><div class="r"><span class="k">${esc(bd.volLabel)}</span><span class="mono">${r.vol}</span></div>` +
        extras.map(([k, v]) => `<div class="r"><span class="k">${esc({ cmp: 'CMP %', xcmp: 'EXP CMP %', ypc: 'YPC', box8: '8+ BOX %', cushion: 'CUSHION YD', yac: 'YAC', xyac: 'EXP YAC', sacks: 'SACKS' }[k] || k.toUpperCase())}</span><span class="mono">${fmt(v)}</span></div>`).join('');
      return `<div class="ld-row" data-tip="${esc(tipHtml)}"><span class="rk mono">${rank}</span>
        <span class="nm" data-open-player="${esc(r.name)}">${esc(r.name)}</span>
        <span class="tm" style="--tc:${teamColor(kalshiCode(r.team))}">${esc(r.team)}</span>
        <div class="ld-bar"><div class="f ${v < a ? 'neg' : ''}" style="left:${left}%;width:${width}%;background:${col}"></div><div class="avg" style="left:${a}%"></div></div>
        <span class="v mono">${fmt(r.value, bd.dec)}${bd.unit === '%' ? '%' : ''}</span><span class="vol mono">${r.vol}</span></div>`;
    }).join('')}
    <div class="ld-foot"><span>League avg ${fmt(bd.avg, bd.dec)}${unit}</span><span>${bd.higherBetter && !bd.neutral ? 'Higher is better' : bd.neutral ? 'Descriptive, not good/bad' : 'Lower is better'}</span></div></div>`;
}

// ---------------------------------------------------------------- TELLS
function teamPicker(sel) {
  return `<div class="lab-teams">${Object.keys(TEAM_COLORS).sort().map(t => `<button type="button" data-lab-team="${t}" class="${t === sel ? 'on' : ''}" style="--tc:${TEAM_COLORS[t]}">${t}</button>`).join('')}</div>`;
}

async function loadTeam(S, kind) {
  if (S.data[S.team] || S.loading) return;
  S.loading = true; S.error = null;
  try { S.data[S.team] = await getJSON(`/api/lab/${kind}?team=${S.team}`); } catch (e) { S.error = e.message; }
  S.loading = false;
  if (L.tab === kind) renderLab();
}

function renderTells() {
  const S = L.tells, b = body();
  const d = S.data[S.team];
  if (!d) {
    b.innerHTML = teamPicker(S.team) + `<div class="empty" ${S.error ? 'style="color:var(--orange)"' : ''}>${S.error ? esc(S.error) : 'Loading tendencies…'}</div>`;
    loadTeam(S, 'tells');
    return;
  }
  b.innerHTML = teamPicker(S.team) + `<div class="lab-note" style="margin-bottom:8px">How often a pre-snap look turns into the thing a bettor cares about, next to the league rate for the same look (white tick).
    The full Big Data Bowl version reads disguise from player tracking frame by frame; this is the charting-data version. Offense = <b>${esc(S.team)} with the ball</b>, defense = <b>${esc(S.team)} defending</b>.</div>
    <div class="tl-grid">${d.sections.map(tellSection).join('')}</div>`;
}

function tellSection(s) {
  // Strongest tell: biggest gap to league on a look used at least 10% of the time.
  let best = null;
  for (const g of s.groups) for (const r of g.rows) {
    if (r.league == null || r.share < 0.1 || s.mix) continue;
    const gap = r.rate - r.league;
    if (!best || Math.abs(gap) > Math.abs(best.gap)) best = { gap, r, g };
  }
  const sum = best && Math.abs(best.gap) >= 0.05
    ? `Biggest tell: <b>${esc(best.r.label)}</b> → ${pct(best.r.rate)} ${esc(s.metric.replace(' %', '').toLowerCase())} vs ${pct(best.r.league)} league (${best.gap > 0 ? '+' : ''}${(100 * best.gap).toFixed(0)} pts, ${best.r.n} plays).`
    : s.mix ? 'Share of dropbacks in each shell (white tick = league share), with EPA per dropback allowed.' : 'No look here differs from league by 5+ points.';
  const side = s.side === 'offense' ? 'OFFENSE' : 'DEFENSE';
  return `<div class="panel"><div class="panel-head"><span class="panel-title">${side} · ${esc(s.title.toUpperCase())}</span><span class="legend">${s.season}</span></div>
    <div class="tl-sum">${sum}</div>
    ${s.groups.map(g => `<div class="tl-grp">${esc(g.name.toUpperCase())}</div>
      <div class="tl-row h"><span>LOOK</span><span>PLAYS</span><span>${esc(s.metric)} · TEAM vs LEAGUE</span><span style="text-align:right">GAP</span><span style="text-align:right">EPA</span></div>
      ${g.rows.map(r => {
        const gap = r.league == null ? null : r.rate - r.league;
        return `<div class="tl-row"><span>${esc(r.label)}</span><span class="mono" style="color:var(--dim)">${r.n} · ${pct(r.share)}</span>
          <div style="display:flex;align-items:center;gap:6px"><div class="tl-bar" style="flex:1"><div class="f" style="width:${(100 * r.rate).toFixed(1)}%"></div>${r.league != null ? `<div class="lg" style="left:calc(${(100 * r.league).toFixed(1)}% - 1px)"></div>` : ''}</div><span class="mono" style="width:34px;text-align:right">${pct(r.rate)}</span></div>
          <span class="mono" style="text-align:right;color:var(--label)">${gap == null ? '—' : (gap > 0 ? '+' : '') + (100 * gap).toFixed(0)}</span>
          <span class="mono ${r.epa == null ? '' : (s.side === 'offense') === (r.epa > 0) ? 'pos' : 'neg'}" style="text-align:right">${sgn(r.epa)}</span></div>`;
      }).join('') || '<div class="empty">Not enough plays yet.</div>'}`).join('')}</div>`;
}

// ---------------------------------------------------------------- ROUTES
function renderRoutes() {
  const S = L.routes, b = body();
  const d = S.data[S.team];
  if (!d) {
    b.innerHTML = teamPicker(S.team) + `<div class="empty" ${S.error ? 'style="color:var(--orange)"' : ''}>${S.error ? esc(S.error) : 'Loading route trees…'}</div>`;
    loadTeam(S, 'routes');
    return;
  }
  b.innerHTML = teamPicker(S.team) + `<div class="lab-note" style="margin-bottom:8px">${d.season} targeted routes for ${esc(S.team)} receivers with 15+ charted targets: what each player gets thrown on, and what it's worth.
    nflverse publishes coverage/route charting only after a season ends, so this is last year's tree; rosters may have changed. The 2019 contest's route-combination work used full route charting for every receiver (not public).</div>
    <div class="tl-grid">${d.receivers.map(rec => {
      const mx = Math.max(...rec.routes.map(r => r.share));
      return `<div class="panel"><div class="panel-head"><span class="panel-title">${esc(rec.name.toUpperCase())}</span><span class="legend">${rec.tgt} TGT · ${sgn(rec.epa)} EPA/TGT</span></div>
        <div class="rt-row h"><span>ROUTE</span><span>SHARE OF TARGETS</span><span style="text-align:right">TGT</span><span style="text-align:right">CATCH</span><span style="text-align:right">YD/TGT</span><span style="text-align:right">EPA</span><span style="text-align:right">aDOT</span></div>
        ${rec.routes.map(r => `<div class="rt-row"><span>${esc(r.route)}</span>
          <div style="display:flex;align-items:center;gap:6px"><div class="tl-bar" style="flex:1;background:none"><div class="f" style="width:${(100 * r.share / mx).toFixed(1)}%"></div></div><span class="mono" style="width:30px;text-align:right;color:var(--dim)">${pct(r.share)}</span></div>
          <span class="mono" style="text-align:right">${r.tgt}</span><span class="mono" style="text-align:right">${pct(r.catch)}</span>
          <span class="mono" style="text-align:right">${fmt(r.ypt)}</span><span class="mono ${r.epa > 0 ? 'pos' : 'neg'}" style="text-align:right">${sgn(r.epa)}</span>
          <span class="mono" style="text-align:right;color:var(--dim)">${fmt(r.adot)}</span></div>`).join('')}
        <div class="rt-vs">${['man', 'zone'].map(k => rec.vs[k] ? `<span>vs ${k.toUpperCase()}: <span class="mono">${rec.vs[k].tgt}</span> tgt · <span class="mono ${rec.vs[k].epa > 0 ? 'pos' : 'neg'}">${sgn(rec.vs[k].epa)}</span> EPA · <span class="mono">${fmt(rec.vs[k].ypt)}</span> yd/tgt</span>` : '').join('')}</div></div>`;
    }).join('') || '<div class="empty">No receivers with 15+ charted targets.</div>'}</div>`;
}

// ---------------------------------------------------------------- DRAFT CLASSES (historical)
async function renderDraft() {
  const D = L.draft, b = body();
  const key = D.group + ':' + D.season;
  const d = D.data[key];
  if (!d) {
    b.innerHTML = `<div class="empty" ${D.error ? 'style="color:var(--orange)"' : ''}>${D.error ? esc(D.error) : 'Loading draft-class tracking…'}</div>`;
    if (!D.loading) {
      D.loading = true; D.error = null;
      try { D.data[key] = await getJSON(`/api/lab/draft?group=${D.group}&season=${D.season}`); } catch (e) { D.error = e.message; }
      D.loading = false;
      if (L.tab === 'draft') renderDraft();
    }
    return;
  }
  const cols = d.columns;
  const sortKey = D.sort || d.minimum.col;
  const rows = d.rows.slice().sort((a, b) => {
    const va = a[sortKey], vb = b[sortKey];
    if (va == null) return 1; if (vb == null) return -1;
    if (typeof va === 'string') return D.desc ? vb.localeCompare(va) : va.localeCompare(vb);
    return D.desc ? vb - va : va - vb;
  });
  // Best-10% marker per metric column, respecting which end is better.
  const cut = {};
  for (const c of cols) {
    if (!c.better) continue;
    const v = d.rows.map(r => r[c.key]).filter(x => x != null).sort((a, b) => c.better === 'hi' ? b - a : a - b);
    cut[c.key] = v[Math.floor(v.length * 0.1)];
  }
  const best = (c, v) => c.better && v != null && cut[c.key] != null && (c.better === 'hi' ? v >= cut[c.key] : v <= cut[c.key]);
  const th = (k, l, cls = '') => `<th class="${cls} ${sortKey === k ? 'on' : ''}" data-lab-sort="${k}">${l}${sortKey === k ? (D.desc ? ' ▼' : ' ▲') : ''}</th>`;
  b.innerHTML = `<div style="display:flex;gap:6px;align-items:center;margin-bottom:6px;flex-wrap:wrap">
      ${Object.entries(d.groups).map(([k, l]) => `<button type="button" class="lab-chip ${k === D.group ? 'on' : ''}" data-lab-group="${k}">${esc(l.toUpperCase())}</button>`).join('')}
      <span style="width:12px"></span>
      ${d.seasons.map(y => `<button type="button" class="lab-chip ${y === D.season ? 'on' : ''}" data-lab-season="${y}">${y}</button>`).join('')}
    </div>
    <div class="lab-note" style="margin-bottom:8px"><b>Historical, Viz Lab only.</b> Per-player tracking + charting from the 2027 Big Data Bowl, which covers only the 2023-25 draft classes (510 players, no QBs or RBs; many first-rounders are absent).
      ${d.rows.length} players with ${d.minimum.n}+ ${esc(d.minimum.col.replace('_', ' '))} in ${d.season} (regular season + playoffs). Click a column to sort; <span class="pos">blue</span> = best 10% of the group in that column (lower is better for get-off, time to pressure and pressure allowed).</div>
    <div class="panel"><div class="panel-body"><table class="dr-table"><thead><tr>
      ${th('name', 'PLAYER', 'l')}<th class="l">POS</th><th class="l">TEAM</th>${th('draft', 'CLASS')}${th('pick', 'PICK')}${th('forty', '40')}${th('ath', 'NGS ATH')}
      ${cols.map(c => th(c.key, esc(c.label))).join('')}</tr></thead><tbody>
      ${rows.map(r => `<tr><td class="l" style="color:var(--on)">${esc(r.name)}</td><td class="l" style="color:var(--muted)">${esc(r.pos)}</td>
        <td class="l"><span class="tm" style="border-left:3px solid ${teamColor(kalshiCode(r.team))};padding-left:4px;color:var(--muted)">${esc(r.team || '')}</span></td>
        <td class="mono">${r.draft}</td><td class="mono">${r.pick ?? 'UDFA'}</td><td class="mono">${fmt(r.forty, 2)}</td><td class="mono">${fmt(r.ath, 0)}</td>
        ${cols.map(c => `<td class="mono ${best(c, r[c.key]) ? 'hi' : ''}">${fmt(r[c.key], c.dec)}</td>`).join('')}</tr>`).join('')}
    </tbody></table></div></div>`;
}

// ---------------------------------------------------------------- STATUS
// Mirrors docs/viz-lab.md -- keep the two in sync.
const STATUS = [
  ['All-22 play animator', 'built', 'ALL-22 REPLAY: 2021 Weeks 1-8 (122 games, every dropback, snap to throw/sack) and 2017 Weeks 1-6 (91 games, every play). Trails, full routes, scrub with event ticks, hover readouts.', 'Historical only. 2021 tracking stops at the release (the 2023 contest was about the pass rush), so catches aren\'t shown there; 2017 has whole plays. Raw tracking for this season isn\'t public.'],
  ['Completion-probability ticker', 'built', 'Replay chart: per-receiver "if thrown now" catch probability on every frame from snap to throw. Logistic model on 14k 2023 passes (BDB 2026); holdout AUC 0.73, well calibrated.', 'Learned from passes actually thrown to that receiver (selection bias); 2026 data tracks only coverage defenders, so rushers are excluded. Not the GAT+BiLSTM from the finalist.'],
  ['Pocket pressure heat map', 'built', 'Pressure ring + spokes on the field, synced index chart, PFF hit/hurry/sack credits on 2021 plays. Validated on 8,556 dropbacks: AUC 0.71 vs PFF pressure.', 'Distance + closing-speed rule, not the 2023 winners\' trained CPP model.'],
  ['Leaderboards from computed metrics', 'built', 'NGS LEADERS (this season) + the NGS tab on every research ticket. CPOE, time to throw, RYOE, separation, YAC over expected, missed tackles, pressures…', 'Uses the NFL\'s published tracking metrics instead of SLIME / RYAE / OPLE, which need raw tracking.'],
  ['Draft-class tracking profiles (new)', 'built', 'DRAFT CLASSES: routes, separation at the throw, yards per route; pressure allowed; get-off and time to pressure; coverage snaps and man rate for the 2023-25 classes.', 'Historical (2023-25), Lab only. BDB 2027 covers 510 players; no QBs or RBs and many first-rounders missing.'],
  ['Contested space / field control', 'approx', '"Space control" toggle: 1-yd cells owned by the nearest player projected 0.5 s ahead.', 'Velocity-projected Voronoi, not the physics time-to-reach model.'],
  ['Pre-snap tell explorer', 'built', 'PRE-SNAP TELLS: QB alignment / backfield / motion / tempo → pass rate and box → blitz rate (this season); personnel, man rate, coverage mix (last season).', 'Charting-based; the tracking version (disguise, safety rotation) needs pre-snap frames.'],
  ['Route-combination chart', 'approx', 'ROUTE TREES: targeted route share, catch %, yd/tgt, EPA per receiver, man vs zone.', 'Last season only; targeted routes, not full combinations.'],
  ['Optimal-path overlay', 'blocked', '—', 'Deferred. Buildable on the 2017 games (whole plays) with a speed/acceleration movement model.'],
  ['Ghost-defender map (SLIME)', 'blocked', '—', 'BDB 2026 has the post-throw data but tracks only ~3 players in flight; needs conditional-density modeling. High effort.'],
  ['Tackle-probability timeline', 'blocked', '—', 'Needs the 2024 contest data (all plays, 2022), which Kaggle removed at the host\'s request.'],
  ['Block-or-route dials (TEndencIQ)', 'blocked', '—', 'Needs the 2025 contest\'s pre-snap data, also removed by the host.'],
  ['Any tracking view for this week\'s games', 'blocked', '—', 'Raw tracking isn\'t public in-season. Betting pages use only this season\'s published NGS / charting aggregates.'],
];
const BADGE = {
  built: ['BUILT', '#3D8DE0'], 'built-sample': ['BUILT · SAMPLE DATA', '#24A084'], approx: ['APPROXIMATION', '#A08A10'],
  'approx-sample': ['APPROX · SAMPLE DATA', '#A08A10'], blocked: ['NOT BUILDABLE YET', '#E0602B'],
};
function renderStatus() {
  body().innerHTML = `<div class="panel"><div class="panel-head"><span class="panel-title">BIG DATA BOWL REPORT → WHAT'S IN THE LAB</span></div>
    <div class="lab-note" style="padding:8px 10px">Rule: <b>betting pages only use this season's data</b> (NGS + charting aggregates). Everything built from Big Data Bowl tracking is a past season and lives only in this tab.
    Tracking sources: BDB 2019 (2017, public), BDB 2023 (2021), BDB 2026 (2023), BDB 2027 (2023-25 draft classes). Full write-up: <span class="mono">docs/viz-lab.md</span>.</div>
    <table class="st-table"><thead><tr><th>REPORT IDEA</th><th>STATUS</th><th>WHAT'S BUILT</th><th>LIMITS / WHAT UNLOCKS IT</th></tr></thead><tbody>
    ${STATUS.map(([n, s, what, lim]) => `<tr><td>${esc(n)}</td><td><span class="st-badge" style="color:${BADGE[s][1]};border-color:${BADGE[s][1]}">${BADGE[s][0]}</span></td><td>${esc(what)}</td><td>${esc(lim)}</td></tr>`).join('')}
    </tbody></table></div>`;
}
})();
