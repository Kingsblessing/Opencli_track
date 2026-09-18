/* Opencli_track WebUI 前端逻辑 */
const $ = (id) => document.getElementById(id);

// ---------- 首次环境引导 ----------
const CHECK_LABELS = [
  ['daemon', 'Daemon'],
  ['extension', 'Chrome 扩展'],
  ['connectivity', '浏览器连通'],
];

function renderSetup(st) {
  const gate = $('setup-gate');
  const checks = $('setup-checks');
  const doc = st.doctor || {};
  checks.innerHTML = CHECK_LABELS.map(([k, label]) => {
    const ok = !!doc[k];
    return `<li class="${ok ? 'ok' : 'bad'}">${ok ? '✓' : '✗'} ${label}</li>`;
  }).join('');
  $('setup-raw').textContent = doc.raw || '';
  const logins = st.confirmed_logins || {};
  $('login-bilibili').checked = !!logins.bilibili;
  $('login-douyin').checked = !!logins.douyin;
  $('login-xiaohongshu').checked = !!logins.xiaohongshu;
  gate.hidden = !st.show_gate;
  return st;
}

async function refreshSetup(showAlways) {
  const st = await api('/api/setup/status');
  if (!st.ok) return st;
  if (showAlways) st.show_gate = true;
  return renderSetup(st);
}

async function openSetup(action) {
  const r = await api('/api/setup/open', 'POST', {action});
  if (!r.ok) alert('无法打开: ' + (r.detail || r.message || ''));
}

$('btn-open-store').onclick = () => openSetup('extension_store');
$('btn-open-ext').onclick = () => openSetup('chrome_extensions');
$('btn-setup-refresh').onclick = () => refreshSetup(true);
$('btn-setup').onclick = () => refreshSetup(true);
document.querySelectorAll('[data-open]').forEach(btn => {
  btn.onclick = () => openSetup(btn.dataset.open);
});
$('btn-setup-enter').onclick = async () => {
  const st = await api('/api/setup/status');
  if (st.ok && !st.ready) {
    const go = confirm('doctor 尚未全部通过。仍要进入控制台?采集前需要扩展已连接。');
    if (!go) return;
  }
  await api('/api/setup/ack', 'POST', {
    dismissed: true,
    confirmed_logins: {
      bilibili: $('login-bilibili').checked,
      douyin: $('login-douyin').checked,
      xiaohongshu: $('login-xiaohongshu').checked,
    },
  });
  $('setup-gate').hidden = true;
};
refreshSetup(false);

// ---------- 标签页 ----------
document.querySelectorAll('.tab').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    $('tab-' + btn.dataset.tab).classList.add('active');
    if (btn.dataset.tab === 'results') loadOutputs();
    if (btn.dataset.tab === 'config') loadConfig();
  };
});

// ---------- 任务控制 ----------
$('btn-run').onclick = async () => {
  const setup = await api('/api/setup/status');
  if (setup.ok && !setup.ready) {
    const go = confirm('环境检测未通过(扩展或 daemon)。仍要开始采集?');
    if (!go) {
      refreshSetup(true);
      return;
    }
  }
  const step = $('run-step').value;
  const r = await api('/api/run', 'POST', {step});
  if (!r.ok) alert('启动失败: ' + (r.detail ? JSON.stringify(r.detail) : r.message || r));
};
$('btn-pause').onclick = () => api('/api/pause', 'POST');
$('btn-resume').onclick = () => api('/api/resume', 'POST');
$('btn-stop').onclick = () => api('/api/stop', 'POST');

async function api(path, method = 'GET', body) {
  const opt = {method, headers: {'Content-Type': 'application/json'}};
  if (body) opt.body = JSON.stringify(body);
  const res = await fetch(path, opt);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return {ok: false, ...data, detail: data.detail};
  return {ok: true, ...data};
}

// ---------- 进度更新: WebSocket 优先,失败时 HTTP 轮询兜底 ----------
const STATE_CN = {idle: '空闲', running: '运行中', paused: '已暂停', stopping: '停止中',
                  done: '已完成', error: '出错', stopped: '已停止'};
let ws, wsAlive = false, pollTimer = null;

function connectWS() {
  try {
    ws = new WebSocket(`ws://${location.host}/api/ws`);
  } catch (e) { startPolling(); return; }
  ws.onopen = () => { wsAlive = true; stopPolling(); ws.send('ping'); };
  ws.onmessage = (ev) => {
    try { renderJob(JSON.parse(ev.data)); } catch (e) { /* 忽略坏帧 */ }
    if (ws.readyState === 1) ws.send('ping');
  };
  ws.onerror = () => { wsAlive = false; startPolling(); };
  ws.onclose = () => {
    wsAlive = false;
    startPolling();
    setTimeout(connectWS, 5000);   // 稍后重试 WS
  };
}
function startPolling() {
  if (pollTimer) return;
  const tick = async () => {
    if (!wsAlive) {
      const st = await api('/api/job');
      if (st.ok) renderJob(st);
    }
  };
  tick();
  pollTimer = setInterval(tick, 1500);
}
function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}
connectWS();
setTimeout(() => { if (!wsAlive) startPolling(); }, 2000);   // 兜底:2s 内没连上就轮询
function renderJob(st) {
  const badge = $('job-badge');
  badge.textContent = STATE_CN[st.state] || st.state;
  badge.className = 'badge ' + st.state;
  $('btn-run').disabled = ['running', 'paused', 'stopping'].includes(st.state);
  $('btn-pause').disabled = st.state !== 'running';
  $('btn-resume').disabled = st.state !== 'paused';
  $('btn-stop').disabled = !['running', 'paused'].includes(st.state);
  const pct = st.total > 0 ? Math.round(st.current / st.total * 100) : 0;
  $('progress-fill').style.width = (st.state === 'done' ? 100 : pct) + '%';
  const phaseName = st.phase === 'collect' ? '① 采集' : st.phase === 'comments' ? '② 评论' : '';
  $('progress-text').textContent = st.state === 'idle' ? '尚未开始'
    : `${phaseName || ''} ${st.current}/${st.total} ${st.detail || ''}`.trim();
  if (st.logs && st.logs.length) {
    const box = $('log-box');
    const text = st.logs.join('\n');
    if (box.textContent !== text) {
      box.textContent = text;
      box.scrollTop = box.scrollHeight;
    }
  }
  if (st.state === 'done' || st.state === 'error') refreshOutputsSoon();
}

// ---------- 配置表单 ----------
// 分区定义:配置页按此渲染为多张卡片
const CONFIG_SECTIONS = [
  {id: 'collect', icon: '🔍', title: '采集设置', desc: '平台、关键词、排序与数量'},
  {id: 'intent',  icon: '🎯', title: '意图识别与线索分级', desc: '匹配模式、分级阈值与各阶段开关;规则可在下方分组卡片中直接编辑'},
  {id: 'legacy',  icon: '📜', title: '旧模式回退(关键词命中)', desc: '仅在匹配模式选择「关键词命中(旧行为)」时生效'},
  {id: 'runtime', icon: '⚙️', title: '运行时', desc: '限速、重试与浏览器窗口'},
];

const CONFIG_FIELDS = [
  // ---- 采集 ----
  {section: 'collect', key: 'platforms', label: '平台', type: 'platforms', wide: true,
   hint: 'douyin/redbook 需先在 Chrome 登录对应网站'},
  {section: 'collect', key: 'keywords', label: '搜索关键词(每行一个)', type: 'lines', wide: true},
  {section: 'collect', key: 'search_order', label: '搜索排序', type: 'select',
   options: [['relevance', '综合排序(默认)'], ['pubdate', '最新发布'], ['views', '最多播放(仅B站)']]},
  {section: 'collect', key: 'publish_window_hours', label: '发布时间窗(小时,0=不限)', type: 'int'},
  {section: 'collect', key: 'crawl_count', label: '每关键词采集条数', type: 'int'},
  {section: 'collect', key: 'dedup_across_runs', label: '跨运行去重', type: 'bool',
   hint: '记住已采集过的帖子,下次运行跳过,避免重复采集(只作用于采集步骤)'},
  {section: 'collect', key: 'enrich_detail', label: '详情数据增强(redbook)', type: 'bool',
   hint: '逐条调详情接口补充收藏数/评论数/正文/标签(不含发布时间);每条多一次请求'},
  {section: 'collect', key: 'max_comments_per_video', label: '每视频评论上限(≤50)', type: 'int'},
  {section: 'collect', key: 'max_replies', label: '楼中楼展开上限(0=不展开)', type: 'int'},

  // ---- 意图识别 ----
  {section: 'intent', key: 'match.mode', label: '匹配模式', type: 'select', wide: true,
   options: [['intent', '意图识别(推荐:客户/供给方/无关 三分类 + 线索分级)'],
             ['keyword', '关键词命中(旧行为:命中任一正则即算客户)']]},
  {section: 'intent', key: 'match.thresholds.high', label: 'HIGH 阈值', type: 'number'},
  {section: 'intent', key: 'match.thresholds.medium', label: 'MEDIUM 阈值', type: 'number'},
  {section: 'intent', key: 'match.thresholds.low', label: 'LOW 阈值', type: 'number'},
  {section: 'intent', key: 'match.keep_all', label: '输出全部评论(含被排除的)', type: 'bool',
   hint: '保留完整证据链便于复核;关闭则只输出非 EXCLUDE 的记录'},
  {section: 'intent', key: 'match.only_leads', label: '只输出客户线索', type: 'bool',
   hint: '仅保留 HIGH/MEDIUM/LOW,直接得到可用线索表'},
  {section: 'intent', key: 'match.direction.enabled', label: '双向词方向消歧', type: 'bool',
   hint: '区分"我要接单"(供给方)与"现在有人接吗"(客户)'},
  {section: 'intent', key: 'match.post_context.enabled', label: '帖子上下文加权', type: 'bool',
   hint: '避雷/讨论帖的评论降权,服务售卖帖提权'},
  {section: 'intent', key: 'match.author_role.enabled', label: '作者角色识别', type: 'bool',
   hint: '按账号历史评论判断是同行还是客户;B站无作者ID,仅按昵称匹配'},
  {section: 'intent', key: 'match.author_role.min_comments', label: '作者判定最少评论数', type: 'int'},
  {section: 'intent', key: 'match.author_role.seller_ratio', label: '卖家占比阈值(0~1)', type: 'number'},
  {section: 'intent', key: 'match.dedup.enabled', label: '评论去重', type: 'bool',
   hint: '合并同作者近似重复,并标记跨作者刷屏'},
  {section: 'intent', key: 'match.dedup.similarity', label: '近似判定相似度(0~1)', type: 'number'},
  {section: 'intent', key: 'match.dedup.spam_min_authors', label: '刷屏判定作者数', type: 'int'},

  {section: 'intent', key: 'match.recall', label: '正向召回规则(意图分组)', type: 'rules', wide: true,
   accent: 'positive',
   hint: '命中即按分组权重加分;权重越高越接近成交。组名左侧蓝条=正向'},
  {section: 'intent', key: 'match.negative', label: '负向排除规则(供给方词库)', type: 'rules', wide: true,
   accent: 'negative',
   hint: '命中即扣分;扣到阈值以下判为供给方并排除。组名左侧红条=负向'},

  // ---- 旧模式 ----
  {section: 'legacy', key: 'match.patterns', label: '匹配正则(每行一个)', type: 'lines', wide: true, mono: true},
  {section: 'legacy', key: 'match.exclude_patterns', label: '排除正则(每行一个)', type: 'lines', wide: true, mono: true},

  // ---- 运行时 ----
  {section: 'runtime', key: 'runtime.request_interval_seconds', label: '请求间隔(秒)', type: 'int'},
  {section: 'runtime', key: 'runtime.retry', label: '重试次数', type: 'int'},
  {section: 'runtime', key: 'runtime.timeout', label: '单次超时(秒)', type: 'int'},
  {section: 'runtime', key: 'runtime.browser_window', label: '浏览器窗口', type: 'select',
   options: [['background', '后台'], ['foreground', '前台']]},
];

// 规则分组的中文说明(显示在组名旁)
const RULE_GROUP_CN = {
  order: '下单咨询', availability: '可用性询问', presence_weak: '在吗(弱)',
  inquiry: '咨询式提问', looking_for: '明确找陪玩', price_query: '价格咨询',
  skill_demand: '技能需求', game_demand: '上分带打', social_weak: '一起玩(弱)',
  topic_generic: '泛话题(仅低权重召回)',
  self_promo: '自我营销', recruitment: '招募/俱乐部', contact_solicit: '引流联系方式',
  pricing_offer: '报价促销', wait_order: '在线等单/秀战绩', voice_sales: '声音卖点',
  teaching_offer: '教学/包赢卖点',
};

function getVal(obj, path) {
  return path.split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj);
}
function setVal(obj, path, val) {
  const keys = path.split('.');
  let o = obj;
  keys.slice(0, -1).forEach(k => { o[k] = o[k] || {}; o = o[k]; });
  o[keys[keys.length - 1]] = val;
}

// 规则分组编辑器: 每组一张卡片(组名 / 权重 / 正则多行 / 删除)
function buildRulesEditor(groups, accent) {
  const box = document.createElement('div');
  box.className = 'rules-editor';
  const addCard = (name, spec) => {
    const card = document.createElement('div');
    card.className = 'rule-card ' + (accent || '');
    const head = document.createElement('div');
    head.className = 'rule-head';
    const nameIn = document.createElement('input');
    nameIn.className = 'rule-name';
    nameIn.value = name || '';
    nameIn.placeholder = '分组名(英文)';
    const cn = document.createElement('span');
    cn.className = 'rule-cn';
    cn.textContent = RULE_GROUP_CN[name] || '';
    const wIn = document.createElement('input');
    wIn.className = 'rule-weight';
    wIn.type = 'number';
    wIn.step = '0.5';
    wIn.value = (spec && spec.weight != null) ? spec.weight : 0;
    wIn.title = '权重(正数加分,负数扣分)';
    const badge = document.createElement('span');
    badge.className = 'rule-weight-badge ' + ((spec && spec.weight < 0) ? 'neg' : 'pos');
    const syncBadge = () => {
      const v = parseFloat(wIn.value) || 0;
      badge.textContent = (v > 0 ? '+' : '') + v;
      badge.className = 'rule-weight-badge ' + (v < 0 ? 'neg' : 'pos');
    };
    wIn.addEventListener('input', syncBadge);
    syncBadge();
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'small danger';
    del.textContent = '删除';
    del.onclick = () => card.remove();
    head.append(nameIn, cn, wIn, badge, del);
    const pats = document.createElement('textarea');
    pats.className = 'rule-patterns';
    pats.rows = 3;
    const list = (spec && spec.patterns) || [];
    pats.value = list.join('\n');
    pats.placeholder = '每行一个正则';
    card.append(head, pats);
    box.appendChild(card);
  };
  Object.entries(groups || {}).forEach(([n, s]) => addCard(n, s));
  const addBtn = document.createElement('button');
  addBtn.type = 'button';
  addBtn.className = 'small';
  addBtn.textContent = '+ 新增分组';
  addBtn.onclick = () => addCard('', {weight: 0, patterns: []});
  box.appendChild(addBtn);
  return box;
}

function readRulesEditor(box) {
  const out = {};
  box.querySelectorAll('.rule-card').forEach(card => {
    const name = card.querySelector('.rule-name').value.trim();
    if (!name) return;
    const weight = parseFloat(card.querySelector('.rule-weight').value) || 0;
    const patterns = card.querySelector('.rule-patterns').value
      .split('\n').map(s => s.trim()).filter(Boolean);
    out[name] = {weight, patterns};
  });
  return out;
}

// 单个字段的控件构建(返回 DOM 节点)
function buildFieldInput(f, config, available_platforms) {
  let input;
  if (f.type === 'select') {
    input = document.createElement('select');
    f.options.forEach(([v, t]) => {
      const o = document.createElement('option');
      o.value = v; o.textContent = t;
      input.appendChild(o);
    });
    input.value = getVal(config, f.key) ?? f.options[0][0];
  } else if (f.type === 'lines') {
    input = document.createElement('textarea');
    input.rows = 3;
    input.style.width = '100%';
    if (f.mono) input.className = 'mono-input';
    const v = getVal(config, f.key) || [];
    input.value = Array.isArray(v) ? v.join('\n') : v;
  } else if (f.type === 'platforms') {
    const box = document.createElement('div');
    box.className = 'checks';
    (available_platforms || []).forEach(p => {
      const l = document.createElement('label');
      const cb = document.createElement('input');
      cb.type = 'checkbox'; cb.value = p;
      cb.checked = (getVal(config, f.key) || []).includes(p);
      l.appendChild(cb);
      l.appendChild(document.createTextNode(p));
      box.appendChild(l);
    });
    input = box;
  } else if (f.type === 'bool') {
    const box = document.createElement('div');
    box.className = 'checks';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = !!getVal(config, f.key);
    box.appendChild(cb);
    input = box;
  } else if (f.type === 'rules') {
    input = buildRulesEditor(getVal(config, f.key), f.accent);
  } else if (f.type === 'number') {
    input = document.createElement('input');
    input.type = 'number';
    input.step = '0.05';
    input.value = getVal(config, f.key) ?? 0;
  } else {
    input = document.createElement('input');
    input.type = 'number';
    input.value = getVal(config, f.key) ?? 0;
  }
  input.dataset.key = f.key;
  input.dataset.type = f.type;
  return input;
}

async function loadConfig() {
  const {config, available_platforms} = await api('/api/config');
  const form = $('config-form');
  form.innerHTML = '';
  // 按分区渲染为多张卡片
  for (const sec of CONFIG_SECTIONS) {
    const fields = CONFIG_FIELDS.filter(f => f.section === sec.id);
    if (!fields.length) continue;
    const card = document.createElement('section');
    card.className = 'card config-section';
    card.dataset.section = sec.id;
    const head = document.createElement('div');
    head.className = 'section-head';
    head.innerHTML = `<span class="sec-icon">${sec.icon}</span>`;
    const h2 = document.createElement('h2');
    h2.textContent = sec.title;
    head.appendChild(h2);
    const desc = document.createElement('p');
    desc.className = 'section-desc';
    desc.textContent = sec.desc;
    card.append(head, desc);
    const grid = document.createElement('div');
    grid.className = 'grid';
    for (const f of fields) {
      // 内部含独立控件的字段用 div 包裹,避免 label 嵌套
      const hasInnerLabels = ['platforms', 'bool', 'rules'].includes(f.type);
      const wrap = document.createElement(hasInnerLabels ? 'div' : 'label');
      wrap.className = 'field' + (f.wide ? ' wide' : '');
      wrap.appendChild(document.createTextNode(f.label));
      if (f.hint) {
        const h = document.createElement('span');
        h.className = 'hint'; h.textContent = f.hint;
        wrap.appendChild(h);
      }
      wrap.appendChild(buildFieldInput(f, config, available_platforms));
      grid.appendChild(wrap);
    }
    card.appendChild(grid);
    form.appendChild(card);
  }
  syncSectionVisibility();
  // 匹配模式切换时,实时调整旧模式区域的视觉状态
  const modeSel = document.querySelector('[data-key="match.mode"]');
  if (modeSel) modeSel.addEventListener('change', syncSectionVisibility);
}

// mode=intent 时旧模式区域降透明度(仍可编辑)
function syncSectionVisibility() {
  const modeSel = document.querySelector('[data-key="match.mode"]');
  const legacy = document.querySelector('.config-section[data-section="legacy"]');
  if (!modeSel || !legacy) return;
  legacy.classList.toggle('dimmed', modeSel.value === 'intent');
}

$('btn-save-config').onclick = async () => {
  const msg = $('config-msg');
  // 以服务端当前配置为基底合并,避免表单未覆盖的键(output.* / webui.*)被整份覆盖丢失
  const base = await api('/api/config');
  const config = JSON.parse(JSON.stringify(base.config || {}));
  document.querySelectorAll('#config-form [data-key]').forEach(el => {
    const t = el.dataset.type;
    let v;
    if (t === 'platforms') {
      v = [...el.querySelectorAll('input:checked')].map(i => i.value);
    } else if (t === 'lines') {
      v = el.value.split('\n').map(s => s.trim()).filter(Boolean);
    } else if (t === 'bool') {
      v = el.querySelector('input').checked;
    } else if (t === 'rules') {
      v = readRulesEditor(el);
    } else if (t === 'int') {
      v = parseInt(el.value, 10) || 0;
    } else if (t === 'number') {
      v = parseFloat(el.value);
      if (Number.isNaN(v)) v = 0;
    } else {
      v = el.value;
    }
    setVal(config, el.dataset.key, v);
  });
  const r = await api('/api/config', 'PUT', {config});
  if (r.ok) { msg.textContent = '✓ 已保存'; msg.className = 'ok'; }
  else { msg.textContent = '✗ 保存失败: ' + JSON.stringify(r.detail || r); msg.className = 'err'; }
  setTimeout(() => msg.textContent = '', 4000);
};

// ---------- 结果查看 ----------
let outputsCache = [];
async function loadOutputs() {
  const {files} = await api('/api/outputs');
  outputsCache = files;
  const ul = $('file-list');
  ul.innerHTML = '';
  const kindCN = {videos: '视频', comments: '评论', tables: '表格'};
  for (const f of files) {
    const li = document.createElement('li');
    li.innerHTML = `<span>${f.name}</span><span class="kind">${kindCN[f.kind]} · ${fmtSize(f.size)}</span>`;
    li.onclick = () => {
      ul.querySelectorAll('li').forEach(x => x.classList.remove('active'));
      li.classList.add('active');
      previewFile(f);
    };
    ul.appendChild(li);
  }
}
function fmtSize(n) {
  return n > 1048576 ? (n / 1048576).toFixed(1) + 'MB'
       : n > 1024 ? (n / 1024).toFixed(1) + 'KB' : n + 'B';
}
async function previewFile(f) {
  const view = $('table-view');
  view.innerHTML = '<p class="hint">加载中…</p>';
  const r = await api(`/api/output?path=${encodeURIComponent(f.path)}`);
  if (!r.ok) { view.innerHTML = `<p class="hint">加载失败</p>`; return; }
  if (r.type === 'text') {
    view.innerHTML = `<pre>${esc(r.text)}</pre>`;
    return;
  }
  const total = r.total > r.rows.length
    ? `<p class="hint">共 ${r.total} 条,预览前 ${r.rows.length} 条。` +
      `<a href="/api/download?path=${encodeURIComponent(f.path)}">下载完整文件</a></p>`
    : `<p class="hint">共 ${r.total} 条</p>`;
  const table = document.createElement('table');
  const head = document.createElement('tr');
  r.columns.forEach(c => {
    const th = document.createElement('th');
    th.textContent = c;
    head.appendChild(th);
  });
  table.appendChild(head);
  for (const row of r.rows) {
    const tr = document.createElement('tr');
    r.columns.forEach(c => {
      const td = document.createElement('td');
      let val = row[c] !== undefined ? row[c]
        : (r.type === 'csv' ? row[r.columns.indexOf(c)] : '');
      if (Array.isArray(val)) val = val.join('|');
      val = String(val ?? '');
      if (/^https?:\/\//.test(val)) {
        const a = document.createElement('a');
        a.href = val; a.target = '_blank'; a.textContent = '打开链接';
        td.appendChild(a);
      } else {
        td.textContent = val;
      }
      tr.appendChild(td);
    });
    table.appendChild(tr);
  }
  view.innerHTML = '';
  view.appendChild(total ? (() => { const d = document.createElement('div'); d.innerHTML = total; return d.firstChild; })() : document.createTextNode(''));
  view.appendChild(table);
}
function esc(s) {
  return String(s).replace(/[&<>]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;'}[c]));
}
$('btn-refresh-outputs').onclick = loadOutputs;
let _outTimer;
function refreshOutputsSoon() {
  clearTimeout(_outTimer);
  _outTimer = setTimeout(() => {
    if ($('tab-results').classList.contains('active')) loadOutputs();
  }, 2000);
}
