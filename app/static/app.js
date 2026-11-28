'use strict';
const $ = id => document.getElementById(id);
let data = {repos: [], services: [], meta: {}};
let editingId = null;
let editingTileId = null;
let setupMode = false;
let layoutMode = false;
let draggedServiceId = null;

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
    $('alerts').append(alert);
}

function refreshTileStyles() {
    let stylesheet = $('tileStylesheet');
    if (!stylesheet) {
        stylesheet = el('link');
        stylesheet.id = 'tileStylesheet';
        stylesheet.rel = 'stylesheet';
        document.head.append(stylesheet);
    }
    stylesheet.href = '/api/tiles.css?v=' + Date.now();
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

async function saveTileOrder(serviceIds) {
    data.services = data.services
        .map(service => ({...service, tile_position: serviceIds.indexOf(service.id)}))
        .sort((left, right) => left.tile_position - right.tile_position);
    render();
    try {
        await api('/api/tiles/order', {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({service_ids: serviceIds})
        });
    } catch (error) {
        showError(error.message);
        await refresh();
    }
}

function moveTileRelative(sourceId, targetId, after) {
    if (!layoutMode || sourceId === targetId) return;
    const serviceIds = data.services.map(service => service.id);
    const sourceIndex = serviceIds.indexOf(sourceId);
    const targetIndex = serviceIds.indexOf(targetId);
    if (sourceIndex < 0 || targetIndex < 0) return;
    serviceIds.splice(sourceIndex, 1);
    serviceIds.splice(serviceIds.indexOf(targetId) + Number(after), 0, sourceId);
    void saveTileOrder(serviceIds);
}

function moveTileBy(serviceId, offset) {
    const serviceIds = data.services.map(service => service.id);
    const currentIndex = serviceIds.indexOf(serviceId);
    const nextIndex = Math.max(0, Math.min(serviceIds.length - 1, currentIndex + offset));
    if (currentIndex < 0 || nextIndex === currentIndex) return;
    serviceIds.splice(currentIndex, 1);
    serviceIds.splice(nextIndex, 0, serviceId);
    void saveTileOrder(serviceIds);
}

function openTileDialog(service) {
    editingTileId = service.id;
    const form = $('tileForm');
    form.elements.width.value = service.tile_width;
    form.elements.height.value = service.tile_height;
    form.elements.color.value = service.tile_color;
    form.elements.shape.value = service.tile_shape;
    $('tileFormError').textContent = '';
    $('tileDialog').showModal();
}

function setView(view) {
    layoutMode = false;
    $('dashboardView').hidden = view !== 'dashboard';
    $('settingsView').hidden = view !== 'settings';
    $('layoutDoneButton').hidden = true;
    $('settingsButton').hidden = view === 'settings';
    $('layoutHint').hidden = true;
    $('appShell').classList.remove('layout-editing');
    render();
}

function deleteService(service) {
    if (!confirm(`Service „${service.name}“ wirklich löschen?`)) return;
    api('/api/services/' + service.id, {method: 'DELETE'})
        .then(refresh)
        .catch(error => showError(error.message));
}

function renderTile(service) {
    const card = el(
        'article',
        `service-tile tile--w${service.tile_width} tile--h${service.tile_height} tile--${service.tile_shape}`
    );
    card.id = `service-tile-${service.id}`;
    card.draggable = layoutMode;
    const mainLink = link(service.url, '', 'tile-main');
    mainLink.onclick = event => {
        if (layoutMode) event.preventDefault();
    };
    const heading = el('div', 'tile-heading');
    const identity = el('div');
    identity.append(
        el('div', 'tile-name', service.name),
        el('div', 'tile-repo', service.repo_full_name || 'Ohne Repository-Zuordnung')
    );
    heading.append(
        identity,
        el('span', 'status ' + service.status, ({
            online: 'Online',
            offline: 'Offline',
            degraded: 'Gestört',
            pending: 'Ausstehend'
        })[service.status] || service.status)
    );
    const footer = el('div', 'tile-footer');
    footer.append(
        el('span', service.error ? 'tile-error' : 'tile-meta', service.error
            || (service.last_checked ? `${service.latency_ms} ms · ${dateStr(service.last_checked)}` : 'Noch nicht geprüft')),
        el('span', 'tile-open', '↗')
    );
    mainLink.append(heading, footer);
    card.append(mainLink);

    if (layoutMode) {
        const controls = el('div', 'tile-controls');
        controls.append(el('span', 'tile-handle', '⠿ Ziehen'));
        const up = el('button', 'mini', '↑');
        up.type = 'button';
        up.title = 'Kachel nach oben verschieben';
        up.onclick = () => moveTileBy(service.id, -1);
        const down = el('button', 'mini', '↓');
        down.type = 'button';
        down.title = 'Kachel nach unten verschieben';
        down.onclick = () => moveTileBy(service.id, 1);
        controls.append(up, down);
        card.append(controls);

        card.addEventListener('dragstart', event => {
            draggedServiceId = service.id;
            card.classList.add('is-dragging');
            event.dataTransfer.effectAllowed = 'move';
            event.dataTransfer.setData('text/plain', String(service.id));
        });
        card.addEventListener('dragover', event => {
            if (draggedServiceId === null || draggedServiceId === service.id) return;
            event.preventDefault();
            event.dataTransfer.dropEffect = 'move';
            card.classList.add('drop-target');
        });
        card.addEventListener('dragleave', () => card.classList.remove('drop-target'));
        card.addEventListener('drop', event => {
            event.preventDefault();
            card.classList.remove('drop-target');
            if (draggedServiceId !== null) {
                const bounds = card.getBoundingClientRect();
                moveTileRelative(draggedServiceId, service.id, event.clientY > bounds.top + bounds.height / 2);
            }
        });
        card.addEventListener('dragend', () => {
            draggedServiceId = null;
            card.classList.remove('is-dragging');
            document.querySelectorAll('.service-tile').forEach(tile => tile.classList.remove('drop-target'));
        });
    }
    return card;
}

function renderServiceSettings(service) {
    const row = el('article', 'service-admin-row');
    const info = el('div', 'service-admin-info');
    const name = el('strong', '', service.name);
    const target = el('span', 'service-admin-target', hostLabel(service.url));
    const error = service.error ? el('span', 'service-admin-error', service.error) : null;
    info.append(name, target);
    if (error) info.append(error);
    const status = el('span', 'status ' + service.status, ({
        online: 'Online',
        offline: 'Offline',
        degraded: 'Gestört',
        pending: 'Ausstehend'
    })[service.status] || service.status);
    const actions = el('div', 'service-admin-actions');
    const design = el('button', 'mini', 'Kachel');
    design.type = 'button';
    design.onclick = () => openTileDialog(service);
    const edit = el('button', 'mini', 'Bearbeiten');
    edit.type = 'button';
    edit.onclick = () => openDialog(service);
    const remove = el('button', 'mini', 'Löschen');
    remove.type = 'button';
    remove.onclick = () => deleteService(service);
    actions.append(design, edit, remove);
    row.append(info, status, actions);
    return row;
}

function renderRepositories(repos) {
    const list = $('repoNavList');
    const query = $('repoNavSearch').value.toLowerCase().trim();
    const found = repos.filter(repo =>
        (repo.full_name + ' ' + (repo.description || '')).toLowerCase().includes(query));
    $('repoNavCount').textContent = repos.length;
    list.replaceChildren();
    if (!found.length) {
        list.append(el('div', 'repo-nav-empty', repos.length
            ? 'Keine passenden Repositories.'
            : 'Noch keine Repositories synchronisiert.'));
        return;
    }

    for (const repo of found) {
        const item = link(repo.url, '', 'repo-nav-link');
        item.title = repo.full_name;
        item.append(
            el('span', 'repo-nav-icon', '⌘'),
            el('span', 'repo-nav-name', repo.full_name)
        );
        list.append(item);
    }
}

function render() {
    const services = data.services || [];
    const repos = data.repos || [];
    $('repoCount').textContent = repos.length;
    $('serviceCount').textContent = services.length;
    $('onlineCount').textContent = services.filter(service => service.status === 'online').length;
    $('offlineCount').textContent = services.filter(service => ['offline', 'degraded'].includes(service.status)).length;
    $('serviceBadge').textContent = services.length;
    $('settingsMeta').textContent = `GitHub-Sync: ${dateStr(data.meta.github_sync)} · Letzter Check: ${data.meta.monitor ? new Date(data.meta.monitor).toLocaleString('de-DE') : '—'}`;
    renderRepositories(repos);

    $('alerts').replaceChildren();
    if (data.meta.github_error) showError(data.meta.github_error);
    if (data.meta.monitor_error) showError(data.meta.monitor_error);

    $('appShell').classList.toggle('layout-editing', layoutMode);
    $('layoutHint').hidden = !layoutMode;
    $('layoutDoneButton').hidden = !layoutMode;
    const dashboardServiceList = $('dashboardServiceList');
    dashboardServiceList.replaceChildren();
    if (!services.length) {
        const empty = el('div', 'dashboard-empty');
        empty.append(
            el('h1', '', 'Dein Dashboard'),
            el('p', '', 'Füge in den Einstellungen deinen ersten Service hinzu.'),
        );
        const settingsLink = el('button', 'btn primary', 'Einstellungen öffnen');
        settingsLink.type = 'button';
        settingsLink.onclick = () => setView('settings');
        empty.append(settingsLink);
        dashboardServiceList.append(empty);
    } else {
        for (const service of services) dashboardServiceList.append(renderTile(service));
    }

    const serviceAdminList = $('serviceAdminList');
    serviceAdminList.replaceChildren();
    const filteredServices = services.filter(service =>
        $('statusFilter').value === 'all' || $('statusFilter').value === service.status);
    if (!filteredServices.length) {
        showEmpty(serviceAdminList, services.length ? 'Keine Services mit diesem Status.' : 'Noch keine Services konfiguriert.');
    } else {
        for (const service of filteredServices) serviceAdminList.append(renderServiceSettings(service));
    }

}

function showEmpty(parent, text) {
    parent.replaceChildren(el('div', 'empty', text));
}

async function refresh() {
    try {
        data = await api('/api/dashboard');
        if (!$('tileStylesheet')) refreshTileStyles();
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
$('repoNavToggle').onclick = () => {
    const expanded = $('repoNavToggle').getAttribute('aria-expanded') !== 'true';
    $('repoNavToggle').setAttribute('aria-expanded', String(expanded));
    $('repoNavPanel').hidden = !expanded;
    if (expanded) $('repoNavSearch').focus();
};
$('repoNavSearch').oninput = () => renderRepositories(data.repos || []);
$('settingsButton').onclick = () => setView('settings');
$('backButton').onclick = () => setView('dashboard');
$('layoutButton').onclick = () => {
    layoutMode = true;
    $('dashboardView').hidden = false;
    $('settingsView').hidden = true;
    $('settingsButton').hidden = true;
    $('layoutDoneButton').hidden = false;
    render();
};
$('layoutDoneButton').onclick = () => {
    layoutMode = false;
    $('settingsButton').hidden = false;
    render();
};
$('closeTileDialog').onclick = $('cancelTileDialog').onclick = () => $('tileDialog').close();
$('tileForm').onsubmit = async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const settings = {
        width: Number(form.elements.width.value),
        height: Number(form.elements.height.value),
        color: form.elements.color.value,
        shape: form.elements.shape.value
    };
    const submit = form.querySelector('button[type="submit"]');
    submit.disabled = true;
    $('tileFormError').textContent = '';
    try {
        await api(`/api/services/${editingTileId}/tile`, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(settings)
        });
        const service = data.services.find(item => item.id === editingTileId);
        Object.assign(service, {
            tile_width: settings.width,
            tile_height: settings.height,
            tile_color: settings.color,
            tile_shape: settings.shape
        });
        $('tileDialog').close();
        refreshTileStyles();
        render();
    } catch (error) {
        $('tileFormError').textContent = error.message;
    } finally {
        submit.disabled = false;
    }
};
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
