'use strict';
/* 观页 AI 策划浏览器模块。加载后提供 window.GuanyeAI 与 window.GuanyePlanning。 */
(() => {
  const JSON_HEADERS = {'Accept': 'application/json'};
  const AI = {
    async request(path, options = {}) {
      const method = String(options.method || 'GET').toUpperCase();
      const headers = {...JSON_HEADERS, ...(options.headers || {})};
      let body;
      if (options.body !== undefined) {
        headers['Content-Type'] = 'application/json; charset=utf-8';
        body = typeof options.body === 'string' ? options.body : JSON.stringify(options.body);
      }
      const response = await fetch(path, {
        method,
        headers,
        body,
        credentials: options.credentials || 'same-origin',
        signal: options.signal,
      });
      const type = response.headers?.get?.('content-type') || '';
      let payload;
      if (type.includes('application/json')) payload = await response.json();
      else {
        const text = await response.text();
        try { payload = text ? JSON.parse(text) : null; } catch { payload = text; }
      }
      if (!response.ok) {
        const message = payload?.error || payload?.message || payload?.detail || `请求失败（HTTP ${response.status}）`;
        const error = new Error(String(message));
        error.status = response.status;
        error.payload = payload;
        throw error;
      }
      return payload;
    },
  };
  window.GuanyeAI = AI;

  const LIMITS = {
    maxFileBytes: 12 * 1024 * 1024,
    maxReferenceBytes: 8 * 1024 * 1024,
    maxTotalBytes: 32 * 1024 * 1024,
    maxFiles: 16,
    contentExtensions: ['.txt', '.md', '.csv', '.json', '.pdf', '.ppt', '.pptx'],
    styleExtensions: ['.pdf', '.ppt', '.pptx'],
    imageExtensions: ['.png', '.jpg', '.jpeg', '.webp'],
  };
  const NARRATIVE_ROLE_OPTIONS = [
    ['cover', '封面'],
    ['data', '数据'],
    ['transition', '过渡'],
    ['argument', '论证'],
    ['case', '案例'],
    ['summary', '总结'],
    ['other', '其他'],
  ];
  const COPY_KIND_LABELS = {
    title: '标题',
    subtitle: '副标题',
    body: '正文',
    bullet: '要点',
    label: '标签',
    caption: '图注',
    quote: '引语',
    source: '来源',
    note: '备注',
  };

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function extensionOf(name) {
    const index = String(name || '').lastIndexOf('.');
    return index >= 0 ? String(name).slice(index).toLowerCase() : '';
  }

  function humanBytes(value) {
    if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} KB`;
    return `${(value / 1024 / 1024).toFixed(1)} MB`;
  }

  function normalizeHistoryCursor(value) {
    return typeof value === 'string' && value ? value : null;
  }

  function modeLabel(value) {
    const mode = String(value || '');
    return mode === 'template_sample' ? '模板样板' : mode === 'content_draft' ? '内容稿' : mode || '模式未标明';
  }

  function statusLabel(value) {
    const status = String(value || '').toLowerCase();
    return status === 'confirmed' ? '已确认' : status === 'draft' ? '草稿' : status || '状态未标明';
  }

  function formatHistoryDate(value) {
    if (!value) return '';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString('zh-CN', {year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'});
  }

  function normalizeHistoryItem(raw) {
    if (!raw || typeof raw !== 'object') return null;
    const planId = String(raw.planId ?? raw.plan_id ?? '').trim();
    if (!planId) return null;
    const pageCount = Number.parseInt(raw.pageCount ?? raw.page_count, 10);
    const revision = Number.parseInt(raw.revision, 10);
    return {
      planId,
      title: String(raw.title || '未命名策划').trim() || '未命名策划',
      mode: String(raw.mode || ''),
      pageCount: Number.isFinite(pageCount) && pageCount >= 0 ? pageCount : 0,
      status: String(raw.status || '').toLowerCase(),
      revision: Number.isFinite(revision) && revision >= 0 ? revision : 0,
      createdAt: String(raw.createdAt ?? raw.created_at ?? ''),
    };
  }

  function mergeHistoryItems(previous, incoming) {
    const result = [];
    const positions = new Map();
    for (const raw of [...(previous || []), ...(incoming || [])]) {
      const item = normalizeHistoryItem(raw);
      if (!item) continue;
      const position = positions.get(item.planId);
      if (position === undefined) {
        positions.set(item.planId, result.length);
        result.push(item);
      } else {
        result[position] = {...result[position], ...item, createdAt: item.createdAt || result[position].createdAt};
      }
    }
    return result;
  }

  async function fileToBase64(file) {
    const buffer = await file.arrayBuffer();
    const bytes = new Uint8Array(buffer);
    let binary = '';
    const chunk = 0x8000;
    for (let index = 0; index < bytes.length; index += chunk) {
      binary += String.fromCharCode(...bytes.subarray(index, index + chunk));
    }
    return btoa(binary);
  }

  async function filesToPayload(input, kind, totalState) {
    const files = Array.from(input.files || []);
    const result = [];
    for (const file of files) {
      const extension = extensionOf(file.name);
      const allowed = kind === 'content' ? LIMITS.contentExtensions
        : kind === 'style' ? LIMITS.styleExtensions : LIMITS.imageExtensions;
      if (!allowed.includes(extension)) throw new Error(`${file.name} 的格式不受支持`);
      const limit = kind === 'reference' ? LIMITS.maxReferenceBytes : LIMITS.maxFileBytes;
      if (file.size > limit) throw new Error(`${file.name} 超过 ${humanBytes(limit)} 上限`);
      totalState.bytes += file.size;
      if (totalState.bytes > LIMITS.maxTotalBytes) throw new Error(`资料总量超过 ${humanBytes(LIMITS.maxTotalBytes)}`);
      totalState.count += 1;
      if (totalState.count > LIMITS.maxFiles) throw new Error(`单次最多上传 ${LIMITS.maxFiles} 个文件`);
      result.push({name: file.name, mime_type: file.type || 'application/octet-stream', data_base64: await fileToBase64(file)});
    }
    return result;
  }

  const STYLE = `
    :host { color:#17201e; font-family:"Noto Sans SC","Source Han Sans SC","Microsoft YaHei",sans-serif; }
    * { box-sizing:border-box; }
    .gp-shell { --ink:#17201e; --muted:#66706d; --line:#dce3df; --paper:#fbfcfa; --panel:#fff;
      --accent:#185f56; --accent-soft:#e7f1ee; --warn:#9a5b16; background:var(--paper); border:1px solid var(--line);
      border-radius:24px; padding:clamp(18px,3vw,34px); box-shadow:0 18px 60px rgba(23,32,30,.08); }
    .gp-head { display:flex; gap:20px; justify-content:space-between; align-items:flex-start; margin-bottom:24px; }
    .gp-eyebrow { color:var(--accent); font-size:12px; font-weight:800; letter-spacing:.16em; text-transform:uppercase; }
    h1, h2, h3, p { margin:0; }
    h1 { font-size:clamp(26px,4vw,42px); letter-spacing:-.04em; margin-top:6px; }
    h2 { font-size:21px; letter-spacing:-.02em; }
    h3 { font-size:16px; }
    .gp-intro { color:var(--muted); margin-top:8px; max-width:760px; line-height:1.65; }
    .gp-model { flex:none; border:1px solid var(--line); border-radius:999px; padding:7px 12px; font-size:12px; color:var(--muted); background:#fff; }
    .gp-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; }
    .gp-grid.three { grid-template-columns:repeat(3,minmax(0,1fr)); }
    .gp-field { display:grid; gap:7px; color:var(--ink); font-size:13px; font-weight:700; }
    .gp-field small { color:var(--muted); font-weight:500; line-height:1.5; }
    input, textarea, select { width:100%; border:1px solid var(--line); border-radius:12px; background:#fff; color:var(--ink);
      font:inherit; padding:11px 12px; outline:none; transition:border-color .15s, box-shadow .15s; }
    textarea { resize:vertical; min-height:86px; line-height:1.6; }
    input:focus, textarea:focus, select:focus { border-color:var(--accent); box-shadow:0 0 0 3px rgba(24,95,86,.12); }
    .gp-section { border-top:1px solid var(--line); margin-top:24px; padding-top:24px; }
    .gp-section-head { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:14px; }
    .gp-file-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:14px; margin-top:16px; }
    .gp-file-card { border:1px dashed #c9d4cf; border-radius:16px; padding:14px; background:#fff; }
    .gp-file-card input[type=file] { padding:0; border:0; background:transparent; font-size:12px; }
    .gp-actions { display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin-top:20px; }
    button { border:1px solid var(--line); border-radius:999px; background:#fff; color:var(--ink); padding:10px 16px;
      font:inherit; font-weight:800; cursor:pointer; transition:transform .15s, background .15s, border-color .15s; }
    button:hover:not(:disabled) { transform:translateY(-1px); border-color:#b8c6c0; }
    button:disabled { opacity:.55; cursor:not-allowed; }
    button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
    button.ghost { background:transparent; }
    button.small { padding:7px 11px; font-size:12px; }
    .gp-status { min-height:24px; margin-top:12px; color:var(--muted); font-size:13px; line-height:1.5; }
    .gp-status[data-kind=error] { color:#a3332b; }
    .gp-status[data-kind=success] { color:var(--accent); }
    .gp-history { margin:22px 0 26px; border-top:1px solid var(--line); border-bottom:1px solid var(--line); padding:20px 0; }
    .gp-history-head { display:flex; align-items:flex-start; justify-content:space-between; gap:16px; margin-bottom:12px; }
    .gp-history-head h2 { margin-bottom:5px; }
    .gp-history-status { min-height:22px; color:var(--muted); font-size:12px; line-height:1.55; display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
    .gp-history-status[data-status=error] { color:#a3332b; }
    .gp-history-status[data-status=loading] { color:var(--accent); }
    .gp-history-list { display:grid; gap:9px; margin-top:10px; }
    .gp-history-item { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:12px; align-items:center;
      border:1px solid var(--line); border-radius:14px; background:#fff; padding:12px 14px; }
    .gp-history-item[data-selected=true] { border-color:#9fbeb7; box-shadow:0 0 0 2px rgba(24,95,86,.08); }
    .gp-history-item-main { min-width:0; display:grid; gap:5px; }
    .gp-history-item-title { font-size:14px; overflow-wrap:anywhere; }
    .gp-history-item-meta { display:flex; flex-wrap:wrap; gap:6px 12px; color:var(--muted); font-size:12px; line-height:1.45; }
    .gp-history-badge { display:inline-flex; align-items:center; border-radius:999px; padding:3px 8px; background:var(--accent-soft); color:var(--accent); font-size:11px; font-weight:800; }
    .gp-history-badge[data-status=confirmed] { background:#eef0f8; color:#46548a; }
    .gp-history-badge[data-status=draft] { background:#fff5e8; color:var(--warn); }
    .gp-history-actions { display:flex; align-items:center; gap:8px; flex-wrap:wrap; justify-content:flex-end; }
    .gp-history-footer { margin-top:10px; display:flex; justify-content:flex-end; }
    .gp-history-empty { border:1px dashed #c9d4cf; border-radius:14px; padding:15px; color:var(--muted); background:#fff; font-size:13px; }
    .gp-history-error { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
    .gp-training { margin: 20px 0; }
    .gp-training summary { cursor:pointer; font-weight:750; }
    .gp-training [role=status] { min-height:1.5em; margin:12px 0; }
    .gp-training [data-training-error=true] { color:#a3332b; }
    .gp-training .gp-actions { flex-wrap:wrap; margin:12px 0; }
    .gp-result[hidden] { display:none; }
    .gp-result { margin-top:28px; }
    .gp-banner { display:flex; flex-wrap:wrap; align-items:center; gap:10px; border-radius:16px; padding:14px 16px;
      background:var(--accent-soft); color:var(--accent); font-weight:700; }
    .gp-banner.warn { background:#fff5e8; color:var(--warn); }
    .gp-card { border:1px solid var(--line); border-radius:18px; padding:18px; background:#fff; }
    .gp-stack { display:grid; gap:14px; }
    .gp-style { display:grid; grid-template-columns:1.2fr 1fr; gap:18px; }
    .gp-palette { display:flex; flex-wrap:wrap; gap:8px; }
    .gp-swatch { display:inline-flex; align-items:center; gap:6px; border:1px solid var(--line); border-radius:999px; padding:5px 9px; font-size:12px; }
    .gp-dot { width:16px; height:16px; border-radius:50%; border:1px solid rgba(0,0,0,.12); }
    .gp-tags { display:flex; flex-wrap:wrap; gap:7px; }
    .gp-tag { border:1px solid var(--line); border-radius:999px; padding:5px 9px; color:var(--muted); font-size:12px; }
    .gp-page { border:1px solid var(--line); border-radius:18px; background:#fff; overflow:hidden; }
    .gp-page-head { display:flex; flex-wrap:wrap; align-items:center; gap:10px; padding:13px 15px; background:#f7f9f7; border-bottom:1px solid var(--line); }
    .gp-page-head > input { flex:1 1 180px; min-width:0; }
    .gp-role-field { flex:0 1 164px; min-width:140px; gap:4px; font-size:12px; }
    .gp-role-field select { padding:7px 9px; }
    .gp-page-number { display:grid; place-items:center; width:30px; height:30px; border-radius:50%; background:var(--ink); color:#fff; font-size:12px; font-weight:800; }
    .gp-page-body { padding:15px; display:grid; gap:13px; }
    .gp-copy-row { display:grid; grid-template-columns:96px 1fr; gap:10px; align-items:start; }
    .gp-copy-kind { font-size:11px; font-weight:800; color:var(--muted); text-transform:uppercase; padding-top:12px; }
    .gp-meta { color:var(--muted); font-size:12px; line-height:1.55; }
    .gp-confirmed { display:grid; gap:16px; }
    .gp-mono { font-family:ui-monospace,SFMono-Regular,Consolas,monospace; overflow-wrap:anywhere; }
    .gp-hidden { display:none !important; }
    @media (max-width:760px) {
      .gp-shell { border-radius:18px; padding:16px; }
      .gp-head, .gp-style { grid-template-columns:1fr; display:grid; }
      .gp-grid, .gp-grid.three, .gp-file-grid { grid-template-columns:1fr; }
      .gp-copy-row { grid-template-columns:1fr; }
      .gp-copy-kind { padding-top:0; }
      .gp-page-head > input { flex:1 1 calc(100% - 40px); }
      .gp-role-field { flex:1 1 140px; min-width:0; }
      .gp-history-head { display:grid; }
      .gp-history-item { grid-template-columns:1fr; }
      .gp-history-actions { justify-content:flex-start; }
    }

    .gp-reference-preview { display:block; width:min(100%,520px); max-height:280px; object-fit:contain; border:1px solid var(--line); border-radius:14px; background:#f4f7f4; margin-top:10px; }`;

  function mount(root, opts = {}) {
    if (!root || (typeof root.attachShadow !== 'function' && !root.shadowRoot)) {
      throw new Error('GuanyePlanning.mount 需要有效的 DOM 容器');
    }
    if (root.__guanyePlanningController && typeof root.__guanyePlanningController.destroy === 'function') {
      root.__guanyePlanningController.destroy();
    }
    const shadow = root.shadowRoot || root.attachShadow({mode: 'open'});
    shadow.innerHTML = `
      <style>${STYLE}</style>
      <section class="gp-shell" aria-labelledby="gp-title">
        <header class="gp-head">
          <div>
            <div class="gp-eyebrow">GUANYE · AI PLANNING</div>
            <h1 id="gp-title">生成策划</h1>
            <p class="gp-intro">上传大纲、资料、论文、模板与参考图。系统会区分内容与风格，优先参考已完成评分且非保留集的高分作品；确认后版本锁定。</p>
          </div>
          <div class="gp-model">GPT-6.2 Sol · 策划通道</div>
        </header>
        <section class="gp-history" aria-labelledby="gp-history-title">
          <div class="gp-history-head">
            <div>
              <h2 id="gp-history-title">我的策划</h2>
              <p class="gp-meta">刷新或换设备后，可从这里继续草稿与已确认方案。</p>
            </div>
            <button class="ghost small" type="button" data-gp-history-action="refresh">刷新</button>
          </div>
          <div class="gp-history-status" id="gp-history-status" data-gp-history-status="loading" role="status" aria-live="polite"></div>
          <div class="gp-history-list" id="gp-history-list" data-gp-history-list></div>
          <div class="gp-history-footer"><button class="ghost small" type="button" data-gp-history-action="more" hidden>加载更多</button></div>
        </section>
        <form id="gp-form" novalidate>
          <div class="gp-grid">
            <label class="gp-field">策划模式
              <select name="mode">
                <option value="content_draft">内容稿：先成文案与页序</option>
                <option value="template_sample">模板样板：匹配模板页与版式</option>
              </select>
              <small>模板样板模式至少上传一份 PDF / PPT / PPTX 模板。</small>
            </label>
            <label class="gp-field">页数
              <input name="pageCount" type="number" min="1" max="30" value="8" step="1">
              <small>1–30 页，模型必须按此页数返回。</small>
            </label>
          </div>
          <div class="gp-section">
            <div class="gp-section-head"><h2>内容资料</h2><span class="gp-tag">只用于事实与文案</span></div>
            <div class="gp-stack">
              <label class="gp-field">大纲<textarea name="outline" rows="5" placeholder="章节、页序或叙事结构…"></textarea></label>
              <div class="gp-grid">
                <label class="gp-field">资料<textarea name="materials" rows="5" placeholder="业务资料、数据、案例…"></textarea></label>
                <label class="gp-field">论文 / 研究材料<textarea name="papers" rows="5" placeholder="摘要、引用与关键结论…"></textarea></label>
              </div>
            </div>
          </div>
          <div class="gp-section">
            <div class="gp-section-head"><h2>风格资料</h2><span class="gp-tag">只学版式、配色与图表语言</span></div>
            <label class="gp-field">风格要求<textarea name="styleBrief" rows="3" placeholder="例如：克制、留白、深绿与米白、避免渐变…"></textarea></label>
            <div class="gp-file-grid">
              <div class="gp-file-card"><label class="gp-field">内容文件<input name="contentFiles" type="file" multiple accept=".txt,.md,.csv,.json,.pdf,.ppt,.pptx"><small>TXT / MD / CSV / JSON / PDF / PPT / PPTX</small></label></div>
              <div class="gp-file-card"><label class="gp-field">模板文件<input name="styleFiles" type="file" multiple accept=".pdf,.ppt,.pptx"><small>PDF / PPT / PPTX；Windows 下复用现有 document_import 转换。</small></label></div>
              <div class="gp-file-card"><label class="gp-field">参考图<input name="referenceImages" type="file" multiple accept="image/png,image/jpeg,image/webp"><small>PNG / JPG / WEBP，单张 8 MB。</small></label></div>
            </div>
          </div>
          <div class="gp-actions">
            <button class="primary" type="submit" id="gp-analyze">生成策划</button>
            <button class="ghost" type="button" id="gp-reset">清空</button>
          </div>
          <div class="gp-status" id="gp-status" role="status" aria-live="polite"></div>
        </form>
        <section class="gp-result" id="gp-result" hidden aria-live="polite"></section>
      </section>`;
    const $ = selector => shadow.querySelector(selector);
    const form = $('#gp-form');
    const status = $('#gp-status');
    const result = $('#gp-result');
    const analyzeButton = $('#gp-analyze');
    const historyStatus = $('#gp-history-status');
    const historyList = $('#gp-history-list');
    const historyMore = $('[data-gp-history-action="more"]');
    const api = opts.request || AI;
    let state = {phase: 'idle', planId: null, revision: 0, plan: null, confirmed: null};
    let busy = false;
    let busyAction = '';
    let openingPlanId = '';
    let selectedPlanId = null;
    let planEpoch = 0;
    let historyEpoch = 0;
    let selectedCoverReference = null;
    let currentCoverStyleEvidence = null;
    let destroyed = false;
    const history = {items: [], nextCursor: null, loaded: false, loading: false, error: ''};

    const hostedTraining = window.Cloud?.coverTrainingSite === true;
    let trainingReady = !hostedTraining;
    if (hostedTraining) {
      $('#gp-title').textContent = '封面训练';
      $('.gp-intro').textContent = '先盘点已评分素材，再用 Sol 对照和人工复核。生成仅限单页封面，每次改稿都由人确认。';
      const pages = form.elements.pageCount;
      pages.value = '1';
      pages.max = '1';
      pages.readOnly = true;
      pages.nextElementSibling.textContent = '试点固定为单页封面。';
      form.classList.add('gp-hidden');
      $('.gp-history').classList.add('gp-hidden');
    }
    if (window.Cloud?.coverTrainingEnabled === true) {
      const pane = element('details', 'gp-card gp-training');
      pane.open = hostedTraining;
      pane.append(element('summary', '', '封面训练 · 素材盘点与 Sol 对照'));
      const hint = element('p', 'gp-meta', '先盘点旧分。有合格样本后，每次只手动跑一张图的两次 Sol 评审。留出样本不能用于纠错。');
      hint.setAttribute('role', 'status');
      if (hostedTraining) hint.textContent = '读取素材盘点后，才会开放单页策划与人工改稿。';
      const bar = element('div', 'gp-actions');
      const entries = element('div');
      const referencePicker = element('div', 'gp-reference-picker gp-card');
      const rows = new Map(), pairs = new Map();
      let coverReferences = [], referencesLoaded = false;
      let cursor = '', rubric = '', running = false, complete = false;
      let referencesLoading = false;
      const request = (path, body) => api.request('/api/ai/training/cover-' + path, body === undefined ? {method:'GET'} : {method:'POST',body});
      const button = (text, run) => {
        const node = element('button', 'ghost', text);
        node.type = 'button';
        node.disabled = running;
        node.addEventListener('click', run);
        return node;
      };
      function download(name, value) {
        const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], {type:'application/json'}));
        const link = element('a');
        link.href = url; link.download = name; link.click();
        setTimeout(() => URL.revokeObjectURL(url), 2000);
      }
      function renderReferencePicker() {
        if (!hostedTraining) return;
        referencePicker.replaceChildren();
        referencePicker.append(element('h3', '', '第一步：选择 Sol 要学习的素材库封面'));
        referencePicker.append(element('p', 'gp-meta', '这张原图会在策划阶段由 Sol 实际读图，并在生成阶段再次作为同一张视觉参考。未选择前不能生成策划。'));
        if (!coverReferences.length) {
          referencePicker.append(element('p', 'gp-meta', '暂无符合条件的授权高分封面；请先检查评分口径、授权状态或素材来源。'));
          return;
        }
        const select = element('select', 'gp-reference-select');
        select.setAttribute('aria-label', '选择素材库视觉参考');
        select.append(Object.assign(element('option', '', '请选择一张原图'), {value: ''}));
        coverReferences.forEach(ref => {
          const score = ref.score == null ? '无分数' : `均分 ${ref.score}`;
          select.append(Object.assign(element('option', '', `${ref.title || ref.deckId} · ${score}`), {value: ref.deckId}));
        });
        select.value = selectedCoverReference?.deckId || '';
        select.addEventListener('change', () => {
          selectedCoverReference = coverReferences.find(ref => ref.deckId === select.value) || null;
          if (selectedCoverReference && hostedTraining && !trainingReady && !destroyed) {
            trainingReady = true;
            form.classList.remove('gp-hidden');
            $('.gp-history').classList.remove('gp-hidden');
            rules.hidden = false;
            void loadHistory({reset: true});
          }
          renderReferencePicker();
          updateActionState();
        });
        referencePicker.append(select);
        if (selectedCoverReference?.imagePaths?.[0]) {
          const image = element('img', 'gp-reference-preview');
          image.alt = `素材库参考：${selectedCoverReference.title || selectedCoverReference.deckId}`;
          image.loading = 'lazy';
          referencePicker.append(image);
          void window.Cloud.imageUrl(selectedCoverReference.imagePaths[0]).then(url => { image.src = url; }).catch(() => {
            image.replaceWith(element('p', 'gp-meta', '原图预览暂不可用；为避免误用，不能继续策划。'));
            selectedCoverReference = null; updateActionState();
          });
          referencePicker.append(element('p', 'gp-meta', `来源：授权素材库 · ${selectedCoverReference.completeReviewCount || 0} 个完整评分 · 仅用于学习视觉，不复制原文`));
        }
      }
      async function loadCoverReferences() {
        if (!hostedTraining || referencesLoaded || referencesLoading || destroyed) return;
        referencesLoading = true;
        renderReferencePicker();
        try {
          const referencePayload = await request('references');
          coverReferences = Array.isArray(referencePayload?.references) ? referencePayload.references : [];
          referencesLoaded = true;
          renderReferencePicker();
        } catch (error) {
          referencesLoaded = true;
          referencePicker.replaceChildren(
            element('h3', '', '第一步：选择 Sol 要学习的素材库封面'),
            element('p', 'gp-meta', `读取素材库原图失败：${error?.message || '请稍后重试'}`),
            button('重新读取参考图', () => { referencesLoaded = false; void loadCoverReferences(); }),
          );
        } finally {
          referencesLoading = false;
          updateActionState();
        }
      }
      const task = fn => async () => {
        if (running) return;
        running = true;
        for (const node of pane.querySelectorAll('button')) node.disabled = true;
        try { delete hint.dataset.trainingError; await fn(); }
        catch (e) { hint.dataset.trainingError = 'true'; hint.textContent = e.message || '训练操作失败'; }
        finally { running = false; if (!destroyed) { for (const node of pane.querySelectorAll('button')) node.disabled = false; draw(); } }
      };
      const exclusionText = {notAuthorized:'未授权 AI', notCover:'非封面', unknownSource:'无可信来源',
        sourceReviewExcluded:'人工排除来源', legacyUnverifiedRubric:'旧分口径未确认',
        noCurrentPageScore:'没有现行逐页分', unavailablePreview:'授权预览不可用'};
      const held = row => parseInt(row.sourceGroupSha256?.slice(0, 2), 16) % 5 === 0;
      const deltas = (row, pair) => pair ? ['visual','layout'].map(k => ({k, diff:Math.abs(row.human[k] - pair.assisted[k]),
        bad:row.human[k] <= 3 && pair.assisted[k] >= 4})) : [];
      const priority = row => Math.max(0, ...deltas(row, pairs.get(row.id)).map(x => x.diff + (x.bad ? 10 : 0)));
      function draw() {
        const load = button(cursor ? '继续盘点' : '读取素材盘点', task(async () => {
          const inventory = await request('inventory?limit=40&after=' + encodeURIComponent(cursor));
          if (rubric && rubric !== inventory.rubricVersion) {
            rows.clear(); pairs.clear(); cursor = ''; rubric = '';
            throw new Error('评分标准已经更新，请重新盘点。');
          }
          if (hostedTraining && !trainingReady && selectedCoverReference && !destroyed) {
            trainingReady = true;
            form.classList.remove('gp-hidden');
            $('.gp-history').classList.remove('gp-hidden');
            rules.hidden = false;
            void loadHistory({reset: true});
          }
          rubric = inventory.rubricVersion;
          for (const {pair, ...row} of inventory.rows || []) {
            rows.set(row.id, row);
            if (pair && !pairs.has(row.id)) pairs.set(row.id, pair);
          }
          cursor = inventory.nextAfter || '';
          complete = !cursor;
          const kept = [...rows.values()].filter(row => row.eligible);
          hint.textContent = `已盘点 ${rows.size} 套：逐页可用 ${kept.length} 张（留出 ${kept.filter(held).length} 张）。`;
        }));
        load.disabled = running || complete;
        const exportData = button('下载盘点与留出预测 JSON', () => {
          download('cover-inventory.json', {rubricVersion:rubric, rows:[...rows.values()]});
          const sampled = [...pairs.values()].filter(pair => held(rows.get(pair.id)));
          download('cover-predictions.json', {rubricVersion:rubric, modelConfigId:sampled[0]?.modelConfigId || 'gpt-6.2-sol:cover-rubric-v1', rows:sampled});
        });
        exportData.disabled = running || !complete;
        bar.replaceChildren(load, exportData);
        const sorted = [...rows.values()].sort((a,b) => priority(b)-priority(a) || Number(b.eligible)-Number(a.eligible));
        entries.replaceChildren(...sorted.map(row => {
          const line = element('div', 'gp-meta');
          const pair = pairs.get(row.id);
          line.append(element('strong', '', row.id + ' · ' + (row.eligible ? (held(row) ? '留出' : '校准') : (exclusionText[row.exclusion] || '不可用于对照'))
            + (row.eligible && row.pageTypeEvidence === 'first-preview-proxy' ? '（首张待确认）' : '')));
          if (row.eligible) line.append(button(pair ? '重跑 Sol 对照（2 次）' : '运行 Sol 对照（2 次）', task(async () => {
            if (!window.confirm('向 Sol 发送这张封面和获授权的校准案例，并运行两次独立评审？')) return;
            const next = await request('pair', {deck_id:row.id});
            pairs.set(row.id, next);
            hint.textContent = '已保存独立对照；差异较大的记录会排在前面。';
          })));
          if (pair) {
            const format = scores => `${scores.visual} / ${scores.layout}`;
            line.append(element('span', '', ` · 人评 ${format(row.human)} · baseline ${format(pair.baseline)} · assisted ${format(pair.assisted)}`));
            if (priority(row) >= 1) line.append(element('p', '', ['visual','layout'].map(key => pair.reasons.assisted[key]).join('；')));
            if (priority(row) >= 1 && !held(row) && !pair.correctionId) {
              line.append(button('确认人工纠错', task(async () => {
                const reason = window.prompt('填写纠错原因（可留空），以现有逐页人工分为准。', '');
                if (reason === null) return;
                Object.assign(pair, await request('correct', {pair_id:pair.pairId, reason}));
              })));
            }
          }
          return line;
        }));
      }
      const markdown = element('textarea');
      markdown.rows = 4;
      markdown.maxLength = 12000;
      markdown.placeholder = '负责人：粘贴团队风格 Markdown';
      markdown.setAttribute('aria-label', '团队风格 Markdown');
      const previous = element('input');
      previous.type = 'number'; previous.min = '1'; previous.placeholder = '恢复版本号';
      previous.setAttribute('aria-label','恢复版本号');
      const publish = button('发布规则', task(async () => {
        if (!window.confirm('确认规则只参考训练组和已人工确认的新作，没有使用留出组？')) return;
        const data = await request('style', {markdown:markdown.value});
        hint.textContent = `已发布团队风格 v${data.version}。`;
      }));
      const restore = button('恢复规则版本', task(async () => {
        const data = await request('style', {previous:Number(previous.value)});
        hint.textContent = `已恢复团队风格 v${data.version}。`;
      }));
      const rules = element('div');
      rules.append(element('h3', '', '团队风格规则（仅负责人）'), markdown, publish, previous, restore);
      rules.hidden = hostedTraining;
      // In the hosted pilot the actual reference image is the first gate.
      // Do not hide it behind the optional old-library inventory: otherwise a
      // user can start a plan without ever giving Sol a real style sample.
      pane.append(hint, referencePicker, bar, entries, rules);
      pane.addEventListener('toggle', () => { if (trainingReady) rules.hidden = false; });
      form.before(pane);
      draw();
      if (hostedTraining) void loadCoverReferences();
    }

    function emit(name, detail) {
      if (typeof window.CustomEvent !== 'function') return;
      window.dispatchEvent(new CustomEvent(name, {detail}));
    }

    function setStatus(message, kind = '') {
      status.textContent = message || '';
      status.dataset.kind = kind;
    }

    function updateActionState() {
      const locked = state.phase === 'confirmed';
      analyzeButton.disabled = busy || locked || Boolean(openingPlanId);
      analyzeButton.textContent = busy && busyAction === 'analyze' ? '生成中…' : '生成策划';
      shadow.querySelectorAll('button[data-gp-action]').forEach(button => {
        const action = button.dataset.gpAction;
        if (button.dataset.gpBoundaryDisabled === undefined) {
          button.dataset.gpBoundaryDisabled = button.disabled ? 'true' : 'false';
        }
        let disabled = busy || Boolean(openingPlanId) || button.dataset.gpBoundaryDisabled === 'true';
        if (action === 'save' || action === 'confirm') disabled = disabled || locked || !state.plan;
        if (action === 'generate') disabled = disabled || state.phase !== 'confirmed';
        else if (locked) disabled = true;
        button.disabled = disabled;
        if (action === 'save') button.textContent = busy && busyAction === 'save' ? '保存中…' : '保存草稿';
        if (action === 'confirm') button.textContent = busy && busyAction === 'confirm' ? '确认中…' : '确认并锁定策划';
        if (action === 'generate') button.textContent = busy && busyAction === 'generate' ? '提交中…' : (button.dataset.gpLabel || '提交生成');
      });
      shadow.querySelectorAll('button[data-gp-history-action], button[data-gp-history-open]').forEach(button => {
        const action = button.dataset.gpHistoryAction;
        button.disabled = busy || (action === 'more' && history.loading) || (action === 'refresh' && history.loading);
      });
      for (const control of form.querySelectorAll('input, textarea, select')) {
        control.disabled = busy || Boolean(openingPlanId);
      }
      for (const control of result.querySelectorAll('input, textarea, select, button')) {
        if (control.matches('button[data-gp-action]')) continue;
        if (control.dataset.gpBoundaryDisabled === undefined) {
          control.dataset.gpBoundaryDisabled = control.disabled ? 'true' : 'false';
        }
        control.disabled = busy || Boolean(openingPlanId) || control.dataset.gpBoundaryDisabled === 'true';
      }
    }

    function setBusy(value, action = '') {
      busy = value;
      busyAction = value ? action : '';
      updateActionState();
    }

    function setOpening(planId) {
      openingPlanId = planId || '';
      updateActionState();
    }

    function currentHistoryItem() {
      if (!state.planId) return null;
      const existing = history.items.find(item => item.planId === state.planId);
      const plan = state.plan || {};
      return normalizeHistoryItem({
        planId: state.planId,
        title: plan.title || existing?.title || '未命名策划',
        mode: plan.mode || existing?.mode || '',
        pageCount: Array.isArray(plan.pages) ? plan.pages.length : plan.pageCount ?? existing?.pageCount,
        status: state.phase === 'confirmed' ? 'confirmed' : 'draft',
        revision: state.revision,
        createdAt: existing?.createdAt || '',
      });
    }

    function upsertHistoryItem(raw) {
      const item = normalizeHistoryItem(raw);
      if (!item) return;
      selectedPlanId = item.planId;
      const exists = history.items.some(candidate => candidate.planId === item.planId);
      history.items = exists ? mergeHistoryItems(history.items, [item]) : mergeHistoryItems([item], history.items);
      renderHistory();
    }

    function historyStatusRow(message, status) {
      historyStatus.replaceChildren(element('span', '', message));
      historyStatus.dataset.status = status;
      if (status === 'error') {
        const retry = element('button', 'small', '重试');
        retry.type = 'button';
        retry.dataset.gpHistoryAction = 'retry';
        retry.addEventListener('click', () => { void loadHistory({reset: true}); });
        historyStatus.append(retry);
      }
    }

    function renderHistory() {
      if (destroyed) return;
      let message = '';
      let statusName = 'ready';
      if (history.loading && !history.loaded) {
        message = '正在读取我的策划…';
        statusName = 'loading';
      } else if (history.loading) {
        message = '正在刷新我的策划，当前列表保持可用…';
        statusName = 'loading';
      } else if (history.error) {
        message = history.items.length
          ? `刷新失败：${history.error}。当前列表和编辑内容仍保留。`
          : `读取失败：${history.error}`;
        statusName = 'error';
      } else if (!history.loaded) {
        message = '正在读取我的策划…';
        statusName = 'loading';
      } else if (!history.items.length) {
        message = '还没有保存的策划，可从下方生成第一份。';
        statusName = 'empty';
      } else {
        message = `已加载 ${history.items.length} 份策划`;
        statusName = 'ready';
      }
      historyStatusRow(message, statusName);
      historyList.replaceChildren();
      if (!history.items.length && (history.loaded || history.error)) {
        historyList.append(element('div', 'gp-history-empty', history.error ? '历史暂时不可用，可直接重试。' : '暂无已保存策划。'));
      }
      for (const item of history.items) {
        const row = element('article', 'gp-history-item');
        row.dataset.gpHistoryItem = '';
        row.dataset.planId = item.planId;
        row.dataset.revision = String(item.revision);
        row.dataset.selected = String(selectedPlanId === item.planId);
        const main = element('div', 'gp-history-item-main');
        const titleLine = element('div', 'gp-history-item-title');
        titleLine.append(element('strong', '', item.title));
        const badge = element('span', 'gp-history-badge', statusLabel(item.status));
        badge.dataset.status = item.status || 'unknown';
        titleLine.append(badge);
        main.append(titleLine);
        const meta = element('div', 'gp-history-item-meta');
        meta.append(element('span', '', modeLabel(item.mode)), element('span', '', `${item.pageCount} 页`));
        if (item.revision > 0) meta.append(element('span', '', `修订 ${item.revision}`));
        if (item.createdAt) meta.append(element('span', '', formatHistoryDate(item.createdAt)));
        main.append(meta);
        const actions = element('div', 'gp-history-actions');
        const open = element('button', 'small', openingPlanId === item.planId ? '打开中…' : '打开');
        open.type = 'button';
        open.dataset.gpHistoryOpen = item.planId;
        open.addEventListener('click', () => {
          const active = root.__guanyePlanningController;
          if (active && typeof active.loadPlan === 'function') void active.loadPlan(item.planId);
        });
        actions.append(open);
        row.append(main, actions);
        historyList.append(row);
      }
      historyMore.hidden = history.nextCursor === null;
      historyMore.textContent = history.loading && history.loaded ? '加载中…' : '加载更多';
      updateActionState();
    }

    async function requestHistoryPage(cursor) {
      const suffix = cursor ? `?cursor=${encodeURIComponent(cursor)}` : '';
      const payload = await api.request(`/api/ai/planning/history${suffix}`, {method: 'GET'});
      if (!payload || typeof payload !== 'object' || !Array.isArray(payload.items)) {
        throw new Error('我的策划返回格式无效');
      }
      return {
        items: payload.items.map(normalizeHistoryItem).filter(Boolean),
        nextCursor: normalizeHistoryCursor(payload.nextCursor),
      };
    }

    async function loadHistory({reset = false} = {}) {
      if (destroyed) return null;
      if (!reset && (history.loading || history.nextCursor === null)) return null;
      if (reset) historyEpoch += 1;
      const token = historyEpoch;
      const cursor = reset ? null : history.nextCursor;
      history.loading = true;
      history.error = '';
      renderHistory();
      try {
        const page = await requestHistoryPage(cursor);
        if (destroyed || token !== historyEpoch) return null;
        if (reset) {
          const selected = selectedPlanId
            ? history.items.find(item => item.planId === selectedPlanId) || currentHistoryItem()
            : null;
          history.items = mergeHistoryItems([], page.items);
          if (selected && !history.items.some(item => item.planId === selected.planId)) {
            history.items = mergeHistoryItems(history.items, [selected]);
          }
          history.nextCursor = page.nextCursor;
          history.loaded = true;
        } else {
          history.items = mergeHistoryItems(history.items, page.items);
          history.nextCursor = page.nextCursor;
          history.loaded = true;
        }
        history.error = '';
        emit('guanye:planning-history-loaded', {items: history.items.map(item => ({...item})), nextCursor: history.nextCursor, reset});
        return page;
      } catch (error) {
        if (!destroyed && token === historyEpoch) {
          history.error = error?.message || String(error) || '读取我的策划失败';
          history.loaded = true;
        }
        return null;
      } finally {
        if (!destroyed && token === historyEpoch) {
          history.loading = false;
          renderHistory();
        }
      }
    }

    $('[data-gp-history-action="refresh"]').addEventListener('click', () => { void loadHistory({reset: true}); });
    historyMore.addEventListener('click', () => { void loadHistory({reset: false}); });

    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (busy || state.phase === 'confirmed') return;
      const epoch = ++planEpoch;
      setBusy(true, 'analyze');
      currentCoverStyleEvidence = null;
      setStatus('正在提取资料并请求 GPT-6.2 Sol 策划…');
      try {
        const total = {bytes: 0, count: 0};
        const contentFiles = await filesToPayload(form.elements.contentFiles, 'content', total);
        const styleFiles = await filesToPayload(form.elements.styleFiles, 'style', total);
        const referenceImages = await filesToPayload(form.elements.referenceImages, 'reference', total);
        const body = {
          mode: form.elements.mode.value,
          pageCount: Number(form.elements.pageCount.value),
          outline: form.elements.outline.value,
          materials: form.elements.materials.value,
          papers: form.elements.papers.value,
          styleBrief: form.elements.styleBrief.value,
          content_files: contentFiles,
          style_files: styleFiles,
          reference_images: referenceImages,
        };
        if (hostedTraining) {
          if (!selectedCoverReference?.deckId) throw new Error('请先在上方选择一张素材库原图，Sol 才能真正学习它的视觉风格。');
          body.reference_deck_id = selectedCoverReference.deckId;
        }
        const payload = await api.request('/api/ai/planning/analyze', {method: 'POST', body});
        if (destroyed || epoch !== planEpoch) return;
        if (!payload?.planId || !payload?.plan) throw new Error('策划接口返回内容不完整');
        const revision = Number(payload.revision || 1);
        currentCoverStyleEvidence = payload.coverStyleEvidence || null;
        state = {phase: 'draft', planId: payload.planId, revision, plan: payload.plan, confirmed: null};
        upsertHistoryItem({
          planId: payload.planId,
          title: payload.plan.title,
          mode: payload.plan.mode,
          pageCount: Array.isArray(payload.plan.pages) ? payload.plan.pages.length : payload.plan.pageCount,
          status: 'draft',
          revision,
          createdAt: payload.createdAt || '',
        });
        renderPlan(payload);
        setStatus('策划草稿已生成。请检查并编辑逐页文案、页序与风格，然后确认。', 'success');
        emit('guanye:planning-analyzed', {planId: state.planId, revision: state.revision});
      } catch (error) {
        if (!destroyed && epoch === planEpoch) setStatus(error.message || '策划失败', 'error');
      } finally {
        if (busyAction === 'analyze') setBusy(false);
      }
    });

    $('#gp-reset').addEventListener('click', () => {
      if (busy) return;
      planEpoch += 1;
      form.reset();
      form.elements.pageCount.value = hostedTraining ? '1' : '8';
      state = {phase: 'idle', planId: null, revision: 0, plan: null, confirmed: null};
      selectedPlanId = null;
      result.hidden = true;
      result.replaceChildren();
      setStatus('');
      setBusy(false);
      renderHistory();
    });

    function renderTags(values) {
      const wrap = element('div', 'gp-tags');
      for (const value of values || []) {
        if (value !== null && value !== undefined && String(value).trim()) wrap.append(element('span', 'gp-tag', String(value)));
      }
      return wrap;
    }

    function renderCoverEvidence(container, visual) {
      if (!visual || !visual.path) return;
      const card = element('div', 'gp-banner success');
      card.append(element('strong', '', '本次实际学习的素材库原图'), document.createTextNode(`：${visual.title || visual.deckId} · SHA-256 ${String(visual.sha256 || '').slice(0, 12)}…`));
      const image = element('img', 'gp-reference-preview'); image.alt = '本次锁定的素材库视觉参考'; image.loading = 'lazy';
      card.append(image);
      void window.Cloud.imageUrl(visual.path).then(url => { image.src = url; }).catch(() => {
        image.replaceWith(element('p', 'gp-meta', '本次锁定的素材原图预览暂不可用。'));
      });
      const facts = visual.facts || {}; const line = [facts.typography, facts.composition, facts.whitespace].filter(Boolean).join('；');
      if (line) card.append(element('p', 'gp-meta', `Sol 已记录：${line}`));
      container.append(card);
    }

    function field(labelText, value, onInput, multiline = false, rows = 3) {
      const label = element('label', 'gp-field', labelText);
      const input = element(multiline ? 'textarea' : 'input');
      if (multiline) input.rows = rows;
      input.value = value || '';
      input.addEventListener('input', () => onInput(input.value));
      label.append(input);
      return label;
    }

    function renderPlan(payload) {
      const plan = state.plan;
      result.hidden = false;
      result.replaceChildren();
      const stack = element('div', 'gp-stack');
      if ((payload.warnings || plan.warnings || []).length) {
        const warning = element('div', 'gp-banner warn');
        warning.append(element('strong', '', '注意'), document.createTextNode([...new Set([...(payload.warnings || []), ...(plan.warnings || [])])].join('；')));
        stack.append(warning);
      }
      const summaryCard = element('div', 'gp-card gp-stack');
      const titleField = field('策划标题', plan.title, value => { plan.title = value; });
      titleField.querySelector('input').dataset.gpPlanField = 'title';
      const summaryField = field('策划摘要', plan.summary, value => { plan.summary = value; }, true, 3);
      summaryField.querySelector('textarea').dataset.gpPlanField = 'summary';
      const briefField = field('生成约束', plan.generationBrief, value => { plan.generationBrief = value; }, true, 5);
      briefField.querySelector('textarea').dataset.gpPlanField = 'generationBrief';
      summaryCard.append(titleField, summaryField, briefField);
      stack.append(summaryCard);

      const sourcesCard = element('div', 'gp-card gp-stack');
      sourcesCard.append(element('h2', '', '可核实依据'));
      const manifest = plan.sourceManifest || [];
      if (manifest.length) {
        const sourceTags = manifest.map(item => `${item.role === 'content' ? '内容' : item.role === 'style' ? '模板' : '参考图'}：${item.name}`);
        sourcesCard.append(element('p', 'gp-meta', `实际上传：${sourceTags.join(' · ')}`));
      } else {
        sourcesCard.append(element('p', 'gp-meta', '实际上传：仅文字输入'));
      }
      renderCoverEvidence(stack, plan.coverStyleEvidence || payload?.coverStyleEvidence || currentCoverStyleEvidence);
      const actualReferences = plan.references || [];
      if (actualReferences.length) {
        for (const reference of actualReferences) {
          const row = element('div', 'gp-card');
          const scoreLabel = reference.scoreSource === 'formal' ? '正式整套分'
            : reference.scoreSource === 'team' ? '团队均分' : '评分';
          const countLabel = reference.scoreSource ? '完整团队评分' : '完整评分';
          row.append(element('strong', '', reference.title || reference.deckId));
          row.append(element('div', 'gp-meta', `${scoreLabel} ${reference.score ?? '—'} · ${countLabel} ${reference.completeReviewCount || 0} 人 · ${reference.pageCount || 0} 页`));
          row.append(element('div', 'gp-meta', `作品 ID：${reference.deckId} · 非保留集`));
          sourcesCard.append(row);
        }
      } else {
        sourcesCard.append(element('p', 'gp-meta', '服务器未返回可核实的高分参考作品。'));
      }
      stack.append(sourcesCard);
      const styleImages = plan.generationStyleImages || [];
      const handoffCard = element('div', 'gp-card gp-stack');
      handoffCard.append(element('h2', '', '生成资产交接（服务端）'));
      if (styleImages.length) {
        handoffCard.append(element('p', 'gp-meta', `可直接用于生图的私有风格图：${styleImages.length} 张，按“用户参考图优先、模板代表页随后”排序。`));
        handoffCard.append(renderTags(styleImages.map(path => path.split('/').pop())));
      } else {
        handoffCard.append(element('p', 'gp-meta', '本次没有可直接交接的用户风格图或模板代表页；generation 应回退到已核实参考作品的 refs.imagePaths。'));
      }
      stack.append(handoffCard);
      if (plan.decisionBasis) {
        const basisCard = element('div', 'gp-card gp-stack');
        basisCard.append(element('h2', '', 'AI 解释（未核实）'));
        if (plan.decisionBasis.contentAudience) basisCard.append(element('p', 'gp-meta', `AI 判断的内容/受众：${plan.decisionBasis.contentAudience}`));
        if ((plan.decisionBasis.userReferences || []).length) basisCard.append(element('p', 'gp-meta', `AI 对用户参考的解释：${plan.decisionBasis.userReferences.join(' · ')}`));
        if ((plan.decisionBasis.libraryEvidence || []).length) basisCard.append(element('p', 'gp-meta', `AI 对高分库的解释（以上述服务器候选为准）：${plan.decisionBasis.libraryEvidence.join(' · ')}`));
        if ((plan.decisionBasis.tradeoffs || []).length) basisCard.append(element('p', 'gp-meta', `AI 取舍说明：${plan.decisionBasis.tradeoffs.join(' · ')}`));
        stack.append(basisCard);
      }
      if ((plan.designDirections || []).length) {
        const directionsWrap = element('div', 'gp-stack');
        directionsWrap.append(element('h2', '', '文字方向备选（AI 建议）'));
        for (const direction of plan.designDirections) {
          const card = element('div', 'gp-card gp-stack');
          card.append(element('h3', '', direction.name || '未命名方向'));
          if (direction.summary) card.append(element('p', 'gp-meta', `AI 解释：${direction.summary}`));
          if (direction.formLogic) card.append(element('p', 'gp-meta', `AI 解释 · 内容决定形式：${direction.formLogic}`));
          if (direction.contentAudience) card.append(element('p', 'gp-meta', `AI 判断的内容/受众：${direction.contentAudience}`));
          if ((direction.userReferenceSupport || []).length) card.append(element('p', 'gp-meta', `AI 对用户参考的说明：${direction.userReferenceSupport.join(' · ')}`));
          if (direction.rationale) card.append(element('p', 'gp-meta', `AI 推荐理由（未核实）：${direction.rationale}`));
          if ((direction.libraryEvidence || []).length) card.append(element('p', 'gp-meta', `AI 对高分库的解释（未核实）：${direction.libraryEvidence.join(' · ')}`));
          if ((direction.tradeoffs || []).length) card.append(element('p', 'gp-meta', `AI 取舍说明：${direction.tradeoffs.join(' · ')}`));
          if (direction.whenToChoose) card.append(element('p', 'gp-meta', `AI 建议的适用场景：${direction.whenToChoose}`));
          directionsWrap.append(card);
        }
        stack.append(directionsWrap);
      }
      const styleCard = element('div', 'gp-card gp-style');
      const styleText = element('div', 'gp-stack');
      const styleNameField = field('风格名称', plan.style.name, value => { plan.style.name = value; });
      styleNameField.querySelector('input').dataset.gpPlanField = 'styleName';
      const styleSummaryField = field('风格说明', plan.style.summary, value => { plan.style.summary = value; }, true, 4);
      styleSummaryField.querySelector('textarea').dataset.gpPlanField = 'styleSummary';
      styleText.append(element('h2', '', '推荐风格'), styleNameField, styleSummaryField, field('版式语言', plan.style.layoutLanguage, value => { plan.style.layoutLanguage = value; }, true, 4));
      const palette = element('div', 'gp-palette');
      for (const color of plan.style.palette || []) {
        const chip = element('span', 'gp-swatch');
        const dot = element('span', 'gp-dot');
        if (/^#[0-9a-f]{6}$/i.test(color)) dot.style.background = color;
        chip.append(dot, document.createTextNode(color));
        palette.append(chip);
      }
      styleText.append(palette);
      styleCard.append(styleText);
      stack.append(styleCard);

      const pagesHeader = element('div', 'gp-section-head');
      pagesHeader.append(element('h2', '', `逐页策划 · ${plan.pages.length} 页`), renderTags([`${plan.mode === 'template_sample' ? '模板样板' : '内容稿'}`]));
      stack.append(pagesHeader);
      plan.pages.forEach((page, index) => stack.append(renderPage(page, index)));
      stack.append(renderConfirmActions());
      result.append(stack);
    }

    function renderPage(page, index) {
      const shell = element('article', 'gp-page');
      const head = element('div', 'gp-page-head');
      head.append(element('span', 'gp-page-number', String(index + 1)));
      const titleInput = element('input');
      titleInput.value = page.title || '';
      titleInput.dataset.gpPageField = 'title';
      titleInput.setAttribute('aria-label', `第 ${index + 1} 页标题`);
      titleInput.addEventListener('input', () => { page.title = titleInput.value; });
      const currentRole = String(page.narrativeRole ?? '');
      const roleLabel = element('label', 'gp-field gp-role-field', '叙事角色');
      const roleSelect = element('select');
      roleSelect.name = 'narrativeRole';
      roleSelect.dataset.gpRole = 'narrativeRole';
      roleSelect.setAttribute('aria-label', `第 ${index + 1} 页叙事角色`);
      if (!currentRole) {
        const unset = element('option', '', '未设置');
        unset.value = '';
        roleSelect.append(unset);
      }
      for (const [value, label] of NARRATIVE_ROLE_OPTIONS) {
        const option = element('option', '', label);
        option.value = value;
        roleSelect.append(option);
      }
      if (currentRole && !NARRATIVE_ROLE_OPTIONS.some(([value]) => value === currentRole)) {
        const custom = element('option', '', `历史值：${currentRole}`);
        custom.value = currentRole;
        roleSelect.append(custom);
      }
      roleSelect.value = currentRole;
      roleSelect.addEventListener('change', () => { page.narrativeRole = roleSelect.value; });
      roleLabel.append(roleSelect);
      head.append(titleInput, roleLabel);
      const up = element('button', 'small', '上移');
      up.type = 'button';
      up.dataset.gpAction = 'up';
      up.disabled = index === 0;
      up.addEventListener('click', () => {
        if (index === 0) return;
        state.plan.pages.splice(index - 1, 0, state.plan.pages.splice(index, 1)[0]);
        state.plan.pages.forEach((item, position) => { item.index = position + 1; });
        renderPlan({warnings: []});
      });
      const down = element('button', 'small', '下移');
      down.type = 'button';
      down.dataset.gpAction = 'down';
      down.disabled = index === state.plan.pages.length - 1;
      down.addEventListener('click', () => {
        if (index >= state.plan.pages.length - 1) return;
        state.plan.pages.splice(index + 1, 0, state.plan.pages.splice(index, 1)[0]);
        state.plan.pages.forEach((item, position) => { item.index = position + 1; });
        renderPlan({warnings: []});
      });
      head.append(up, down);
      shell.append(head);

      const body = element('div', 'gp-page-body');
      body.append(element('p', 'gp-meta', '逐字文案按原文提交；版式与视觉要求仅用于后续模型生成。'));
      for (const item of page.copy || []) {
        const kind = String(item.kind || 'body');
        const kindLabel = COPY_KIND_LABELS[kind] || kind;
        const row = element('div', 'gp-copy-row');
        row.append(element('div', 'gp-copy-kind', `逐字文案 · ${kindLabel}`));
        const textarea = element('textarea');
        textarea.rows = 3;
        textarea.value = item.text || '';
        textarea.dataset.gpCopyKind = kind;
        textarea.setAttribute('aria-label', `第 ${index + 1} 页逐字文案 · ${kindLabel}`);
        textarea.addEventListener('input', () => { item.text = textarea.value; });
        row.append(textarea);
        body.append(row);
      }
      const layoutField = field('版式要求（用于模型）', page.layout, value => { page.layout = value; }, true, 4);
      layoutField.querySelector('textarea').dataset.gpPageField = 'layout';
      const visualField = field('视觉要求（用于模型）', page.visualBrief, value => { page.visualBrief = value; }, true, 4);
      visualField.querySelector('textarea').dataset.gpPageField = 'visualBrief';
      body.append(layoutField, visualField);
      if ((page.contentSources || []).length) {
        const sourceLine = element('div', 'gp-meta');
        sourceLine.append(element('strong', '', '内容来源：'), document.createTextNode(page.contentSources.join(' · ')));
        body.append(sourceLine);
      }
      if ((page.styleReferenceDeckIds || []).length) {
        body.append(renderTags((page.styleReferenceDeckIds || []).map(id => `参考 ${id}`)));
      }
      shell.append(body);
      return shell;
    }

    function renderConfirmActions() {
      const actions = element('div', 'gp-actions');
      const note = element('p', 'gp-meta', '确认后逐页文案、页序、风格和参考作品将写入不可变版本，客户端不能再修改。');
      const save = element('button', 'ghost', '保存草稿');
      save.type = 'button';
      save.dataset.gpAction = 'save';
      save.addEventListener('click', saveDraft);
      const button = element('button', 'primary', '确认并锁定策划');
      button.type = 'button';
      button.dataset.gpAction = 'confirm';
      button.addEventListener('click', confirmPlan);
      actions.append(save, button, note);
      return actions;
    }

    async function saveDraft() {
      if (busy || !state.plan || state.phase === 'confirmed') return;
      const targetPlanId = state.planId;
      if (!targetPlanId) { setStatus('当前策划还没有可保存的服务器 ID', 'error'); return; }
      const expectedRevision = Number(state.revision) || 0;
      const epoch = ++planEpoch;
      setBusy(true, 'save');
      setStatus('正在保存草稿…');
      try {
        const draft = JSON.parse(JSON.stringify(state.plan));
        const payload = await api.request('/api/ai/planning/save', {
          method: 'POST',
          body: {plan_id: targetPlanId, expected_revision: expectedRevision, draft},
        });
        if (destroyed || epoch !== planEpoch || state.planId !== targetPlanId) return;
        if (payload?.status && payload.status !== 'draft') throw new Error('保存接口返回状态无效');
        if (!payload?.draft || typeof payload.draft !== 'object' || Array.isArray(payload.draft)) {
          throw new Error('保存接口返回的草稿无效');
        }
        const revision = Number(payload.revision);
        if (!Number.isFinite(revision) || revision < 1) throw new Error('保存接口未返回有效修订号');
        const planId = String(payload.planId || targetPlanId);
        state = {phase: 'draft', planId, revision, plan: payload.draft, confirmed: null};
        upsertHistoryItem({
          planId,
          title: payload.draft.title,
          mode: payload.draft.mode,
          pageCount: Array.isArray(payload.draft.pages) ? payload.draft.pages.length : payload.draft.pageCount,
          status: 'draft',
          revision,
          createdAt: history.items.find(item => item.planId === planId)?.createdAt || '',
        });
        renderPlan({warnings: []});
        setStatus(`草稿已保存，服务器修订 ${revision}。`, 'success');
        emit('guanye:planning-saved', {planId, revision, status: 'draft'});
      } catch (error) {
        if (!destroyed && epoch === planEpoch) {
          const message = error?.message || '保存失败';
          const conflict = error?.status === 409 || /版本|冲突|更新|刷新/.test(String(message));
          const recovery = conflict
            ? '当前输入已保留；请刷新“我的策划”并打开服务器最新版本，打开会替换当前内容。'
            : '当前输入已保留，可直接重试。';
          setStatus(`保存失败：${message}。${recovery}`, 'error');
        }
      } finally {
        if (busyAction === 'save') setBusy(false);
      }
    }

    async function confirmPlan() {
      if (busy || !state.plan) return;
      const title = String(state.plan.title || '').trim();
      if (!title) { setStatus('策划标题不能为空', 'error'); return; }
      if (typeof window.confirm === 'function' && !window.confirm('确认后将锁定逐页文案、页序、风格和参考作品，不能再修改。继续吗？')) return;
      const targetPlanId = state.planId;
      const epoch = ++planEpoch;
      setBusy(true, 'confirm');
      setStatus('正在写入不可变策划版本…');
      try {
        state.plan.title = title;
        state.plan.pageCount = state.plan.pages.length;
        state.plan.pages.forEach((page, index) => { page.index = index + 1; });
        const payload = await api.request('/api/ai/planning/confirm', {
          method: 'POST',
          body: {plan_id: targetPlanId, expected_revision: state.revision, draft: state.plan},
        });
        if (destroyed || epoch !== planEpoch || state.planId !== targetPlanId) return;
        currentCoverStyleEvidence = payload?.coverStyleEvidence || currentCoverStyleEvidence;
        state.phase = 'confirmed';
        state.confirmed = payload;
        const revised = Number(payload?.revision);
        state.revision = Number.isFinite(revised) && revised >= 1 ? revised : state.revision + 1;
        upsertHistoryItem({
          planId: targetPlanId,
          title: state.plan.title,
          mode: state.plan.mode,
          pageCount: state.plan.pages.length,
          status: 'confirmed',
          revision: state.revision,
          createdAt: history.items.find(item => item.planId === targetPlanId)?.createdAt || '',
        });
        renderConfirmed(payload);
        setStatus('策划已确认并锁定，可以提交生成。', 'success');
        emit('guanye:planning-confirmed', {planId: targetPlanId, revision: state.revision, status: 'confirmed'});
        if (typeof opts.onConfirmed === 'function') opts.onConfirmed(payload, state);
      } catch (error) {
        if (!destroyed && epoch === planEpoch) setStatus(error.message || '确认失败', 'error');
      } finally {
        if (busyAction === 'confirm') setBusy(false);
      }
    }

    function renderConfirmed(payload) {
      result.hidden = false;
      result.replaceChildren();
      const card = element('div', 'gp-card gp-confirmed');
      const banner = element('div', 'gp-banner');
      banner.append(element('strong', '', '已确认 · 不可变版本'), document.createTextNode(`版本 ${payload.version ?? '—'} · 已锁定逐页文案、页序、风格和参考作品`));
      card.append(banner);
      renderCoverEvidence(card, currentCoverStyleEvidence);
      const versionField = field('版本 ID', payload.versionId || '', () => {}, false);
      versionField.querySelector('input').readOnly = true;
      card.append(versionField);
      const hash = field('内容哈希', payload.contentHash || '', () => {}, false);
      hash.querySelector('input').readOnly = true;
      hash.classList.add('gp-mono');
      card.append(hash);
      const styleImageCount = (state.plan?.generationStyleImages || []).length;
      card.append(element('p', 'gp-meta', styleImageCount ? `已锁定生成风格图：${styleImageCount} 张私有路径；若下游不可用，应回退到已核实 refs.imagePaths。` : '未生成直接可用于生图的风格图；下游应回退到已核实 refs.imagePaths。'));
      card.append(element('p', 'gp-meta', '生成队列读取使用 service-role RPC，客户端不能修改此版本。'));
      const actions = element('div', 'gp-actions');
      const cover = window.Cloud?.coverTrainingEnabled === true && state.plan?.pages?.length === 1
        && state.plan.pages[0].narrativeRole === 'cover';
      const refs = (payload.payload || state.plan)?.references || [];
      const choices = refs.flatMap(ref => (ref.imagePaths || []).slice(0, 1).map(path => ({ref, path})));
      const select = element('select');
      const submit = element('button', 'primary', cover ? '提交封面训练首稿' : '提交生成');
      if (cover) {
        const label = element('label', 'gp-meta', '选择一张已确认的高分原作封面参考');
        const empty = element('option', '', choices.length ? '请选择' : '没有可用原作参考，请重新策划');
        empty.value = '';
        select.append(empty);
        choices.forEach(({ref}, i) => {
          const option = element('option', '', ref.title || ref.deckId);
          option.value = String(i);
          select.append(option);
        });
        label.append(select);
        actions.append(label);
      }
      submit.type = 'button';
      submit.dataset.gpAction = 'generate';
      if (cover) submit.dataset.gpLabel = '提交封面训练首稿';
      if (hostedTraining && !cover) {
        card.append(element('p', 'gp-meta', '试点只支持单页封面，请新建一份封面策划。'));
        result.append(card);
        return;
      }
      submit.addEventListener('click', () => {
        if (!cover) return submitGeneration();
        const selected = select.value === '' ? null : choices[Number(select.value)];
        if (!selected) return setStatus('请先选择一张确认的原作参考。', 'error');
        return submitGeneration(selected);
      });
      actions.append(submit);
      card.append(actions);
      result.append(card);
    }

    async function submitGeneration(reference) {
      if (busy || !state.planId || state.phase !== 'confirmed') return;
      const targetPlanId = state.planId;
      const epoch = planEpoch;
      setBusy(true, 'generate');
      setStatus('正在提交生成队列…');
      try {
        const endpoint = reference ? '/api/ai/generation/cover/start' : '/api/ai/generation';
        const body = {plan_id: targetPlanId};
        if (reference) Object.assign(body, {reference_deck_id: reference.ref.deckId, reference_path: reference.path});
        const payload = await api.request(endpoint, {method: 'POST', body});
        if (destroyed || epoch !== planEpoch || state.planId !== targetPlanId) return;
        const jobId = payload?.jobId || payload?.job_id || payload?.id;
        if (jobId) {
          emit('guanye:generation-created', {jobId, planId: targetPlanId});
        }
        setStatus(`已提交生成队列${jobId ? `：${jobId}` : ''}`, 'success');
        if (typeof opts.onGenerationSubmitted === 'function') opts.onGenerationSubmitted(payload, state);
      } catch (error) {
        if (!destroyed && epoch === planEpoch) {
          const suffix = error.status === 404 ? '（生成队列入口尚未接入，请稍后重试）' : '';
          setStatus(`${error.message || '提交生成失败'}${suffix}`, 'error');
        }
      } finally {
        if (busyAction === 'generate') setBusy(false);
      }
    }

    const controller = {
      getState() { return JSON.parse(JSON.stringify(state)); },
      getHistory() { return history.items.map(item => ({...item})); },
      async refreshHistory() { return loadHistory({reset: true}); },
      async loadPlan(planId) {
        const requested = String(planId || '').trim();
        if (!requested) throw new Error('策划 ID 不能为空');
        if (destroyed) return null;
        const epoch = ++planEpoch;
        setOpening(requested);
        setStatus('正在打开策划…');
        try {
          const payload = await api.request(`/api/ai/planning?plan_id=${encodeURIComponent(requested)}`, {method: 'GET'});
          if (destroyed || epoch !== planEpoch) return null;
          const planIdValue = String(payload?.planId || requested);
          currentCoverStyleEvidence = payload?.coverStyleEvidence || null;
          if (payload?.status === 'confirmed' && payload?.confirmed?.payload) {
            const plan = payload.draft || payload.confirmed.payload;
            state = {phase: 'confirmed', planId: planIdValue, revision: Number(payload.revision) || 0, plan, confirmed: payload.confirmed};
            selectedPlanId = planIdValue;
            upsertHistoryItem({
              planId: planIdValue,
              title: plan?.title || payload.confirmed.payload.title,
              mode: plan?.mode || payload.confirmed.payload.mode,
              pageCount: Array.isArray(plan?.pages) ? plan.pages.length : plan?.pageCount,
              status: 'confirmed',
              revision: state.revision,
              createdAt: payload.createdAt || '',
            });
            renderConfirmed(payload.confirmed);
            setStatus('已打开已确认方案，可继续提交生成。', 'success');
            emit('guanye:planning-opened', {planId: planIdValue, status: 'confirmed', revision: state.revision});
          } else if (payload?.draft) {
            state = {phase: 'draft', planId: planIdValue, revision: Number(payload.revision) || 0, plan: payload.draft, confirmed: null};
            selectedPlanId = planIdValue;
            upsertHistoryItem({
              planId: planIdValue,
              title: payload.draft.title,
              mode: payload.draft.mode,
              pageCount: Array.isArray(payload.draft.pages) ? payload.draft.pages.length : payload.draft.pageCount,
              status: 'draft',
              revision: state.revision,
              createdAt: payload.createdAt || '',
            });
            renderPlan(payload);
            setStatus('已打开草稿，可继续编辑、保存或确认。', 'success');
            emit('guanye:planning-opened', {planId: planIdValue, status: 'draft', revision: state.revision});
          } else {
            throw new Error('策划读取返回格式无效');
          }
          return payload;
        } catch (error) {
          if (!destroyed && epoch === planEpoch) setStatus(error?.message || '打开策划失败', 'error');
          return null;
        } finally {
          if (!destroyed && epoch === planEpoch) setOpening('');
        }
      },
      destroy() {
        if (destroyed) return;
        destroyed = true;
        planEpoch += 1;
        historyEpoch += 1;
        openingPlanId = '';
        busy = false;
        shadow.replaceChildren();
        root.__guanyePlanningController = null;
      },
    };
    root.__guanyePlanningController = controller;
    const autoLoad = () => { if (!destroyed && trainingReady) void loadHistory({reset: true}); };
    if (typeof queueMicrotask === 'function') queueMicrotask(autoLoad);
    else setTimeout(autoLoad, 0);
    return controller;
  }

  window.GuanyePlanning = {mount, version: '1.0.0'};

  function autoMount() {
    document.querySelectorAll('[data-guanye-planning]').forEach(root => {
      if (!root.__guanyePlanningController) {
        try { mount(root); } catch (error) { console.error('GuanyePlanning mount failed', error); }
      }
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', autoMount, {once: true});
  else queueMicrotask(autoMount);
})();
