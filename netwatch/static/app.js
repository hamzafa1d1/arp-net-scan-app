/* NetWatch — Frontend App */

// ─── State ─────────────────────────────────────────────────────────────────
const state = {
  devices: {},
  status: {},
  policy: { enabled: true, download_kbps: 2048, upload_kbps: null },
  ws: null,
  wsReconnectTimer: null,
  selectedMac: null,
  currentView: 'topology',
  simulation: null,
  svg: null,
  zoom: null,
  g: null,
};

// ─── DOM refs ──────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const els = {
  wsStatus:      $('ws-status'),
  sIface:        $('s-iface'),
  sOurip:        $('s-ourip'),
  sGw:           $('s-gw'),
  sNet:          $('s-net'),
  btnScan:       $('btn-scan'),
  scanOverlay:   $('scan-overlay'),
  // stats
  statOnline:    $('stat-online'),
  statThrottled: $('stat-throttled'),
  statBlocked:   $('stat-blocked'),
  // policy
  policyToggle:  $('policy-toggle'),
  qosDlVal:      $('qos-dl-val'),
  qosUlVal:      $('qos-ul-val'),
  qosDlBar:      $('qos-dl-bar'),
  qosUlBar:      $('qos-ul-bar'),
  qosLimits:     $('qos-limits'),
  qosDisabled:   $('qos-disabled-msg'),
  qosEditBtn:    $('qos-edit-btn'),
  qosEditForm:   $('qos-edit-form'),
  qosInDl:       $('qos-in-dl'),
  qosInUl:       $('qos-in-ul'),
  qosSaveBtn:    $('qos-save-btn'),
  qosCancelBtn:  $('qos-cancel-btn'),
  // panel
  panel:         $('device-panel'),
  panelClose:    $('panel-close'),
  panelIcon:     $('panel-icon'),
  panelHost:     $('panel-hostname'),
  panelIp:       $('panel-ip'),
  pMac:          $('p-mac'),
  pVendor:       $('p-vendor'),
  pStatus:       $('p-status'),
  pBtnCut:       $('p-btn-cut'),
  pBtnRestore:   $('p-btn-restore'),
  pDlLabel:      $('p-dl-label'),
  pUlLabel:      $('p-ul-label'),
  pDlMeter:      $('p-dl-meter'),
  pUlMeter:      $('p-ul-meter'),
  pDl:           $('p-dl'),
  pDlSlider:     $('p-dl-slider'),
  pDlVal:        $('p-dl-val'),
  pUl:           $('p-ul'),
  pUlSlider:     $('p-ul-slider'),
  pUlVal:        $('p-ul-val'),
  pBtnThrottle:  $('p-btn-throttle'),
  deviceTbody:   $('device-tbody'),
  searchInput:   $('search-input'),
  tableEmpty:    $('table-empty'),
  topoEmpty:     $('topo-empty'),
  topoSvg:       $('topology-svg'),
};

// ─── WebSocket ─────────────────────────────────────────────────────────────
function connectWS() {
  state.ws = new WebSocket(`ws://${location.host}/ws`);

  state.ws.onopen = () => {
    setWsStatus('connected', '● Connected');
    clearTimeout(state.wsReconnectTimer);
  };

  state.ws.onmessage = ({ data }) => {
    const msg = JSON.parse(data);
    if (msg.type === 'init') {
      updateStatus(msg.status);
      if (msg.policy) updatePolicy(msg.policy, false);
      setDevices(msg.data);
    } else if (msg.type === 'devices') {
      setDevices(msg.data);
    } else if (msg.type === 'device_update') {
      updateDevice(msg.data);
    } else if (msg.type === 'policy') {
      updatePolicy(msg.data, false);
    }
  };

  state.ws.onclose = () => {
    setWsStatus('disconnected', '● Disconnected');
    state.wsReconnectTimer = setTimeout(connectWS, 3000);
  };

  state.ws.onerror = () => setWsStatus('error', '● Connection error');

  setInterval(() => {
    if (state.ws?.readyState === WebSocket.OPEN)
      state.ws.send(JSON.stringify({ type: 'ping' }));
  }, 20000);
}

function setWsStatus(cls, text) {
  els.wsStatus.className = `ws-status ${cls}`;
  els.wsStatus.textContent = text;
}

// ─── State management ──────────────────────────────────────────────────────
function setDevices(list) {
  state.devices = {};
  list.forEach(d => { state.devices[d.mac] = d; });
  render();
}

function updateDevice(d) {
  state.devices[d.mac] = d;
  render();
  if (state.selectedMac === d.mac) openPanel(d.mac);
}

function updateStatus(s) {
  state.status = s;
  els.sIface.textContent = s.iface || '—';
  els.sOurip.textContent = s.our_ip || '—';
  els.sGw.textContent    = s.gateway_ip || '—';
  els.sNet.textContent   = s.network || '—';
}

// ─── Policy widget ─────────────────────────────────────────────────────────
const MAX_BW = 50000; // 50 Mbps = 100% bar width

function updatePolicy(p, sync = true) {
  state.policy = p;

  els.policyToggle.checked = p.enabled;
  els.qosLimits.classList.toggle('hidden', !p.enabled);
  els.qosDisabled.classList.toggle('hidden', p.enabled);

  const dlText = p.download_kbps ? fmtBw(p.download_kbps) : '—';
  const ulText = p.upload_kbps   ? fmtBw(p.upload_kbps)   : '—';
  els.qosDlVal.textContent = dlText;
  els.qosUlVal.textContent = ulText;

  const dlPct = p.download_kbps ? Math.min(100, p.download_kbps / MAX_BW * 100).toFixed(1) : 0;
  const ulPct = p.upload_kbps   ? Math.min(100, p.upload_kbps   / MAX_BW * 100).toFixed(1) : 0;
  els.qosDlBar.style.width = dlPct + '%';
  els.qosUlBar.style.width = ulPct + '%';

  // Pre-fill edit form with current values
  els.qosInDl.value = p.download_kbps || '';
  els.qosInUl.value = p.upload_kbps   || '';

  if (sync) render(); // update stats & table after policy change
}

async function savePolicy() {
  const dl = parseInt(els.qosInDl.value) || null;
  const ul = parseInt(els.qosInUl.value) || null;
  try {
    const data = await api('POST', '/policy', {
      enabled: state.policy.enabled,
      download_kbps: dl,
      upload_kbps: ul,
    });
    updatePolicy(data, true);
    closeEditForm();
    toast(`Policy updated: DL=${dl ? fmtBw(dl) : '—'} UL=${ul ? fmtBw(ul) : '—'}`, 'success');
  } catch (e) {
    toast(`Policy update failed: ${e.message}`, 'error');
  }
}

async function togglePolicy(enabled) {
  try {
    const data = await api('POST', '/policy', {
      enabled,
      download_kbps: state.policy.download_kbps,
      upload_kbps:   state.policy.upload_kbps,
    });
    updatePolicy(data, true);
    toast(enabled ? 'Policy enabled' : 'Policy disabled', 'info');
  } catch (e) {
    toast(`Failed: ${e.message}`, 'error');
    els.policyToggle.checked = !enabled; // revert
  }
}

function closeEditForm() {
  els.qosEditForm.classList.add('hidden');
  els.qosEditBtn.textContent = 'Edit limits';
}

// ─── Stats ─────────────────────────────────────────────────────────────────
function renderStats(devs) {
  const controllable = devs.filter(d => !d.is_gateway && !d.is_self);
  const online    = controllable.filter(d => d.online && !d.is_cut && !(d.download_limit || d.upload_limit)).length;
  const throttled = controllable.filter(d => d.online && (d.download_limit || d.upload_limit) && !d.is_cut).length;
  const blocked   = controllable.filter(d => d.is_cut).length;
  els.statOnline.textContent    = online;
  els.statThrottled.textContent = throttled;
  els.statBlocked.textContent   = blocked;
}

// ─── Render dispatcher ─────────────────────────────────────────────────────
function render() {
  const devs = Object.values(state.devices);
  renderStats(devs);
  if (state.currentView === 'topology') renderTopology(devs);
  else renderTable(devs);
}

// ─── D3 Topology ───────────────────────────────────────────────────────────
function initTopology() {
  const container = $('topology-container');
  const w = container.clientWidth;
  const h = container.clientHeight;

  const svg = d3.select('#topology-svg');
  svg.selectAll('*').remove();

  const zoom = d3.zoom().scaleExtent([0.2, 4]).on('zoom', e => g.attr('transform', e.transform));
  svg.call(zoom);

  const g = svg.append('g');
  state.svg = svg; state.zoom = zoom; state.g = g;
  g.append('g').attr('class', 'links');
  g.append('g').attr('class', 'nodes');

  const simulation = d3.forceSimulation()
    .force('link', d3.forceLink().id(d => d.mac).distance(120).strength(0.8))
    .force('charge', d3.forceManyBody().strength(-340))
    .force('center', d3.forceCenter(w / 2, h / 2))
    .force('collision', d3.forceCollide(55));
  state.simulation = simulation;

  $('topo-fit').onclick = () =>
    svg.transition().duration(500).call(zoom.transform, d3.zoomIdentity.translate(w/2, h/2).scale(0.9));
  $('topo-zoom-in').onclick  = () => svg.transition().duration(300).call(zoom.scaleBy, 1.4);
  $('topo-zoom-out').onclick = () => svg.transition().duration(300).call(zoom.scaleBy, 0.7);
}

function renderTopology(devs) {
  if (!state.svg) initTopology();

  const hasDevices = devs.length > 0;
  els.topoEmpty.style.display = hasDevices ? 'none' : 'flex';
  if (!hasDevices) return;

  const container = $('topology-container');
  const w = container.clientWidth, h = container.clientHeight;

  const gw = devs.find(d => d.is_gateway);
  const nodes = devs.map(d => ({
    ...d,
    x: (state.simulation.nodes().find(n => n.mac === d.mac) || {}).x,
    y: (state.simulation.nodes().find(n => n.mac === d.mac) || {}).y,
  }));

  const links = gw
    ? nodes.filter(n => n.mac !== gw.mac).map(n => ({
        source: gw.mac, target: n.mac,
        cut: n.is_cut,
        throttled: n.download_limit || n.upload_limit,
      }))
    : [];

  const linkSel = state.g.select('.links').selectAll('.link').data(links, d => d.target);
  linkSel.exit().remove();
  const linkMerge = linkSel.enter().append('line').attr('class', 'link').merge(linkSel);
  linkMerge.attr('class', d => `link${d.cut ? ' cut' : d.throttled ? ' throttled' : ''}`);

  const nodeSel = state.g.select('.nodes').selectAll('.node-group').data(nodes, d => d.mac);
  nodeSel.exit().remove();

  const drag = d3.drag()
    .on('start', (e, d) => { if (!e.active) state.simulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
    .on('drag',  (e, d) => { d.fx = e.x; d.fy = e.y; })
    .on('end',   (e, d) => { if (!e.active) state.simulation.alphaTarget(0); d.fx = null; d.fy = null; });

  const nodeEnter = nodeSel.enter().append('g').attr('class', 'node-group')
    .call(drag).on('click', (e, d) => openPanel(d.mac));

  nodeEnter.append('circle').attr('class', 'node-circle');
  nodeEnter.append('text').attr('class', 'node-emoji').attr('text-anchor', 'middle').attr('dy', 8).attr('font-size', '18px');
  nodeEnter.append('text').attr('class', 'node-label');
  nodeEnter.append('text').attr('class', 'node-sublabel');
  // Throttle badge ring
  nodeEnter.append('circle').attr('class', 'node-badge-ring');
  nodeEnter.append('text').attr('class', 'node-badge-text').attr('text-anchor', 'middle').attr('font-size', '9px');

  const nodeMerge = nodeEnter.merge(nodeSel);
  const r = d => d.is_gateway ? 30 : d.is_self ? 26 : 22;

  nodeMerge.select('.node-circle')
    .attr('r', r)
    .attr('fill', d => nodeColor(d))
    .attr('stroke', d => d.mac === state.selectedMac ? '#fff' : 'transparent')
    .attr('stroke-width', 2);

  nodeMerge.select('.node-emoji').text(d => nodeEmoji(d));

  nodeMerge.select('.node-label')
    .attr('dy', d => r(d) + 16).text(d => truncate(d.hostname || d.ip, 14));

  nodeMerge.select('.node-sublabel')
    .attr('dy', d => r(d) + 28).text(d => d.ip);

  // Small limit badge for throttled devices
  nodeMerge.select('.node-badge-ring')
    .attr('r', d => (d.download_limit || d.upload_limit) && !d.is_gateway && !d.is_self ? 10 : 0)
    .attr('cx', d => r(d) * 0.7)
    .attr('cy', d => -r(d) * 0.7)
    .attr('fill', '#1e2130')
    .attr('stroke', '#fbbf24')
    .attr('stroke-width', 1.5);

  nodeMerge.select('.node-badge-text')
    .attr('dx', d => r(d) * 0.7)
    .attr('dy', d => -r(d) * 0.7 + 3.5)
    .attr('fill', '#fbbf24')
    .attr('font-weight', '700')
    .text(d => {
      if (d.is_gateway || d.is_self || !(d.download_limit || d.upload_limit)) return '';
      const dl = d.download_limit;
      if (dl >= 1024) return `${(dl/1024).toFixed(0)}M`;
      return `${dl}k`;
    });

  state.simulation.nodes(nodes).on('tick', () => {
    linkMerge
      .attr('x1', d => d.source.x).attr('y1', d => d.source.y)
      .attr('x2', d => d.target.x).attr('y2', d => d.target.y);
    nodeMerge.attr('transform', d => `translate(${d.x},${d.y})`);
  });
  state.simulation.force('link').links(links);
  state.simulation.alpha(0.3).restart();
}

function nodeColor(d) {
  if (d.is_gateway) return '#60a5fa';
  if (d.is_self)    return '#a78bfa';
  if (d.is_cut)     return '#f87171';
  if (d.download_limit || d.upload_limit) return '#fbbf24';
  if (!d.online)    return '#374151';
  return '#6ee7b7';
}

function nodeEmoji(d) {
  if (d.is_gateway) return '🌐';
  if (d.is_self)    return '💻';
  const v = (d.vendor || '').toLowerCase();
  if (v.includes('apple'))    return '🍎';
  if (v.includes('samsung'))  return '📱';
  if (v.includes('amazon'))   return '📦';
  if (v.includes('google'))   return '🔍';
  if (v.includes('cisco') || v.includes('netgear') || v.includes('tp-link')) return '📡';
  if (v.includes('raspberry')) return '🥧';
  return '🖥';
}

function truncate(str, max) {
  return str && str.length > max ? str.slice(0, max - 1) + '…' : (str || '');
}

// ─── Table View ────────────────────────────────────────────────────────────
function renderTable(devs) {
  const query = els.searchInput.value.toLowerCase();
  const filtered = devs.filter(d =>
    !query ||
    (d.ip || '').includes(query) ||
    (d.hostname || '').toLowerCase().includes(query) ||
    (d.vendor || '').toLowerCase().includes(query) ||
    (d.mac || '').toLowerCase().includes(query)
  );

  els.tableEmpty.style.display = filtered.length === 0 ? 'flex' : 'none';

  const sorted = [...filtered].sort((a, b) => {
    if (a.is_gateway) return -1; if (b.is_gateway) return 1;
    if (a.is_self)    return -1; if (b.is_self)    return 1;
    return ipCompare(a.ip, b.ip);
  });

  els.deviceTbody.innerHTML = sorted.map(d => `
    <tr data-mac="${d.mac}" onclick="openPanel('${d.mac}')">
      <td>${statusBadge(d)}</td>
      <td><code style="color:var(--accent)">${d.ip || '—'}</code></td>
      <td>
        <div style="font-weight:600;font-size:13px">${d.hostname || '—'}</div>
        <div style="font-size:11px;color:var(--text-muted)">${d.vendor || ''}</div>
      </td>
      <td><code style="font-size:11px;color:var(--text-muted)">${d.mac}</code></td>
      <td class="limit-cell">${limitCell(d.download_limit, 'dl')}</td>
      <td class="limit-cell">${limitCell(d.upload_limit,   'ul')}</td>
      <td onclick="event.stopPropagation()">
        <div class="table-actions">
          ${!d.is_gateway && !d.is_self ? `
            ${d.is_cut
              ? `<button class="action-btn action-restore" onclick="restoreDevice('${d.mac}')">Restore</button>`
              : `<button class="action-btn action-cut" onclick="cutDevice('${d.mac}')">Block</button>`
            }
          ` : ''}
          <button class="action-btn action-detail" onclick="openPanel('${d.mac}')">Details</button>
        </div>
      </td>
    </tr>
  `).join('');
}

function limitCell(kbps, dir) {
  if (!kbps) return `<span class="limit-none">—</span>`;
  const pct = Math.min(100, kbps / MAX_BW * 100).toFixed(1);
  return `
    <div class="limit-bar-wrap">
      <span class="limit-text">${fmtBw(kbps)}</span>
      <div class="limit-mini-track">
        <div class="limit-mini-fill limit-mini-${dir}" style="width:${pct}%"></div>
      </div>
    </div>`;
}

function statusBadge(d) {
  if (d.is_gateway) return '<span class="status-badge badge-gateway">Gateway</span>';
  if (d.is_self)    return '<span class="status-badge badge-self">This Device</span>';
  if (!d.online)    return '<span class="status-badge badge-offline">Offline</span>';
  if (d.is_cut)     return '<span class="status-badge badge-cut">Blocked</span>';
  if (d.download_limit || d.upload_limit) return '<span class="status-badge badge-throttled">Throttled</span>';
  return '<span class="status-badge badge-online">Online</span>';
}

function fmtBw(kbps) {
  if (!kbps) return '—';
  if (kbps >= 1000) return `${(kbps/1000).toFixed(kbps % 1000 === 0 ? 0 : 1)} Mbps`;
  return `${kbps} kbps`;
}

function ipCompare(a, b) {
  const an = (a || '').split('.').map(Number);
  const bn = (b || '').split('.').map(Number);
  for (let i = 0; i < 4; i++) {
    if ((an[i]||0) !== (bn[i]||0)) return (an[i]||0) - (bn[i]||0);
  }
  return 0;
}

// ─── Device Panel ──────────────────────────────────────────────────────────
function openPanel(mac) {
  const d = state.devices[mac];
  if (!d) return;
  state.selectedMac = mac;

  els.panelIcon.textContent = nodeEmoji(d);
  els.panelHost.textContent = d.hostname || d.ip || 'Unknown';
  els.panelIp.textContent   = d.ip || '—';
  els.pMac.textContent      = d.mac;
  els.pVendor.textContent   = d.vendor || 'Unknown';
  els.pStatus.textContent   = statusText(d);

  const canControl = !d.is_gateway && !d.is_self;
  $('throttle-section').style.display     = canControl ? '' : 'none';
  $('bw-meters-section').style.display    = canControl ? '' : 'none';

  // Bandwidth meters
  const dl = d.download_limit, ul = d.upload_limit;
  els.pDlLabel.textContent = dl ? fmtBw(dl) : 'Unlimited';
  els.pUlLabel.textContent = ul ? fmtBw(ul) : 'Unlimited';
  els.pDlLabel.style.color = dl ? 'var(--yellow)' : 'var(--text-muted)';
  els.pUlLabel.style.color = ul ? 'var(--blue)'   : 'var(--text-muted)';
  els.pDlMeter.style.width = dl ? Math.min(100, dl / MAX_BW * 100).toFixed(1) + '%' : '0%';
  els.pUlMeter.style.width = ul ? Math.min(100, ul / MAX_BW * 100).toFixed(1) + '%' : '0%';

  // Cut/restore buttons
  if (d.is_cut) {
    els.pBtnCut.classList.add('hidden');
    els.pBtnRestore.classList.remove('hidden');
  } else {
    els.pBtnCut.classList.remove('hidden');
    els.pBtnRestore.classList.add('hidden');
  }
  els.pBtnCut.style.display     = canControl ? '' : 'none';
  els.pBtnRestore.style.display = canControl ? '' : 'none';

  // Sliders
  els.pDlSlider.value = dl || 0;
  els.pDl.value       = dl || '';
  els.pDlVal.textContent = dl ? fmtBw(dl) : 'Unlimited';
  els.pUlSlider.value = ul || 0;
  els.pUl.value       = ul || '';
  els.pUlVal.textContent = ul ? fmtBw(ul) : 'Unlimited';

  els.panel.classList.remove('hidden');

  // Highlight topology node
  if (state.currentView === 'topology' && state.g) {
    state.g.select('.nodes').selectAll('.node-group').select('.node-circle')
      .attr('stroke', n => n.mac === mac ? '#fff' : 'transparent')
      .attr('stroke-width', 2);
  }
}

function closePanel() {
  state.selectedMac = null;
  els.panel.classList.add('hidden');
  if (state.g) {
    state.g.select('.nodes').selectAll('.node-group').select('.node-circle')
      .attr('stroke', 'transparent');
  }
}

function statusText(d) {
  if (d.is_gateway) return 'Gateway';
  if (d.is_self)    return 'This Device';
  if (!d.online)    return 'Offline';
  if (d.is_cut)     return 'Blocked';
  if (d.download_limit || d.upload_limit) return 'Throttled';
  return 'Online';
}

// ─── API helpers ───────────────────────────────────────────────────────────
async function api(method, path, body) {
  const res = await fetch(`/api${path}`, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || 'Request failed');
  }
  return res.json();
}

async function scanNetwork() {
  els.scanOverlay.classList.remove('hidden');
  els.btnScan.disabled = true;
  try {
    const data = await api('POST', '/scan');
    toast(`Found ${data.count} device${data.count !== 1 ? 's' : ''}`, 'success');
    setDevices(await api('GET', '/devices'));
  } catch (e) {
    toast(`Scan failed: ${e.message}`, 'error');
  } finally {
    els.scanOverlay.classList.add('hidden');
    els.btnScan.disabled = false;
  }
}

async function cutDevice(mac) {
  try {
    const data = await api('POST', `/devices/${mac}/cut`);
    updateDevice(data.device);
    toast(`Blocked ${data.device.hostname || data.device.ip}`, 'success');
  } catch (e) {
    toast(`Block failed: ${e.message}`, 'error');
  }
}

async function restoreDevice(mac) {
  try {
    const data = await api('POST', `/devices/${mac}/restore`);
    updateDevice(data.device);
    toast(`Restored ${data.device.hostname || data.device.ip}`, 'success');
  } catch (e) {
    toast(`Restore failed: ${e.message}`, 'error');
  }
}

async function throttleDevice(mac, dlKbps, ulKbps) {
  try {
    const data = await api('POST', `/devices/${mac}/throttle`, {
      download_kbps: dlKbps || null,
      upload_kbps:   ulKbps || null,
    });
    updateDevice(data.device);
    toast('Limits applied', 'success');
  } catch (e) {
    toast(`Throttle failed: ${e.message}`, 'error');
  }
}

async function removeThrottle(mac) {
  try {
    const data = await api('DELETE', `/devices/${mac}/throttle`);
    updateDevice(data.device);
    toast('Limits removed', 'success');
  } catch (e) {
    toast(`Failed: ${e.message}`, 'error');
  }
}

// ─── Toast ─────────────────────────────────────────────────────────────────
function toast(msg, type = 'info') {
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  $('toast-container').appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

// ─── Event listeners ───────────────────────────────────────────────────────
document.querySelectorAll('.view-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    state.currentView = btn.dataset.view;
    document.querySelectorAll('.view-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    $(`view-${state.currentView}`).classList.add('active');
    render();
  });
});

els.btnScan.addEventListener('click', scanNetwork);
els.panelClose.addEventListener('click', closePanel);
document.addEventListener('keydown', e => { if (e.key === 'Escape') closePanel(); });
els.searchInput.addEventListener('input', render);

// Policy controls
els.policyToggle.addEventListener('change', () => togglePolicy(els.policyToggle.checked));

els.qosEditBtn.addEventListener('click', () => {
  const open = !els.qosEditForm.classList.contains('hidden');
  if (open) {
    closeEditForm();
  } else {
    els.qosEditForm.classList.remove('hidden');
    els.qosEditBtn.textContent = 'Cancel';
  }
});

els.qosSaveBtn.addEventListener('click', savePolicy);
els.qosCancelBtn.addEventListener('click', closeEditForm);

document.querySelectorAll('.qpre').forEach(btn => {
  btn.addEventListener('click', () => {
    els.qosInDl.value = btn.dataset.dl || '';
    els.qosInUl.value = btn.dataset.ul || '';
  });
});

// Panel slider sync
els.pDlSlider.addEventListener('input', () => {
  const v = +els.pDlSlider.value;
  els.pDl.value = v || '';
  els.pDlVal.textContent = v ? fmtBw(v) : 'Unlimited';
});
els.pDl.addEventListener('input', () => {
  const v = +els.pDl.value;
  els.pDlSlider.value = Math.min(v, 50000);
  els.pDlVal.textContent = v ? fmtBw(v) : 'Unlimited';
});
els.pUlSlider.addEventListener('input', () => {
  const v = +els.pUlSlider.value;
  els.pUl.value = v || '';
  els.pUlVal.textContent = v ? fmtBw(v) : 'Unlimited';
});
els.pUl.addEventListener('input', () => {
  const v = +els.pUl.value;
  els.pUlSlider.value = Math.min(v, 50000);
  els.pUlVal.textContent = v ? fmtBw(v) : 'Unlimited';
});

document.querySelectorAll('.preset-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const dl = +btn.dataset.dl, ul = +btn.dataset.ul;
    els.pDl.value = dl || ''; els.pDlSlider.value = dl || 0;
    els.pDlVal.textContent = dl ? fmtBw(dl) : 'Unlimited';
    els.pUl.value = ul || ''; els.pUlSlider.value = ul || 0;
    els.pUlVal.textContent = ul ? fmtBw(ul) : 'Unlimited';
  });
});

els.pBtnCut.addEventListener('click', () => { if (state.selectedMac) cutDevice(state.selectedMac); });
els.pBtnRestore.addEventListener('click', () => { if (state.selectedMac) restoreDevice(state.selectedMac); });

els.pBtnThrottle.addEventListener('click', () => {
  if (!state.selectedMac) return;
  const dl = +els.pDl.value || null;
  const ul = +els.pUl.value || null;
  if (!dl && !ul) removeThrottle(state.selectedMac);
  else throttleDevice(state.selectedMac, dl, ul);
});

window.addEventListener('resize', () => {
  if (state.currentView === 'topology') {
    state.svg = state.simulation = state.g = null;
    render();
  }
});

// ─── Init ──────────────────────────────────────────────────────────────────
(async () => {
  try { updateStatus(await api('GET', '/status')); } catch (_) {}
  try { updatePolicy(await api('GET', '/policy'), false); } catch (_) {}
  try { setDevices(await api('GET', '/devices')); } catch (_) {}
  connectWS();
})();
