let activeTab = 'status', pollTimer = null;
let currentView = 'list', currentSkill = null, currentProviders = [], currentSchema = null, currentDetailSchema = null;
let networkSkillsCache = [], localSkillsCache = [];
let statusData = null; // Cached status for bootstrap_node_id etc.
let skillViewMode = 'flat'; // 'flat' or 'tree'

const TABS = {
    status:  {title: 'Status',  endpoint: 'api/status',  interval: 5000, render: renderStatus},
    peers:   {title: 'Peers',   endpoint: 'api/peers',   interval: 5000, render: renderPeers},
    skills:  {title: 'Skills',  endpoint: 'api/skills',  interval: 10000, render: renderSkillsWrapper},
    wants:   {title: 'Wants',   endpoint: null,           interval: 60000, render: renderWants},
    tasks:   {title: 'Tasks',   endpoint: 'api/tasks',   interval: 5000, render: renderTasks},
    economy: {title: 'Economy', endpoint: 'api/economy',  interval: 30000, render: renderEconomy},
    assets:  {title: 'Assets',  endpoint: 'api/assets',   interval: 15000, render: renderAssets},
    secrets:   {title: 'Secrets',   endpoint: 'api/secrets',    interval: 30000, render: renderSecrets},
    messages:  {title: 'Messages',  endpoint: 'api/messages',   interval: 10000, render: renderMessages},
    exposures: {title: 'Exposures', endpoint: 'api/exposures', interval: 30000, render: renderExposures},
};

function switchTab(n) {
    if (!TABS[n]) return;
    activeTab = n;
    if (activeTab === 'skills') currentView = 'list';
    document.querySelectorAll('.nav-item').forEach(el => el.classList.toggle('active', el.dataset.tab === n));
    document.getElementById('tab-title').textContent = TABS[n].title;
    if (pollTimer) clearInterval(pollTimer);
    poll();
    pollTimer = setInterval(poll, TABS[n].interval);
}

async function poll() {
    if (activeTab === 'skills' && (currentView === 'execute' || currentView === 'detail')) return;
    if (activeTab === 'wants' && document.getElementById('want-form')?.style.display !== 'none') return;
    const c = TABS[activeTab], ind = document.getElementById('connection-status');
    try {
        if (c.endpoint) {
            const r = await fetch(c.endpoint, {headers: authHeaders()});
            if (r.status === 401) { showAuthPrompt(); return; }
            if (!r.ok) throw new Error(r.statusText);
            const d = await r.json();
            ind.textContent = '\u25cf Online'; ind.className = 'status-indicator online';
            if (activeTab === 'status') {
                document.getElementById('node-id-short').textContent = d.node_id.substring(0, 8);
                statusData = d;
            }
            c.render(d);
        } else {
            ind.textContent = '\u25cf Online'; ind.className = 'status-indicator online';
            c.render(null);
        }
    } catch (e) {
        ind.textContent = '\u25cb Offline'; ind.className = 'status-indicator offline';
        console.error(e);
    }
}

function showAuthPrompt() {
    const ind = document.getElementById('connection-status');
    ind.textContent = '\u25cb Auth required'; ind.className = 'status-indicator offline';
    const c = document.getElementById('tab-content');
    c.innerHTML = '<div style="max-width:360px;margin:60px auto;text-align:center">'
        + '<h2 style="margin-bottom:8px">Authentication Required</h2>'
        + '<p class="text-muted" style="margin-bottom:20px">Enter your cockpit auth token to connect.</p>'
        + '<input type="password" id="auth-input" placeholder="Auth token" style="width:100%;padding:8px;margin-bottom:12px;border:1px solid var(--border-color);border-radius:4px;background:var(--card-bg);color:var(--text-color)">'
        + '<button id="auth-btn" class="btn-primary" style="width:100%;padding:8px">Connect</button>'
        + '<p id="auth-err" style="color:var(--danger-color);margin-top:10px;display:none">Invalid token</p>'
        + '</div>';
    const inp = document.getElementById('auth-input');
    const btn = document.getElementById('auth-btn');
    const err = document.getElementById('auth-err');
    async function tryAuth() {
        const token = inp.value.trim();
        if (!token) return;
        btn.disabled = true; err.style.display = 'none';
        const r = await fetch('api/status', {headers: {'Authorization': 'Bearer ' + token}});
        if (r.ok) {
            localStorage.setItem('knarr_auth_token', token);
            poll();
        } else {
            err.style.display = 'block';
            btn.disabled = false;
            inp.focus();
        }
    }
    btn.addEventListener('click', tryAuth);
    inp.addEventListener('keydown', e => { if (e.key === 'Enter') tryAuth(); });
    inp.focus();
}

function esc(s) {
    if (s === null || s === undefined) return '';
    const d = document.createElement('div');
    d.appendChild(document.createTextNode(String(s)));
    return d.innerHTML;
}

function authHeaders() {
    const t = localStorage.getItem('knarr_auth_token') || '';
    return t ? {'Authorization': `Bearer ${t}`} : {};
}

function formatUptime(s) {
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return (d ? d + 'd ' : '') + (h ? h + 'h ' : '') + (m ? m + 'm ' : '') + (s % 60) + 's';
}

function relativeTime(t) {
    if (!t) return 'never';
    const s = Math.floor(Date.now() / 1000 - t);
    if (s < 60) return s + 's ago';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    if (s < 86400) return Math.floor(s / 3600) + 'h ago';
    return Math.floor(s / 86400) + 'd ago';
}

function renderStatus(d) {
    const hasUpdate = d.latest_network_version && d.latest_network_version !== d.version;
    const versionHtml = hasUpdate
        ? `${esc(d.version)} <span style="color:var(--warning-color)">\u2192 ${esc(d.latest_network_version)} available</span>`
        : esc(d.version);
    let banner = '';
    if (d.version_gated) {
        banner = `<div style="background:var(--danger-color);color:#fff;padding:0.75rem;border-radius:6px;margin-bottom:1rem">Node version ${esc(d.version)} is below network minimum. Update: <code>pip install --upgrade --force-reinstall git+https://github.com/knarrnet/knarr.git</code></div>`;
    }
    if (d.upgrading) {
        banner += `<div style="background:var(--warning-color);color:#000;padding:0.75rem;border-radius:6px;margin-bottom:1rem">Node is upgrading... Tasks will receive RETRY_AFTER until complete.</div>`;
    }
    const walletHtml = d.wallet ? `<div class="card"><div class="card-label">Wallet</div><div class="card-value mono" style="font-size:0.75rem" title="${esc(d.wallet)}">${esc(d.wallet.slice(0,8))}...${esc(d.wallet.slice(-4))}</div></div>` : '';
    document.getElementById('tab-content').innerHTML = `${banner}
<div class="card-grid summary-cards">
<div class="card summary-card"><div class="card-value accent">${d.peer_count}</div><div class="card-label">Peers</div></div>
<div class="card summary-card"><div class="card-value accent">${d.network_skill_count || 0}</div><div class="card-label">Network Skills</div></div>
<div class="card summary-card"><div class="card-value accent">${d.skill_count}</div><div class="card-label">Local Skills</div></div>
<div class="card summary-card"><div class="card-value accent">${d.task_count || 0}</div><div class="card-label">Tasks</div></div>
<div class="card summary-card"><div class="card-value accent">${formatUptime(d.uptime_seconds)}</div><div class="card-label">Uptime</div></div>
</div>
<div class="card-grid">
<div class="card"><div class="card-label">Node ID</div><div class="card-value mono">${esc(d.node_id.substring(0, 16))}...</div></div>
<div class="card"><div class="card-label">Version</div><div class="card-value">${versionHtml}</div></div>
<div class="card"><div class="card-label">Network</div><div class="card-value">${esc(d.advertise_host)}:${d.port}</div></div>
<div class="card"><div class="card-label">Task Slots</div><div class="card-value">${d.task_slots.used}/${d.task_slots.total}</div></div>
${walletHtml}
</div>`;
}

let _sortState = {};
function sortableTable(containerId, data, columns, emptyMsg) {
    const c = document.getElementById(containerId);
    if (!data.length) { c.innerHTML = `<p class="text-muted">${emptyMsg || 'None.'}</p>`; return; }
    const st = _sortState[containerId] || {col: null, asc: true, filter: ''};
    _sortState[containerId] = st;
    let filtered = data;
    if (st.filter) {
        const q = st.filter.toLowerCase();
        filtered = data.filter(row => columns.some(col => {
            const v = col.raw ? col.raw(row) : col.val(row);
            return String(v).toLowerCase().includes(q);
        }));
    }
    if (st.col !== null) {
        const col = columns[st.col];
        const key = col.sort || col.raw || col.val;
        filtered.sort((a, b) => {
            let va = key(a), vb = key(b);
            if (typeof va === 'string') va = va.toLowerCase();
            if (typeof vb === 'string') vb = vb.toLowerCase();
            if (va < vb) return st.asc ? -1 : 1;
            if (va > vb) return st.asc ? 1 : -1;
            return 0;
        });
    }
    let h = `<input type="text" class="table-filter" placeholder="Filter..." value="${esc(st.filter)}">`;
    h += '<table><thead><tr>';
    columns.forEach((col, i) => {
        const arrow = st.col === i ? (st.asc ? ' \u25b2' : ' \u25bc') : '';
        h += `<th class="sortable-th" data-ci="${i}">${esc(col.label)}${arrow}</th>`;
    });
    h += '</tr></thead><tbody>';
    filtered.forEach(row => {
        h += '<tr>';
        columns.forEach(col => h += `<td${col.cls ? ' class="' + col.cls + '"' : ''}>${col.val(row)}</td>`);
        h += '</tr>';
    });
    h += '</tbody></table>';
    c.innerHTML = h;
    c.querySelectorAll('.sortable-th').forEach(th => {
        th.addEventListener('click', () => {
            const ci = parseInt(th.dataset.ci);
            if (st.col === ci) st.asc = !st.asc;
            else { st.col = ci; st.asc = true; }
            sortableTable(containerId, data, columns, emptyMsg);
        });
    });
    const fi = c.querySelector('.table-filter');
    if (fi) fi.addEventListener('input', e => { st.filter = e.target.value; sortableTable(containerId, data, columns, emptyMsg); });
}

function renderPeers(d) {
    sortableTable('tab-content', d, [
        {label: 'Node ID', val: p => `<span class="mono">${esc(p.node_id.substring(0, 16))}</span>`, raw: p => p.node_id},
        {label: 'Host:Port', val: p => `<span class="mono">${esc(p.host)}:${p.port}</span>`, raw: p => p.host},
        {label: 'Last', val: p => { const s = Math.floor(Date.now()/1000 - p.last_seen); const col = s<60?'var(--success-color)':(s<300?'var(--warning-color)':'var(--danger-color)'); return `<span style="color:${col}">${relativeTime(p.last_seen)}</span>`; }, sort: p => p.last_seen},
        {label: 'Load', val: p => p.load >= 0 ? String(p.load) : '-', sort: p => p.load >= 0 ? p.load : 999},
    ], 'No peers.');
}

function renderSkillsWrapper(d) { if (currentView === 'list') renderSkills(d); }

function buildSkillTree(skills) {
    const tree = {};
    for (const s of skills) {
        const uri = s.uri || '';
        const path = uri.replace(/^knarr:\/\/\/?[a-f0-9]*\/?/, '').replace(/@.*$/, '');
        const parts = path ? path.split('/') : ['uncategorized'];
        let node = tree;
        for (const p of parts.slice(0, -1)) {
            if (!node[p]) node[p] = {_children: {}, _skills: []};
            node = node[p]._children;
        }
        const leaf = parts[parts.length - 1] || s.name;
        if (!node[leaf]) node[leaf] = {_children: {}, _skills: []};
        node[leaf]._skills.push(s);
    }
    return tree;
}

function renderTreeNode(name, node, depth, allSkills) {
    let h = '';
    const indent = depth * 16;
    const skills = node._skills || [];
    const children = node._children || {};
    const childKeys = Object.keys(children);
    const totalSkills = skills.length + childKeys.reduce((sum, k) => sum + countTreeSkills(children[k]), 0);
    if (totalSkills > 0 || skills.length > 0) {
        h += `<div style="margin-left:${indent}px;margin-bottom:4px"><strong style="color:var(--accent-color)">${esc(name)}</strong> <span class="text-muted">(${totalSkills})</span></div>`;
    }
    for (const s of skills) {
        const idx = allSkills.indexOf(s);
        h += `<div style="margin-left:${indent + 16}px;padding:4px 0;border-bottom:1px solid var(--border-color)"><a href="#" class="skill-link mono" data-detail="${idx}"><strong>${esc(s.name)}</strong></a> <span class="text-muted">v${esc(s.version)}</span> <span class="text-muted">(${s.providers.length} providers)</span> <button class="btn-primary btn-sm" data-idx="${idx}">Execute</button><br><small class="text-muted">${esc(s.description)}</small></div>`;
    }
    for (const k of childKeys.sort()) h += renderTreeNode(k, children[k], depth + 1, allSkills);
    return h;
}

function countTreeSkills(node) {
    let count = (node._skills || []).length;
    for (const k of Object.keys(node._children || {})) count += countTreeSkills(node._children[k]);
    return count;
}

function renderSkills(d) {
    const c = document.getElementById('tab-content');
    networkSkillsCache = d.network || []; localSkillsCache = d.local || [];
    let h = '<h3>My Skills</h3>';
    if (!d.local.length) h += '<p class="text-muted">None.</p>';
    else {
        h += '<table><thead><tr><th>Name</th><th>Vis</th><th>Handler</th><th>Act</th></tr></thead><tbody>';
        d.local.forEach((s, i) => {
            h += `<tr><td class="mono">${esc(s.name)}</td><td><span class="badge badge-${esc(s.visibility)}">${esc(s.visibility)}</span></td><td class="text-muted mono">${esc(s.handler)}</td><td><button class="btn-primary btn-sm" data-local="${i}">Execute</button> <button class="btn-primary btn-sm" style="background:var(--danger-color)" data-remove="${esc(s.name)}">Remove</button></td></tr>`;
        });
        h += '</tbody></table>';
    }
    h += '<h3 style="margin-top:40px">Network Skills <button class="btn-primary btn-sm" id="skill-view-toggle" style="margin-left:10px">' + (skillViewMode === 'tree' ? 'Flat' : 'Tree') + '</button></h3>';
    if (!d.network.length) h += '<p class="text-muted">None.</p>';
    else if (skillViewMode === 'tree') {
        const tree = buildSkillTree(d.network);
        h += '<div id="skill-tree">';
        for (const k of Object.keys(tree).sort()) h += renderTreeNode(k, tree[k], 0, d.network);
        h += '</div>';
    } else {
        h += '<table><thead><tr><th>Name</th><th>Ver</th><th>Providers</th><th>Act</th></tr></thead><tbody>';
        d.network.forEach((s, i) => {
            h += `<tr><td class="mono"><a href="#" class="skill-link" data-detail="${i}"><strong>${esc(s.name)}</strong></a><br><small class="text-muted">${esc(s.description)}</small></td><td>${esc(s.version)}</td><td>${s.providers.length}</td><td><button class="btn-primary btn-sm" data-idx="${i}">Execute</button></td></tr>`;
        });
        h += '</tbody></table>';
    }
    h += '<h3 style="margin-top:40px">Install Skill</h3><div style="display:flex;gap:8px;align-items:end"><div class="form-group" style="flex:1;margin:0"><label>Source</label><input type="text" id="install-source" placeholder="./my-skill/ or git+https://..."></div><button class="btn-primary" id="install-btn">Install</button></div><div id="install-result" style="margin-top:10px"></div>';
    c.innerHTML = h;
    const toggleBtn = document.getElementById('skill-view-toggle');
    if (toggleBtn) toggleBtn.addEventListener('click', () => { skillViewMode = skillViewMode === 'tree' ? 'flat' : 'tree'; renderSkills(d); });
    c.querySelectorAll('button[data-local]').forEach(btn => btn.addEventListener('click', () => openExecutionPanel(localSkillsCache[btn.dataset.local].name, true)));
    c.querySelectorAll('button[data-idx]').forEach(btn => btn.addEventListener('click', () => openExecutionPanel(networkSkillsCache[btn.dataset.idx].name, false)));
    c.querySelectorAll('a.skill-link[data-detail]').forEach(a => a.addEventListener('click', e => { e.preventDefault(); openSkillDetail(networkSkillsCache[a.dataset.detail].name); }));
}

async function openSkillDetail(n) {
    currentView = 'detail'; currentSkill = n;
    const c = document.getElementById('tab-content');
    c.innerHTML = `<div class="loading">Loading ${esc(n)}...</div>`;
    try {
        const r = await fetch(`api/skills/${encodeURIComponent(n)}/schema`, {headers: authHeaders()});
        const s = await r.json(); currentDetailSchema = s;
        let h = `<div style="margin-bottom:20px"><a href="#" id="back-link">\u2190 Back</a></div><h2>${esc(s.name)}</h2>`;
        if (s.description) h += `<p class="text-muted">${esc(s.description)}</p>`;
        h += `<button class="btn-primary" id="detail-execute">Execute</button>`;
        c.innerHTML = h;
        document.getElementById('back-link').addEventListener('click', e => { e.preventDefault(); currentView = 'list'; poll(); });
        document.getElementById('detail-execute').addEventListener('click', () => openExecutionPanel(n, false));
    } catch(e) { c.innerHTML = `<div class="result-error">Error: ${esc(e.message)}</div><a href="#" id="back-link">\u2190 Back</a>`; }
}

async function openExecutionPanel(n, local) {
    currentView = 'execute'; currentSkill = n; isLocalExecution = !!local;
    const c = document.getElementById('tab-content');
    c.innerHTML = `<div class="loading">Loading ${esc(n)}...</div>`;
    try {
        const r = await fetch(`api/skills/${encodeURIComponent(n)}/schema`, {headers: authHeaders()});
        const s = await r.json(); currentSchema = s; currentProviders = s.providers || [];
        let h = `<div style="margin-bottom:16px"><a href="#" id="back-link">\u2190 Back</a></div><h2>Execute: ${esc(n)}</h2><div class="card" style="padding:20px">`;
        if (!isLocalExecution && currentProviders.length) {
            h += '<div class="form-group"><label>Provider</label><select id="exec-provider">';
            s.providers.forEach((p, i) => h += `<option value="${i}">${esc(p.host)}:${p.port} (load: ${p.load>=0?p.load:'?'})</option>`);
            h += '</select></div>';
        }
        h += '<div id="schema-form"></div><button id="exec-btn" class="btn-primary">Execute</button><div id="exec-result"></div></div>';
        c.innerHTML = h;
        schemaToForm('schema-form', s.input_schema);
        document.getElementById('back-link').addEventListener('click', e => { e.preventDefault(); currentView = 'list'; poll(); });
        document.getElementById('exec-btn').addEventListener('click', executeSkill);
    } catch(e) { c.innerHTML = `<div class="result-error">Error: ${esc(e.message)}</div>`; }
}

function renderSchemaField(k, spec, prefix) {
    if (typeof spec === 'string') spec = {type: spec};
    const type = (spec.type || 'string').toLowerCase(), id = `field-${prefix || ''}${k}`, label = esc(k.replace(/_/g, ' '));
    if (type === 'bool' || type === 'boolean') return `<div class="form-group"><label><input type="checkbox" id="${esc(id)}"> ${label}</label></div>`;
    if (type === 'number' || type === 'int' || type === 'float') return `<div class="form-group"><label>${label}</label><input type="number" id="${esc(id)}" step="${type==='int'?'1':'any'}"></div>`;
    if (type === 'file' || type === 'asset') return `<div class="form-group"><label>${label}</label><input type="file" id="${esc(id)}" data-type="file"></div>`;
    if (type.startsWith('enum:')) {
        let s = `<div class="form-group"><label>${label}</label><select id="${esc(id)}"><option value="">-- Select --</option>`;
        type.substring(5).split(',').forEach(o => s += `<option value="${esc(o.trim())}">${esc(o.trim())}</option>`);
        return s + '</select></div>';
    }
    return `<div class="form-group"><label>${label}</label>${/text|description|content|prompt|body/i.test(k)?`<textarea id="${esc(id)}" rows="4"></textarea>`:`<input type="text" id="${esc(id)}">`}</div>`;
}

function schemaToForm(containerId, schema) {
    const c = document.getElementById(containerId);
    if (!schema || !Object.keys(schema).length) { c.innerHTML = '<div class="form-group"><label>Input (JSON)</label><textarea id="field-__raw_json" rows="6" placeholder="{}"></textarea></div>'; return; }
    let h = ''; for (const [k, s] of Object.entries(schema)) h += renderSchemaField(k, s, '');
    c.innerHTML = h;
}

async function executeSkill() {
    const btn = document.getElementById('exec-btn'), res = document.getElementById('exec-result'), input = {};
    try {
        const raw = collectFormValues(currentSchema.input_schema, '');
        for (const [k, v] of Object.entries(raw)) {
            if (v instanceof HTMLElement && v.dataset.type === 'file' && v.files.length) {
                res.innerHTML = `<div class="loading">Uploading ${esc(v.files[0].name)}...</div>`;
                const h = await uploadFile(v.files[0]); input[k] = `knarr-asset://${h}`;
            } else input[k] = v;
        }
        const p = currentProviders[document.getElementById('exec-provider')?.value || 0];
        btn.disabled = true; res.innerHTML = '<div class="loading">Executing...</div>';
        const r = await fetch('api/execute', {
            method: 'POST', headers: {'Content-Type': 'application/json', ...authHeaders()},
            body: JSON.stringify({skill: currentSkill, provider: p, input, timeout: 60})
        });
        
        if (r.status === 202) {
            const j = await r.json();
            pollJob(j.job_id, p);
            return;
        }
        
        renderResult(await r.json(), p || {});
    } catch (e) { res.innerHTML = `<div class="result-error">Error: ${esc(e.message)}</div>`; } finally { btn.disabled = false; }
}

async function pollJob(jobId, p) {
    const res = document.getElementById('exec-result');
    const btn = document.getElementById('exec-btn');
    
    // Elder review: Poll is LOCAL ONLY — hits async_jobs SQLite table via cockpit. 
    // Do NOT poll over network in future consumer-side implementation.
    const poll = async () => {
        try {
            const r = await fetch(`api/jobs/${jobId}`, {headers: authHeaders()});
            if (!r.ok) return;
            const j = await r.json();
            if (j.status === 'completed' || j.status === 'failed') {
                const resR = await fetch(`api/jobs/${jobId}/result`, {headers: authHeaders()});
                renderResult(await resR.json(), p || {});
                btn.disabled = false;
                return;
            }
            res.innerHTML = `<div class="loading">Queued (position ${j.position})...</div>`;
            setTimeout(poll, 5000);
        } catch (e) { res.innerHTML = `<div class="result-error">Polling error: ${esc(e.message)}</div>`; btn.disabled = false; }
    };
    poll();
}

function collectFormValues(schema, prefix) {
    const data = {};
    if (!schema || !Object.keys(schema).length) {
        const raw = document.getElementById('field-__raw_json');
        return raw ? JSON.parse(raw.value || '{}') : {};
    }
    for (const k of Object.keys(schema)) {
        const el = document.getElementById(`field-${prefix}${k}`); if (!el) continue;
        if (el.type === 'checkbox') data[k] = el.checked;
        else if (el.type === 'number') data[k] = el.value === '' ? 0 : parseFloat(el.value);
        else if (el.dataset?.type === 'file') data[k] = el;
        else data[k] = el.value;
    }
    return data;
}

async function uploadFile(f) {
    const p = currentProviders[document.getElementById('exec-provider').value];
    const b = await f.arrayBuffer();
    const r = await fetch(`api/upload?host=${encodeURIComponent(p.host)}&sidecar_port=${p.sidecar_port}`, {
        method: 'POST', headers: authHeaders(), body: b
    });
    if (!r.ok) throw new Error('Upload failed');
    return (await r.json()).hash;
}

function renderResult(r, p) {
    const c = document.getElementById('exec-result');
    if (r.status === 'failed') {
        const err = r.error || {};
        c.innerHTML = `<div class="result-error"><strong>${esc(err.code || 'ERR')}</strong>: ${esc(err.message || 'Unknown')}</div>`;
        return;
    }
    let h = `<div class="result-success"><div class="result-meta">Done in ${r.wall_time_ms || '?'}ms</div>`;
    const o = r.output_data || r.output || {};
    for (const [k, v] of Object.entries(o)) {
        const isA = typeof v === 'string' && (v.startsWith('knarr-asset://') || (v.length === 64 && /^[0-9a-f]+$/.test(v)));
        h += `<div class="result-field"><span class="result-label">${esc(k)}</span>`;
        if (isA) {
            const ha = v.startsWith('knarr-asset://') ? v.substring(14) : v;
            h += `<a href="api/assets/${encodeURIComponent(ha)}?host=${esc(p.host)}&sidecar_port=${p.sidecar_port}" class="btn-download" download>Download</a>`;
        } else if (typeof v === 'object' && v !== null) h += `<pre>${esc(JSON.stringify(v, null, 2))}</pre>`;
        else h += `<span class="result-value">${esc(String(v))}</span>`;
        h += '</div>';
    }
    c.innerHTML = h + '</div>';
}

function renderTasks(d) {
    sortableTable('tab-content', d, [
        {label: 'Time', val: t => `<span class="text-muted">${relativeTime(t.created_at)}</span>`, sort: t => t.created_at || 0},
        {label: 'Skill', val: t => `<span class="mono">${esc(t.skill_name)}</span>`, raw: t => t.skill_name},
        {label: 'Status', val: t => { const col = t.status==='completed'?'var(--success-color)':(t.status==='failed'?'var(--danger-color)':''); return `<span style="color:${col}">${esc(t.status)}</span>`; }, raw: t => t.status},
        {label: 'Duration', val: t => t.wall_time_ms ? t.wall_time_ms + 'ms' : '-', sort: t => t.wall_time_ms || 0},
        {label: 'Input', val: t => `<span class="text-muted">${t.input_size_bytes || 0}B</span>`, sort: t => t.input_size_bytes || 0},
    ], 'None.');
}

function renderEconomy(d) {
    const c = document.getElementById('tab-content');
    const s = d.summary || {}, peers = d.peers || [];
    let h = `<div class="card-grid summary-cards">
<div class="card summary-card"><div class="card-value accent">${s.net_position || 0}</div><div class="card-label">Net Position</div></div>
<div class="card summary-card"><div class="card-value accent">${peers.length}</div><div class="card-label">Counterparties</div></div>
</div><h3>Bilateral Positions</h3><div id="economy-table"></div>`;
    c.innerHTML = h;
    sortableTable('economy-table', peers, [
        {label: 'Peer', val: p => `<span class="mono">${esc(p.node_id)}</span>`, raw: p => p.node_id},
        {label: 'Balance', val: p => { const col = p.balance < 0 ? 'var(--danger-color)' : (p.balance > 0 ? 'var(--success-color)' : ''); return `<span style="color:${col}">${p.balance.toFixed(1)}</span>`; }, sort: p => p.balance},
        {label: 'Tasks', val: p => `P:${p.tasks_provided} C:${p.tasks_consumed}`},
        {label: 'Last', val: p => `<span class="text-muted">${relativeTime(p.last_activity)}</span>`, sort: p => p.last_activity || 0},
    ], 'No peer relationships.');
}

function renderAssets(d) {
    const c = document.getElementById('tab-content');
    if (!d.enabled) { c.innerHTML = '<p class="text-muted">Sidecar not enabled.</p>'; return; }
    sortableTable('tab-content', d.assets || [], [
        {label: 'Hash', val: a => `<span class="mono">${esc(a.hash.substring(0, 16))}...</span>`, raw: a => a.hash},
        {label: 'Size', val: a => (a.size / 1024).toFixed(1) + 'KB', sort: a => a.size},
        {label: 'Uploaded', val: a => `<span class="text-muted">${relativeTime(a.uploaded_at)}</span>`, sort: a => a.uploaded_at || 0},
        {label: 'Actions', val: a => `<a href="api/assets/${esc(a.hash)}" class="btn-download" download>Download</a>`},
    ], 'No assets.');
}

function renderSecrets(d) {
    const c = document.getElementById('tab-content');
    const skills = Object.keys(d);
    if (!skills.length) { c.innerHTML = '<p class="text-muted">No secrets.</p>'; return; }
    let h = '';
    skills.forEach(skill => {
        h += `<div class="card" style="margin-bottom:15px;padding:20px"><h3 class="mono">${esc(skill)}</h3><table><thead><tr><th>Key</th><th>Value</th></tr></thead><tbody>`;
        Object.keys(d[skill]).forEach(k => h += `<tr><td class="mono">${esc(k)}</td><td><code>${esc(d[skill][k].masked)}</code></td></tr>`);
        h += '</tbody></table></div>';
    });
    c.innerHTML = h;
}

function renderExposures(d) {
    const c = document.getElementById('tab-content');
    const exps = d.exposures || [];
    if (!exps.length) { c.innerHTML = '<p class="text-muted">No exposures.</p>'; return; }
    let h = '<table><thead><tr><th>Name</th><th>Skill</th><th>Path</th><th>Rate</th></tr></thead><tbody>';
    exps.forEach(e => h += `<tr><td class="mono">${esc(e.name)}</td><td>${esc(e.skill)}</td><td><a href="s/${esc(e.path)}" target="_blank">/s/${esc(e.path)}</a></td><td>${e.rate_limit}/min</td></tr>`);
    c.innerHTML = h + '</tbody></table>';
}

function renderMessages(d) {
    const msgs = d.messages || [];
    const unread = msgs.filter(m => m.status === 'unread').length;
    TABS.messages.title = `Messages${unread ? ` (${unread})` : ''}`;
    document.querySelector('.nav-item[data-tab="messages"]').textContent = TABS.messages.title;

    sortableTable('tab-content', msgs, [
        {label: 'Time', val: m => `<span class="text-muted">${relativeTime(m.timestamp)}</span>`, sort: m => m.timestamp},
        {label: 'From', val: m => `<span class="mono">${esc(m.from_node.substring(0, 16))}</span>`, raw: m => m.from_node},
        {label: 'Type', val: m => `<span class="badge">${esc(m.msg_type)}</span>`, raw: m => m.msg_type},
        {label: 'Subject/Preview', val: m => {
            const body = m.body || '';
            const preview = body.length > 50 ? body.substring(0, 50) + '...' : body;
            const style = m.status === 'unread' ? 'font-weight:bold' : '';
            return `<div style="cursor:pointer;${style}" onclick="expandMessage('${m.message_id}')">${esc(preview)}</div>`;
        }, raw: m => m.body},
        {label: 'Status', val: m => esc(m.status), raw: m => m.status},
        {label: 'Act', val: m => m.status === 'unread' ? `<button class="btn-primary btn-sm" onclick="ackMessage('${m.message_id}')">Mark Read</button>` : ''},
    ], 'No messages.');
}

window.expandMessage = async (id) => {
    const r = await fetch(`api/messages/${id}`, {headers: authHeaders()});
    const j = await r.json();
    const m = j.message || j;
    alert(`From: ${m.from_node}\nTime: ${new Date(m.timestamp * 1000).toLocaleString()}\n\n${m.body}`);
    if (m.status === 'unread') ackMessage(id);
};

window.ackMessage = async (id) => {
    await fetch('api/messages/ack', {
        method: 'POST', headers: {'Content-Type': 'application/json', ...authHeaders()},
        body: JSON.stringify({message_ids: [id]})
    });
    poll();
};

// Wants tab — polls bootstrap mailbox for wanted-skill posts
let wantsCache = null;
let wantsLoading = false;

async function loadNetworkWants() {
    try {
        const r = await fetch('api/execute', {
            method: 'POST',
            headers: {'Content-Type': 'application/json', ...authHeaders()},
            body: JSON.stringify({
                skill: 'knarr-mail',
                input: {action: 'poll', filters: {type: 'wanted', status: 'all'}, limit: 50},
                timeout: 30
            })
        });
        if (!r.ok) return [];
        const j = await r.json();
        return (j.output_data || j.output || {}).messages || [];
    } catch(e) { return []; }
}

async function renderWants() {
    const c = document.getElementById('tab-content');
    if (!statusData) return;
    if (wantsLoading) return;
    // Show cached data or loading state
    if (!wantsCache) {
        c.innerHTML = '<div class="loading">Loading network wants...</div>';
        wantsLoading = true;
        wantsCache = await loadNetworkWants();
        wantsLoading = false;
    }
    const wants = wantsCache || [];
    let h = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">';
    h += '<h3 style="margin:0">Network Wants</h3>';
    h += '<div><button class="btn-primary btn-sm" id="wants-refresh">Refresh</button> <button class="btn-primary btn-sm" id="wants-post">Post Want</button></div>';
    h += '</div>';
    if (!wants.length) {
        h += '<p class="text-muted">No wanted skills posted to the network yet.</p>';
    } else {
        h += '<table><thead><tr><th>Description</th><th>Suggested URI</th><th>From</th><th>Posted</th></tr></thead><tbody>';
        wants.forEach(w => {
            const content = typeof w.content === 'string' ? JSON.parse(w.content || '{}') : (w.content || {});
            h += '<tr>';
            h += `<td>${esc(content.description || w.content || '')}</td>`;
            h += `<td class="mono text-muted">${esc(content.uri || '-')}</td>`;
            h += `<td class="mono">${esc((w.from || '').substring(0, 12))}...</td>`;
            h += `<td class="text-muted">${relativeTime(w.timestamp)}</td>`;
            h += '</tr>';
        });
        h += '</tbody></table>';
    }
    // Post want form (hidden by default)
    h += '<div id="want-form" style="display:none;margin-top:20px;padding:16px;border:1px solid var(--border-color);border-radius:8px">';
    h += '<h3>Post a Want</h3>';
    h += '<div class="form-group"><label>Description <span style="color:var(--danger-color)">*</span></label><textarea id="want-desc" rows="3" placeholder="What skill do you need?"></textarea></div>';
    h += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">';
    h += '<div class="form-group"><label>Suggested URI</label><input type="text" id="want-uri" placeholder="knarr:///category/name"></div>';
    h += '<div class="form-group"><label>Bounty (credits)</label><input type="number" id="want-bounty" value="0" min="0"></div>';
    h += '</div>';
    h += '<button class="btn-primary" id="want-submit">Submit</button> <span id="want-status" class="text-muted"></span>';
    h += '</div>';
    c.innerHTML = h;
    // Bind refresh
    document.getElementById('wants-refresh').addEventListener('click', async () => {
        wantsCache = null;
        renderWants();
    });
    // Bind post toggle
    document.getElementById('wants-post').addEventListener('click', () => {
        const form = document.getElementById('want-form');
        form.style.display = form.style.display === 'none' ? 'block' : 'none';
    });
    // Bind submit
    const submitBtn = document.getElementById('want-submit');
    if (submitBtn) submitBtn.addEventListener('click', async () => {
        const desc = document.getElementById('want-desc').value.trim();
        if (!desc) return;
        const uri = document.getElementById('want-uri').value.trim();
        const bounty = parseInt(document.getElementById('want-bounty').value) || 0;
        const wantStatus = document.getElementById('want-status');
        submitBtn.disabled = true;
        wantStatus.textContent = 'Posting...';
        try {
            const content = JSON.stringify({type: 'wanted', description: desc, uri: uri || undefined, bounty});
            const r = await fetch('api/execute', {
                method: 'POST',
                headers: {'Content-Type': 'application/json', ...authHeaders()},
                body: JSON.stringify({
                    skill: 'knarr-mail',
                    input: {action: 'send', to: statusData.node_id, content, message_type: 'text'},
                    timeout: 30
                })
            });
            if (r.ok) {
                wantStatus.textContent = 'Posted!';
                document.getElementById('want-desc').value = '';
                document.getElementById('want-uri').value = '';
                wantsCache = null;
                setTimeout(() => renderWants(), 2000);
            } else {
                wantStatus.textContent = 'Failed to post.';
            }
        } catch(e) { wantStatus.textContent = 'Error: ' + e.message; }
        submitBtn.disabled = false;
    });
}

document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.nav-item').forEach(el => el.addEventListener('click', e => { e.preventDefault(); switchTab(el.dataset.tab); }));
    document.getElementById('logout-btn').addEventListener('click', () => {
        localStorage.removeItem('knarr_auth_token');
        if (pollTimer) clearInterval(pollTimer);
        showAuthPrompt();
    });
    // Fetch initial status for bootstrap_node_id cache
    fetch('api/status', {headers: authHeaders()}).then(r => r.ok ? r.json() : null).then(d => { if (d) statusData = d; }).catch(() => {});
    window.openExecutionPanel = openExecutionPanel; window.executeSkill = executeSkill; switchTab('status');
});
