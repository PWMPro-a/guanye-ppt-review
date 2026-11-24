'use strict';

window.GuanyeAI = (() => {
  const config = window.CLOUD_CONFIG || {};
  let clientPromise = null;

  function client() {
    if (!clientPromise) {
      if (!config.url || !config.anonKey || !window.supabase?.createClient) {
        return Promise.reject(new Error('云端登录尚未配置'));
      }
      clientPromise = Promise.resolve(window.supabase.createClient(config.url, config.anonKey));
    }
    return clientPromise;
  }

  async function authHeaders() {
    const headers = { 'Accept': 'application/json' };
    if (config.workspaceId) headers['X-Workspace-Id'] = config.workspaceId;
    try {
      const supabase = await client();
      const result = await supabase.auth.getSession();
      if (result.error) throw result.error;
      const token = result.data?.session?.access_token;
      if (token) headers['Authorization'] = 'Bearer ' + token;
    } catch (error) {
      if (!config.url) throw error;
    }
    return headers;
  }

  async function request(path, options = {}) {
    if (typeof path !== 'string' || !path.startsWith('/api/')) throw new Error('接口路径无效');
    const method = String(options.method || 'GET').toUpperCase();
    const headers = { ...(await authHeaders()), ...(options.headers || {}) };
    let body = options.body;
    if (body !== undefined && body !== null && !(body instanceof FormData) && typeof body !== 'string') {
      headers['Content-Type'] = 'application/json; charset=utf-8';
      body = JSON.stringify(body);
    }
    const base = String(window.GUANYE_AI_BASE || window.API_BASE_URL || '').replace(/\/+$/, '');
    const response = await fetch(base + path, {
      method,
      headers,
      body: method === 'GET' || method === 'HEAD' ? undefined : body,
      credentials: 'omit',
      cache: 'no-store',
      ...(options.signal ? { signal: options.signal } : {}),
    });
    const raw = await response.text();
    let payload = {};
    if (raw) {
      try { payload = JSON.parse(raw); }
      catch { payload = { error: raw }; }
    }
    if (!response.ok) {
      const detail = payload?.error?.message || payload?.error || payload?.message || ('HTTP ' + response.status);
      const error = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  return { request };
})();

window.GuanyeGeneration = (() => {
  const terminal = new Set(['completed', 'partial', 'failed', 'cancelled']);
  const maxImageBytes = 25 * 1024 * 1024;
  const imageTypes = Object.freeze({
    'image/png': { extension: '.png' },
    'image/jpeg': { extension: '.jpg' },
    'image/webp': { extension: '.webp' },
  });

  function element(tag, props = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (key === 'className') node.className = value;
      else if (key === 'text') node.textContent = value == null ? '' : String(value);
      else if (key === 'style' && value && typeof value === 'object') Object.assign(node.style, value);
      else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2).toLowerCase(), value);
      else if (value !== null && value !== undefined) node.setAttribute(key, String(value));
    }
    for (const child of Array.isArray(children) ? children : [children]) {
      if (child == null) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function unwrap(payload) {
    if (payload && typeof payload === 'object' && payload.job && typeof payload.job === 'object') return payload.job;
    return payload && typeof payload === 'object' ? payload : {};
  }

  function artifactPayload(payload) {
    return payload && typeof payload === 'object' && payload.data && typeof payload.data === 'object' && !payload.dataBase64
      ? payload.data
      : payload;
  }

  function normalizeImageType(value) {
    const type = String(value || '').split(';', 1)[0].trim().toLowerCase();
    if (type === 'image/jpg') return 'image/jpeg';
    return imageTypes[type] ? type : '';
  }

  function detectImageType(bytes) {
    if (bytes.length >= 8 && bytes[0] === 0x89 && bytes[1] === 0x50 && bytes[2] === 0x4e && bytes[3] === 0x47
      && bytes[4] === 0x0d && bytes[5] === 0x0a && bytes[6] === 0x1a && bytes[7] === 0x0a) return 'image/png';
    if (bytes.length >= 3 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) return 'image/jpeg';
    if (bytes.length >= 12 && bytes[0] === 0x52 && bytes[1] === 0x49 && bytes[2] === 0x46 && bytes[3] === 0x46
      && bytes[8] === 0x57 && bytes[9] === 0x45 && bytes[10] === 0x42 && bytes[11] === 0x50) return 'image/webp';
    return '';
  }

  function decodeBase64(value) {
    let base64 = String(value || '').trim();
    if (/^data:/i.test(base64)) {
      const comma = base64.indexOf(',');
      if (comma < 0 || !/;base64(?:;|$)/i.test(base64.slice(0, comma))) throw new Error('图片数据格式无效');
      base64 = base64.slice(comma + 1);
    }
    base64 = base64.replace(/\s+/g, '');
    if (!base64 || base64.length % 4 === 1 || !/^[A-Za-z0-9+/]+={0,2}$/.test(base64)) {
      throw new Error('图片数据格式无效');
    }
    try {
      const binary = window.atob(base64);
      const bytes = new Uint8Array(binary.length);
      for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
      return { bytes, base64 };
    } catch {
      throw new Error('图片数据格式无效');
    }
  }

  function decodeImagePayload(payload) {
    const value = artifactPayload(payload);
    if (!value || typeof value !== 'object') throw new Error('图片数据格式无效');
    const claimed = normalizeImageType(value.contentType);
    if (value.contentType && !claimed) throw new Error('只支持 PNG、JPEG 或 WebP 图片');
    const decoded = decodeBase64(value.dataBase64);
    if (!decoded.bytes.length || decoded.bytes.length > maxImageBytes) throw new Error('图片大小无效');
    const detected = detectImageType(decoded.bytes);
    if (!detected) throw new Error('只支持 PNG、JPEG 或 WebP 图片');
    if (claimed && claimed !== detected) throw new Error('图片类型与内容不一致');
    return {
      bytes: decoded.bytes,
      base64: decoded.base64,
      type: detected,
      filename: value.filename,
    };
  }

  function filenameFor(row, type, requested) {
    let name = String(requested || '').trim().replace(/^.*[\\/]/, '');
    name = name.replace(/[\u0000-\u001f<>:"/\\|?*]/g, '-').replace(/\.+$/, '').trim();
    const extension = imageTypes[type]?.extension || '.png';
    if (!name) {
      const position = Number(row?.position);
      name = Number.isFinite(position) && position > 0
        ? 'page-' + String(position).padStart(4, '0')
        : 'generated-page';
    }
    const match = name.match(/\.(png|jpe?g|webp)$/i);
    if (match) {
      const found = match[0].toLowerCase();
      const compatible = type === 'image/jpeg'
        ? found === '.jpg' || found === '.jpeg'
        : found === extension;
      if (compatible) return name;
      name = name.slice(0, -match[0].length).trim();
    }
    return (name || 'generated-page') + extension;
  }

  function triggerDownload(href, filename) {
    const link = document.createElement('a');
    link.href = href;
    link.download = filename;
    link.rel = 'noopener';
    link.style.display = 'none';
    document.body.append(link);
    link.click();
    link.remove();
  }

  function triggerBytesDownload(bytes, type, filename) {
    if (typeof Blob === 'function' && typeof URL !== 'undefined' && typeof URL.createObjectURL === 'function') {
      const url = URL.createObjectURL(new Blob([bytes], { type }));
      triggerDownload(url, filename);
      window.setTimeout(() => URL.revokeObjectURL(url), 5000);
      return;
    }
    let binary = '';
    const chunk = 0x8000;
    for (let index = 0; index < bytes.length; index += chunk) {
      binary += String.fromCharCode(...bytes.subarray(index, index + chunk));
    }
    triggerDownload('data:' + type + ';base64,' + window.btoa(binary), filename);
  }

  function isDisplayUrl(value) {
    const text = String(value || '').trim();
    if (!text) return false;
    return /^data:image\/(?:png|jpe?g|webp)(?:;[^,]*)?;base64,/i.test(text)
      || /^blob:/i.test(text)
      || /^https?:\/\//i.test(text)
      || /^\/(?!\/)/.test(text)
      || /^\.{1,2}\//.test(text);
  }

  function safePreviewUrl(value) {
    const text = String(value || '').trim();
    return isDisplayUrl(text) || /^[A-Za-z0-9][A-Za-z0-9._~!$&'()*+,;=:@%/-]*$/.test(text);
  }

  function imageSource(row) {
    const image = row?.image && typeof row.image === 'object' ? row.image : {};
    const previewPath = String(row?.previewPath || image.previewPath || '');
    const rawPath = String(image.path || row?.storagePath || row?.path || '');
    return {
      previewUrl: String(row?.previewUrl || image.previewUrl || ''),
      path: rawPath || (previewPath && !isApiPath(previewPath) ? previewPath : ''),
      artifactPath: String(row?.artifactPath || image.artifactPath || row?.downloadPath || image.downloadPath
        || (isApiPath(previewPath) ? previewPath : '') || (isApiPath(rawPath) ? rawPath : '')),
      downloadUrl: String(row?.downloadUrl || image.downloadUrl || ''),
    };
  }

  function isApiPath(value) {
    return typeof value === 'string' && value.startsWith('/api/');
  }

  function pagePosition(row) {
    const value = row?.position ?? row?.pageNumber ?? row?.page_number;
    const number = Number(value);
    return Number.isFinite(number) ? number : Number.MAX_SAFE_INTEGER;
  }

  function statusText(status) {
    return ({
      queued: '排队中', running: '生成中', completed: '已完成',
      partial: '部分失败', failed: '生成失败', cancelled: '已取消',
      succeeded: '已完成', queued_page: '等待生成',
    })[status] || status || '等待中';
  }

  function reviewDeckId(job) {
    if (!job || String(job.status || '') !== 'completed') return '';
    const deckId = String(job.deckId || job.deck_id || '').trim();
    return /^[A-Za-z0-9_-]{1,100}$/.test(deckId) ? deckId : '';
  }

  function mount(root, options = {}) {
    if (!(root instanceof Element)) throw new Error('mount root 必须是 DOM 元素');
    const opts = { ...options };
    const planId = opts.planId || opts.plan_id || '';
    let jobId = opts.jobId || opts.job_id || '';
    let job = opts.job && typeof opts.job === 'object' ? opts.job : null;
    let timer = 0;
    let destroyed = false;
    let busy = false;
    let refreshInFlight = false;
    let stateEpoch = 0;
    let activeRefresh = null;
    const pageEntries = new Map();
    const imageUrls = new Map();
    const downloadControllers = new Set();
    const pendingPreviews = new Map();
    const previewObserver = typeof window.IntersectionObserver === 'function'
      ? new window.IntersectionObserver(entries => {
        for (const { target, isIntersecting } of entries) {
          if (!isIntersecting) continue;
          const entry = pendingPreviews.get(target);
          if (!entry) continue;
          pendingPreviews.delete(target);
          previewObserver.unobserve(target);
          loadPagePreview(entry);
        }
      }, { rootMargin: '160px 0px' })
      : null;

    const style = element('style', { text: `
      .gg-shell{box-sizing:border-box;width:100%;max-width:100%;font:14px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;color:#25313a;background:#fff;border:1px solid #dce3e7;border-radius:14px;padding:18px;overflow-wrap:anywhere}
      .gg-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:14px;flex-wrap:wrap}
      .gg-head>div{min-width:0;flex:1 1 190px}
      .gg-head-actions{display:flex;align-items:center;justify-content:flex-end;gap:8px;flex-wrap:wrap;flex:0 1 auto;min-width:0}
      .gg-title{font-size:18px;font-weight:700;margin:0;overflow-wrap:anywhere}.gg-sub{color:#6a7780;font-size:12px;margin-top:4px;overflow-wrap:anywhere}
      .gg-pill{display:inline-flex;align-items:center;border-radius:999px;padding:4px 10px;background:#eef5f7;color:#2f6574;font-size:12px;white-space:normal;overflow-wrap:anywhere;min-width:0;max-width:100%}
      .gg-cover-status{margin:0 0 14px;padding:12px 14px;border-left:3px solid #37735e;border-radius:8px;background:#eff6f2;color:#284b3d;min-width:0}
      .gg-cover-status strong{display:block;margin-bottom:2px;font-size:15px}.gg-cover-status p{margin:0;font-size:13px}
      .gg-cover-layout{display:grid;gap:14px;min-width:0}
      .gg-cover-layout:not(.is-blind) .gg-cover-card{display:grid;grid-template-columns:minmax(0,1.65fr) minmax(235px,1fr)}
      .gg-cover-layout.is-blind{grid-template-columns:repeat(auto-fit,minmax(min(250px,100%),1fr));align-items:start}
      .gg-cover-card .gg-preview{align-self:start}.gg-cover-card:not(.gg-ready) .gg-preview{aspect-ratio:auto;min-height:115px}
      .gg-cover-info{padding:14px;min-width:0;display:grid;align-content:start;gap:12px}.gg-cover-info h4,.gg-cover-info p{margin:0}
      .gg-cover-info .gg-actions,.gg-cover-controls{padding:0}.gg-cover-controls{margin-bottom:12px}.gg-cover-info .gg-meta{padding:0;font-size:13px}
      .gg-cover-info details ul{margin:8px 0 0;padding-inline-start:18px}.gg-cover-info .gg-actions{align-items:start}
      .gg-cover-selection,.gg-cover-editor{display:grid;gap:8px;min-width:0;width:100%}.gg-cover-selection label,.gg-cover-editor label{font-size:13px}
      .gg-cover-selection textarea,.gg-cover-editor textarea{display:block;width:100%;min-width:0;resize:vertical;margin-top:4px;font:inherit}
      .gg-cover-editor{margin-top:14px;padding:14px;border:1px solid #cfded5;border-radius:10px;background:#f5f9f6}.gg-cover-editor .gg-actions{padding:0}
      .gg-cover-card .gg-button,.gg-cover-controls .gg-button,.gg-cover-editor .gg-button{min-height:40px}
      @media(max-width:760px){.gg-cover-layout:not(.is-blind) .gg-cover-card{grid-template-columns:minmax(0,1fr)}}
      .gg-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px;min-width:0}
      .gg-card{min-width:0;max-width:100%;border:1px solid #e4e9ec;border-radius:13px;overflow:hidden;background:#fff;transition:border-color .18s,box-shadow .18s}
      .gg-card.is-selected{border-color:#427761;box-shadow:0 0 0 3px #e3efe8}
      .gg-preview{aspect-ratio:3/2;background:#111b18;display:flex;align-items:center;justify-content:center;color:#89949b;overflow:hidden;min-width:0;text-align:center}
      .gg-preview img{display:block;width:100%;height:100%;object-fit:contain;background:#111b18}
      .gg-meta{padding:10px 12px;display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;min-width:0}
      .gg-name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;flex:1 1 120px}.gg-state{font-size:12px;color:#6d7b83;flex:0 1 auto;max-width:100%;overflow-wrap:anywhere}
      .gg-error{color:#a53a35;font-size:12px;padding:0 12px 10px;overflow-wrap:anywhere;word-break:break-word}
      .gg-actions{padding:0 12px 12px;display:flex;flex-wrap:wrap;gap:8px;min-width:0}
      .gg-button{border:1px solid #cbd7dc;background:#fff;border-radius:8px;padding:7px 11px;cursor:pointer;color:#2f5967;max-width:100%;white-space:normal}
      .gg-button.gg-review,.gg-button.gg-primary{background:#173d35;border-color:#173d35;color:#fff;font-weight:600}
      .gg-button:hover:not(:disabled){border-color:#6e9587;background:#f2f7f4}.gg-button.gg-primary:hover:not(:disabled),.gg-button.gg-review:hover:not(:disabled){background:#245c49}
      .gg-button:disabled{opacity:.55;cursor:default}.gg-empty{padding:28px;text-align:center;color:#7b878e}
      .gg-note{margin-top:12px;color:#6d7b83;font-size:12px;overflow-wrap:anywhere}.gg-bad{color:#a53a35}
      @media(max-width:420px){
        .gg-shell{padding:14px;border-radius:12px}
        .gg-head{gap:8px}
        .gg-grid{grid-template-columns:minmax(0,1fr)}
        .gg-name{flex-basis:100%;white-space:normal}
      }
    ` });

    const head = element('div', { className: 'gg-head' });
    const title = element('h2', { className: 'gg-title', text: opts.title || 'AI 生成作品' });
    const sub = element('div', { className: 'gg-sub', text: planId ? '计划 ' + planId : '' });
    const status = element('span', { className: 'gg-pill', text: '准备中' });
    const reviewButton = element('button', { className: 'gg-button gg-review', type: 'button', text: '查看生成评审', hidden: true, 'data-action': 'open-generation-review' });
    const headActions = element('div', { className: 'gg-head-actions' }, [status, reviewButton]);
    head.append(element('div', {}, [title, sub]), headActions);
    const grid = element('div', { className: 'gg-grid' });
    const note = element('div', { className: 'gg-note', text: '页面按原顺序展示；失败只允许单页重试。' });
    const coverPanel = element('section', {hidden: true, 'aria-label': '封面训练'});
    const shell = element('section', { className: 'gg-shell', 'aria-live': 'polite' }, [head, grid, note, coverPanel]);
    root.replaceChildren(style, shell);

    function setStatus(text, bad = false) {
      status.textContent = text;
      status.className = 'gg-pill' + (bad ? ' gg-bad' : '');
    }

    function showMessage(text, bad = false) {
      note.textContent = text;
      note.className = 'gg-note' + (bad ? ' gg-bad' : '');
    }

    reviewButton.addEventListener('click', () => {
      if (destroyed || !reviewDeckId(job)) return;
      if (typeof opts.onOpenReview !== 'function') return;
      try { opts.onOpenReview({ jobId: String(job?.id || jobId || '') }); } catch {}
    });

    function invalidateInFlight() {
      stateEpoch += 1;
      if (!activeRefresh) return;
      const active = activeRefresh;
      activeRefresh = null;
      refreshInFlight = false;
      try { active.controller.abort(); } catch {}
    }

    function pageRenderSignature(row) {
      const image = row?.image && typeof row.image === 'object' ? row.image : {};
      const source = imageSource(row);
      return JSON.stringify([
        row?.status ?? '',
        row?.title ?? '',
        pagePosition(row),
        row?.position ?? null,
        row?.pageNumber ?? null,
        row?.page_number ?? null,
        row?.error ?? '',
        source.previewUrl,
        source.path,
        source.artifactPath,
        source.downloadUrl,
        row?.previewPath ?? '',
        row?.artifactPath ?? '',
        row?.downloadPath ?? '',
        row?.downloadUrl ?? '',
        row?.storagePath ?? '',
        row?.path ?? '',
        image.previewPath ?? '',
        image.previewUrl ?? '',
        image.artifactPath ?? '',
        image.downloadPath ?? '',
        image.downloadUrl ?? '',
        image.path ?? '',
        image.name ?? '',
        row?.contentType ?? '',
        row?.filename ?? '',
      ]);
    }

    function pageEntryKey(kind, identity, occurrence = 0) {
      return JSON.stringify([kind, identity, occurrence]);
    }

    function keyedPages(pages) {
      const idCounts = new Map();
      for (const row of pages) {
        const id = String(row?.id ?? '').trim();
        if (id) idCounts.set(id, (idCounts.get(id) || 0) + 1);
      }
      const idOccurrences = new Map();
      const missingOccurrences = new Map();
      return pages.map(row => {
        const id = String(row?.id ?? '').trim();
        let key;
        if (id) {
          const occurrence = idOccurrences.get(id) || 0;
          idOccurrences.set(id, occurrence + 1);
          key = pageEntryKey(idCounts.get(id) > 1 ? 'duplicate-id' : 'id', id, occurrence);
        } else {
          const position = pagePosition(row);
          const occurrence = missingOccurrences.get(position) || 0;
          missingOccurrences.set(position, occurrence + 1);
          key = pageEntryKey('missing-id', position, occurrence);
        }
        return { key, row, signature: pageRenderSignature(row) };
      });
    }

    function currentPreview(entry) {
      return !destroyed
        && entry.active
        && pageEntries.get(entry.key) === entry
        && entry.card.isConnected
        && entry.preview.parentElement === entry.card;
    }

    function disposePageEntry(entry) {
      if (!entry || !entry.active) return;
      entry.active = false;
      pendingPreviews.delete(entry.card);
      previewObserver?.unobserve(entry.card);
      for (const controller of entry.downloads) {
        try { controller.abort(); } catch {}
      }
      entry.downloads.clear();
      if (pageEntries.get(entry.key) === entry) pageEntries.delete(entry.key);
    }

    function clearPreviewObservations() {
      for (const entry of [...pageEntries.values()]) disposePageEntry(entry);
      pageEntries.clear();
      previewObserver?.disconnect();
      pendingPreviews.clear();
    }

    function createPageEntry(key, row, signature) {
      const preview = element('div', { className: 'gg-preview', text: '等待图片' });
      const state = element('span', { className: 'gg-state', text: statusText(row.status) });
      const meta = element('div', { className: 'gg-meta' }, [
        element('span', { className: 'gg-name', text: row.title || ('第 ' + row.position + ' 页') }),
        state,
      ]);
      const children = [preview, meta];
      if (row.error) children.push(element('div', { className: 'gg-error', text: row.error }));
      const entry = {
        key,
        row,
        signature,
        preview,
        card: null,
        active: true,
        loadStarted: false,
        previewFailed: false,
        downloads: new Set(),
      };
      if (row.status === 'failed') {
        const button = element('button', { className: 'gg-button', type: 'button', text: '仅重试本页' });
        button.addEventListener('click', () => retryPage(entry.row?.id, button));
        children.push(element('div', { className: 'gg-actions' }, [button]));
      } else if (row.status === 'succeeded') {
        const button = element('button', { className: 'gg-button', type: 'button', text: '下载图片' });
        button.addEventListener('click', () => downloadPage(entry.row, button, entry));
        children.push(element('div', { className: 'gg-actions' }, [button]));
      }
      entry.card = element('article', { className: 'gg-card', 'data-page-key': key }, children);
      return entry;
    }

    function preparePagePreview(entry) {
      if (!entry.active || entry.loadStarted || entry.previewFailed) return;
      const row = entry.row;
      if (row.status !== 'succeeded') {
        entry.preview.textContent = statusText(row.status);
        entry.loadStarted = true;
        return;
      }
      const source = imageSource(row);
      if (!source.previewUrl && !source.path && !source.artifactPath) {
        entry.preview.textContent = '图片暂不可用';
        entry.loadStarted = true;
        return;
      }
      if (previewObserver) {
        pendingPreviews.set(entry.card, entry);
        previewObserver.observe(entry.card);
      } else {
        loadPagePreview(entry);
      }
    }

    function loadPagePreview(entry) {
      if (!entry.active || entry.loadStarted || !currentPreview(entry)) return;
      entry.loadStarted = true;
      pendingPreviews.delete(entry.card);
      previewObserver?.unobserve(entry.card);
      entry.preview.textContent = '加载图片…';
      resolveImage(entry.row).then(url => {
        if (!url) {
          if (currentPreview(entry)) {
            entry.previewFailed = true;
            invalidateImageCache(entry.row, url);
            entry.preview.textContent = '图片暂不可用';
          }
          return;
        }
        if (!currentPreview(entry)) return;
        const image = element('img', { alt: entry.row.title || ('第 ' + entry.row.position + ' 页'), loading: 'lazy' });
        image.addEventListener('error', () => {
          if (!currentPreview(entry)) return;
          entry.previewFailed = true;
          invalidateImageCache(entry.row, url);
          entry.preview.textContent = '图片暂不可用';
        }, { once: true });
        image.src = url;
        if (!currentPreview(entry)) return;
        entry.preview.replaceChildren(image);
      }).catch(() => {
        if (!currentPreview(entry)) return;
        entry.previewFailed = true;
        entry.preview.textContent = '图片暂不可用';
      });
    }

    function imageCacheKey(row) {
      const source = imageSource(row);
      return [row?.id || '', source.previewUrl, source.path, source.artifactPath].join('|');
    }

    function invalidateImageCache(row, url = '') {
      const key = imageCacheKey(row);
      const entry = imageUrls.get(key);
      if (entry && (!url || entry.url === url)) imageUrls.delete(key);
    }

    async function resolveImage(row) {
      const source = imageSource(row);
      const key = imageCacheKey(row);
      const cached = imageUrls.get(key);
      if (cached) return cached.promise;
      const directPreview = source.previewUrl && !isApiPath(source.previewUrl) && isDisplayUrl(source.previewUrl)
        ? source.previewUrl
        : '';
      const directPath = source.previewUrl && !isApiPath(source.previewUrl) && safePreviewUrl(source.previewUrl)
        ? source.previewUrl
        : '';
      let resolverAttempted = false;
      const entry = { promise: null, url: '' };
      const promise = (async () => {
        if (directPreview) return directPreview;
        if (source.previewUrl && isApiPath(source.previewUrl)) {
          try {
            const artifact = decodeImagePayload(await window.GuanyeAI.request(source.previewUrl));
            return 'data:' + artifact.type + ';base64,' + artifact.base64;
          } catch {}
        }
        if (source.path) {
          if (typeof opts.resolveImage === 'function') {
            resolverAttempted = true;
            try {
              const url = await opts.resolveImage(source.path, row);
              if (url && safePreviewUrl(url)) return url;
            } catch {}
          } else if (window.Cloud?.imageUrl) {
            resolverAttempted = true;
            try {
              const url = await window.Cloud.imageUrl(source.path);
              if (url && safePreviewUrl(url)) return url;
            } catch {}
          }
        }
        if (source.artifactPath && isApiPath(source.artifactPath)) {
          try {
            const artifact = decodeImagePayload(await window.GuanyeAI.request(source.artifactPath));
            return 'data:' + artifact.type + ';base64,' + artifact.base64;
          } catch {}
        }
        if (source.path && (!resolverAttempted || isDisplayUrl(source.path))) return source.path;
        if (directPath) return directPath;
        throw new Error('图片暂不可用');
      })();
      entry.promise = promise;
      imageUrls.set(key, entry);
      promise.then(url => { entry.url = url; }, () => {
        if (imageUrls.get(key) === entry) imageUrls.delete(key);
      });
      return promise;
    }

    function ensureDownloadActive(signal) {
      if (destroyed || signal?.aborted) {
        const error = new Error('下载已取消');
        error.name = 'AbortError';
        throw error;
      }
    }

    async function downloadFromUrl(url, row, signal) {
      ensureDownloadActive(signal);
      const value = String(url || '').trim();
      if (!value) throw new Error('没有可下载的图片');
      if (/^data:/i.test(value)) {
        const comma = value.indexOf(',');
        const contentType = comma >= 0 ? value.slice(5, comma).split(';', 1)[0] : '';
        const decoded = decodeImagePayload({ dataBase64: value, contentType });
        ensureDownloadActive(signal);
        triggerBytesDownload(decoded.bytes, decoded.type, filenameFor(row, decoded.type, ''));
        return;
      }
      let absolute;
      try { absolute = new URL(value, document.baseURI); }
      catch { throw new Error('图片下载地址无效'); }
      if (!['blob:', 'http:', 'https:'].includes(absolute.protocol)) throw new Error('图片下载地址无效');
      ensureDownloadActive(signal);
      let response;
      try {
        response = await window.fetch(absolute.href, {
          credentials: 'same-origin', cache: 'no-store', mode: 'cors', signal,
        });
      } catch (error) {
        if (signal?.aborted) throw error;
        throw new Error('图片下载失败');
      }
      ensureDownloadActive(signal);
      if (!response.ok) throw new Error('图片下载失败');
      const declared = normalizeImageType(response.headers.get('content-type'));
      const bytes = new Uint8Array(await response.arrayBuffer());
      ensureDownloadActive(signal);
      if (!bytes.length || bytes.length > maxImageBytes) throw new Error('图片大小无效');
      const detected = detectImageType(bytes);
      if (!detected || (declared && declared !== detected)) throw new Error('只支持 PNG、JPEG 或 WebP 图片');
      ensureDownloadActive(signal);
      triggerBytesDownload(bytes, detected, filenameFor(row, detected, ''));
    }

    async function downloadPage(row, button, entry = null) {
      if (destroyed) return;
      const downloadPath = String(row?.downloadPath || row?.image?.downloadPath || '');
      const downloadUrl = String(row?.downloadUrl || row?.image?.downloadUrl || '');
      const controller = new AbortController();
      downloadControllers.add(controller);
      if (entry) entry.downloads.add(controller);
      const label = button.textContent;
      button.disabled = true;
      button.textContent = '准备下载…';
      try {
        if (downloadPath) {
          if (!isApiPath(downloadPath)) throw new Error('下载地址无效');
          const payload = await window.GuanyeAI.request(downloadPath, { signal: controller.signal });
          ensureDownloadActive(controller.signal);
          const artifact = decodeImagePayload(payload);
          ensureDownloadActive(controller.signal);
          triggerBytesDownload(artifact.bytes, artifact.type, filenameFor(row, artifact.type, artifact.filename));
        } else if (downloadUrl) {
          await downloadFromUrl(downloadUrl, row, controller.signal);
        } else {
          await downloadFromUrl(await resolveImage(row), row, controller.signal);
        }
      } catch (error) {
        if (!destroyed && !controller.signal.aborted) showMessage(error.message || '下载图片失败', true);
      } finally {
        downloadControllers.delete(controller);
        if (entry) entry.downloads.delete(controller);
        if (!destroyed && button.isConnected) {
          button.disabled = false;
          button.textContent = label;
        }
      }
    }

    function renderPages() {
      const pages = Array.isArray(job?.pages) ? job.pages.slice().sort((a, b) => pagePosition(a) - pagePosition(b) || String(a?.id || '').localeCompare(String(b?.id || ''))) : [];
      const desired = keyedPages(pages);
      if (!desired.length) {
        clearPreviewObservations();
        grid.replaceChildren(element('div', { className: 'gg-empty', text: '还没有生成页面。' }));
        return;
      }
      const empty = Array.from(grid.children).find(child => child.classList.contains('gg-empty'));
      empty?.remove();
      const desiredKeys = new Set(desired.map(item => item.key));
      for (const entry of [...pageEntries.values()]) {
        if (desiredKeys.has(entry.key)) continue;
        disposePageEntry(entry);
        entry.card.remove();
      }
      const created = [];
      const cards = desired.map(item => {
        let entry = pageEntries.get(item.key);
        if (entry && (entry.signature !== item.signature || entry.previewFailed || entry.card.parentElement !== grid)) {
          disposePageEntry(entry);
          entry.card.remove();
          entry = null;
        }
        if (!entry) {
          entry = createPageEntry(item.key, item.row, item.signature);
          pageEntries.set(entry.key, entry);
          created.push(entry);
        } else {
          entry.row = item.row;
        }
        return entry.card;
      });
      cards.forEach((card, index) => {
        if (grid.children[index] !== card) grid.insertBefore(card, grid.children[index] || null);
      });
      for (const entry of created) preparePagePreview(entry);
    }

    // Read-only polling. The model and generation endpoints are only called by explicit buttons.
    const coverEndpoint = '/api/ai/generation/cover';
    let coverTimer = 0, coverState = null, coverSignature = '', coverBusy = false;
    let proposed = null, failure = '', blind = false, coverEpoch = 0;
    const coverLabels = ['keep', 'change', 'doNotChange'];
    const coverStatus = element('div', {className:'gg-cover-status', role:'status', tabindex:'-1'});
    const coverBody = element('div');
    coverPanel.append(coverStatus, coverBody);
    const choiceDrafts = new Map();
    const waitsForSol = feedback => !feedback?.recordId && ['queued','running','retrying'].includes(feedback?.reviewStatus);
    let coverRefreshing = false, readFailure = '';
    let humanSavePending = false;
    const blindOrder = new Map();

    async function updateCover() {
      if (destroyed || !jobId || coverBusy || coverRefreshing || window.Cloud?.coverTrainingEnabled !== true) return;
      coverRefreshing = true;
      clearTimeout(coverTimer);
      const epoch = ++coverEpoch;
      try {
        const next = unwrap(await window.GuanyeAI.request(coverEndpoint + '?job_id=' + encodeURIComponent(jobId)));
        if (destroyed || coverBusy || epoch !== coverEpoch) return;
        // `unwrap()` can also return the full response on installations with no pilot row.
        if (next?.rootPageId) {
          const signature = JSON.stringify(next);
          coverState = next;
          if (signature !== coverSignature || readFailure || humanSavePending) {
            readFailure = '';
            humanSavePending = false;
            coverSignature = signature;
            renderCover();
          }
        }
      } catch (err) {
        if (coverState && !destroyed && epoch === coverEpoch) {
          readFailure = '暂时无法同步状态。已保存的评分不会丢失，可稍后刷新。';
          renderCover();
        }
      } finally {
        coverRefreshing = false;
        if (!destroyed && !coverBusy && (epoch !== coverEpoch || humanSavePending
            || coverState?.candidates?.some(x => ['queued','running','pending'].includes(x.status) || waitsForSol(x.feedback)))) {
          coverTimer = window.setTimeout(updateCover, 3000);
        }
      }
    }

    async function coverAction(action, body, apply) {
      if (coverBusy || destroyed) return;
      coverBusy = true;
      coverEpoch++;
      failure = '';
      renderCover();
      try {
        const response = unwrap(await window.GuanyeAI.request(coverEndpoint + '/' + action, {method: 'POST', body}));
        if (!destroyed) apply(response);
      } catch (err) {
        if (!destroyed) failure = err.message || '封面训练操作失败';
      } finally {
        coverBusy = false;
        if (!destroyed) { renderCover(); void updateCover(); }
      }
    }

    function coverProgress(candidates, selected, canCompare) {
      const latest = candidates.at(-1) || {};
      const feedback = latest.feedback || {};
      if (blind) return ['盲比候选', '不显示版本顺序和评分。先核对文字、裁切和版式，再填写选用理由。'];
      if (selected?.feedback?.libraryVisible) return ['已收入素材库', '本次训练已完成。选用理由会成为后续封面策划可参考的案例。'];
      if (selected) return ['已选版，等待入库', '进入所选版本的评分页，确认内容和入库条件，再收入素材库。'];
      if (['queued','running','pending'].includes(latest.status)) return ['正在生成封面', '图片就绪后可独立打分。不需要重复提交生图。'];
      if (latest.status === 'failed') return ['本版生成失败', '已有版本和评分仍保留。不自动重发生成请求。'];
      if (humanSavePending) return ['人工评分已保存', '正在同步独立评审状态，无需重复打分。'];
      if (latest.status === 'succeeded' && !feedback.human) return ['下一步：提交人工评分', '先评视觉、排版和内容匹配。提交后才会显示 Sol 的独立评分。'];
      if (waitsForSol(feedback)) {
        const detail = {queued:'Sol 评审已排队，结果会自动更新。', running:'Sol 正在独立评分，结果会自动更新。', retrying:'评审暂时失败，正在等待系统重试；人工分已保留。'};
        return ['人工评分已保存', detail[feedback.reviewStatus]];
      }
      if (canCompare) return ['下一步：对比选版', '候选的人工分均已保存。进入盲比后，核对内容并说明选用理由。'];
      if (feedback.recordId) return ['Sol 已完成独立评分', '查看两边分数；可以修正正式分、获取改稿建议，也可以直接选用这一版。'];
      if (feedback.human && feedback.reviewStatus === 'failed') return ['Sol 评审未完成', '人工分已保留。可刷新查看状态，或使用已确认的人工评分继续。'];
      if (feedback.human) return ['人工评分已保存', '尚未同步到 Sol 结果。可使用「刷新状态」查看。'];
      return ['读取训练状态', '封面就绪后，可以从这里继续。'];
    }

    function renderCoverStatus(heading, detail) {
      if (coverStatus.firstElementChild?.textContent === heading && coverStatus.lastElementChild?.textContent === detail) return;
      coverStatus.replaceChildren(element('strong', {text:heading}), element('p', {text:detail}));
    }

    function showHumanSaved() {
      humanSavePending = true;
      coverEpoch++;
      renderCoverStatus('人工评分已保存', '正在同步独立评审状态，无需重复打分。');
    }

    function renderCover() {
      if (!coverState) return;
      coverPanel.hidden = false;
      reviewButton.hidden = true;
      grid.hidden = true;
      sub.hidden = true;
      note.hidden = true;
      const {candidates, selectedPageId, enabled} = coverState;
      const latest = candidates.at(-1);
      const selected = candidates.find(row => row.pageId === selectedPageId);
      const finished = candidates.filter(row => row.status === 'succeeded');
      const queueMoving = candidates.some(row => ['queued','running','pending'].includes(row.status));
      const multiple = finished.length >= 2;
      const canCompare = multiple && finished.every(row => row.feedback?.decisionId && !waitsForSol(row.feedback));
      if (selected?.feedback?.libraryVisible || !canCompare) blind = false;
      renderCoverStatus(...coverProgress(candidates, selected, canCompare));
      const button = (label, action, primary = false) => {
        const node = element('button', {className:'gg-button' + (primary ? ' gg-primary' : ''), type:'button', text:label});
        node.disabled = coverBusy;
        node.addEventListener('click', action);
        return node;
      };
      const bar = element('div', {className:'gg-actions gg-cover-controls'});
      if (!blind) bar.append(button('刷新状态', updateCover));
      if (canCompare && !queueMoving && enabled && !selected?.feedback?.libraryVisible) {
        bar.prepend(button(blind ? '退出盲比' : '开始盲比', () => {
          blind = !blind; renderCover(); coverStatus.focus();
        }, !blind));
      }
      const gallery = element('div', {className:'gg-cover-layout' + (blind ? ' is-blind' : '')});
      const activeElement = document.activeElement;
      const draftFocus = coverBody.contains(activeElement) ? activeElement?.dataset?.choiceFocus : '';
      const selectionRange = draftFocus && activeElement.tagName === 'TEXTAREA' ? [activeElement.selectionStart, activeElement.selectionEnd] : null;
      const detailOpen = new Set([...coverBody.querySelectorAll('details[open][data-cover-details]')].map(node => node.dataset.coverDetails));
      coverBody.replaceChildren(bar);
      if (failure || readFailure) coverBody.append(element('p', {className:'gg-error', role:'alert', text:failure || readFailure}));
      if (!enabled) coverBody.append(element('p', {text:'训练已关闭，仅可查看历史。'}));
      coverBody.append(gallery);
      const visible = blind ? [...finished] : [...candidates].reverse();
      if (blind) {
        for (const row of visible) if (!blindOrder.has(row.pageId)) blindOrder.set(row.pageId, Math.random());
        visible.sort((a, b) => blindOrder.get(a.pageId) - blindOrder.get(b.pageId));
      } else if (selected) {
        visible.sort((a, b) => Number(b === selected) - Number(a === selected));
      }
      for (const [index, row] of visible.entries()) {
        const label = blind ? `候选 ${String.fromCharCode(65 + index)}` : row.ordinal ? `改稿 ${row.ordinal}` : '首稿';
        const preview = element('div', {className:'gg-preview', text: row.image?.path ? '正在加载私有预览…' : statusText(row.status)});
        const isSelected = !blind && row === selected;
        const feedback = row.feedback || {};
        const badge = isSelected ? (feedback.libraryVisible ? ' · 已入库' : ' · 已选用') : '';
        const info = element('div', {className:'gg-cover-info'}, [element('h4', {text:label + badge})]);
        const card = element('article', {className:'gg-card gg-cover-card' + (isSelected ? ' is-selected' : '') + (row.image?.path ? ' gg-ready' : '')}, [preview, info]);
        if (row.image?.path) {
          const artifactPath = `/api/ai/generation/artifact?job_id=${encodeURIComponent(row.jobId)}&page_id=${encodeURIComponent(row.pageId)}`;
          resolveImage({...row, id: row.pageId, artifactPath}).then(url => {
            if (preview.isConnected) preview.replaceChildren(element('img', {alt: label, src: url}));
          }).catch(() => { if (preview.isConnected) preview.textContent = '私有预览暂不可用'; });
        }
        const actions = element('div', {className:'gg-actions'});
        if (row.status === 'succeeded' && !blind) {
          const summarize = scores => `视觉 ${scores?.visual ?? '—'} · 排版 ${scores?.layout ?? '—'} · 内容 ${scores?.match ?? '—'}`;
          if (feedback.human) {
            info.append(element('p', {className:'gg-meta', text:`首次人评：${summarize(feedback.human)}`}));
            if (feedback.formalScores && summarize(feedback.formalScores) !== summarize(feedback.human)) {
              info.append(element('p', {className:'gg-meta', text:`正式分：${summarize(feedback.formalScores)}`}));
            }
            if (feedback.recordId) {
              info.append(element('p', {className:'gg-meta', text:`Sol 原判：${summarize(feedback.aiScores)}`}));
              const reasons = (Array.isArray(feedback.aiReasons) ? feedback.aiReasons : []).filter(reason => typeof reason === 'string');
              if (reasons.length) {
                const details = element('details', {className:'gg-meta', 'data-cover-details':row.pageId}, [element('summary', {text:'Sol 为什么这样打分？'}),
                  element('ul', {}, reasons.map(reason => element('li', {text:reason}))) ]);
                details.open = detailOpen.has(row.pageId);
                info.append(details);
              }
            }
          }
          if (isSelected && feedback.libraryVisible) {
            actions.append(button('开始下一张', () => opts.onNewCover?.(), true), button('查看素材库', () => opts.onOpenLibrary?.()));
          } else if (enabled && !selected?.feedback?.libraryVisible) {
            const openReview = () => opts.onOpenReview?.({jobId:row.jobId, coverDeckId:row.deckId, coverRootJobId:jobId});
            const label = isSelected ? '核对评分并入库' : !feedback.human ? '独立打分' : feedback.recordId ? '查看或纠正评分' : '查看已保存评分';
            actions.append(button(label, openReview, isSelected || !feedback.human));
            if (!selected && !queueMoving && latest === row && row.ordinal < 2 && feedback.recordId && !proposed) {
              actions.append(button('获取改稿建议', () => coverAction('suggest', {
                job_id:jobId, page_id:row.pageId, record_id:feedback.recordId,
              }, value => { proposed = {page_id:row.pageId, record_id:value.recordId,
                request_key:crypto.randomUUID(), instructions:value.instructions}; }), !canCompare));
            }
          }
        }
        if (enabled && !queueMoving && !selected?.feedback?.libraryVisible && feedback.decisionId && !waitsForSol(feedback)
            && !humanSavePending
            && row.pageId !== selectedPageId && (blind || !multiple)) {
          const draftKey = `${blind ? 'blind' : 'single'}:${row.pageId}`;
          if (!choiceDrafts.has(draftKey)) choiceDrafts.set(draftKey, {reason:'', checked:false});
          const draft = choiceDrafts.get(draftKey);
          const form = element('form', {className:'gg-cover-selection'});
          const reason = element('textarea', {rows:'2', maxlength:'700', required:'required', 'data-choice-focus':draftKey+':reason'});
          reason.value = draft.reason;
          reason.disabled = coverBusy;
          reason.addEventListener('input', () => { draft.reason = reason.value; reason.setCustomValidity(''); });
          const check = element('input', {type:'checkbox', required:'required', 'data-choice-focus':draftKey+':check'});
          check.checked = draft.checked;
          check.disabled = coverBusy;
          check.addEventListener('change', () => { draft.checked = check.checked; });
          const submit = button(blind ? `选用${label}` : '确认选用此版', () => {}, true);
          submit.type = 'submit';
          form.append(element('label', {text:'选用理由（必填）'}, [reason]),
            element('label', {}, [check, document.createTextNode(' 已核对文案，无错漏和裁切')]), submit);
          form.addEventListener('submit', event => {
            event.preventDefault();
            if (!draft.reason.trim()) { reason.setCustomValidity('请写一句选用理由'); reason.reportValidity(); return; }
            if (!draft.checked) { check.reportValidity(); return; }
            void coverAction('select', {page_id:row.pageId, decision_id:feedback.decisionId,
              reason:draft.reason.trim(), content_ok:draft.checked, blind}, value => { coverState = value; blind = false; proposed = null; choiceDrafts.clear(); coverStatus.focus(); });
          });
          actions.append(form);
        }
        if (actions.children.length) info.append(actions);
        gallery.append(card);
      }
      if (enabled && !blind && proposed?.page_id === latest?.pageId) {
        const editor = element('div', {className:'gg-cover-editor'}, [element('strong', {text:'核对改稿指令'}),
          element('p', {text:'确认后只生成一张新候选。文案、参考图与首稿记录保持不变。'})]);
        coverLabels.forEach((key, i) => {
          const label = element('label', {text:['保留什么','改哪里','不得改什么'][i]});
          const field = element('textarea', {maxlength:'700', rows:'2', 'data-choice-focus':'revision:'+key});
          field.value = proposed.instructions[key] || '';
          field.addEventListener('input', () => { proposed.instructions[key] = field.value; });
          field.disabled = coverBusy;
          label.append(field);
          editor.append(label);
        });
        editor.append(element('div', {className:'gg-actions'}, [button('按建议改一版', () => {
          if (!coverLabels.every(key => proposed.instructions[key].trim())) { failure = '请填写全部三项指令'; renderCover(); return; }
          void coverAction('revise', proposed, value => { coverState = value; proposed = null; coverStatus.focus(); });
        }, true), button('放弃建议', () => { proposed = null; renderCover(); })]));
        coverBody.append(editor);
      }
      if (draftFocus && !coverBusy) {
        const target = [...coverBody.querySelectorAll('[data-choice-focus]')].find(node => node.dataset.choiceFocus === draftFocus);
        target?.focus({preventScroll:true});
        if (selectionRange) target?.setSelectionRange(...selectionRange);
      }
    }

    function applyJob(next) {
      if (destroyed || !next || typeof next !== 'object') return;
      const expectedJobId = String(opts.jobId || opts.job_id || '');
      const nextJobId = String(next.id || '');
      if (expectedJobId && nextJobId && nextJobId !== expectedJobId) return;
      job = next;
      jobId = next.id || jobId;
      title.textContent = next.title || opts.title || 'AI 生成作品';
      sub.textContent = next.planId ? '计划 ' + next.planId : sub.textContent;
      const pages = Array.isArray(next.pages) ? next.pages : [];
      const done = pages.filter(row => row.status === 'succeeded').length;
      setStatus(statusText(next.status) + (pages.length ? ' · ' + done + '/' + pages.length : ''), next.status === 'failed');
      const deckId = reviewDeckId(next);
      reviewButton.hidden = !deckId;
      reviewButton.disabled = !deckId;
      if (deckId) {
        reviewButton.dataset.jobId = String(next.id || jobId || '');
        reviewButton.dataset.deckId = deckId;
      } else {
        delete reviewButton.dataset.jobId;
        delete reviewButton.dataset.deckId;
      }
      renderPages();
      if (coverState) reviewButton.hidden = true;
      void updateCover();
      if (typeof opts.onUpdate === 'function') {
        try { opts.onUpdate(next); } catch {}
      }
      if (terminal.has(next.status)) stopPolling();
    }

    function stopPolling() {
      if (timer) clearTimeout(timer);
      timer = 0;
    }

    function schedule() {
      stopPolling();
      if (destroyed || busy || !jobId || terminal.has(job?.status)) return;
      const requestedDelay = Number(opts.pollMs);
      timer = window.setTimeout(refresh, Number.isFinite(requestedDelay) && requestedDelay >= 1000 ? requestedDelay : 3000);
    }

    async function refresh() {
      if (destroyed || busy || refreshInFlight) return;
      stopPolling();
      const controller = new AbortController();
      const handle = { controller, epoch: stateEpoch };
      activeRefresh = handle;
      refreshInFlight = true;
      const isCurrent = () => !destroyed && activeRefresh === handle && stateEpoch === handle.epoch && !controller.signal.aborted;
      try {
        const query = jobId ? '?job_id=' + encodeURIComponent(jobId) : '?plan_id=' + encodeURIComponent(planId);
        const next = unwrap(await window.GuanyeAI.request('/api/ai/generation' + query, { signal: controller.signal }));
        if (!isCurrent()) return;
        applyJob(next);
        if (!terminal.has(job?.status)) schedule();
      } catch (error) {
        if (!isCurrent()) return;
        setStatus('读取失败', true);
        showMessage(error.message || '读取生成状态失败', true);
        schedule();
      } finally {
        if (activeRefresh === handle) {
          activeRefresh = null;
          refreshInFlight = false;
        }
      }
    }

    async function start() {
      if (destroyed || busy) return;
      if (!planId && !jobId) {
        setStatus('缺少计划', true);
        showMessage('请提供 planId。', true);
        return;
      }
      invalidateInFlight();
      stopPolling();
      busy = true;
      try {
        if (!jobId) {
          setStatus('提交中');
          const next = unwrap(await window.GuanyeAI.request('/api/ai/generation', { method: 'POST', body: { plan_id: planId } }));
          applyJob(next);
        }
      } catch (error) {
        if (!destroyed) {
          setStatus('提交失败', true);
          showMessage(error.message || '提交生成计划失败', true);
        }
      } finally {
        busy = false;
        if (!destroyed && jobId && !terminal.has(job?.status)) schedule();
      }
    }

    async function retryPage(pageId, button) {
      if (!pageId || !jobId || busy || destroyed) return;
      invalidateInFlight();
      stopPolling();
      busy = true;
      button.disabled = true;
      try {
        const next = unwrap(await window.GuanyeAI.request('/api/ai/generation/retry', {
          method: 'POST', body: { job_id: jobId, page_ids: [pageId] },
        }));
        if (!destroyed) applyJob(next);
      } catch (error) {
        if (!destroyed) showMessage(error.message || '单页重试失败', true);
      } finally {
        busy = false;
        if (!destroyed && button.isConnected) button.disabled = false;
        if (!destroyed && jobId && !terminal.has(job?.status)) schedule();
      }
    }

    function destroy() {
      destroyed = true;
      clearTimeout(coverTimer);
      clearPreviewObservations();
      invalidateInFlight();
      stopPolling();
      for (const controller of downloadControllers) {
        try { controller.abort(); } catch {}
      }
      downloadControllers.clear();
      root.replaceChildren();
    }

    if (job) applyJob(job);
    if (jobId && !terminal.has(job?.status)) schedule();
    else if (!jobId && opts.autoStart !== false) Promise.resolve().then(start);

    return { start, refresh, refreshCover:updateCover, showHumanSaved, focusCover:() => {
      if (coverPanel.hidden) return false;
      coverStatus.focus();
      return true;
    }, retryPage, destroy, get job() { return job; } };
  }

  return { mount };
})();
