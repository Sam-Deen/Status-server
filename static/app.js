const $ = id => document.getElementById(id);
let csrf = null;
let monitors = [];
let editing = null;
const label = {operational:'Operational', degraded:'Degraded', down:'Down', unknown:'Unknown'};

function el(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined) node.textContent = value;
  return node;
}
function clear(node) { node.replaceChildren(); }
function stamp(value) { return value ? new Date(value).toLocaleString(undefined, {dateStyle:'medium', timeStyle:'short'}) : 'Never'; }
function ago(value) {
  if (!value) return 'Never';
  const sec = Math.max(0, Math.round((Date.now() - new Date(value).getTime())/1000));
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec/60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec/3600)}h ago`;
  return `${Math.floor(sec/86400)}d ago`;
}
function updateLastChecked() {
  for (const node of document.querySelectorAll('[data-last-checked]')) {
    node.textContent = ago(node.dataset.lastChecked);
  }
}
function duration(start, end) {
  const sec = Math.max(0, Math.round((new Date(end || Date.now()) - new Date(start))/1000));
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec/60)}m ${sec%60}s`;
  if (sec < 86400) return `${Math.floor(sec/3600)}h ${Math.floor(sec%3600/60)}m`;
  return `${Math.floor(sec/86400)}d ${Math.floor(sec%86400/3600)}h`;
}
function statusNode(status) {
  const box = el('span', 'status-cell');
  box.append(el('i', `status-dot ${status}`), el('span', `status-text ${status}`, label[status] || 'Unknown'));
  return box;
}
function dockerDetail(m) {
  const state = m.container_state || m.docker_state || 'unknown';
  if (state === 'missing') return 'Container not found';
  if (state === 'unknown') return 'Docker state unavailable';
  const health = m.docker_health || 'unknown';
  const healthText = health === 'none' ? 'No health check' : `Health: ${health}`;
  return `${state[0].toUpperCase() + state.slice(1)} · ${healthText}`;
}
function percentage(value) { return value == null ? '—' : value.toFixed(2) + '%'; }
function healthDetails(detail) {
  const panel = el('details', 'component-health');
  panel.append(el('summary', '', 'Components · measured ' + ago(detail.timestamp)));
  for (const [name, component] of Object.entries(detail.components || {})) {
    const row = el('div', 'service-description');
    row.append(el('strong', '', name + ': '), statusNode(component.status));
    row.append(el('span', '', component.measurementType === 'passive' ? ' · Passive observation' :
      component.critical ? ' · Critical component' : ' · Non-critical component'));
    if (component.latencyMs != null) row.append(el('span', '', ' · ' + component.latencyMs + ' ms'));
    if (component.reason) row.append(el('span', '', ' · ' + component.reason));
    if (component.reconnectAttempts != null) row.append(el('span', '', ' · Reconnects: ' + component.reconnectAttempts));
    row.append(el('div', '', (component.measurementType === 'passive' ? 'Passive observation' : 'Active check') +
      (component.stale ? ' · Stale' : '') +
      (component.intervalMs ? ' · Every ' + Math.round(component.intervalMs / 1000) + 's' : '')));
    row.append(el('div', '', 'Last attempt: ' + stamp(component.lastAttempt) +
      ' · Last measurement: ' + stamp(component.lastMeasurement)));
    row.append(el('div', '', 'Last success: ' + stamp(component.lastSuccess) +
      ' · Last failure: ' + stamp(component.lastFailure)));
    row.append(el('div', '', 'Status changed: ' + stamp(component.lastStatusChange) +
      ' · Failures: ' + (component.consecutiveFailures || 0)));
    panel.append(row);
  }
  return panel;
}
function availabilityCell(m) {
  const box = el('td', 'numeric');
  const a = m.measured_availability;
  if (!a) return box;
  box.append(el('div', '', 'Core: ' + percentage(a.core.availability)),
    el('div', '', 'Full: ' + percentage(a.full.availability)),
    el('div', 'service-description', 'Coverage core/full: ' + percentage(a.core.coverage) + ' / ' + percentage(a.full.coverage)));
  box.title = 'Time measured within the last 30 days. Unknown time is excluded and shown as missing coverage. This is not a contractual SLA.';
  return box;
}
function empty(node, message) { clear(node); node.append(el('div', 'empty-state', message)); }
async function api(path, options = {}) {
  const headers = {...options.headers};
  if (options.body) headers['Content-Type'] = 'application/json';
  if (csrf && options.method && options.method !== 'GET') headers['X-CSRF-Token'] = csrf;
  const response = await fetch(path, {...options, headers, credentials:'same-origin'});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}
async function refreshPublic() {
  try {
    const data = await api('/api/status');
    const count = data.monitors.length;
    $('service-count').textContent = `${count} monitor${count === 1 ? '' : 's'}`;
    $('updated').textContent = `Updated ${ago(data.updated_at)}`;
    const overall = $('overall');
    overall.className = `overall ${data.overall}`;
    clear(overall);
    const copy = el('div');
    const message = {operational:'All systems operational', degraded:'Some services degraded', down:'Service disruption detected', unknown:'Status unavailable'}[data.overall];
    copy.append(el('strong', '', message), el('small', '', count ? `${count} service${count === 1 ? '' : 's'} monitored` : 'No monitors configured yet'));
    overall.append(el('i', `status-dot ${data.overall}`), copy);
    const body = $('services-body');
    const expandedComponents = new Set(Array.from(body.querySelectorAll('details[data-monitor-id]'))
      .filter(panel => panel.open).map(panel => panel.dataset.monitorId));
    clear(body);
    for (const m of data.monitors) {
      const tr = el('tr');
      const name = el('td'); name.append(el('div','service-name',m.name));
      if (m.description) name.append(el('div','service-description',m.description));
      if (m.type === 'docker') {
        name.append(el('div','service-description',`Container: ${m.container_name} · ${dockerDetail(m)}`));
        if (m.last_state_change) name.append(el('div','service-description',`State changed ${stamp(m.last_state_change)}`));
      }
      if (m.application_health) {
        const panel = healthDetails(m.application_health);
        panel.dataset.monitorId = String(m.id);
        panel.open = expandedComponents.has(panel.dataset.monitorId);
        name.append(panel);
      }
      if (m.monitoring_stale) name.append(el('div', 'service-description', 'Monitoring data stale'));
      const state = el('td'); state.append(statusNode(m.status));
      if ((m.status === 'down' || m.status === 'degraded') && m.state_since)
        state.append(el('div','state-duration',`for ${duration(m.state_since)}`));
      const lastChecked = el('td','muted',ago(m.last_checked));
      lastChecked.dataset.lastChecked = m.last_checked || '';
      tr.append(name, state, availabilityCell(m),
        el('td','numeric',m.response_ms === null ? '—' : `${m.response_ms} ms`),
        lastChecked);
      body.append(tr);
    }
    if (!count) { const tr = el('tr'); const td = el('td','empty-state','No services are monitored yet. Sign in to Admin to add one.'); td.colSpan=5; tr.append(td); body.append(tr); }
    const histories = $('history-list'); clear(histories);
    if (!count) empty(histories, 'History appears after the first checks.');
    for (const m of data.monitors) {
      const row = el('div','history-row'); const bars = el('div','bars');
      for (const block of (m.measured_availability?.history || [])) {
        const kind = {operational:'good', degraded:'warn', down:'bad', unknown:'empty'}[block.status] || 'empty';
        const bar = el('i',`bar ${kind}`);
        bar.title = stamp(block.from) + ' – ' + stamp(block.to) + ': ' + (label[block.status] || 'Unknown') +
          ' · full ' + percentage(block.full.availability) + ' · coverage ' + percentage(block.full.coverage);
        bars.append(bar);
      }
      row.append(el('div','history-name',m.name), bars, el('div','history-uptime',percentage(m.measured_availability?.full.availability)));
      histories.append(row);
    }
    renderIncidents($('incidents-list'), data.incidents);
  } catch (err) { $('updated').textContent = 'Connection unavailable'; }
}
function renderIncidents(node, incidents) {
  clear(node);
  if (!incidents.length) return empty(node, 'No recent incidents.');
  for (const i of incidents) {
    const row = el('div','incident-row');
    const service = el('div'); service.append(el('span',`status-text ${i.severity}`,`${i.monitor_name} ${i.severity === 'down' ? 'unavailable' : 'degraded'}`));
    service.append(incidentDetails(i.details));
    if (i.ended_reason) service.append(el('div', 'service-description', 'Observation ended: ' + i.ended_reason));
    row.append(el('div','time',stamp(i.started_at)), service,
      el('div','duration',i.ended_at ? duration(i.started_at,i.ended_at) : `Ongoing · ${duration(i.started_at)}`));
    node.append(row);
  }
}
function latencyRange(value) {
  if (!value) return 'not recorded';
  return value.min === value.max ? `${value.min} ms` : `${value.min}–${value.max} ms`;
}
function incidentDetails(detail) {
  const box = el('div', 'incident-evidence');
  const line = value => box.append(el('div', 'service-description', value));
  if (!detail?.check_count) {
    line('Cause, component impact, and latency were not recorded.');
    return box;
  }
  const sources = detail.sources || [];
  const core = detail.core_states || [];
  if (sources.includes('application')) {
    if (core.includes('unavailable')) line('Core functionality was unavailable in at least one recorded application report.');
    else if (core.length === 1 && core[0] === 'available') line('Core functionality remained available in the recorded application reports.');
    if (core.includes('unknown')) line('Some application reports could not establish core availability.');
  }
  if (sources.includes('http') || sources.includes('tcp'))
    line('Endpoint check affected. Internal component health was not measured.');
  if (sources.includes('docker')) {
    line('Container check affected: ' + (detail.container_states || []).join(', ') +
      ((detail.container_health || []).length ? ' · Health: ' + detail.container_health.join(', ') : '') +
      '. Internal component health was not measured.');
  }
  if (sources.includes('unknown')) line('Internal component health was not recorded for some checks.');
  for (const [name, component] of Object.entries(detail.components || {})) {
    const roles = component.roles.map(role => ({critical:'critical', noncritical:'non-critical',
      passive:'passive observation; does not determine availability'}[role] || role)).join(', ');
    line(name + ' · ' + component.statuses.map(status => label[status] || status).join(', ') +
      ' · ' + roles + (component.reasons.length ? ' · ' + component.reasons.join(', ') : '') +
      ' · Latency while affected: ' + latencyRange(component.latency_ms));
  }
  if (sources.includes('application') && !Object.keys(detail.components || {}).length)
    line('The application report did not identify an affected component.');
  if (detail.reasons?.length) line('Check reason: ' + detail.reasons.join(', '));
  line((sources.length === 1 && sources[0] === 'application' ? 'Application latency' : 'Check response time') +
    ' during incident: ' + latencyRange(detail.response_ms));
  line('Evidence: ' + detail.check_count + ' recorded incident check' + (detail.check_count === 1 ? '' : 's'));
  return box;
}
function route() {
  const admin = location.pathname === '/admin';
  $('overview-view').hidden = admin; $('admin-view').hidden = !admin;
  $('public-link').classList.toggle('active',!admin); $('admin-link').classList.toggle('active',admin);
  if (admin) loadAdmin(); else refreshPublic();
}
async function loadAdmin() {
  try {
    const me = await api('/api/admin/me');
    csrf = me.csrf;
    $('login-form').hidden = me.authenticated;
    $('admin-content').hidden = !me.authenticated;
    $('logout').hidden = !me.authenticated;
    if (me.authenticated) await refreshAdmin();
  } catch (err) { $('login-message').textContent = 'Cannot reach the application.'; }
}
async function refreshAdmin() {
  try {
    const [m, i] = await Promise.all([api('/api/admin/monitors'), api('/api/admin/incidents')]);
    monitors = m.monitors; renderAdminMonitors(); renderIncidents($('admin-incidents'), i.incidents);
  } catch (err) { $('monitor-message').textContent = err.message; }
}
function renderAdminMonitors() {
  const body = $('admin-monitors'); clear(body);
  if (!monitors.length) { const tr=el('tr'); const td=el('td','empty-state','No monitors yet. Add one to start checking a service.'); td.colSpan=6;tr.append(td);body.append(tr); }
  for (const m of monitors) {
    const tr=el('tr');const name=el('td');name.append(el('div','service-name',m.name));
    if (m.type === 'docker') {
      name.append(el('div','service-description',`${m.target} · ${dockerDetail(m)}`));
      if (m.docker_health === 'none' && m.docker_state !== 'missing') name.append(el('div','service-description','Only container state is monitored'));
      if (m.docker_state_since) name.append(el('div','service-description',`State changed ${stamp(m.docker_state_since)}`));
    }
    const state=el('td');state.append(statusNode(m.enabled ? m.status : 'unknown'));
    const actions=el('td','actions'); const group=el('div','action-group');
    for (const [title,callback,style] of [
      ['History',()=>showHistory(m),'subtle'],['Edit',()=>openEditor(m),'subtle'],
      [m.enabled?'Disable':'Enable',()=>toggleMonitor(m),'subtle'],['Delete',()=>deleteMonitor(m),'subtle danger']]) {
      const b=el('button',style,title);b.type='button';b.addEventListener('click',callback);group.append(b);
    }
    actions.append(group);
    tr.append(name,el('td','muted',m.type === 'docker' ? 'Docker' : m.type.toUpperCase()),state,el('td','numeric',`${m.interval}s`),
      el('td','muted',m.enabled?'Yes':'No'),actions);body.append(tr);
  }
}
function openEditor(m=null) {
  editing=m?.id ?? null;
  const form=$('monitor-form');form.reset();form.hidden=false;
  $('editor-title').textContent=m?'Edit monitor':'Add monitor';$('monitor-message').textContent='';
  if (m) for (const key of ['name','description','type','target','interval','timeout','threshold']) form.elements[key].value=m[key];
  form.elements.enabled.checked=m?Boolean(m.enabled):true;
  syncMonitorType(m?.type === 'docker' ? m.target : '');
  form.scrollIntoView({behavior:'smooth',block:'nearest'});
}
function syncMonitorType(selected='') {
  const form=$('monitor-form');
  const docker=form.elements.type.value==='docker';
  $('target-label').hidden=docker;
  $('docker-target-label').hidden=!docker;
  $('docker-controls').hidden=!docker;
  $('timeout-label').hidden=docker;
  $('threshold-label').hidden=docker;
  form.elements.target.disabled=docker;
  form.elements.docker_target.disabled=!docker;
  if (docker) loadDockerChoices(selected);
}
async function loadDockerChoices(selected='') {
  const select=$('monitor-form').elements.docker_target;
  clear(select);
  const loading=el('option','', 'Discovering containers…'); loading.value=''; select.append(loading);
  $('docker-selection-note').textContent='';
  try {
    const data=await api('/api/admin/containers');
    if ($('monitor-form').elements.type.value!=='docker') return;
    clear(select);
    const placeholder=el('option','','Select a container');placeholder.value='';select.append(placeholder);
    for (const c of data.containers) {
      const option=el('option','',`${c.name} · ${c.state} · ${c.health === 'none' ? 'no health check' : c.health}`);
      option.value=c.name; select.append(option);
    }
    if (selected && !data.containers.some(c=>c.name===selected)) {
      const missing=el('option','',`${selected} · currently unavailable`);missing.value=selected;select.append(missing);
    }
    select.value=selected || '';
    if (!data.containers.length) $('docker-selection-note').textContent='No containers discovered.';
    updateDockerSelectionNote(data.containers);
  } catch(err) {
    clear(select);
    if (selected) {const option=el('option','',`${selected} · discovery unavailable`);option.value=selected;select.append(option);select.value=selected;}
    else {const option=el('option','','Docker discovery unavailable');option.value='';select.append(option);}
    $('docker-selection-note').textContent=err.message;
  }
}
function updateDockerSelectionNote(containers) {
  const selected=$('monitor-form').elements.docker_target.value;
  const item=containers.find(c=>c.name===selected);
  if (item) $('docker-selection-note').textContent=item.health==='none'
    ? 'No Docker HEALTHCHECK. Only container running or stopped state is monitored.'
    : `Docker health: ${item.health}. Last state change: ${stamp(item.state_since)}.`;
}
function monitorPayload(m) { return {name:m.name,description:m.description,type:m.type,target:m.target,interval:m.interval,timeout:m.timeout,threshold:m.threshold,enabled:Boolean(m.enabled)}; }
async function toggleMonitor(m) {
  try { await api(`/api/admin/monitors/${m.id}`,{method:'PUT',body:JSON.stringify({...monitorPayload(m),enabled:!m.enabled})}); await refreshAdmin(); }
  catch(err){alert(err.message);}
}
async function deleteMonitor(m) {
  if (!confirm(`Delete ${m.name} and its history?`)) return;
  try { await api(`/api/admin/monitors/${m.id}`,{method:'DELETE'}); await refreshAdmin(); }
  catch(err){alert(err.message);}
}
async function showHistory(m) {
  $('details-title').textContent=`${m.name} · recent checks`;
  const node=$('check-history');node.textContent='Loading…';
  try {
    const data=await api(`/api/admin/monitors/${m.id}/history`);clear(node);
    if (!data.checks.length) return empty(node,'No checks recorded yet.');
    for (const c of data.checks) {
      const row=el('div','detail-row');row.append(el('span','muted',stamp(c.checked_at)),
        el('span',`status-text ${c.status}`,label[c.status]),el('span','numeric',c.response_ms===null?'—':`${c.response_ms} ms`));node.append(row);
      if (c.reason) node.append(el('div', 'service-description', c.reason));
      if (c.application_health) node.append(healthDetails(c.application_health));
    }
  } catch(err){node.textContent=err.message;}
  node.scrollIntoView({behavior:'smooth',block:'nearest'});
}
$('login-form').addEventListener('submit',async event=>{
  event.preventDefault();const form=event.currentTarget;$('login-message').textContent='';
  try {const data=await api('/api/admin/login',{method:'POST',body:JSON.stringify({username:form.elements.username.value,password:form.elements.password.value})});csrf=data.csrf;form.reset();await loadAdmin();}
  catch(err){$('login-message').textContent=err.message;}
});
$('logout').addEventListener('click',async()=>{try{await api('/api/admin/logout',{method:'POST'});csrf=null;await loadAdmin();}catch(err){alert(err.message);}});
$('add-monitor').addEventListener('click',()=>openEditor());
$('monitor-form').elements.type.addEventListener('change',()=>syncMonitorType());
$('refresh-containers').addEventListener('click',()=>loadDockerChoices($('monitor-form').elements.docker_target.value));
$('monitor-form').elements.docker_target.addEventListener('change',()=>{
  const f=$('monitor-form');
  if (!f.elements.name.value) f.elements.name.value=f.elements.docker_target.value;
  $('docker-selection-note').textContent='';
  if (f.elements.docker_target.selectedOptions[0]?.textContent.includes('no health check'))
    $('docker-selection-note').textContent='No Docker HEALTHCHECK. Only container running or stopped state is monitored.';
});
$('cancel-edit').addEventListener('click',()=>{$('monitor-form').hidden=true;editing=null;});
$('monitor-form').addEventListener('submit',async event=>{
  event.preventDefault();const f=event.currentTarget;
  const docker=f.elements.type.value==='docker';
  const data={name:f.elements.name.value,description:f.elements.description.value,type:f.elements.type.value,
    target:docker?f.elements.docker_target.value:f.elements.target.value,interval:Number(f.elements.interval.value),
    timeout:docker?5:Number(f.elements.timeout.value),threshold:docker?1:Number(f.elements.threshold.value),enabled:f.elements.enabled.checked};
  try {await api(editing?`/api/admin/monitors/${editing}`:'/api/admin/monitors',
    {method:editing?'PUT':'POST',body:JSON.stringify(data)});f.hidden=true;editing=null;await refreshAdmin();}
  catch(err){$('monitor-message').textContent=err.message;}
});
route();setInterval(()=>{if(location.pathname==='/')refreshPublic();},15000);
setInterval(updateLastChecked,1000);
