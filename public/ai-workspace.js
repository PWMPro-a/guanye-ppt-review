'use strict';
(() => {
  const config = window.CLOUD_CONFIG;
  if (!config || !config.aiGatewayUrl) return;
  if (window.GuanyeAICore) window.GuanyeAI = window.GuanyeAICore;

  const safe = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  let generationCleanup = null, generationContext = {planId:'',jobId:'',job:null};
  let planningCleanup = null;
  let reviewCleanups = {uploaded:null,generated:null};
  let recentJobsRequest = 0, generationEpoch = 0, reviewNavigationEpoch = 0;
  let generationHistoryLoaded = false, generationHistoryLoading = false;
  let mounted = {reviews:{uploaded:false,generated:false},planning:false,generation:false};
  let decorating = false;

  function ready() {
    return Boolean(window.Cloud?.aiAvailable && window.Cloud?.member && window.GuanyeAI);
  }

  function currentDeckId() {
    try {
      const fromHash = new URLSearchParams(location.hash.slice(1)).get('deck');
      if (fromHash) return fromHash;
    } catch {}
    try {
      if (typeof activeId !== 'undefined' && activeId) return activeId;
    } catch {}
    return '';
  }

  function deckList() {
    try {
      return Array.isArray(decks) ? decks.map(deck => ({id:deck.id,title:deck.title || deck.name || deck.id})) : [];
    } catch {
      return [];
    }
  }

  function injectStyle() {
    if (document.querySelector('#guanye-ai-workspace-style')) return;
    const style = document.createElement('style');
    style.id = 'guanye-ai-workspace-style';
    style.textContent = `
      .ai-workspace-shell{display:grid;gap:18px;min-width:0}
      .ai-workspace-toolbar{display:flex;gap:12px;align-items:end;justify-content:space-between;flex-wrap:wrap;padding:0 2px}
      .ai-workspace-toolbar label{display:grid;gap:6px;min-width:min(360px,100%);font-size:12px;color:var(--muted,#6f7770)}
      .ai-workspace-tabs{display:flex;gap:8px;flex-wrap:wrap;border-bottom:1px solid #e6ebe5;padding-bottom:10px}
      .ai-workspace-tab{border:1px solid #dfe6dc;background:#f8faf7;border-radius:999px;padding:8px 14px;cursor:pointer;color:var(--ink,#263128)}
      .ai-workspace-tab[aria-selected="true"]{background:#24352a;border-color:#24352a;color:#fff}
      .ai-workspace-panel{display:grid;gap:16px;min-width:0}
      .ai-workspace-panel[hidden]{display:none!important}
      .ai-queue-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
      .ai-operation-card{border:1px solid #e2e8df;border-radius:12px;background:#fbfcfa;padding:14px;min-width:0}
      .ai-operation-card h3{margin:0 0 10px;font-size:14px}
      .ai-operation-card [data-ai-root]{min-height:96px}
      .ai-generation-history{display:flex;gap:10px;align-items:end;flex-wrap:wrap;margin-bottom:12px}
      .ai-generation-history label{display:grid;gap:4px;flex:1 1 220px;min-width:0;font-size:12px}
      .ai-generation-history select{width:100%;min-width:0}
      .ai-generation-history [role="status"]{flex:1 0 100%;font-size:12px;color:var(--muted,#6f7770)}
      .ai-generation-history [role="status"]:empty{display:none}
      .ai-module-empty{padding:18px;border:1px dashed #cfd8cb;border-radius:10px;color:#6f7770;background:#fff}
      .ai-module-error{color:#a33b32;background:#fff7f5;border:1px solid #f0d7d2;border-radius:10px;padding:12px}
      @media(max-width:760px){.ai-queue-grid{grid-template-columns:1fr}.ai-workspace-shell{padding:0}.ai-workspace-toolbar label{min-width:100%}}
    `;
    document.head.appendChild(style);
  }

  function ensureEntry() {
    if (decorating || !ready()) return;
    decorating = true;
    try {
      const sidebar = document.querySelector('.sidebar');
      if (sidebar && !sidebar.querySelector('[data-ai-action="open"]')) {
        const anchor = sidebar.querySelector('[data-cloud-action="password"]')
          || sidebar.querySelector('[data-cloud-action="team"]')
          || sidebar.querySelector('.sidebar-divider');
        const html = '<button class="nav-item" data-ai-action="open">' + (window.icon?.('star') || '') + '<span>AI 工作台</span></button>';
        if (anchor) anchor.insertAdjacentHTML('afterend', html);
        else sidebar.insertAdjacentHTML('beforeend', html);
      }
      const actions = document.querySelector('.topbar-actions');
      if (actions && !actions.querySelector('[data-ai-action="open"]')) {
        actions.insertAdjacentHTML('afterbegin', '<button class="button small" data-ai-action="open">AI 工作台</button>');
      }
    } finally {
      decorating = false;
    }
  }

  function runCleanup() {
    generationEpoch++;
    reviewNavigationEpoch++;
    recentJobsRequest++;
    generationHistoryLoaded = false;
    generationHistoryLoading = false;
    mounted = {reviews:{uploaded:false,generated:false},planning:false,generation:false};
    for (const kind of ['uploaded', 'generated']) {
      dispose(reviewCleanups[kind]);
      reviewCleanups[kind] = null;
    }
    dispose(planningCleanup);
    planningCleanup = null;
    dispose(generationCleanup);
    generationCleanup = null;
  }

  function dispose(task) {
    if (!task) return;
    try { task(); } catch {}
  }

  function cleanupFor(result) {
    if (typeof result === 'function') return result;
    if (result && typeof result.destroy === 'function') return () => result.destroy();
    return null;
  }

  function mountModule(globalName, root, options) {
    const module = window[globalName];
    if (!module || typeof module.mount !== 'function') {
      root.innerHTML = '<div class="ai-module-empty">' + safe(globalName + ' 组件尚未加载，请确认对应前端脚本已部署。') + '</div>';
      return null;
    }
    try {
      return cleanupFor(module.mount(root, options));
    } catch (error) {
      root.innerHTML = '<div class="ai-module-error">' + safe(error?.message || 'AI 操作区加载失败') + '</div>';
      return null;
    }
  }

  function mountReview(kind, deckId) {
    const key = kind === 'generated' ? 'generated' : 'uploaded';
    dispose(reviewCleanups[key]);
    reviewCleanups[key] = null;
    const root = document.querySelector('[data-ai-root="reviews-' + key + '"]');
    if (!root) {
      mounted.reviews[key] = false;
      return null;
    }
    const options = {...baseOptions(), kind:key};
    if (deckId !== undefined) options.deckId = String(deckId || '');
    mounted.reviews[key] = true;
    reviewCleanups[key] = mountModule('GuanyeReviews', root, options);
    return root;
  }

  function ensureReviewModules() {
    for (const kind of ['uploaded', 'generated']) {
      if (!mounted.reviews[kind]) mountReview(kind);
    }
  }

  function mountPlanning() {
    if (mounted.planning) return;
    const root = document.querySelector('[data-ai-root="planning"]');
    if (!root) return;
    mounted.planning = true;
    planningCleanup = mountModule('GuanyePlanning', root, {...baseOptions(), onGenerationSubmitted:useGenerationReceipt});
  }

  function selectedDeck() {
    return document.querySelector('#guanye-ai-deck')?.value || currentDeckId() || undefined;
  }

  function baseOptions() {
    return {deckId:selectedDeck(), request:window.GuanyeAICore || window.GuanyeAI, onRequestClose:() => document.querySelector('#modal')?.close()};
  }

  function switchTab(name, {focus = false} = {}) {
    let selectedTab = null;
    document.querySelectorAll('[data-ai-tab]').forEach(button => {
      const active = button.dataset.aiTab === name;
      button.setAttribute('aria-selected', String(active));
      button.tabIndex = active ? 0 : -1;
      if (active) selectedTab = button;
    });
    document.querySelectorAll('[data-ai-panel]').forEach(panel => {
      const active = panel.dataset.aiPanel === name;
      panel.hidden = !active;
      panel.dataset.state = active ? 'active' : 'inactive';
    });
    if (focus) selectedTab?.focus({preventScroll:true});
  }

  function enterTab(name, {focus = false, history = true} = {}) {
    switchTab(name, {focus});
    if (name === 'reviews') ensureReviewModules();
    else if (name === 'planning') mountPlanning();
    else if (name === 'generation') {
      if (!mounted.generation) mountGeneration();
      if (history && !generationHistoryLoaded && !generationHistoryLoading) void refreshRecentJobs();
    }
  }

  function updateJobOptions(select, jobs) {
    const current = generationContext.jobId;
    const options = jobs.map(job => {
      const date = new Date(job.createdAt || '');
      const label = Number.isNaN(date.getTime()) ? '' : ' · ' + date.toLocaleString('zh-CN');
      return new Option((job.title || '未命名作品') + label, job.id);
    });
    if (current && !jobs.some(job => job.id === current)) {
      options.unshift(new Option(generationContext.job?.title || '刚提交的作品', current));
    }
    select.replaceChildren(...options);
    select.disabled = !options.length;
    select.value = current || options[0]?.value || '';
  }

  async function refreshRecentJobs({force = false} = {}) {
    const select = document.querySelector('#guanye-ai-generation-job');
    const status = document.querySelector('#guanye-ai-generation-history-status');
    if (!select || !status || generationHistoryLoading) return;
    if (generationHistoryLoaded && !force) return;
    generationHistoryLoading = true;
    const requestId = ++recentJobsRequest;
    status.textContent = '正在读取生成任务…';
    try {
      const payload = await window.GuanyeAI.request('/api/ai/generation?action=recent');
      if (requestId !== recentJobsRequest || !select.isConnected) return;
      if (!Array.isArray(payload?.jobs)) throw new Error('生成任务列表格式无效');
      const jobs = payload.jobs.filter(job => typeof job?.id === 'string' && job.id);
      generationHistoryLoaded = true;
      updateJobOptions(select, jobs);
      status.textContent = select.disabled ? '暂无生成任务' : '';
      if (!generationContext.jobId) {
        if (jobs.length) selectGenerationJob(select.value);
        else if (!mounted.generation) mountGeneration();
      }
    } catch (error) {
      if (requestId !== recentJobsRequest || !select.isConnected) return;
      generationHistoryLoaded = false;
      status.textContent = error.message || '读取生成任务失败';
    } finally {
      if (requestId === recentJobsRequest) generationHistoryLoading = false;
    }
  }

  function selectGenerationJob(jobId) {
    if (!jobId || generationContext.jobId === jobId) return;
    generationContext = {planId:'', jobId, job:null};
    mountGeneration();
  }

  function acceptGenerationUpdate(epoch, expectedJobId, next) {
    const modal = document.querySelector('#modal');
    if (epoch !== generationEpoch || !modal?.open || !modal.querySelector('.ai-workspace-shell')) return;
    if (generationContext.jobId !== expectedJobId || String(next?.id || '') !== expectedJobId) return;
    generationContext = {...generationContext, job:next};
  }

  function openGenerationReview(epoch, expectedJobId, payload) {
    const modal = document.querySelector('#modal');
    if (epoch !== generationEpoch || !modal?.open || !modal.querySelector('.ai-workspace-shell')) return;
    if (generationContext.jobId !== expectedJobId || String(payload?.jobId || '') !== expectedJobId) return;
    const current = generationContext.job;
    if (!current || String(current.id || '') !== expectedJobId || String(current.status || '') !== 'completed') return;
    const deckId = String(current.deckId || current.deck_id || '').trim();
    if (!/^[A-Za-z0-9_-]{1,100}$/.test(deckId)) return;
    const navigationEpoch = ++reviewNavigationEpoch;
    enterTab('reviews');
    mountReview('generated', deckId);
    const focusTarget = document.querySelector('[data-review-focus-target="generated"]');
    requestAnimationFrame(() => {
      if (navigationEpoch !== reviewNavigationEpoch || epoch !== generationEpoch) return;
      if (!modal.open || document.querySelector('[data-ai-tab="reviews"]')?.getAttribute('aria-selected') !== 'true') return;
      if (focusTarget?.isConnected) {
        focusTarget.focus();
        focusTarget.scrollIntoView({block:'start', inline:'nearest'});
      }
    });
  }

  function useGenerationReceipt(payload, state = {}) {
    const job = payload?.job || payload;
    const jobId = String(job?.id || job?.jobId || job?.job_id || '');
    if (!jobId) return;
    const select = document.querySelector('#guanye-ai-generation-job');
    const changed = jobId !== generationContext.jobId;
    if (changed) {
      generationContext = {
        planId:String(job?.planId || job?.plan_id || state?.planId || payload?.planId || ''),
        jobId,
        job:Array.isArray(job?.pages) ? job : null,
      };
      if (select && !Array.from(select.options).some(option => option.value === jobId)) {
        select.add(new Option(job.title || state.plan?.title || '刚提交的作品', jobId), 0);
      }
    }
    const wasMounted = mounted.generation;
    enterTab('generation', {history:false});
    if (changed && wasMounted) mountGeneration();
    if (select) {
      select.disabled = false;
      select.value = jobId;
    }
    void refreshRecentJobs({force:true});
  }

  function mountGeneration() {
    mounted.generation = true;
    const epoch = ++generationEpoch;
    reviewNavigationEpoch++;
    dispose(generationCleanup);
    generationCleanup = null;
    const root = document.querySelector('[data-ai-root="generation"]');
    if (!root) {
      mounted.generation = false;
      return;
    }
    if (!generationContext.jobId) {
      const hint = document.createElement('p');
      hint.className = 'ai-module-empty';
      hint.textContent = '选取已有任务，或在「生成策划」中提交新的任务。';
      root.replaceChildren(hint);
      return;
    }
    const module = window.GuanyeGeneration;
    if (!module || typeof module.mount !== 'function') {
      root.innerHTML = '<div class="ai-module-empty">GuanyeGeneration 组件尚未加载，请确认对应前端脚本已部署。</div>';
      return;
    }
    try {
      const expectedJobId = String(generationContext.jobId || '');
      const result = module.mount(root, {
        ...baseOptions(),
        planId:generationContext.planId || undefined,
        jobId:generationContext.jobId || undefined,
        job:generationContext.job || undefined,
        autoStart:Boolean(generationContext.planId || generationContext.jobId),
        onUpdate:next => acceptGenerationUpdate(epoch, expectedJobId, next),
        onOpenReview:payload => openGenerationReview(epoch, expectedJobId, payload),
      });
      generationCleanup = cleanupFor(result);
      if (!generationContext.job) void result?.refresh?.();
    } catch (error) {
      root.innerHTML = '<div class="ai-module-error">' + safe(error?.message || '生成进度加载失败') + '</div>';
    }
  }

  function rebindDeckModules() {
    const modal = document.querySelector('#modal');
    if (!modal?.open || !modal.querySelector('.ai-workspace-shell')) return;
    for (const kind of ['uploaded', 'generated']) {
      if (mounted.reviews[kind]) mountReview(kind);
    }
  }

  function openWorkspace() {
    if (!ready()) {
      window.toast?.('AI 工作台尚未配置或尚未登录', true);
      return;
    }
    const currentModal = document.querySelector('#modal');
    if (currentModal?.open && currentModal.querySelector('.ai-workspace-shell')) {
      currentModal.querySelector('[data-ai-tab][aria-selected="true"]')?.focus();
      return;
    }
    injectStyle();
    const choices = deckList();
    const active = currentDeckId();
    if (active && !choices.some(item => item.id === active)) choices.unshift({id:active,title:active});
    const deckOptions = ['<option value="">不限定作品 / 由操作区选择</option>']
      .concat(choices.map(item => '<option value="' + safe(item.id) + '"' + (item.id === active ? ' selected' : '') + '>' + safe(item.title) + '</option>'))
      .join('');
    const header = window.modalHeader
      ? window.modalHeader('AI 工作台', '评审、策划与生成共用云端身份；工作区和角色由 gateway 校验。')
      : '<div class="modal-header"><div><h2 id="modal-title">AI 工作台</h2></div></div>';
    const html = header + `
      <div class="ai-workspace-shell">
        <div class="ai-workspace-toolbar">
          <label>当前作品（可选）<select class="text-input" id="guanye-ai-deck">${deckOptions}</select></label>
          <div class="ai-workspace-tabs" role="tablist" aria-label="AI 操作区">
            <button id="guanye-ai-tab-reviews" type="button" class="ai-workspace-tab" role="tab" aria-selected="true" aria-controls="guanye-ai-panel-reviews" tabindex="0" data-ai-tab="reviews">AI 评审</button>
            <button id="guanye-ai-tab-planning" type="button" class="ai-workspace-tab" role="tab" aria-selected="false" aria-controls="guanye-ai-panel-planning" tabindex="-1" data-ai-tab="planning">生成策划</button>
            <button id="guanye-ai-tab-generation" type="button" class="ai-workspace-tab" role="tab" aria-selected="false" aria-controls="guanye-ai-panel-generation" tabindex="-1" data-ai-tab="generation">生成进度</button>
          </div>
        </div>
        <section id="guanye-ai-panel-reviews" class="ai-workspace-panel" role="tabpanel" aria-labelledby="guanye-ai-tab-reviews" data-ai-panel="reviews">
          <div class="ai-queue-grid">
            <div class="ai-operation-card"><h3>已上传作品队列</h3><div data-ai-root="reviews-uploaded"></div></div>
            <div class="ai-operation-card" role="region" aria-label="生成作品评审" tabindex="-1" data-review-focus-target="generated"><h3>生成作品队列</h3><div data-ai-root="reviews-generated"></div></div>
          </div>
        </section>
        <section id="guanye-ai-panel-planning" class="ai-workspace-panel" role="tabpanel" aria-labelledby="guanye-ai-tab-planning" data-ai-panel="planning" hidden>
          <div class="ai-operation-card"><h3>策划生成</h3><div data-ai-root="planning"></div></div>
        </section>
        <section id="guanye-ai-panel-generation" class="ai-workspace-panel" role="tabpanel" aria-labelledby="guanye-ai-tab-generation" data-ai-panel="generation" hidden>
          <div class="ai-operation-card">
            <div class="ai-generation-history">
              <label>生成任务<select class="text-input" id="guanye-ai-generation-job" disabled></select></label>
              <button class="button small" type="button" data-ai-action="refresh-generation">刷新任务</button>
              <div id="guanye-ai-generation-history-status" role="status"></div>
            </div>
            <div data-ai-root="generation"></div>
          </div>
        </section>
      </div>
      <div class="modal-footer"><button class="button primary" data-action="close-modal">完成</button></div>`;
    if (window.showModal) window.showModal(html, 'ai-workspace-modal');
    else {
      const modal = document.querySelector('#modal');
      modal.className = 'ai-workspace-modal';
      modal.innerHTML = html;
      modal.showModal();
    }
    const modal = document.querySelector('#modal');
    enterTab('reviews');
    document.querySelector('#guanye-ai-deck')?.addEventListener('change', rebindDeckModules);
    modal.querySelector('#guanye-ai-generation-job')?.addEventListener('change', event => selectGenerationJob(event.target.value));
  }

  document.addEventListener('click', event => {
    const opener = event.target.closest('[data-ai-action="open"]');
    if (opener) {
      event.preventDefault();
      openWorkspace();
      return;
    }
    if (event.target.closest('[data-ai-action="refresh-generation"]')) {
      void refreshRecentJobs({force:true});
      return;
    }
    const tab = event.target.closest('[data-ai-tab]');
    if (!tab) return;
    reviewNavigationEpoch++;
    enterTab(tab.dataset.aiTab);
  });

  document.addEventListener('keydown', event => {
    const tab = event.target.closest?.('[data-ai-tab]');
    if (!tab) return;
    const tabs = Array.from(document.querySelectorAll('[data-ai-tab]'));
    const index = tabs.indexOf(tab);
    if (index < 0) return;
    let next = -1;
    if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
    else if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = tabs.length - 1;
    if (next < 0) return;
    event.preventDefault();
    reviewNavigationEpoch++;
    enterTab(tabs[next].dataset.aiTab, {focus:true});
  });

  document.querySelector('#modal')?.addEventListener('close', () => {
    runCleanup();
    recentJobsRequest++;
    generationContext = {planId:'', jobId:'', job:null};
  });
  const onGenerationCreated = event => {
    const modal = document.querySelector('#modal');
    if (modal?.open && modal.querySelector('.ai-workspace-shell')) useGenerationReceipt(event.detail);
  };
  window.addEventListener('guanye:generation-created', onGenerationCreated);
  window.addEventListener('guanye:generation-submitted', onGenerationCreated);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => { injectStyle(); ensureEntry(); });
  else { injectStyle(); ensureEntry(); }
  if (typeof MutationObserver === 'function') new MutationObserver(ensureEntry).observe(document.documentElement, {childList:true,subtree:true});
  if (typeof setInterval === 'function') setInterval(ensureEntry, 1500);
})();
