/* annotation.js — 「视频 ↔ 波形」同步标注工作台。
 *
 * 原实现内联在标注后端的 static/index.html 里；并入 AceMate 后端后拆到本文件。
 * 三点注意事项：
 *   1. tailwind.config 的 content 含 ./web/assets/js/**\/*.js，所以类名一律写成
 *      **完整字面量**（形如 'border-primary-fixed bg-surface-container-high'），
 *      绝不用 'bg-' + tone 这类运行时拼接 —— 那样 Tailwind CLI 扫不到，样式会被 purge。
 *   2. 配色改用 AceMate 设计令牌：正手绿 #c3f400 / 反手蓝 #7bd0ff /
 *      切削橙 #ffb783 / 发球柔红 #ffb4ab，与小程序端击球分色保持一致。
 *   3. 接口基址留空串（同源）—— 页面与 API 现在同在 :8787。
 */
const API = '';

/* 标签空间 8 类，与后端 schemas.LABELS_ALL 一致。
   数字键 1-8 按此顺序切换。标注阶段刻意保留富标签，训练时再按需归并。 */
const LABELS = [
  { k: 'forehand', cn: '正手' }, { k: 'backhand', cn: '反手' },
  { k: 'serve', cn: '发球' }, { k: 'volley', cn: '截击' },
  { k: 'slice', cn: '切削' }, { k: 'smash', cn: '高压' },
  { k: 'lob', cn: '挑高' }, { k: 'drop', cn: '放小球' },
];

const LABEL_COLOR = {
  forehand: '#c3f400', backhand: '#7bd0ff', serve: '#ffb4ab', volley: '#c4a7ff',
  slice: '#ffb783', smash: '#ff9ad1', lob: '#ffd166', drop: '#8e9379',
};

/* Canvas 用的中性色，取自设计令牌 */
const CANVAS_BG = '#0a0f0d';        // surface-container-lowest
const CANVAS_GRID = '#262b29';      // surface-container-high
const CANVAS_TEXT = '#8e9379';      // outline
const PLAYHEAD = '#7bd0ff';         // secondary
const HEUR_LINE = '#8e9379';        // 预标注暗色虚线

function cn(k) { return (LABELS.find((l) => l.k === k) || {}).cn || k; }

/** 转义：会话 id / 标注者名都是自由文本，直接拼进 innerHTML 会有 XSS 风险 */
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

/** 轻量提示，替代原 alert()，与外站视觉一致 */
function toast(msg, tone) {
  const box = document.createElement('div');
  const toneCls = tone === 'error'
    ? 'border-error/40 text-error'
    : 'border-primary-fixed/40 text-primary-fixed';
  box.className = 'fixed top-24 right-6 z-[60] px-space-md py-2.5 rounded-xl ' +
    'bg-surface-container border shadow-2xl font-label-md text-label-md ' + toneCls;
  box.textContent = msg;
  document.body.appendChild(box);
  setTimeout(() => box.remove(), 2600);
}

let current = null;
let channels = [];
let duration = 0;
let annotations = [];        // [{impact_time, label, annotator, source, status, confidence}]
let rejectedHeuristic = [];  // 被删除的启发式误检 impact_time
let curLabel = 'forehand';
let raf = null;

/* ---------- 标注者（持久化） ---------- */
const ANNO_KEY = 'netpulse.annotator';
(function initAnnotator() {
  const el = document.getElementById('annotator');
  el.value = localStorage.getItem(ANNO_KEY) || '';
  el.addEventListener('change', () => localStorage.setItem(ANNO_KEY, el.value.trim()));
})();
function curAnnotator() {
  return (document.getElementById('annotator').value || 'human').trim() || 'human';
}

/* ---------- 左侧会话列表 ---------- */
async function loadSessions() {
  const list = await fetch(API + '/api/sessions').then((r) => r.json());
  const box = document.getElementById('sessions');
  const cnt = document.getElementById('sessCount');
  if (cnt) cnt.textContent = list.length + ' 场';
  box.innerHTML = '';
  if (!list.length) {
    box.innerHTML = '<div class="py-space-lg text-center font-label-sm text-label-sm text-outline">' +
      '还没有会话，右侧上传一个 raw_*.json 开始</div>';
    return;
  }
  list.forEach((s) => {
    const active = s.id === current;
    const d = document.createElement('div');
    d.className = 'p-space-sm rounded-lg border cursor-pointer transition-colors ' +
      (active ? 'border-primary-fixed bg-surface-container-high'
              : 'border-outline/20 bg-surface-container-low hover:border-primary-fixed/60');
    const meta = [s.wrist || '', s.annotation_count + ' 标注', s.has_video ? '有视频' : '']
      .filter(Boolean).join(' · ');
    d.innerHTML = '<div class="font-body-md text-body-md text-on-surface font-semibold truncate">' +
      esc(s.id) + '</div>' +
      '<div class="font-label-sm text-label-sm text-on-surface-variant mt-0.5">' + esc(meta) + '</div>';
    d.onclick = () => openSession(s.id);
    box.appendChild(d);
  });
}

/* ---------- 上传会话 ---------- */
document.getElementById('rawFile').addEventListener('change', (e) => {
  const t = document.getElementById('rawName');
  t.textContent = (e.target.files[0] || {}).name || '选择 raw_*.json';
});
document.getElementById('vidFile').addEventListener('change', (e) => {
  const t = document.getElementById('vidName');
  t.textContent = (e.target.files[0] || {}).name || '选择视频（可选）';
});

document.getElementById('uploadBtn').onclick = async () => {
  const id = document.getElementById('sid').value.trim();
  if (!id) return toast('请填写 session id', 'error');
  const raw = document.getElementById('rawFile').files[0];
  if (!raw) return toast('请选择 raw_*.json', 'error');

  const fd = new FormData();
  fd.append('id', id);
  fd.append('player_id', document.getElementById('player').value.trim());
  fd.append('raw', raw);
  const vid = document.getElementById('vidFile').files[0];
  if (vid) fd.append('video', vid);

  const res = await fetch(API + '/api/sessions', { method: 'POST', body: fd })
    .then((r) => r.json());
  if (res.prefilled) {
    toast('已上传，自动预标注 ' + res.prefilled + ' 条启发式挥拍，请纠错');
  } else {
    toast('已上传会话 ' + id);
  }
  await loadSessions();
  openSession(id);
};

/* ---------- 打开会话 ---------- */
async function openSession(id) {
  current = id;
  rejectedHeuristic = [];
  const [samples, ann] = await Promise.all([
    fetch(API + '/api/sessions/' + encodeURIComponent(id) + '/samples?downsample=60').then((r) => r.json()),
    fetch(API + '/api/sessions/' + encodeURIComponent(id) + '/annotations').then((r) => r.json()),
  ]);
  channels = samples.channels || [];
  duration = samples.duration || 0;
  annotations = (ann || []).map((a) => Object.assign({}, a));
  renderStage();
  drawLabels();
  await loadSessions();
}

function renderStage() {
  const stage = document.getElementById('stage');
  stage.innerHTML =
    '<video id="vid" controls class="w-full max-h-[38vh] rounded-xl bg-black block"' +
    (current ? ' src="' + API + '/api/sessions/' + encodeURIComponent(current) + '/video"' : '') +
    '></video>' +
    '<div class="rounded-xl border border-outline/20 bg-surface-container-lowest overflow-hidden">' +
    '<canvas id="cv" class="block w-full cursor-crosshair"></canvas></div>' +
    '<div class="flex flex-wrap items-center gap-space-md font-label-sm text-label-sm text-on-surface-variant">' +
    '<span class="flex items-center gap-space-xs"><span class="inline-block w-4 border-t-2 border-primary-fixed align-middle"></span>人工 / 确认</span>' +
    '<span class="flex items-center gap-space-xs"><span class="inline-block w-4 border-t border-dashed border-outline align-middle"></span>Watch 预标注（待纠错）</span>' +
    '<span class="text-outline">点击波形打当前标签；数字键 1-8 切换；空格播放/暂停视频。</span>' +
    '</div>' +
    '<div class="pt-space-xs"><table class="data-table">' +
    '<thead><tr class="font-label-sm text-label-sm text-outline">' +
    '<th class="text-left px-space-sm py-2 font-semibold">序号</th>' +
    '<th class="text-left px-space-sm py-2 font-semibold">时间(s)</th>' +
    '<th class="text-left px-space-sm py-2 font-semibold">类型</th>' +
    '<th class="text-left px-space-sm py-2 font-semibold">来源</th>' +
    '<th class="text-left px-space-sm py-2 font-semibold"></th>' +
    '</tr></thead><tbody id="annBody"></tbody></table></div>';

  const cv = document.getElementById('cv');
  cv.width = cv.clientWidth * (window.devicePixelRatio || 1);
  cv.height = 380 * (window.devicePixelRatio || 1);
  cv.onclick = onCanvasClick;
  const vid = document.getElementById('vid');
  vid.ontimeupdate = draw;
  vid.onplay = loop;
  vid.onpause = () => { cancelAnimationFrame(raf); draw(); };
  drawLabels();
  renderAnnoList();
  draw();
}

function drawLabels() {
  const box = document.getElementById('labels');
  if (!box) return;
  box.innerHTML = '';
  LABELS.forEach((l, i) => {
    const on = l.k === curLabel;
    const d = document.createElement('div');
    d.className = 'px-space-sm py-1.5 rounded-lg border font-label-sm text-label-sm cursor-pointer transition-colors ' +
      (on ? 'bg-primary-container text-on-primary-fixed border-primary-container font-bold'
          : 'bg-surface-container-low text-on-surface-variant border-outline/25 hover:border-primary-fixed');
    d.innerHTML = esc(l.cn) + '<span class="ml-1 opacity-60">' + (i + 1) + '</span>';
    d.onclick = () => { curLabel = l.k; drawLabels(); };
    box.appendChild(d);
  });
}

document.addEventListener('keydown', (e) => {
  if (!current) return;
  if (e.key >= '1' && e.key <= '8') { curLabel = LABELS[+e.key - 1].k; drawLabels(); }
  if (e.key === ' ' && document.activeElement.tagName !== 'INPUT' &&
      document.activeElement.tagName !== 'SELECT') {
    e.preventDefault();
    const v = document.getElementById('vid');
    if (v) (v.paused ? v.play() : v.pause());
  }
});

/* 画布点击：新增一条「人工」标注（补检 / 漏检） */
function onCanvasClick(ev) {
  const cv = document.getElementById('cv');
  const rect = cv.getBoundingClientRect();
  const t = +(((ev.clientX - rect.left) / rect.width) * duration).toFixed(3);
  annotations.push({ impact_time: t, label: curLabel, annotator: curAnnotator(),
                     source: 'human', status: 'confirmed' });
  annotations.sort((a, b) => a.impact_time - b.impact_time);
  renderAnnoList();
  draw();
}

/* 列表：每行可改标签 / 对预标注纠错 / 删除 */
function renderAnnoList() {
  const body = document.getElementById('annBody');
  if (!body) return;
  body.innerHTML = '';
  annotations.forEach((a, i) => {
    const isHeur = a.annotator === 'heuristic' && a.source === 'auto';
    const color = LABEL_COLOR[a.label] || '#8e9379';
    const badge = isHeur
      ? '<span class="px-1.5 py-0.5 rounded bg-surface-container-highest text-on-surface-variant font-label-sm text-label-sm">AI 预标注</span>'
      : '<span class="px-1.5 py-0.5 rounded bg-primary-container/20 text-primary-fixed font-label-sm text-label-sm">' +
        esc(a.annotator) + '</span>';
    const sel = '<select data-i="' + i + '" class="lblSel px-space-sm py-1 rounded-lg bg-surface-container-low border border-outline/25 text-on-surface font-label-sm text-label-sm focus:outline-none focus:border-primary-fixed">' +
      LABELS.map((l) => '<option value="' + l.k + '"' + (l.k === a.label ? ' selected' : '') + '>' +
        l.cn + '</option>').join('') + '</select>';
    const actions = isHeur
      ? '<span class="ml-space-xs text-outline font-label-sm text-label-sm cursor-pointer hover:text-primary-fixed" data-fix="' + i + '">纠错</span>' +
        '<span class="ml-space-xs text-outline font-label-sm text-label-sm cursor-pointer hover:text-error" data-delh="' + i + '">删误检</span>'
      : '<span class="ml-space-xs text-outline font-label-sm text-label-sm cursor-pointer hover:text-error" data-del="' + i + '">删除</span>';
    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td class="px-space-sm py-2 font-label-sm text-label-sm text-outline">' + (i + 1) + '</td>' +
      '<td class="px-space-sm py-2 font-label-md text-label-md text-on-surface">' + a.impact_time.toFixed(3) + '</td>' +
      '<td class="px-space-sm py-2"><span class="px-2 py-0.5 rounded font-label-sm text-label-sm font-bold" style="background:' + color + ';color:#0a0f0d">' + esc(cn(a.label)) + '</span></td>' +
      '<td class="px-space-sm py-2">' + badge + '</td>' +
      '<td class="px-space-sm py-2">' + sel + actions + '</td>';
    body.appendChild(tr);
  });
  body.querySelectorAll('.lblSel').forEach((s) => {
    s.onchange = (e) => {
      annotations[+e.target.dataset.i].label = e.target.value;
      draw();
    };
  });
  body.querySelectorAll('[data-del]').forEach((b) => {
    b.onclick = (e) => {
      annotations.splice(+e.target.dataset.del, 1);
      renderAnnoList(); draw();
    };
  });
  body.querySelectorAll('[data-delh]').forEach((b) => {
    b.onclick = (e) => {
      const i = +e.target.dataset.delh;
      rejectedHeuristic.push(annotations[i].impact_time);
      annotations.splice(i, 1);
      renderAnnoList(); draw();
    };
  });
  body.querySelectorAll('[data-fix]').forEach((b) => {
    b.onclick = (e) => {
      // 纠错：把预标注转为当前标注者的人工项（原启发式在保存时会被标记 rejected）
      const i = +e.target.dataset.fix;
      const a = annotations[i];
      annotations[i] = { impact_time: a.impact_time, label: a.label,
                         annotator: curAnnotator(), source: 'human', status: 'confirmed' };
      renderAnnoList(); draw();
    };
  });
}

/* ---------- 波形绘制（Canvas 原样逻辑，仅换配色） ---------- */
function draw() {
  const cv = document.getElementById('cv');
  if (!cv || !channels.length) return;
  const ctx = cv.getContext('2d');
  const W = cv.width, H = cv.height;
  const dpr = window.devicePixelRatio || 1;
  ctx.clearRect(0, 0, W, H);
  const padL = 44 * dpr, padR = 10 * dpr, padT = 10 * dpr, padB = 18 * dpr;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const half = plotH / 2;
  ctx.fillStyle = CANVAS_BG; ctx.fillRect(padL, padT, plotW, plotH);
  ctx.strokeStyle = CANVAS_GRID; ctx.strokeRect(padL, padT, plotW, plotH);
  ctx.fillStyle = CANVAS_TEXT; ctx.font = (11 * dpr) + 'px sans-serif';
  ctx.fillText('加速度', padL + 4 * dpr, padT + 14 * dpr);
  ctx.fillText('旋转速率', padL + 4 * dpr, padT + half + 14 * dpr);

  const n = channels.length;
  const COLORS = { ax: '#ffb4ab', ay: '#ffd166', az: '#7bd0ff',
                   rx: '#c4a7ff', ry: '#c3f400', rz: '#ff9ad1' };
  const groups = [
    { keys: ['ax', 'ay', 'az'], y0: padT, y1: padT + half },
    { keys: ['rx', 'ry', 'rz'], y0: padT + half, y1: padT + plotH },
  ];
  for (const g of groups) {
    const ranges = {};
    for (const k of g.keys) {
      let mn = Infinity, mx = -Infinity;
      for (const s of channels) { if (s[k] < mn) mn = s[k]; if (s[k] > mx) mx = s[k]; }
      ranges[k] = [mn, mx];
    }
    const cols = Math.max(1, Math.floor(plotW));
    for (const k of g.keys) {
      ctx.strokeStyle = COLORS[k]; ctx.lineWidth = 1 * dpr; ctx.beginPath();
      const r = ranges[k];
      const span = (r[1] - r[0]) || 1;
      for (let c = 0; c < cols; c++) {
        const i0 = Math.floor(c / cols * n);
        const i1 = Math.max(i0 + 1, Math.floor((c + 1) / cols * n));
        let lmin = Infinity, lmax = -Infinity;
        for (let i = i0; i < i1 && i < n; i++) {
          const v = channels[i][k];
          if (v < lmin) lmin = v;
          if (v > lmax) lmax = v;
        }
        const x = padL + c;
        const yHi = g.y1 - ((lmax - r[0]) / span) * (g.y1 - g.y0);
        const yLo = g.y1 - ((lmin - r[0]) / span) * (g.y1 - g.y0);
        if (c === 0) ctx.moveTo(x, yHi); else ctx.lineTo(x, yHi);
        ctx.lineTo(x, yLo);
      }
      ctx.stroke();
    }
  }

  // 标注刻度：人工实色，预标注暗色虚线
  for (const a of annotations) {
    const x = padL + (a.impact_time / duration) * plotW;
    const isHeur = a.annotator === 'heuristic' && a.source === 'auto';
    ctx.strokeStyle = isHeur ? HEUR_LINE : (LABEL_COLOR[a.label] || '#ffffff');
    ctx.globalAlpha = isHeur ? 0.5 : 0.95;
    ctx.lineWidth = (isHeur ? 1 : 2) * dpr;
    ctx.setLineDash(isHeur ? [5 * dpr, 4 * dpr] : []);
    ctx.beginPath(); ctx.moveTo(x, padT); ctx.lineTo(x, padT + plotH); ctx.stroke();
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;
  }

  const vid = document.getElementById('vid');
  if (vid && duration > 0 && !isNaN(vid.currentTime)) {
    const x = padL + (vid.currentTime / duration) * plotW;
    ctx.strokeStyle = PLAYHEAD; ctx.lineWidth = 2 * dpr;
    ctx.beginPath(); ctx.moveTo(x, padT); ctx.lineTo(x, padT + plotH); ctx.stroke();
  }
}

function loop() { draw(); raf = requestAnimationFrame(loop); }
window.addEventListener('resize', () => {
  if (!current) return;
  const cv = document.getElementById('cv');
  if (cv) { cv.width = cv.clientWidth * (window.devicePixelRatio || 1); draw(); }
});

/* ---------- 保存 / 导出 ---------- */
document.getElementById('saveBtn').onclick = async () => {
  if (!current) return toast('请先打开一个会话', 'error');
  const name = curAnnotator();
  // 本轮只提交「人工」项（含纠错与新增）；未更正的预标注保留在库里作为训练标签
  const items = annotations
    .filter((a) => a.source !== 'auto')
    .map((a) => ({ impact_time: a.impact_time, label: a.label,
                   confidence: a.confidence == null ? null : a.confidence,
                   note: a.note == null ? null : a.note }));
  await fetch(API + '/api/sessions/' + encodeURIComponent(current) + '/annotations', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ annotator: name, items: items, rejected_heuristic: rejectedHeuristic }),
  });
  const ann = await fetch(API + '/api/sessions/' + encodeURIComponent(current) + '/annotations')
    .then((r) => r.json());
  annotations = ann.map((a) => Object.assign({}, a));
  rejectedHeuristic = [];
  renderAnnoList(); draw();
  await loadSessions();
  toast('已保存 ' + items.length + ' 条（标注者：' + name + '）');
};

document.getElementById('exportBtn').onclick = () => {
  if (!current) return toast('请先打开一个会话', 'error');
  window.open(API + '/api/sessions/' + encodeURIComponent(current) + '/export/annotation.json', '_blank');
};
document.getElementById('dsBtn').onclick = () => {
  window.open(API + '/api/export/dataset.csv', '_blank');
};

/* ---------- 一致性评分 ---------- */
document.getElementById('consBtn').onclick = async () => {
  if (!current) return toast('请先打开一个会话', 'error');
  const data = await fetch(API + '/api/sessions/' + encodeURIComponent(current) + '/consistency')
    .then((r) => r.json());
  const body = document.getElementById('consBody');
  const thCls = 'text-left px-space-sm py-2 font-label-sm text-label-sm text-outline font-semibold';
  const tdCls = 'px-space-sm py-2 font-label-md text-label-md text-on-surface';

  let html = '<table class="data-table"><thead><tr>' +
    ['标注者', '人工标注数', '采纳预标注', '纠错(硬例)', '会话'].map((t) =>
      '<th class="' + thCls + '">' + t + '</th>').join('') +
    '</tr></thead><tbody>';
  (data.annotators || []).forEach((a) => {
    html += '<tr>' +
      '<td class="' + tdCls + ' font-semibold">' + esc(a.name) + '</td>' +
      '<td class="' + tdCls + '">' + a.human_count + '</td>' +
      '<td class="' + tdCls + ' text-primary-fixed">' + a.heuristic_accepted + '</td>' +
      '<td class="' + tdCls + ' text-tertiary-fixed-dim">' + a.heuristic_corrected + '</td>' +
      '<td class="' + tdCls + ' text-on-surface-variant">' + esc((a.sessions || []).join(', ')) + '</td>' +
      '</tr>';
  });
  html += '</tbody></table>';

  const h = data.heuristic_summary || {};
  const pill = (t, v, tone) =>
    '<span class="px-space-sm py-1 rounded-lg bg-surface-container-low font-label-sm text-label-sm ' +
    tone + '">' + t + ' ' + v + '</span>';
  html += '<div class="flex flex-wrap items-center gap-space-xs">' +
    pill('启发式待审阅', h.proposed || 0, 'text-on-surface-variant') +
    pill('被认可', h.confirmed || 0, 'text-primary-fixed') +
    pill('被纠错/删', h.rejected || 0, 'text-error') + '</div>';

  html += '<h4 class="font-headline-sm text-headline-sm text-primary font-bold mt-space-sm">成对一致性</h4>';
  const pairs = data.pairs || [];
  if (!pairs.length) {
    html += '<div class="py-space-lg text-center font-body-md text-body-md text-outline">' +
      '该会话尚不足 2 名人工标注者，无法计算一致性。</div>';
  } else {
    html += '<table class="data-table"><thead><tr>' +
      ['标注者 A', '标注者 B', '匹配数', '一致数', '一致率', "Cohen's κ"].map((t) =>
        '<th class="' + thCls + '">' + t + '</th>').join('') +
      '</tr></thead><tbody>';
    pairs.forEach((p) => {
      html += '<tr>' +
        '<td class="' + tdCls + '">' + esc(p.a) + '</td>' +
        '<td class="' + tdCls + '">' + esc(p.b) + '</td>' +
        '<td class="' + tdCls + '">' + p.matched + '</td>' +
        '<td class="' + tdCls + '">' + p.agree + '</td>' +
        '<td class="' + tdCls + '">' + (p.agreement * 100).toFixed(1) + '%</td>' +
        '<td class="' + tdCls + ' text-primary-fixed font-semibold">' +
        (p.kappa == null ? '—' : p.kappa) + '</td></tr>';
    });
    html += '</tbody></table>';
    html += '<div class="font-body-md text-body-md text-on-surface">整体 κ = <span class="text-primary-fixed font-bold">' +
      (data.overall_kappa == null ? '—' : data.overall_kappa) + '</span></div>' +
      '<div class="font-label-sm text-label-sm text-outline">' + esc(data.kappa_guide || '') + '</div>';
  }
  body.innerHTML = html;
  document.getElementById('cons').classList.remove('hidden');
  document.getElementById('cons').classList.add('flex');
};

document.getElementById('consClose').onclick = () => {
  const el = document.getElementById('cons');
  el.classList.add('hidden');
  el.classList.remove('flex');
};

loadSessions();
