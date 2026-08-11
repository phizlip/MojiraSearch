const API_BASE = '/api';

const searchInput = document.getElementById('searchInput');
const projectSelect = document.getElementById('projectSelect');
const sortOrder = document.getElementById('sortOrder');
const openWith = document.getElementById('openWith');
const searchMode = document.getElementById('searchMode');
const searchBtn = document.getElementById('searchBtn');
const resultsContainer = document.getElementById('results');
const statusFooter = document.getElementById('statusFooter');

// reset inputs on load
window.onload = () => {
    searchMode.value = 'semantic';
    projectSelect.value = '';
    sortOrder.value = 'relevance';
    openWith.value = 'mojira';
    searchInput.value = '';
    searchInput.placeholder = 'Describe a bug report';
};

function getIssueUrl(key) {
    const tracker = openWith.value;
    if (tracker === 'bugs_legacy') return `https://bugs-legacy.mojang.com/browse/${key}`;
    if (tracker === 'bugs')        return `https://bugs.mojang.com/browse/${key}`;
    if (tracker === 'report')      return `https://report.bugs.mojang.com/servicedesk/customer/portal/2/${key}`;
    if (tracker === 'atlassian')   return `https://mojira.atlassian.net/browse/${key}`;
    return `https://mojira.dev/${key}`;
}
const searchWrapper = document.getElementById('searchWrapper');
const expandedProjects = new Set();
const loadingProjects = new Set();
let lastStatusData = null;
let lastFetchTime = null;

// Octicons
const SVG_CHEVRON_RIGHT = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12" width="12" height="12"><path d="M4.7 10c-.2 0-.4-.1-.5-.2-.3-.3-.3-.8 0-1.1L6.9 6 4.2 3.3c-.3-.3-.3-.8 0-1.1.3-.3.8-.3 1.1 0l3.3 3.2c.3.3.3.8 0 1.1L5.3 9.7c-.2.2-.4.3-.6.3Z"></path></svg>`;
const SVG_CHEVRON_DOWN = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12" width="12" height="12"><path d="M6 8.825c-.2 0-.4-.1-.5-.2l-3.3-3.3c-.3-.3-.3-.8 0-1.1.3-.3.8-.3 1.1 0l2.7 2.7 2.7-2.7c.3-.3.8-.3 1.1 0 .3.3.3.8 0 1.1l-3.2 3.2c-.2.2-.4.3-.6.3Z"></path></svg>`;
const SVG_DOT_FILL = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" width="16" height="16"><path d="M8 4a4 4 0 1 1 0 8 4 4 0 0 1 0-8Z"></path></svg>`;
const SVG_REFRESH = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" width="16" height="16"><path d="M1.705 8.005a.75.75 0 0 1 .834.656 5.5 5.5 0 0 0 9.592 2.97l-1.204-1.204a.25.25 0 0 1 .177-.427h3.646a.25.25 0 0 1 .25.25v3.646a.25.25 0 0 1-.427.177l-1.38-1.38A7.002 7.002 0 0 1 1.05 8.84a.75.75 0 0 1 .656-.834ZM8 2.5a5.487 5.487 0 0 0-4.131 1.869l1.204 1.204A.25.25 0 0 1 4.896 6H1.25A.25.25 0 0 1 1 5.75V2.104a.25.25 0 0 1 .427-.177l1.38 1.38A7.002 7.002 0 0 1 14.95 7.16a.75.75 0 0 1-1.49.178A5.5 5.5 0 0 0 8 2.5Z"></path></svg>`;
const SVG_HOURGLASS = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" width="16" height="16"><path d="M2.75 1h10.5a.75.75 0 0 1 0 1.5h-.75v1.25a4.75 4.75 0 0 1-1.9 3.8l-.333.25a.25.25 0 0 0 0 .4l.333.25a4.75 4.75 0 0 1 1.9 3.8v1.25h.75a.75.75 0 0 1 0 1.5H2.75a.75.75 0 0 1 0-1.5h.75v-1.25a4.75 4.75 0 0 1 1.9-3.8l.333-.25a.25.25 0 0 0 0-.4L5.4 7.55a4.75 4.75 0 0 1-1.9-3.8V2.5h-.75a.75.75 0 0 1 0-1.5ZM11 2.5H5v1.25c0 1.023.482 1.986 1.3 2.6l.333.25c.934.7.934 2.1 0 2.8l-.333.25a3.251 3.251 0 0 0-1.3 2.6v1.25h6v-1.25a3.251 3.251 0 0 0-1.3-2.6l-.333-.25a1.748 1.748 0 0 1 0-2.8l.333-.25a3.251 3.251 0 0 0 1.3-2.6Z"></path></svg>`;
const SVG_INFO = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" width="14" height="14" fill="currentColor"><path d="M0 8a8 8 0 1 1 16 0A8 8 0 0 1 0 8Zm8-6.5a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13ZM6.5 7.75A.75.75 0 0 1 7.25 7h1a.75.75 0 0 1 .75.75v2.75h.25a.75.75 0 0 1 0 1.5h-2a.75.75 0 0 1 0-1.5h.25v-2h-.25a.75.75 0 0 1-.75-.75ZM8 6a1 1 0 1 1 0-2 1 1 0 0 1 0 2Z"></path></svg>`;

function toggleProject(project) {
    if (expandedProjects.has(project)) {
        expandedProjects.delete(project);
        loadingProjects.delete(project);
        if (lastStatusData) renderStatus(lastStatusData);
    } else {
        expandedProjects.add(project);
        loadingProjects.add(project);
        const marker = document.getElementById(`marker-${project}`);
        if (marker) marker.innerHTML = SVG_HOURGLASS;
        fetchStatusOnce();
    }
}

// attached once so it keeps working after innerHTML is replaced
statusFooter.addEventListener('click', (e) => {
    const row = e.target.closest('[data-project]');
    if (row) toggleProject(row.dataset.project);
    if (e.target.closest('[data-action="refresh"]')) {
        e.target.closest('[data-action="refresh"]').innerHTML = SVG_HOURGLASS;
        fetchStatusOnce();
    }
});

function renderStatus(data) {
    lastStatusData = data;
    const isTotalExpanded = expandedProjects.has('total');
    const totalArrow = isTotalExpanded ? SVG_CHEVRON_DOWN : SVG_CHEVRON_RIGHT;
    loadingProjects.delete('total');
    let totalResHtml = '';

    const totalResolutions = data.total_resolutions || {};
    const indexedCount = data.indexed_count || 0;

    if (isTotalExpanded) {
        const sortedTotalRes = Object.entries(totalResolutions)
            .sort((a, b) => b[1] - a[1]);

        totalResHtml = `
                <div class="resolution-list">
                    ${sortedTotalRes.map(([name, count]) => {
            const isInvalid = name === 'Invalid';
            const colorStyle = isInvalid ? 'color: red;' : '';
            const countStr = (count || 0).toLocaleString();
            const infoTooltip = `Excluded to preserve the quality and relevance of search results.`;
            const displayCount = isInvalid
                ? `(${countStr} excluded) <span title="${infoTooltip}" style="cursor:help; margin-left: 4px; display: flex;">${SVG_INFO}</span>`
                : countStr;
            return `
                            <div class="res-item" style="${colorStyle}">
                                <span>${name}:</span>
                                <span class="res-count">${displayCount}</span>
                            </div>
                        `;
        }).join('')}
                </div>
            `;
    }

    // breakdown by project
    let projectHtml = '';
    const projects = data.projects || {};
    const displayOrder = ["MC", "MCPE", "MCL", "REALMS", "WEB", "BDS"];

    for (const project of displayOrder) {
        const stats = projects[project];
        if (!stats) continue;

        const isExpanded = expandedProjects.has(project);
        const arrow = isExpanded ? SVG_CHEVRON_DOWN : SVG_CHEVRON_RIGHT;
        loadingProjects.delete(project);

        const maxKey = stats.max_key || 0;
        let resHtml = '';
        if (isExpanded) {
            const resolutions = stats.resolutions || {};
            const sortedRes = Object.entries(resolutions)
                .sort((a, b) => b[1] - a[1]);

            resHtml = `
                    <div class="resolution-list">
                        ${sortedRes.map(([name, count]) => {
                const isInvalid = name === 'Invalid';
                const colorStyle = isInvalid ? 'color: red;' : '';
                const countStr = (count || 0).toLocaleString();
                const infoTooltip = `Excluded to preserve the quality and relevance of search results.`;
                const displayCount = isInvalid
                    ? `(${countStr} excluded) <span title="${infoTooltip}" style="cursor:help; margin-left: 4px; display: flex;">${SVG_INFO}</span>`
                    : countStr;
                return `
                                <div class="res-item" style="${colorStyle}">
                                    <span>${name}:</span>
                                    <span class="res-count">${displayCount}</span>
                                </div>
                            `;
            }).join('')}
                    </div>
                `;
        }

        projectHtml += `
                <div class="project-group">
                    <div class="project-row" data-project="${project}">
                        <span class="toggle-marker" id="marker-${project}">${arrow}</span>
                        <span>${project}-${maxKey}</span>
                    </div>
                    ${resHtml}
                </div>
            `;
    }

    const timeStr = lastFetchTime
        ? lastFetchTime.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
        : '';

    statusFooter.innerHTML = `
            <div class="project-group">
                <div class="project-row" data-project="total">
                    <span class="toggle-marker" id="marker-total">${totalArrow}</span>
                    <span>Tracked Issues: ${indexedCount.toLocaleString()}</span>
                </div>
                ${totalResHtml}
            </div>
            <div style="margin-top: 0.5rem; font-size: 0.85rem;">Latest Keys:</div>
            <div class="project-list">${projectHtml}</div>
            <div style="margin-top: 0.75rem; font-size: 0.8rem; display: flex; align-items: center; gap: 0.4rem;">
                ${timeStr ? `Updated ${timeStr}` : ''}
                <span data-action="refresh" style="cursor:pointer; display:flex; align-items:center;" title="Refresh">${SVG_REFRESH}</span>
            </div>
        `;
}

async function fetchStatusOnce() {
    try {
        const res = await fetch(`${API_BASE}/status`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        lastFetchTime = new Date();
        renderStatus(await res.json());
    } catch (e) {
        console.error('Status fetch failed:', e);
        statusFooter.innerHTML = `<div style="color: red; font-size: 0.85rem;">Status Error: ${e.message}</div>`;
    }
}

fetchStatusOnce();

function displayResults(results) {
    resultsContainer.innerHTML = '';

    if (!results || results.length === 0) {
        resultsContainer.innerHTML = `<div style="color: var(--fg); padding: 1rem 0;">No matches found.</div>`;
        return;
    }

    results.forEach(issue => {
        const card = document.createElement('a');
        card.href = getIssueUrl(issue.key);
        card.target = "_blank";
        card.className = "card";

        const scorePercent = (issue.score * 100).toFixed(1);
        const dateSplit = (issue.updated_date || '').substring(0, 10);

        card.innerHTML = `
            <div class="card-header">
                <span style="color: var(--fg); font-weight: normal;">${issue.key}</span>
                <span class="score">${scorePercent}% Relevance</span>
            </div>
            <h3 class="card-title">${issue.summary}</h3>
            <div class="card-footer">
                <div class="card-meta">
                    <span>Resolution: ${(!issue.resolution || issue.resolution === "Unresolved") ? "Unresolved" : issue.resolution}</span>
                </div>
                <div class="card-updated">Updated: ${dateSplit}</div>
            </div>
        `;
        resultsContainer.appendChild(card);
    });
}

async function performSearch() {
    const query = searchInput.value.trim();
    if (!query) return;

    const mode = searchMode.value;
    const currentProject = projectSelect.value;

    searchWrapper.classList.add('loading');
    try {
        let url;

        if (mode === 'duplicate') {
            const sort = sortOrder.value;
            url = `${API_BASE}/similar/${query.toUpperCase()}?sort=${sort}`;
            if (currentProject) {
                url += `&project=${currentProject}`;
            }
        } else {
            const sort = sortOrder.value;
            url = `${API_BASE}/search?q=${encodeURIComponent(query)}&sort=${sort}`;
            if (currentProject) {
                url += `&project=${currentProject}`;
            }
        }

        const res = await fetch(url);
        if (!res.ok) {
            if (res.status === 404) throw new Error("Issue key not found in index.");
            if (res.status === 400) throw new Error("Invalid issue key format (e.g. MC-6767).");
            throw new Error("API Error: " + res.statusText);
        }
        const data = await res.json();
        displayResults(data.results);

    } catch (e) {
        resultsContainer.innerHTML = `<div style="color: red;">${e.message}</div>`;
    } finally {
        searchWrapper.classList.remove('loading');
    }
}

searchMode.addEventListener('change', () => {
    if (searchMode.value === 'duplicate') {
        searchInput.placeholder = "Enter an issue key (e.g. MC-6767)";
    } else {
        searchInput.placeholder = "Describe a bug report";
    }
    if (searchInput.value.trim()) performSearch();
});

searchBtn.addEventListener('click', performSearch);
searchInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') performSearch();
});
projectSelect.addEventListener('change', () => {
    if (searchInput.value.trim()) performSearch();
});
sortOrder.addEventListener('change', () => {
    if (searchInput.value.trim()) performSearch();
});
openWith.addEventListener('change', () => {
    if (searchInput.value.trim()) performSearch();
});
