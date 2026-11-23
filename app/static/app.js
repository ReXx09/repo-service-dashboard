'use strict';
const $ = id => document.getElementById(id);
let data = {repos: [], services: [], meta: {}};
let editingId = null;
let setupMode = false;

const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
};

function dateStr(value) {
    if (!value) return '—';
    const date = new Date(value);
    return Number.isNaN(date.getTime())
        ? '—'
        : new Intl.DateTimeFormat('de-DE', {day: '2-digit', month: 'short', year: 'numeric'}).format(date);
}

function hostLabel(url) {
    try {
        const parsed = new URL(url);
        return parsed.host + (parsed.pathname === '/' ? '' : parsed.pathname);
    } catch {
        return url;
    }
}

function safeLink(url) {
    try {
        const parsed = new URL(url);
        return ['http:', 'https:'].includes(parsed.protocol) ? parsed.href : null;
    } catch {
        return null;
    }
}

function link(url, text, className) {
    const anchor = el('a', className, text);
    const safe = safeLink(url);
    if (safe) {
        anchor.href = safe;
        anchor.target = '_blank';
        anchor.rel = 'noopener noreferrer';
    } else {
        anchor.removeAttribute('href');
        anchor.title = 'Keine gültige Web-Adresse';
    }
    return anchor;
}

async function api(path, options = {}) {
    const response = await fetch(path, {
        cache: 'no-store',
        ...options,
        headers: {'X-Dashboard-Request': '1', ...(options.headers || {})}
    });
    const json = await response.json().catch(() => ({}));
    if (!response.ok) {
        const error = new Error(typeof json.detail === 'string' ? json.detail : `HTTP ${response.status}`);
        error.status = response.status;
        throw error;
    }
    return json;
}

function showError(message) {
    const alert = el('div', 'alert', message);
    $('alerts').replaceChildren(alert);
}

function showAuth(requiredSetup) {
    setupMode = requiredSetup;
    $('appShell').hidden = true;
    $('authScreen').hidden = false;
    $('authEyebrow').textContent = requiredSetup ? 'ERSTEINRICHTUNG' : 'GESCHÜTZTER BEREICH';
    $('authTitle').replaceChildren(
        requiredSetup ? 'Willkommen' : 'Anmelden',
        el('span', 'accent', '.')
    );
    $('authDescription').textContent = requiredSetup
        ? 'Lege das einmalige Admin-Konto für dein Dashboard an.'
        : 'Melde dich mit deinem Admin-Konto an.';
    $('confirmPasswordWrap').hidden = !requiredSetup;
    $('authConfirmPassword').required = requiredSetup;
    $('authPassword').autocomplete = requiredSetup ? 'new-password' : 'current-password';
    $('authSubmit').textContent = requiredSetup ? 'Admin-Konto erstellen' : 'Anmelden';
    $('authError').textContent = '';
    $('authForm').reset();
    $('authUsername').focus();
}

function render() {
    const services = data.services || [];
    const repos = data.repos || [];
    $('repoCount').textContent = repos.length;
    $('serviceCount').textContent = services.length;
    $('onlineCount').textContent = services.filter(service => service.status === 'online').length;
    $('offlineCount').textContent = services.filter(service => ['offline', 'degraded'].includes(service.status)).length;
    $('serviceBadge').textContent = services.length;
    $('repoBadge').textContent = repos.length;
    $('updated').textContent = 'GitHub-Sync: ' + dateStr(data.meta.github_sync);
    $('lastMonitor').textContent = 'Letzter Check: ' + (data.meta.monitor ? new Date(data.meta.monitor).toLocaleString('de-DE') : '—');

    $('alerts').replaceChildren();
    if (data.meta.github_error) showError(data.meta.github_error);
    if (data.meta.monitor_error) showError(data.meta.monitor_error);

    const filtered = services.filter(service => $('statusFilter').value === 'all' || $('statusFilter').value === service.status);
    const serviceList = $('serviceList');
    serviceList.replaceChildren();
    if (!filtered.length) {
        showEmpty(serviceList, services.length
            ? 'Keine Services mit diesem Status.'
            : 'Noch keine Services. Mit „Service hinzufügen“ kannst du deinen ersten Endpunkt anlegen.');
    }

    for (const service of filtered) {
        const card = el('article', 'service-card');
        const top = el('div', 'svc-top');
        const identity = el('div');
        identity.append(
            el('div', 'svc-name', service.name),
            el('div', 'svc-sub', service.repo_full_name || 'Ohne Repository-Zuordnung')
        );
        top.append(
            identity,
            el('span', 'status ' + service.status, ({
                online: 'Online',
                offline: 'Offline',
                degraded: 'Gestört',
                pending: 'Ausstehend'
            })[service.status] || service.status)
        );
        card.append(top, link(service.url, hostLabel(service.url), 'svc-url'));

        const bottom = el('div', 'svc-bottom');
        const links = el('div', 'svc-links');
        links.append(link(service.url, '↗ Öffnen', 'mini'));
        if (service.repo_full_name) {
            const repo = repos.find(item => item.full_name === service.repo_full_name);
            if (repo) links.append(link(repo.url, 'GitHub', 'mini'));
        }

        const edit = el('button', 'mini', 'Bearbeiten');
        edit.onclick = () => openDialog(service);
        const remove = el('button', 'mini', '×');
        remove.title = 'Service löschen';
        remove.onclick = async () => {
            if (!confirm(`Service „${service.name}“ wirklich löschen?`)) return;
            try {
                await api('/api/services/' + service.id, {method: 'DELETE'});
                await refresh();
            } catch (error) {
                showError(error.message);
            }
        };
        links.append(edit, remove);
        bottom.append(
            el('div', 'meta', service.last_checked
                ? `${service.latency_ms} ms · ${dateStr(service.last_checked)}`
                : 'Noch nicht geprüft'),
            links
        );
        card.append(bottom);
        if (service.error) {
            const note = el('div', 'svc-sub', service.error);
            note.title = service.error;
            card.append(note);
        }
        serviceList.append(card);
    }

    const repoList = $('repoList');
    repoList.replaceChildren();
    const query = $('repoSearch').value.toLowerCase().trim();
    const found = repos.filter(repo => (repo.full_name + ' ' + (repo.description || '')).toLowerCase().includes(query));
    if (!found.length) {
        showEmpty(repoList, repos.length
            ? 'Keine passenden Repositories.'
            : 'Noch keine GitHub-Repositories. Bitte GITHUB_TOKEN hinterlegen und synchronisieren.');
    }

    for (const repo of found) {
        const item = el('div', 'repo-item');
        const left = el('div', 'repo-left');
        const info = el('div');
        info.append(
            el('div', 'repo-name', repo.full_name),
            el('div', 'repo-desc', repo.description || 'Keine Beschreibung')
        );
        left.append(el('span', 'repo-icon', '⌘'), info);
        const right = el('div', 'repo-right');
        right.append(el('span', 'tag', repo.visibility === 'private' ? 'Privat' : 'Öffentlich'));
        if (repo.language) right.append(el('span', 'tag', repo.language));
        right.append(el('span', 'repo-date', dateStr(repo.pushed_at)), link(repo.url, 'GitHub ↗', 'openlink'));
        item.append(left, right);
        repoList.append(item);
    }
}

function showEmpty(parent, text) {
    parent.replaceChildren(el('div', 'empty', text));
}

async function refresh() {
    try {
        data = await api('/api/dashboard');
        $('authScreen').hidden = true;
        $('appShell').hidden = false;
        render();
    } catch (error) {
        if (error.status === 401) {
            try {
                const status = await api('/api/auth/status');
                showAuth(status.setup_required);
            } catch (statusError) {
                showAuth(false);
                $('authError').textContent = 'Anmeldestatus konnte nicht geladen werden: ' + statusError.message;
            }
            return;
        }
        showError('Dashboard konnte nicht geladen werden: ' + error.message);
    }
}

async function initialize() {
    try {
        const status = await api('/api/auth/status');
        if (status.authenticated) {
            await refresh();
        } else {
            showAuth(status.setup_required);
        }
    } catch (error) {
        showAuth(false);
        $('authError').textContent = 'Anmeldestatus konnte nicht geladen werden: ' + error.message;
    }
}

function openDialog(service = null) {
    editingId = service ? service.id : null;
    $('dialogTitle').textContent = service ? 'Service bearbeiten' : 'Service hinzufügen';
    $('formError').textContent = '';
    const form = $('serviceForm');
    form.reset();
    const select = $('repoSelect');
    select.replaceChildren(new Option('Ohne Zuordnung', ''));
    for (const repo of data.repos) select.add(new Option(repo.full_name, repo.full_name));
    if (service) {
        form.elements.name.value = service.name;
        if (service.repo_full_name && !data.repos.some(repo => repo.full_name === service.repo_full_name)) {
            select.add(new Option(service.repo_full_name + ' (nicht mehr synchronisiert)', service.repo_full_name));
        }
        select.value = service.repo_full_name || '';
        form.elements.url.value = service.url;
        form.elements.check_url.value = service.check_url;
        form.elements.check_type.value = service.check_type;
    }
    $('serviceDialog').showModal();
}

$('authForm').onsubmit = async event => {
    event.preventDefault();
    const password = $('authPassword').value;
    if (setupMode && password !== $('authConfirmPassword').value) {
        $('authError').textContent = 'Die Passwörter stimmen nicht überein.';
        return;
    }
    const button = $('authSubmit');
    button.disabled = true;
    $('authError').textContent = '';
    try {
        await api(setupMode ? '/api/auth/setup' : '/api/auth/login', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({username: $('authUsername').value, password})
        });
        $('authScreen').hidden = true;
        $('appShell').hidden = false;
        await refresh();
    } catch (error) {
        $('authError').textContent = error.message;
    } finally {
        button.disabled = false;
    }
};

$('logoutButton').onclick = async () => {
    try {
        await api('/api/auth/logout', {method: 'POST'});
        showAuth(false);
    } catch (error) {
        showError(error.message);
    }
};

$('addButton').onclick = () => openDialog();
$('closeDialog').onclick = $('cancelDialog').onclick = () => $('serviceDialog').close();
$('statusFilter').onchange = render;
$('repoSearch').oninput = render;
$('checkType').onchange = () => {
    const form = $('serviceForm');
    const current = form.elements.check_url.value;
    if (form.elements.check_type.value === 'tcp' && current.startsWith('http')) {
        try {
            const url = new URL(current);
            form.elements.check_url.value = `tcp://${url.hostname}:${url.port || (url.protocol === 'https:' ? '443' : '80')}`;
        } catch {
        }
    }
};

$('serviceForm').onsubmit = async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const payload = {
        name: form.elements.name.value,
        repo_full_name: form.elements.repo_full_name.value || null,
        url: form.elements.url.value,
        check_url: form.elements.check_url.value,
        check_type: form.elements.check_type.value
    };
    try {
        await api(editingId ? `/api/services/${editingId}` : '/api/services', {
            method: editingId ? 'PUT' : 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        });
        $('serviceDialog').close();
        await refresh();
        await api('/api/check', {method: 'POST'});
        await refresh();
    } catch (error) {
        $('formError').textContent = error.message;
    }
};

async function action(button, path) {
    button.disabled = true;
    try {
        await api(path, {method: 'POST'});
        await refresh();
    } catch (error) {
        showError(error.message);
    } finally {
        button.disabled = false;
    }
}

$('syncButton').onclick = () => action($('syncButton'), '/api/sync');
$('checkButton').onclick = () => action($('checkButton'), '/api/check');

initialize();
setInterval(refresh, 30000);
