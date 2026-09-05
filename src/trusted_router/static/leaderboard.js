"use strict";
(() => {
    const root = document.querySelector('[data-leaderboard]');
    if (!root)
        return;
    const controls = root.querySelector('[data-lb-controls]');
    const search = root.querySelector('[data-lb-search]');
    const provider = root.querySelector('[data-lb-provider]');
    const sort = root.querySelector('[data-lb-sort]');
    const evidence = root.querySelector('[data-lb-evidence]');
    const tabs = Array.from(root.querySelectorAll('[data-lb-tab]'));
    const panels = Array.from(root.querySelectorAll('[data-lb-panel]'));
    const previous = root.querySelector('[data-lb-prev]');
    const next = root.querySelector('[data-lb-next]');
    const count = root.querySelector('[data-lb-count]');
    const params = new URLSearchParams(location.search);
    let view = params.get('view') === 'models' ? 'models' : 'providers';
    let page = 0;
    const pageSize = 25;
    search.value = params.get('q') || '';
    for (const [name, select] of [['provider', provider], ['sort', sort], ['evidence', evidence]]) {
        const value = params.get(name);
        if (value !== null && Array.from(select.options).some(option => option.value === value))
            select.value = value;
    }
    const numeric = (row, key) => {
        const value = row.dataset[key];
        return value !== undefined && value !== '' && Number.isFinite(Number(value)) ? Number(value) : null;
    };
    const update = () => {
        const terms = search.value.toLowerCase().trim().split(/\s+/).filter(Boolean);
        for (const panel of panels) {
            panel.hidden = panel.dataset.lbPanel !== view;
            panel.setAttribute('role', 'tabpanel');
            if (panel.hidden)
                continue;
            const rows = Array.from(panel.querySelectorAll('[data-lb-row]'));
            const matches = rows.filter(row => {
                row.hidden = true;
                return terms.every(term => row.dataset.search.toLowerCase().includes(term))
                    && (!provider.value || row.dataset.provider === provider.value)
                    && (evidence.value !== 'qualified' || row.dataset.qualified === 'yes')
                    && (evidence.value !== 'warming' || row.dataset.qualified === 'no')
                    && (evidence.value !== 'config' || Number(row.dataset.config) > 0);
            });
            const direction = ['throughput', 'completion', 'samples'].includes(sort.value) ? -1 : 1;
            matches.sort((a, b) => {
                const left = numeric(a, sort.value), right = numeric(b, sort.value);
                if (left === null && right !== null)
                    return 1;
                if (left !== null && right === null)
                    return -1;
                return ((left ?? 0) - (right ?? 0)) * direction || a.dataset.search.localeCompare(b.dataset.search);
            });
            page = Math.min(page, Math.max(0, Math.ceil(matches.length / pageSize) - 1));
            const start = page * pageSize;
            const tbody = panel.querySelector('tbody');
            for (const [index, row] of matches.entries()) {
                tbody.append(row);
                row.hidden = index < start || index >= start + pageSize;
            }
            count.value = matches.length ? `${start + 1}-${Math.min(start + pageSize, matches.length)} of ${matches.length} ${view}` : `0 ${view}`;
            panel.querySelector('[data-lb-empty]').hidden = matches.length > 0 || rows.length === 0;
            previous.disabled = page === 0;
            next.disabled = start + pageSize >= matches.length;
        }
        for (const tab of tabs) {
            const selected = tab.dataset.lbTab === view;
            tab.setAttribute('aria-selected', String(selected));
            tab.tabIndex = selected ? 0 : -1;
        }
        const url = new URL(location.href);
        for (const [key, value] of [['view', view === 'models' ? view : ''], ['q', search.value], ['provider', provider.value], ['sort', sort.value === 'rank' ? '' : sort.value], ['evidence', evidence.value === 'all' ? '' : evidence.value]]) {
            if (value)
                url.searchParams.set(key, value);
            else
                url.searchParams.delete(key);
        }
        history.replaceState(null, '', url);
        root.querySelectorAll('[data-lb-window]').forEach(link => {
            const target = new URL(link.href);
            const windowValue = target.searchParams.get('window');
            target.search = url.search;
            if (windowValue)
                target.searchParams.set('window', windowValue);
            else
                target.searchParams.delete('window');
            link.href = target.href;
        });
    };
    for (const input of [search, provider, sort, evidence]) {
        input.addEventListener(input === search ? 'input' : 'change', () => { page = 0; update(); });
    }
    for (const [index, tab] of tabs.entries()) {
        tab.addEventListener('click', () => { view = tab.dataset.lbTab; page = 0; update(); });
        tab.addEventListener('keydown', event => {
            if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key))
                return;
            event.preventDefault();
            const target = event.key === 'Home' ? tabs[0] : event.key === 'End' ? tabs[tabs.length - 1] : tabs[(index + (event.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
            target.click();
            target.focus();
        });
    }
    previous.addEventListener('click', () => { page--; update(); });
    next.addEventListener('click', () => { page++; update(); });
    controls.hidden = false;
    root.querySelector('[data-lb-pagination]').hidden = false;
    update();
})();
