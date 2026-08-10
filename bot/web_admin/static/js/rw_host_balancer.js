(function () {
  'use strict';

  const root = document.getElementById('rwHbRoot');
  if (!root) return;

  let secretPath = root.dataset.secretPath;
  if (secretPath === undefined) {
    secretPath = (typeof SECRET_PATH !== 'undefined' && SECRET_PATH) ? SECRET_PATH : '';
  }
  const cleanPath = secretPath ? (secretPath.startsWith('/') ? secretPath : '/' + secretPath) : '';

  let nodesList = [];
  let lastData = null;
  let addModalCtx = null;

  function nextPoolIndexFromMembers(members) {
    let max = 0;
    for (const m of members || []) {
      const idx = m.index != null ? Number(m.index) : 0;
      if (idx > max) max = idx;
    }
    return max + 1;
  }

  function getAddMode() {
    const active = document.querySelector('#rwHbAddModeSeg button.active');
    return active?.dataset.mode || 'new';
  }

  function setAddMode(mode) {
    document.querySelectorAll('#rwHbAddModeSeg button').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.mode === mode);
    });
    document.getElementById('rwHbAddNewBlock').style.display = mode === 'new' ? '' : 'none';
    document.getElementById('rwHbAddFromNodeBlock').style.display = mode === 'from_node' ? '' : 'none';
    document.getElementById('rwHbAddDupBlock').style.display = mode === 'duplicate' ? '' : 'none';
    const bindBlock = document.getElementById('rwHbAddNodeBindBlock');
    if (bindBlock) bindBlock.style.display = mode === 'from_node' ? 'none' : '';
    updateAddNameHint();
    updateAddNodeUi();
  }

  function findNode(uuid) {
    return nodesList.find(n => String(n.uuid) === String(uuid)) || null;
  }

  function findCatalogHost(uuid) {
    return (lastData?.hosts_catalog || []).find(h => h.uuid === uuid) || null;
  }

  function getAddSourceContext() {
    const mode = getAddMode();
    if (mode === 'duplicate') {
      const uid = document.getElementById('rwHbAddSourceHost')?.value || '';
      return findCatalogHost(uid);
    }
    return addModalCtx?.poolMember || null;
  }

  function updateAddNameHint() {
    const hint = document.getElementById('rwHbAddNameHint');
    if (!hint || !addModalCtx) return;
    const nextIdx = nextPoolIndexFromMembers(addModalCtx.poolMembers);
    hint.textContent = `${addModalCtx.poolCanonical} ${nextIdx}`;
  }

  function fillFromNodeSelect(selected) {
    const sel = document.getElementById('rwHbAddFromNode');
    if (!sel) return;
    const cur = selected || sel.value;
    sel.innerHTML = '<option value="">— выберите ноду —</option>' + nodesList.map(n => {
      const uid = String(n.uuid || '');
      const addr = n.address ? ` · ${n.address}` : '';
      const label = `${n.name || uid}${addr}${n.is_connected ? '' : ' (offline)'}`;
      return `<option value="${esc(uid)}"${uid === String(cur) ? ' selected' : ''}>${esc(label)}</option>`;
    }).join('');
  }

  function onFromNodeChange() {
    const node = findNode(document.getElementById('rwHbAddFromNode')?.value);
    const addrEl = document.getElementById('rwHbAddNodeAddress');
    if (node && addrEl) {
      addrEl.value = node.address || '';
    } else if (addrEl) {
      addrEl.value = '';
    }
  }
  function fillSourceHostSelect() {
    const sel = document.getElementById('rwHbAddSourceHost');
    if (!sel) return;
    const cur = sel.value;
    const poolUuids = new Set((addModalCtx?.poolMembers || []).map(m => m.uuid));
    const items = (lastData?.hosts_catalog || []).filter(h => !poolUuids.has(h.uuid));
    sel.innerHTML = '<option value="">— выберите хост —</option>' + items.map(h => {
      const label = `${h.remark} · ${h.address}:${h.port}`;
      return `<option value="${esc(h.uuid)}">${esc(label)}</option>`;
    }).join('');
    if (cur && items.some(h => h.uuid === cur)) sel.value = cur;
  }

  function fillAddNodePickSelect(selected) {
    const sel = document.getElementById('rwHbAddNode');
    if (!sel) return;
    sel.innerHTML = '<option value="">— выберите ноду —</option>' + nodeOptionsHtml(selected || '');
    if (selected) sel.value = selected;
  }

  function updateAddNodeUi() {
    const modeSel = document.getElementById('rwHbAddNodeMode');
    const pickSel = document.getElementById('rwHbAddNode');
    const hint = document.getElementById('rwHbAddNodeHint');
    if (!modeSel || !pickSel || !hint) return;

    const inheritOpt = modeSel.querySelector('option[value="inherit"]');
    if (inheritOpt) {
      inheritOpt.textContent = getAddMode() === 'duplicate' ? 'Как у исходного хоста' : 'Как у шаблона пула';
    }

    const nodeMode = modeSel.value;
    pickSel.style.display = nodeMode === 'pick' ? '' : 'none';

    const src = getAddSourceContext();
    if (nodeMode === 'inherit') {
      if (!src) {
        hint.textContent = 'Выберите источник — нода подставится автоматически.';
      } else if (src.has_nodes && src.node_name) {
        hint.textContent = `Будет привязана: ${src.node_name}`;
      } else if (src.has_nodes) {
        hint.textContent = 'У источника указана нода, но она не найдена в панели.';
      } else {
        hint.textContent = 'У источника нода не привязана. Можно выбрать ноду ниже.';
      }
    } else if (nodeMode === 'pick') {
      hint.textContent = 'Выберите ноду для режима online.';
      if (src?.node_uuid && !pickSel.value) pickSel.value = src.node_uuid;
    } else {
      hint.textContent = 'Хост создаётся без привязки к ноде.';
    }
  }

  function onAddSourceHostChange() {
    const src = getAddSourceContext();
    const addrEl = document.getElementById('rwHbAddDupAddress');
    const portEl = document.getElementById('rwHbAddDupPort');
    if (src) {
      if (addrEl) addrEl.value = src.address || '';
      if (portEl) portEl.value = src.port || 443;
      const modeSel = document.getElementById('rwHbAddNodeMode');
      if (modeSel && modeSel.value === 'inherit' && !src.has_nodes) {
        modeSel.value = 'none';
      }
      fillAddNodePickSelect(src.node_uuid || '');
    } else {
      if (addrEl) addrEl.value = '';
      if (portEl) portEl.value = '';
    }
    updateAddNodeUi();
  }

  function esc(s) {
    return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function toast(msg, kind) {
    kind = kind || 'success';
    const isErr = kind === 'error';
    const isWarn = kind === 'warning';
    const el = document.createElement('div');
    el.className = 'tw-toast tw-toast-' + (isErr ? 'error' : 'success');
    const icon = isErr ? 'alert-triangle' : (isWarn ? 'alert-circle' : 'check-circle-2');
    el.innerHTML = `<i data-lucide="${icon}" class="w-4 h-4"></i><span></span>`;
    el.querySelector('span').textContent = msg;
    document.body.appendChild(el);
    if (window.lucide && lucide.createIcons) lucide.createIcons();
    setTimeout(() => { el.style.transition = 'opacity .3s'; el.style.opacity = '0'; }, 2700);
    setTimeout(() => { el.remove(); }, 3100);
  }

  function refreshIcons() {
    if (window.lucide && lucide.createIcons) lucide.createIcons();
  }

  function showError(msg) {
    const el = document.getElementById('rwHbError');
    const txt = document.getElementById('rwHbErrorText');
    if (!el || !txt) return;
    if (msg) {
      txt.textContent = msg;
      el.style.display = '';
    } else {
      el.style.display = 'none';
    }
  }

  function nodeOptionsHtml(selected) {
    const sel = selected ? String(selected) : '';
    return nodesList.map(n => {
      const uid = String(n.uuid || '');
      const addr = n.address ? ` · ${n.address}` : '';
      const label = `${n.name || uid}${addr}${n.is_connected ? '' : ' (offline)'}`;
      return `<option value="${esc(uid)}"${uid === sel ? ' selected' : ''}>${esc(label)}</option>`;
    }).join('');
  }

  function fillNodeSelects() {
    fillAddNodePickSelect(document.getElementById('rwHbAddNode')?.value || '');
    const editSel = document.getElementById('rwHbEditNode');
    if (editSel) {
      const cur = editSel.value;
      editSel.innerHTML = '<option value="">— не привязана —</option>' + nodeOptionsHtml(cur);
    }
  }

  function memberRowHtml(m) {
    const addr = `${m.address || ''}:${m.port || ''}`;
    let nodeCell = '—';
    if (m.has_nodes) {
      if (m.node_name) {
        nodeCell = esc(m.node_name);
        if (!m.node_ok) nodeCell += ` <span class="rw-hb-node-warn" title="Offline или выше порога">⚠</span>`;
      } else {
        nodeCell = '<span class="rw-hb-node-warn">нода не найдена</span>';
      }
    } else {
      nodeCell = '<span class="rw-hb-node-warn">не привязана</span>';
    }
    const online = m.has_nodes ? String(m.users_online ?? 0) : '—';
    const rowCls = m.is_disabled ? 'rw-hb-row-disabled' : '';
    return `<tr class="${rowCls}">
      <td>${esc(m.index ?? '—')}</td>
      <td><code class="text-[11px]">${esc(m.remark)}</code></td>
      <td class="text-muted2">${esc(addr)}</td>
      <td>${nodeCell}</td>
      <td class="text-muted2">${esc(online)}</td>
      <td>
        <div class="flex gap-1">
          <button type="button" class="tw-btn tw-btn-ghost tw-btn-icon-sm" data-rw-hb-edit="${esc(m.uuid)}" title="Редактировать">
            <i data-lucide="pencil" class="w-3.5 h-3.5"></i>
          </button>
          <button type="button" class="tw-btn tw-btn-ghost tw-btn-icon-sm" data-rw-hb-del="${esc(m.uuid)}" title="Удалить">
            <i data-lucide="trash-2" class="w-3.5 h-3.5"></i>
          </button>
        </div>
      </td>
    </tr>`;
  }

  function poolCardHtml(pool, isSingle) {
    const badges = [];
    badges.push(`<span class="rw-hb-badge is-violet">${pool.member_count} сервер${pool.member_count === 1 ? '' : (pool.member_count < 5 ? 'а' : 'ов')}</span>`);
    if (pool.is_balanced) badges.push('<span class="rw-hb-badge is-ok">балансируется</span>');
    else badges.push('<span class="rw-hb-badge is-warn">не в пуле</span>');
    (pool.squads || []).slice(0, 4).forEach(sq => {
      badges.push(`<span class="rw-hb-badge">${esc(sq.name)}</span>`);
    });
    const plat = pool.platform_tags;
    if (plat) badges.push(`<span class="rw-hb-badge">${esc(plat.toUpperCase())}</span>`);

    const foot = isSingle
      ? `<button type="button" class="tw-btn tw-btn-secondary tw-btn-sm gap-1.5" data-rw-hb-init="${esc(pool.members[0]?.uuid || '')}">
           <i data-lucide="layers" class="w-3.5 h-3.5"></i> Сделать пул
         </button>`
      : `<button type="button" class="tw-btn tw-btn-primary tw-btn-sm gap-1.5" data-rw-hb-add="${esc(pool.members[0]?.uuid || '')}" data-pool-canonical="${esc(pool.canonical_name || pool.client_name || '')}" data-pool-members="${esc(JSON.stringify(pool.members || []))}">
           <i data-lucide="plus" class="w-3.5 h-3.5"></i> Добавить сервер
         </button>`;

    const members = pool.members || [];
    return `<div class="rw-hb-pool" data-pool-key="${esc(pool.key)}">
      <div class="rw-hb-pool-head">
        <div>
          <h3 class="rw-hb-pool-title">${esc(pool.canonical_name || pool.client_name)}</h3>
          <p class="rw-hb-pool-sub">В подписке клиент видит: <strong>${esc(pool.client_name)}</strong></p>
          <div class="rw-hb-pool-badges">${badges.join('')}</div>
        </div>
      </div>
      <div class="overflow-x-auto">
        <table class="tw-tbl">
          <thead><tr>
            <th>#</th><th>Remark</th><th>Endpoint</th><th>Нода</th><th>Online</th><th></th>
          </tr></thead>
          <tbody>${members.map(memberRowHtml).join('')}</tbody>
        </table>
      </div>
      <div class="rw-hb-pool-foot">${foot}</div>
    </div>`;
  }

  function renderGroups(data) {
    lastData = data;
    const poolsEl = document.getElementById('rwHbPools');
    const singlesEl = document.getElementById('rwHbSingles');
    const loading = document.getElementById('rwHbLoading');
    if (loading) loading.style.display = 'none';

    const pools = data.pools || [];
    const singles = data.singles || [];

    if (poolsEl) {
      poolsEl.style.display = pools.length ? '' : 'none';
      poolsEl.innerHTML = pools.length
        ? pools.map(p => poolCardHtml(p, false)).join('')
        : '';
    }
    if (singlesEl) {
      singlesEl.style.display = singles.length ? '' : 'none';
      if (singles.length) {
        singlesEl.innerHTML = `<h2 class="text-[13px] font-semibold text-muted2 px-1">Одиночные хосты</h2>`
          + singles.map(s => poolCardHtml(s, true)).join('');
      } else {
        singlesEl.innerHTML = '';
      }
    }

    const st = data.stats || {};
    const statsEl = document.getElementById('rwHbStatsLabel');
    if (statsEl) {
      statsEl.textContent = `Хостов: ${st.total_hosts || 0} · пулов: ${st.pool_count || 0} · одиночных: ${st.single_count || 0}`;
    }

    if (!pools.length && !singles.length) {
      if (poolsEl) {
        poolsEl.style.display = '';
        poolsEl.innerHTML = `<div class="rw-hb-empty tw-surface rounded-[var(--radius-card)]">
          <i data-lucide="inbox" class="w-10 h-10 mx-auto mb-2 opacity-40"></i>
          <p>Нет хостов по выбранным фильтрам</p>
        </div>`;
      }
    }
    refreshIcons();
  }

  async function loadGroups() {
    showError('');
    const loading = document.getElementById('rwHbLoading');
    if (loading) loading.style.display = '';
    document.getElementById('rwHbPools').style.display = 'none';
    document.getElementById('rwHbSingles').style.display = 'none';

    const squad = document.getElementById('rwHbFilterSquad')?.value || '';
    const platform = document.getElementById('rwHbFilterPlatform')?.value || 'all';
    const view = document.getElementById('rwHbFilterView')?.value || 'all';
    const qs = new URLSearchParams();
    if (squad) qs.set('squad', squad);
    if (platform && platform !== 'all') qs.set('platform', platform);
    if (view) qs.set('view', view);

    try {
      const r = await fetch(`${cleanPath}/api/remnawave/host-balancer/groups?${qs}`, {
        headers: { 'X-Requested-With': 'fetch' },
        credentials: 'same-origin',
      });
      const data = await r.json();
      if (!data.ok) throw new Error(data.error || 'Ошибка загрузки');
      nodesList = data.nodes || [];
      fillNodeSelects();
      fillSquadsFilter(data.squads || []);
      renderGroups(data);
    } catch (e) {
      if (loading) loading.style.display = 'none';
      showError(e.message || String(e));
      toast('❌ ' + (e.message || e), 'error');
    }
  }

  function openModal(id) {
    const m = document.getElementById(id);
    if (!m) return;
    m.classList.add('is-open');
    m.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
    refreshIcons();
  }

  function closeModals() {
    document.querySelectorAll('.tw-modal.is-open').forEach(m => {
      m.classList.remove('is-open');
      m.setAttribute('aria-hidden', 'true');
    });
    document.body.style.overflow = '';
  }

  function findMember(uuid) {
    if (!lastData || !uuid) return null;
    for (const p of [...(lastData.pools || []), ...(lastData.singles || [])]) {
      for (const m of p.members || []) {
        if (m.uuid === uuid) return m;
      }
    }
    return null;
  }

  function openAddModal(poolUuid, poolCanonical, poolMembersJson) {
    let poolMembers = [];
    try { poolMembers = JSON.parse(poolMembersJson || '[]'); } catch (_) {}
    const poolMember = findMember(poolUuid) || poolMembers[0] || null;

    addModalCtx = {
      poolUuid,
      poolMember,
      poolCanonical: poolCanonical || poolMember?.remark || '',
      poolMembers,
    };

    document.getElementById('rwHbAddPoolUuid').value = poolUuid;
    document.getElementById('rwHbAddPoolRemarks').value = JSON.stringify(poolMembers.map(m => m.remark));
    document.getElementById('rwHbAddPoolCanonical').value = addModalCtx.poolCanonical;

    setAddMode('new');
    document.getElementById('rwHbAddAddress').value = '';
    document.getElementById('rwHbAddPort').value = poolMember?.port || 443;
    document.getElementById('rwHbAddFromNode').value = '';
    document.getElementById('rwHbAddNodeAddress').value = '';
    document.getElementById('rwHbAddNodePort').value = poolMember?.port || 443;
    document.getElementById('rwHbAddDupAddress').value = '';
    document.getElementById('rwHbAddDupPort').value = '';
    document.getElementById('rwHbAddSourceHost').value = '';
    document.getElementById('rwHbAddNodeMode').value = 'inherit';

    fillNodeSelects();
    fillFromNodeSelect('');
    fillSourceHostSelect();
    fillAddNodePickSelect(poolMember?.node_uuid || '');
    updateAddNameHint();
    updateAddNodeUi();
    openModal('rwHbAddModal');
  }

  function openEditModal(uuid) {
    const m = findMember(uuid);
    if (!m) return;
    document.getElementById('rwHbEditUuid').value = uuid;
    document.getElementById('rwHbEditRemark').value = m.remark || '';
    document.getElementById('rwHbEditAddress').value = m.address || '';
    document.getElementById('rwHbEditPort').value = m.port || 443;
    document.getElementById('rwHbEditNode').value = m.node_uuid || '';
    document.getElementById('rwHbEditDisabled').checked = !!m.is_disabled;
    fillNodeSelects();
    openModal('rwHbEditModal');
  }

  async function apiPost(url, body) {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' },
      credentials: 'same-origin',
      body: JSON.stringify(body || {}),
    });
    let data = {};
    try { data = await r.json(); } catch (_) {}
    if (!r.ok) {
      throw new Error(data.error || `HTTP ${r.status}`);
    }
    return data;
  }

  async function apiPatch(url, body) {
    const r = await fetch(url, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' },
      credentials: 'same-origin',
      body: JSON.stringify(body || {}),
    });
    return r.json();
  }

  async function apiDelete(url) {
    const r = await fetch(url, {
      method: 'DELETE',
      headers: { 'X-Requested-With': 'fetch' },
      credentials: 'same-origin',
    });
    return r.json();
  }

  document.getElementById('rwHbRefreshBtn')?.addEventListener('click', loadGroups);
  ['rwHbFilterSquad', 'rwHbFilterPlatform', 'rwHbFilterView'].forEach(id => {
    document.getElementById(id)?.addEventListener('change', loadGroups);
  });

  document.querySelectorAll('[data-rw-hb-close]').forEach(btn => {
    btn.addEventListener('click', closeModals);
  });

  document.querySelectorAll('#rwHbAddModeSeg button').forEach(btn => {
    btn.addEventListener('click', () => setAddMode(btn.dataset.mode || 'new'));
  });
  document.getElementById('rwHbAddFromNode')?.addEventListener('change', onFromNodeChange);
  document.getElementById('rwHbAddSourceHost')?.addEventListener('change', onAddSourceHostChange);
  document.getElementById('rwHbAddNodeMode')?.addEventListener('change', updateAddNodeUi);

  document.getElementById('rwHbAddSubmit')?.addEventListener('click', async () => {
    const mode = getAddMode();
    const pool_uuid = document.getElementById('rwHbAddPoolUuid').value;
    let pool_remarks = [];
    try {
      pool_remarks = JSON.parse(document.getElementById('rwHbAddPoolRemarks').value || '[]');
    } catch (_) {}

    let address = '';
    let port = 443;
    let source_uuid;
    let node_uuid;

    if (mode === 'from_node') {
      node_uuid = document.getElementById('rwHbAddFromNode').value.trim();
      if (!node_uuid) { toast('Выберите ноду', 'warning'); return; }
      address = document.getElementById('rwHbAddNodeAddress').value.trim();
      port = parseInt(document.getElementById('rwHbAddNodePort').value, 10);
      if (!address) { toast('Укажите address (или выберите ноду — подставится автоматически)', 'warning'); return; }
    } else if (mode === 'duplicate') {
      source_uuid = document.getElementById('rwHbAddSourceHost').value.trim();
      if (!source_uuid) { toast('Выберите исходный хост', 'warning'); return; }
      address = document.getElementById('rwHbAddDupAddress').value.trim();
      port = parseInt(document.getElementById('rwHbAddDupPort').value, 10);
    } else {
      address = document.getElementById('rwHbAddAddress').value.trim();
      port = parseInt(document.getElementById('rwHbAddPort').value, 10);
      if (!address) { toast('Укажите address в подписке', 'warning'); return; }
    }

    const body = { pool_uuid, mode, pool_remarks, address, port };
    if (mode === 'duplicate') body.source_uuid = source_uuid;
    if (mode === 'from_node') {
      body.node_uuid = node_uuid;
    } else {
      const nodeMode = document.getElementById('rwHbAddNodeMode').value;
      if (nodeMode === 'none') body.clear_nodes = true;
      else if (nodeMode === 'pick') {
        node_uuid = document.getElementById('rwHbAddNode').value.trim();
        if (!node_uuid) { toast('Выберите ноду', 'warning'); return; }
        body.node_uuid = node_uuid;
      }
    }

    const btn = document.getElementById('rwHbAddSubmit');
    btn.disabled = true;
    try {
      const data = await apiPost(`${cleanPath}/api/remnawave/host-balancer/add-member`, body);
      if (!data.ok) throw new Error(data.error || 'Ошибка');
      closeModals();
      toast('✅ Сервер добавлен в пул', 'success');
      await loadGroups();
    } catch (e) {
      toast('❌ ' + (e.message || e), 'error');
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById('rwHbEditSubmit')?.addEventListener('click', async () => {
    const uuid = document.getElementById('rwHbEditUuid').value;
    const address = document.getElementById('rwHbEditAddress').value.trim();
    const port = parseInt(document.getElementById('rwHbEditPort').value, 10);
    const node_uuid = document.getElementById('rwHbEditNode').value.trim();
    const is_disabled = document.getElementById('rwHbEditDisabled').checked;
    const btn = document.getElementById('rwHbEditSubmit');
    btn.disabled = true;
    try {
      const body = { address, port, is_disabled };
      if (node_uuid) body.node_uuid = node_uuid;
      else body.clear_nodes = true;
      const data = await apiPatch(`${cleanPath}/api/remnawave/host-balancer/members/${uuid}`, body);
      if (!data.ok) throw new Error(data.error || 'Ошибка');
      closeModals();
      toast('✅ Сохранено', 'success');
      await loadGroups();
    } catch (e) {
      toast('❌ ' + (e.message || e), 'error');
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById('rwHbPools')?.addEventListener('click', onPoolClick);
  document.getElementById('rwHbSingles')?.addEventListener('click', onPoolClick);

  async function onPoolClick(e) {
    const addBtn = e.target.closest('[data-rw-hb-add]');
    const initBtn = e.target.closest('[data-rw-hb-init]');
    const editBtn = e.target.closest('[data-rw-hb-edit]');
    const delBtn = e.target.closest('[data-rw-hb-del]');

    if (addBtn) {
      openAddModal(
        addBtn.dataset.rwHbAdd,
        addBtn.getAttribute('data-pool-canonical'),
        addBtn.getAttribute('data-pool-members'),
      );
      return;
    }
    if (initBtn) {
      const uuid = initBtn.dataset.rwHbInit;
      if (!uuid || !confirm('Переименовать хост в «… 1» и включить в балансировку?')) return;
      try {
        const data = await apiPost(`${cleanPath}/api/remnawave/host-balancer/init-pool`, { host_uuid: uuid });
        if (!data.ok) throw new Error(data.error || 'Ошибка');
        toast('✅ Пул создан', 'success');
        await loadGroups();
      } catch (err) {
        toast('❌ ' + (err.message || err), 'error');
      }
      return;
    }
    if (editBtn) {
      openEditModal(editBtn.dataset.rwHbEdit);
      return;
    }
    if (delBtn) {
      const uuid = delBtn.dataset.rwHbDel;
      if (!uuid || !confirm('Удалить этот хост из Remnawave?')) return;
      try {
        const data = await apiDelete(`${cleanPath}/api/remnawave/host-balancer/members/${uuid}`);
        if (!data.ok) throw new Error(data.error || 'Ошибка');
        toast('✅ Хост удалён', 'success');
        await loadGroups();
      } catch (err) {
        toast('❌ ' + (err.message || err), 'error');
      }
    }
  }

  function fillSquadsFilter(squads) {
    const sel = document.getElementById('rwHbFilterSquad');
    if (!sel || sel.dataset.filled === '1') return;
    (squads || []).forEach(sq => {
      const opt = document.createElement('option');
      opt.value = sq.uuid;
      opt.textContent = sq.name || sq.uuid;
      sel.appendChild(opt);
    });
    sel.dataset.filled = '1';
  }

  loadGroups();
  refreshIcons();
})();
