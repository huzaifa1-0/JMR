/* Job Market Research Platform -- frontend.
 *
 * No build step: this is plain ES2020 served straight from FastAPI, so the
 * whole application runs with Python alone. State lives in module scope; there
 * is no browser storage anywhere.
 */
'use strict';

// ==========================================================================
// API helper
// ==========================================================================
async function api(path, { method = 'GET', body = null } = {}) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== null) opts.body = JSON.stringify(body);

  let res;
  try {
    res = await fetch(`/api${path}`, opts);
  } catch (err) {
    throw new AppError('Could not reach the local server.',
      'Is the application still running in the terminal window?');
  }

  if (res.status === 204) return null;

  let data;
  const text = await res.text();
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }

  if (!res.ok) {
    const e = data && data.error ? data.error : {};
    throw new AppError(e.message || `Request failed (HTTP ${res.status}).`,
      e.hint, e.reference);
  }
  return data;
}

class AppError extends Error {
  constructor(message, hint, reference) {
    super(message);
    this.hint = hint;
    this.reference = reference;
  }
}

// ==========================================================================
// State
// ==========================================================================
const state = {
  route: 'dashboard',
  query: emptyQuery(),
  results: null,
  facets: null,
  customFields: [],
  sources: [],
  collecting: false,
  lastCollection: null,
};

function emptyQuery() {
  return {
    job_title: '', keywords: '', skills_text: '', company: '', boolean: '',
    city: '', state: '', country: '', postal_code: '',
    date_posted: '', date_from: '', date_to: '',
    employment_types: [], work_arrangements: [], experience_levels: [],
    salary_min: null, salary_max: null, has_salary: false,
    employer_stated_salary: false,
    title_contains: '', title_not_contains: '',
    description_contains: '', description_not_contains: '',
    required_skills: [], any_skills: [], exclude_skills: [],
    education_levels: [], years_experience_min: null, years_experience_max: null,
    visa_sponsorship: null, relocation: null, urgently_hiring: null,
    max_posting_age_days: null, min_score: null,
    company_industry: '', company_domain: '', company_min_jobs: null,
    company_min_hiring_frequency: null, company_ats: [],
    research_status: [], priority: [], tags: [], custom_fields: [],
    sources: [], include_duplicates: false, include_inactive: false,
    sort: 'relevance', sort_dir: 'desc', page: 1, page_size: 50,
  };
}

/** Strip blanks so the server sees only real filters. */
function cleanQuery(q) {
  const out = {};
  for (const [k, v] of Object.entries(q)) {
    if (v === '' || v === null || v === undefined) continue;
    if (Array.isArray(v) && v.length === 0) continue;
    if (v === false && !['include_duplicates', 'include_inactive',
      'has_salary', 'employer_stated_salary'].includes(k)) continue;
    out[k] = v;
  }
  return out;
}

// ==========================================================================
// Utilities
// ==========================================================================
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function esc(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function money(v) {
  if (v === null || v === undefined) return '—';
  if (v >= 1000) return '$' + Math.round(v / 1000) + 'k';
  return '$' + Math.round(v);
}

function salaryCell(job) {
  if (job.salary_annualized_min === null) return '<span class="t-sub">—</span>';
  const lo = money(job.salary_annualized_min);
  const hi = job.salary_max ? money(job.salary_annualized_min &&
    job.salary_max * (job.salary_annualized_min / (job.salary_min || 1))) : null;
  const range = hi && hi !== lo ? `${lo}–${hi}` : lo;
  const est = job.salary_is_estimated
    ? ' <span class="badge" title="Estimated by the source, not stated by the employer">est</span>'
    : '';
  return range + est;
}

function ago(iso) {
  if (!iso) return '—';
  const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86400000);
  if (days < 0) return 'today';
  if (days === 0) return 'today';
  if (days === 1) return 'yesterday';
  if (days < 30) return `${days}d ago`;
  if (days < 365) return `${Math.floor(days / 30)}mo ago`;
  return `${Math.floor(days / 365)}y ago`;
}

function titleCase(s) {
  if (!s) return '—';
  return String(s).replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

function toast(message, kind = 'info', hint = '') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.innerHTML = `<strong>${esc(message)}</strong>` +
    (hint ? `<span>${esc(hint)}</span>` : '');
  $('#toasts').appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; }, 5000);
  setTimeout(() => el.remove(), 5600);
}

function showError(err) {
  console.error(err);
  toast(err.message || 'Something went wrong.', 'error',
    err.hint || (err.reference ? `Reference ${err.reference}` : ''));
}

function scoreBar(score) {
  if (score === null || score === undefined) return '<span class="t-sub">—</span>';
  return `<span class="score">
      <span class="score-track"><span class="score-fill" style="width:${score}%"></span></span>
      <span class="num">${Math.round(score)}</span>
    </span>`;
}

// ==========================================================================
// Top bar
// ==========================================================================
async function refreshMeters() {
  try {
    state.sources = await api('/sources');
  } catch { return; }

  $('#meters').innerHTML = state.sources.map(s => {
    if (!s.enabled) return '';
    if (s.monthly_quota) {
      const used = s.calls_used || 0;
      const pct = Math.min(100, (used / s.monthly_quota) * 100);
      const low = pct > 85;
      return `<div class="meter ${low ? 'low' : ''}" title="${esc(s.display_name)}: ${used} of ${s.monthly_quota} monthly calls used">
          <div class="meter-top"><span class="src-name">${esc(s.key)}</span>
          <span>${s.monthly_quota - used}</span></div>
          <div class="meter-track"><div class="meter-fill" style="width:${pct}%"></div></div>
        </div>`;
    }
    const ok = s.health === 'ok' || s.health === 'unknown';
    return `<div class="meter" title="${esc(s.display_name)}: unmetered">
        <div class="meter-top"><span class="src-name">${esc(s.key)}</span>
        <span class="dot ${ok ? 'ok' : 'down'}"></span></div>
      </div>`;
  }).join('');
}

function setCollecting(on, label) {
  state.collecting = on;
  $('#collectState').innerHTML =
    `<span class="dot ${on ? 'busy' : 'idle'}"></span>` +
    `<span class="collect-label">${esc(label || (on ? 'Collecting…' : 'Idle'))}</span>`;
}

// ==========================================================================
// Router
// ==========================================================================
const routes = {
  dashboard: renderDashboard,
  search: renderSearch,
  saved: renderSaved,
  results: renderResults,
  companies: renderCompanies,
  analytics: renderAnalytics,
  research: renderResearch,
  exports: renderExports,
  settings: renderSettings,
};

async function navigate() {
  const route = (location.hash.replace('#/', '') || 'dashboard').split('?')[0];
  state.route = routes[route] ? route : 'dashboard';
  $$('#nav a').forEach(a =>
    a.classList.toggle('active', a.dataset.route === state.route));
  $('#workspace').innerHTML = '<div class="loading">Loading…</div>';
  try {
    await routes[state.route]();
  } catch (err) {
    showError(err);
    $('#workspace').innerHTML =
      `<div class="empty"><h3>This page could not load</h3>
       <p>${esc(err.message)}</p>
       ${err.hint ? `<p class="t-sub">${esc(err.hint)}</p>` : ''}</div>`;
  }
}

// ==========================================================================
// Dashboard
// ==========================================================================
async function renderDashboard() {
  const [stats, history, topCompanies] = await Promise.all([
    api('/stats/overview'),
    api('/history?limit=8'),
    api('/analytics/hiring-companies?limit=8').catch(() => []),
  ]);

  const empty = stats.jobs_active === 0;

  $('#workspace').innerHTML = `
    <div class="page-head">
      <div><span class="eyebrow">Overview</span><h1>Dashboard</h1></div>
      <div class="btn-row">
        <a class="btn" href="#/search">Search &amp; collect</a>
      </div>
    </div>

    ${empty ? `
      <div class="panel">
        <div class="panel-head"><h2>Start here</h2></div>
        <div class="sect-body">
          <p>The corpus is empty. Collection runs separately from search: you
             gather postings once, then search them instantly and without limit.</p>
          <ol class="note">
            <li>Open <a href="#/search">Search &amp; collect</a>.</li>
            <li>Enter a job title, then press <strong>Collect new data</strong>.</li>
            <li>Remotive works with no credentials at all. Add an Adzuna key in
                <a href="#/settings">Settings</a> for far wider US coverage.</li>
          </ol>
        </div>
      </div>` : ''}

    <div class="grid">
      <div class="stat"><span class="t-sub">Active postings</span>
        <strong>${stats.jobs_active.toLocaleString()}</strong></div>
      <div class="stat"><span class="t-sub">Companies</span>
        <strong>${stats.companies.toLocaleString()}</strong></div>
      <div class="stat"><span class="t-sub">Duplicates linked</span>
        <strong>${stats.duplicates_linked.toLocaleString()}</strong></div>
      <div class="stat"><span class="t-sub">With salary data</span>
        <strong>${stats.jobs_with_salary.toLocaleString()}</strong></div>
    </div>

    <div class="results-layout">
      <div class="panel">
        <div class="panel-head"><h2>Recent activity</h2>
          <a class="t-sub" href="#/results">All results →</a></div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Run</th><th>Type</th><th>Results</th>
              <th>New</th><th>When</th><th>Status</th></tr></thead>
            <tbody>
              ${history.length ? history.map(h => `
                <tr>
                  <td class="t-title">${esc(h.name || '—')}</td>
                  <td><span class="chip">${esc(h.run_type)}</span></td>
                  <td>${h.results_total ?? 0}</td>
                  <td>${h.results_new ?? 0}</td>
                  <td class="t-sub">${ago(h.started_at)}</td>
                  <td><span class="badge ${h.status}">${esc(h.status)}</span></td>
                </tr>`).join('')
      : '<tr><td colspan="6" class="t-sub">No activity yet.</td></tr>'}
            </tbody>
          </table>
        </div>
      </div>

      <div class="panel">
        <div class="panel-head"><h2>Most active employers</h2></div>
        <div class="sect-body bars">
          ${topCompanies.length ? topCompanies.map(c => `
            <div class="bar-row">
              <span class="bar-label" title="${esc(c.name)}">${esc(c.name)}</span>
              <span class="bar-track"><span class="bar-fill"
                style="width:${Math.min(100, (c.jobs_posted / topCompanies[0].jobs_posted) * 100)}%"></span></span>
              <span class="bar-val">${c.jobs_posted}</span>
            </div>`).join('')
      : '<p class="t-sub">Collect some postings to see hiring patterns.</p>'}
        </div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-head"><h2>Data sources</h2>
        <a class="t-sub" href="#/settings">Configure →</a></div>
      <div class="sect-body">
        ${state.sources.map(s => `
          <div class="src-row">
            <span class="dot ${s.health === 'ok' ? 'ok'
      : s.configured === false ? 'idle' : 'down'}"></span>
            <span class="src-name">${esc(s.display_name)}</span>
            <span class="chip">${esc(s.kind)}</span>
            <span class="t-sub">
              ${s.monthly_quota
      ? `${s.calls_used || 0}/${s.monthly_quota} calls this month`
      : s.kind === 'ats' ? `${s.ats_boards_known} boards known` : 'unmetered'}
            </span>
            <span class="t-sub">${s.enabled ? '' : 'disabled'}</span>
          </div>`).join('')}
      </div>
    </div>`;
}

// ==========================================================================
// Search page
// ==========================================================================
const EMPLOYMENT = ['full_time', 'part_time', 'contract', 'temporary', 'internship'];
const ARRANGEMENTS = ['remote', 'hybrid', 'onsite'];
const LEVELS = ['entry', 'mid', 'senior', 'manager', 'director', 'executive'];
const EDUCATION = ['high_school', 'associates', 'bachelors', 'masters',
  'doctorate', 'certification'];

async function renderSearch() {
  state.customFields = await api('/custom-fields').catch(() => []);
  const q = state.query;

  $('#workspace').innerHTML = `
    <div class="page-head">
      <div><span class="eyebrow">Phase 2 &amp; 3</span><h1>Search &amp; collect</h1></div>
    </div>

    <p class="note">
      <strong>Two separate operations.</strong> <em>Search corpus</em> queries what
      you have already collected — instant, unlimited, offline.
      <em>Collect new data</em> spends API budget to fetch new postings from the
      sources. Collect once, search as often as you like.
    </p>

    <div class="panel">
      <div class="panel-head"><h2>Basic search</h2></div>
      <div class="sect-body">
        <div class="grid">
          <label class="kv"><span>Job title</span>
            <input id="f_job_title" value="${esc(q.job_title)}" placeholder="Medical Biller"></label>
          <label class="kv"><span>Keywords</span>
            <input id="f_keywords" value="${esc(q.keywords)}" placeholder="denial management"></label>
          <label class="kv"><span>Skills</span>
            <input id="f_skills_text" value="${esc(q.skills_text)}" placeholder="ICD-10, Epic"></label>
          <label class="kv"><span>Company</span>
            <input id="f_company" value="${esc(q.company)}" placeholder="Any employer"></label>
          <label class="kv"><span>City</span>
            <input id="f_city" value="${esc(q.city)}" placeholder="Dallas"></label>
          <label class="kv"><span>State</span>
            <input id="f_state" value="${esc(q.state)}" placeholder="TX" maxlength="2"></label>
          <label class="kv"><span>ZIP code</span>
            <input id="f_postal_code" value="${esc(q.postal_code)}" placeholder="75201"></label>
          <label class="kv"><span>Country</span>
            <input id="f_country" value="${esc(q.country)}" placeholder="US" maxlength="2"></label>
        </div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-head"><h2>Job filters</h2></div>
      <div class="sect-body">
        <div class="grid">
          <label class="kv"><span>Date posted</span>
            <select id="f_date_posted">
              <option value="">Any time</option>
              <option value="1">Last 24 hours</option>
              <option value="3">Last 3 days</option>
              <option value="7">Last 7 days</option>
              <option value="14">Last 14 days</option>
              <option value="30">Last 30 days</option>
            </select></label>
          <label class="kv"><span>Custom from</span>
            <input id="f_date_from" type="date" value="${esc(q.date_from)}"></label>
          <label class="kv"><span>Custom to</span>
            <input id="f_date_to" type="date" value="${esc(q.date_to)}"></label>
          <label class="kv"><span>Min salary (annualised)</span>
            <input id="f_salary_min" type="number" step="1000" value="${q.salary_min ?? ''}"></label>
          <label class="kv"><span>Max salary (annualised)</span>
            <input id="f_salary_max" type="number" step="1000" value="${q.salary_max ?? ''}"></label>
          <label class="kv"><span>Minimum score</span>
            <input id="f_min_score" type="number" min="0" max="100" value="${q.min_score ?? ''}"></label>
        </div>

        <div class="checks">
          <span class="t-sub">Employment type</span>
          ${EMPLOYMENT.map(v => checkbox('employment_types', v)).join('')}
        </div>
        <div class="checks">
          <span class="t-sub">Work arrangement</span>
          ${ARRANGEMENTS.map(v => checkbox('work_arrangements', v)).join('')}
        </div>
        <div class="checks">
          <span class="t-sub">Experience level</span>
          ${LEVELS.map(v => checkbox('experience_levels', v)).join('')}
        </div>
        <div class="checks">
          <span class="t-sub">Salary</span>
          ${checkbox('has_salary', true, 'Has salary data')}
          ${checkbox('employer_stated_salary', true, 'Employer-stated only')}
        </div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-head"><h2>Advanced research filters</h2>
        <span class="t-sub">Beyond a normal job search</span></div>
      <div class="sect-body">

        <label class="kv"><span>Boolean expression</span>
          <input id="f_boolean" value="${esc(q.boolean)}"
            placeholder='("medical billing" OR "revenue cycle") AND ("remote" OR "work from home")'></label>
        <div class="bool-status" id="boolStatus">
          Supports AND, OR, NOT, parentheses and "exact phrases".
        </div>

        <div class="grid">
          <label class="kv"><span>Title contains</span>
            <input id="f_title_contains" value="${esc(q.title_contains)}"></label>
          <label class="kv"><span>Title does NOT contain</span>
            <input id="f_title_not_contains" value="${esc(q.title_not_contains)}" placeholder="intern"></label>
          <label class="kv"><span>Description contains</span>
            <input id="f_description_contains" value="${esc(q.description_contains)}"></label>
          <label class="kv"><span>Description does NOT contain</span>
            <input id="f_description_not_contains" value="${esc(q.description_not_contains)}"></label>
          <label class="kv"><span>Required skills (comma separated)</span>
            <input id="f_required_skills" value="${esc(q.required_skills.join(', '))}" placeholder="SQL, Medical Billing"></label>
          <label class="kv"><span>Any of these skills</span>
            <input id="f_any_skills" value="${esc(q.any_skills.join(', '))}"></label>
          <label class="kv"><span>Exclude skills</span>
            <input id="f_exclude_skills" value="${esc(q.exclude_skills.join(', '))}"></label>
          <label class="kv"><span>Years experience (min)</span>
            <input id="f_years_experience_min" type="number" min="0" max="40" value="${q.years_experience_min ?? ''}"></label>
          <label class="kv"><span>Years experience (max)</span>
            <input id="f_years_experience_max" type="number" min="0" max="40" value="${q.years_experience_max ?? ''}"></label>
          <label class="kv"><span>Max posting age (days)</span>
            <input id="f_max_posting_age_days" type="number" min="1" value="${q.max_posting_age_days ?? ''}"></label>
        </div>

        <div class="checks">
          <span class="t-sub">Education</span>
          ${EDUCATION.map(v => checkbox('education_levels', v)).join('')}
        </div>

        <div class="grid">
          ${tristate('visa_sponsorship', 'Visa sponsorship mentioned')}
          ${tristate('relocation', 'Relocation assistance')}
          ${tristate('urgently_hiring', 'Urgently hiring')}
        </div>

        <h3>Company research</h3>
        <div class="grid">
          <label class="kv"><span>Industry</span>
            <input id="f_company_industry" value="${esc(q.company_industry)}"></label>
          <label class="kv"><span>Domain</span>
            <input id="f_company_domain" value="${esc(q.company_domain)}"></label>
          <label class="kv"><span>Min open postings</span>
            <input id="f_company_min_jobs" type="number" min="0" value="${q.company_min_jobs ?? ''}"></label>
          <label class="kv"><span>Min hiring frequency (per week)</span>
            <input id="f_company_min_hiring_frequency" type="number" step="0.5" min="0"
              value="${q.company_min_hiring_frequency ?? ''}"></label>
        </div>

        ${state.customFields.length ? `
          <h3>Custom fields</h3>
          <div class="grid" id="customFilters">
            ${state.customFields.filter(f => f.is_filterable).map(f => `
              <label class="kv"><span>${esc(f.label)}</span>
                ${customFilterInput(f)}</label>`).join('')}
          </div>` : `
          <p class="t-sub">No custom fields yet — create them in
            <a href="#/settings">Settings</a> and they will appear here as filters.</p>`}

        <div class="checks">
          ${checkbox('include_duplicates', true, 'Include duplicate records')}
          ${checkbox('include_inactive', true, 'Include expired postings')}
        </div>
      </div>
    </div>

    <div class="panel">
      <div class="sect-body btn-row">
        <button class="btn primary" id="btnSearch">Search corpus</button>
        <button class="btn" id="btnCollect">Collect new data…</button>
        <button class="btn" id="btnSave">Save this search</button>
        <button class="btn ghost" id="btnReset">Reset</button>
      </div>
    </div>`;

  wireSearchPage();
}

function checkbox(field, value, label) {
  const q = state.query;
  const checked = Array.isArray(q[field]) ? q[field].includes(value) : !!q[field];
  const id = `chk_${field}_${value}`;
  return `<label class="check"><input type="checkbox" id="${id}"
    data-field="${field}" data-value="${value}" ${checked ? 'checked' : ''}>
    <span>${esc(label || titleCase(value))}</span></label>`;
}

function tristate(field, label) {
  const v = state.query[field];
  return `<label class="kv"><span>${esc(label)}</span>
    <select id="f_${field}" data-tristate="1">
      <option value="">Any</option>
      <option value="true" ${v === true ? 'selected' : ''}>Yes</option>
      <option value="false" ${v === false ? 'selected' : ''}>No</option>
    </select></label>`;
}

function customFilterInput(f) {
  const id = `cf_${f.key}`;
  if (f.field_type === 'boolean') {
    return `<select id="${id}" data-cf="${esc(f.key)}" data-cftype="boolean">
      <option value="">Any</option><option value="true">Yes</option>
      <option value="false">No</option></select>`;
  }
  if (f.field_type === 'dropdown' || f.field_type === 'multiselect') {
    return `<select id="${id}" data-cf="${esc(f.key)}" data-cftype="${f.field_type}">
      <option value="">Any</option>
      ${(f.options || []).map(o => `<option value="${esc(o)}">${esc(o)}</option>`).join('')}
    </select>`;
  }
  if (f.field_type === 'number' || f.field_type === 'score') {
    return `<input id="${id}" type="number" data-cf="${esc(f.key)}"
      data-cftype="number" data-cfop="gte" placeholder="at least…">`;
  }
  return `<input id="${id}" data-cf="${esc(f.key)}" data-cftype="text" placeholder="contains…">`;
}

function collectQueryFromForm() {
  const q = state.query;
  const text = id => { const el = $(`#f_${id}`); return el ? el.value.trim() : ''; };
  const num = id => {
    const el = $(`#f_${id}`);
    if (!el || el.value === '') return null;
    const n = Number(el.value);
    return Number.isNaN(n) ? null : n;
  };
  const list = id => text(id).split(',').map(s => s.trim()).filter(Boolean);

  ['job_title', 'keywords', 'skills_text', 'company', 'city', 'state',
    'postal_code', 'country', 'boolean', 'date_posted', 'date_from', 'date_to',
    'title_contains', 'title_not_contains', 'description_contains',
    'description_not_contains', 'company_industry', 'company_domain',
  ].forEach(k => { q[k] = text(k); });

  ['salary_min', 'salary_max', 'min_score', 'years_experience_min',
    'years_experience_max', 'max_posting_age_days', 'company_min_jobs',
    'company_min_hiring_frequency',
  ].forEach(k => { q[k] = num(k); });

  ['required_skills', 'any_skills', 'exclude_skills'].forEach(k => {
    q[k] = list(k);
  });

  ['employment_types', 'work_arrangements', 'experience_levels',
    'education_levels'].forEach(f => {
      q[f] = $$(`input[data-field="${f}"]:checked`).map(el => el.dataset.value);
    });

  ['has_salary', 'employer_stated_salary', 'include_duplicates',
    'include_inactive'].forEach(f => {
      const el = $(`#chk_${f}_true`);
      q[f] = el ? el.checked : false;
    });

  ['visa_sponsorship', 'relocation', 'urgently_hiring'].forEach(f => {
    const el = $(`#f_${f}`);
    q[f] = !el || el.value === '' ? null : el.value === 'true';
  });

  q.custom_fields = $$('[data-cf]').filter(el => el.value !== '').map(el => ({
    key: el.dataset.cf,
    op: el.dataset.cfop || (el.dataset.cftype === 'text' ? 'contains' : 'eq'),
    value: el.dataset.cftype === 'boolean' ? el.value === 'true'
      : el.dataset.cftype === 'number' ? Number(el.value) : el.value,
  }));

  q.page = 1;
  return q;
}

function wireSearchPage() {
  const q = state.query;
  if (q.date_posted) $('#f_date_posted').value = q.date_posted;

  let boolTimer;
  $('#f_boolean').addEventListener('input', e => {
    clearTimeout(boolTimer);
    const expr = e.target.value.trim();
    const el = $('#boolStatus');
    if (!expr) {
      el.className = 'bool-status';
      el.textContent = 'Supports AND, OR, NOT, parentheses and "exact phrases".';
      return;
    }
    boolTimer = setTimeout(async () => {
      try {
        const r = await api(`/search/validate-boolean?expr=${encodeURIComponent(expr)}`);
        if (r.valid) {
          el.className = 'bool-status ok';
          el.textContent = `Valid — matching on: ${(r.terms || []).join(', ')}`;
        } else {
          el.className = 'bool-status bad';
          el.textContent = `${r.message}${r.hint ? '  ·  ' + r.hint : ''}`;
        }
      } catch { /* validation is best-effort */ }
    }, 250);
  });

  $('#btnSearch').addEventListener('click', async () => {
    collectQueryFromForm();
    location.hash = '#/results';
  });

  $('#btnReset').addEventListener('click', () => {
    state.query = emptyQuery();
    renderSearch();
  });

  $('#btnSave').addEventListener('click', async () => {
    collectQueryFromForm();
    const name = prompt('Name this saved search:',
      state.query.job_title || state.query.keywords || 'Untitled search');
    if (!name) return;
    try {
      await api('/saved-searches', {
        method: 'POST',
        body: { name, query: cleanQuery(state.query) },
      });
      toast('Saved search created.', 'ok', name);
    } catch (err) { showError(err); }
  });

  $('#btnCollect').addEventListener('click', runCollection);
}

async function runCollection() {
  collectQueryFromForm();
  const q = state.query;
  const enabled = state.sources.filter(s => s.enabled).map(s => s.key);

  if (!enabled.length) {
    toast('No sources are enabled.', 'error',
      'Enable at least one in Settings.');
    return;
  }

  const payload = {
    job_title: q.job_title || null,
    keywords: q.keywords || null,
    company: q.company || null,
    city: q.city || null,
    state: q.state || null,
    country: (q.country || 'us').toLowerCase(),
    postal_code: q.postal_code || null,
    remote_only: q.work_arrangements.includes('remote'),
    max_days_old: q.date_posted ? Number(q.date_posted) : null,
    salary_min: q.salary_min,
    employment_type: q.employment_types[0] || null,
    sources: enabled,
    name: `Collect: ${q.job_title || q.keywords || 'all'}`,
  };

  setCollecting(true, 'Collecting…');
  toast('Collection started.', 'info',
    'This spends API budget. Searching the corpus afterwards is free.');

  try {
    const summary = await api('/collect', { method: 'POST', body: payload });
    state.lastCollection = summary;
    setCollecting(false);
    await refreshMeters();

    const bits = [`${summary.new} new`, `${summary.updated} updated`,
    `${summary.duplicate} duplicates linked`];
    if (summary.discovered_boards)
      bits.push(`${summary.discovered_boards} ATS boards discovered`);
    toast('Collection finished.', 'ok', bits.join(' · '));

    if (summary.errors && summary.errors.length) {
      summary.errors.forEach(e =>
        toast(`${e.source} had a problem.`, 'error', e.message));
    }
    if (summary.new === 0 && summary.updated === 0) {
      toast('Nothing new was returned.', 'info',
        'Try broader keywords, or enable more sources in Settings.');
    }
  } catch (err) {
    setCollecting(false);
    showError(err);
  }
}

// ==========================================================================
// Results
// ==========================================================================
async function renderResults() {
  const body = cleanQuery(state.query);
  const data = await api('/search', { method: 'POST', body });
  state.results = data;
  state.facets = data.facets;

  const chips = Object.entries(data.query_echo || {})
    .filter(([k]) => !['page', 'page_size', 'sort', 'sort_dir'].includes(k))
    .map(([k, v]) => `<span class="chip" data-clear="${esc(k)}">
        ${esc(titleCase(k))}: ${esc(Array.isArray(v) ? v.join(', ') : v)}
        <button aria-label="Remove filter">×</button></span>`).join('');

  $('#workspace').innerHTML = `
    <div class="page-head">
      <div><span class="eyebrow">${data.total.toLocaleString()} matching</span>
        <h1>Job results</h1></div>
      <div class="btn-row">
        <select id="sortBy">
          ${['relevance', 'date_posted', 'salary', 'company', 'location', 'title',
      'remote', 'experience', 'score'].map(s =>
        `<option value="${s}" ${state.query.sort === s ? 'selected' : ''}>
              Sort: ${titleCase(s)}</option>`).join('')}
        </select>
        <button class="btn" id="btnExportCsv">Export CSV</button>
        <button class="btn" id="btnExportXlsx">Export XLSX</button>
        <a class="btn ghost" href="#/search">Edit filters</a>
      </div>
    </div>

    ${chips ? `<div class="chips">${chips}</div>` : ''}

    <div class="results-layout">
      <aside class="facets">${renderFacets(data.facets)}</aside>

      <div class="panel">
        <div class="table-wrap">
          <table>
            <thead><tr>
              <th>Title</th><th>Company</th><th>Location</th><th>Mode</th>
              <th>Salary</th><th>Type</th><th>Posted</th><th>Score</th><th>Status</th>
            </tr></thead>
            <tbody>
              ${data.results.length ? data.results.map(rowHtml).join('') : `
                <tr><td colspan="9">
                  <div class="empty"><h3>No matches</h3>
                  <p>Nothing in the corpus fits these filters.</p>
                  <p class="t-sub">Either loosen the filters, or
                     <a href="#/search">collect new data</a> — remember that
                     search only ever looks at what you have already collected.</p>
                  </div></td></tr>`}
            </tbody>
          </table>
        </div>

        ${data.pages > 1 ? `
          <div class="pager">
            <button class="btn" id="prevPage" ${data.page <= 1 ? 'disabled' : ''}>Previous</button>
            <span class="t-sub">Page ${data.page} of ${data.pages}</span>
            <button class="btn" id="nextPage" ${data.page >= data.pages ? 'disabled' : ''}>Next</button>
          </div>` : ''}
      </div>
    </div>`;

  wireResults();
}

function rowHtml(job) {
  const dupe = job.duplicate_count
    ? ` <span class="badge" title="${job.duplicate_count} duplicate record(s) linked">⧉ ${job.duplicate_count}</span>` : '';
  const trunc = job.description_is_truncated
    ? ' <span class="badge warn" title="Truncated by the source. ATS collection fills these in.">partial</span>' : '';
  const status = job.research && job.research.status !== 'new'
    ? `<span class="badge ${job.research.status}">${titleCase(job.research.status)}</span>`
    : '<span class="t-sub">—</span>';

  return `<tr data-job="${job.id}">
    <td><span class="t-title">${esc(job.title)}</span>${dupe}${trunc}
        <span class="t-sub">${esc(job.source || '')}</span></td>
    <td>${esc(job.company || '—')}</td>
    <td>${esc(job.location || '—')}</td>
    <td><span class="chip ${esc(job.work_arrangement)}">${titleCase(job.work_arrangement)}</span></td>
    <td>${salaryCell(job)}</td>
    <td class="t-sub">${titleCase(job.employment_type)}</td>
    <td class="t-sub">${ago(job.date_posted)}</td>
    <td>${scoreBar(job.opportunity_score)}</td>
    <td>${status}</td>
  </tr>`;
}

function renderFacets(facets) {
  if (!facets) return '';
  const block = (title, key, items) => !items || !items.length ? '' : `
    <div class="facet">
      <h4>${esc(title)}</h4>
      ${items.slice(0, 8).map(f => `
        <div class="facet-row" data-facet="${esc(key)}" data-value="${esc(f.value)}">
          <span title="${esc(f.value)}">${esc(f.value)}</span>
          <span class="t-sub">${f.count}</span>
        </div>`).join('')}
    </div>`;

  return block('Company', 'company', facets.company)
    + block('Top skills', 'required_skills', facets.skills)
    + block('State', 'state', facets.state)
    + block('City', 'city', facets.city)
    + block('Arrangement', 'work_arrangements', facets.work_arrangement)
    + block('Employment', 'employment_types', facets.employment_type)
    + block('Level', 'experience_levels', facets.experience_level);
}

function wireResults() {
  $$('tr[data-job]').forEach(tr =>
    tr.addEventListener('click', () => openJob(tr.dataset.job)));

  $$('.facet-row').forEach(row => row.addEventListener('click', () => {
    const { facet, value } = row.dataset;
    const q = state.query;
    if (Array.isArray(q[facet])) {
      if (!q[facet].includes(value)) q[facet].push(value);
    } else {
      q[facet] = value;
    }
    q.page = 1;
    renderResults();
  }));

  $$('.chip[data-clear] button').forEach(btn =>
    btn.addEventListener('click', e => {
      e.stopPropagation();
      const key = btn.parentElement.dataset.clear;
      state.query[key] = Array.isArray(state.query[key]) ? []
        : typeof state.query[key] === 'boolean' ? false : '';
      state.query.page = 1;
      renderResults();
    }));

  const sortEl = $('#sortBy');
  if (sortEl) sortEl.addEventListener('change', () => {
    state.query.sort = sortEl.value;
    state.query.page = 1;
    renderResults();
  });

  const prev = $('#prevPage'), next = $('#nextPage');
  if (prev) prev.addEventListener('click', () => {
    state.query.page = Math.max(1, state.query.page - 1); renderResults();
  });
  if (next) next.addEventListener('click', () => {
    state.query.page += 1; renderResults();
  });

  $('#btnExportCsv').addEventListener('click', () => doExport('csv'));
  $('#btnExportXlsx').addEventListener('click', () => doExport('xlsx'));
}

async function doExport(format) {
  try {
    toast('Preparing export…', 'info');
    const res = await api('/exports', {
      method: 'POST',
      body: { format, query: cleanQuery(state.query), include_description: false },
    });
    toast(`Exported ${res.rows.toLocaleString()} rows.`, 'ok', res.filename);
    window.location = res.download_url;
  } catch (err) { showError(err); }
}

// ==========================================================================
// Job detail slide-over
// ==========================================================================
async function openJob(jobId) {
  const so = $('#slideover'), ov = $('#overlay');
  so.hidden = false; ov.hidden = false;
  $('#slideoverBody').innerHTML = '<div class="loading">Loading…</div>';

  try {
    const job = await api(`/jobs/${jobId}`);
    $('#slideoverBody').innerHTML = jobDetailHtml(job);
    wireJobDetail(job);
  } catch (err) {
    $('#slideoverBody').innerHTML =
      `<div class="empty"><h3>Could not load this job</h3>
       <p>${esc(err.message)}</p></div>`;
  }
}

function closeSlideover() {
  $('#slideover').hidden = true;
  $('#overlay').hidden = true;
}

function jobDetailHtml(job) {
  const skills = (job.skills || []);
  const req = skills.filter(s => s.requirement === 'required');
  const pref = skills.filter(s => s.requirement === 'preferred');
  const other = skills.filter(s => s.requirement === 'mentioned');

  const skillChips = arr => arr.length
    ? arr.map(s => `<span class="chip" data-skill="${esc(s.name)}">${esc(s.name)}</span>`).join('')
    : '<span class="t-sub">None detected</span>';

  return `
    <div class="so-head">
      <div>
        <h2>${esc(job.title)}</h2>
        <p class="t-sub">${esc(job.company || 'Unknown employer')} ·
           ${esc(job.location || 'Location not stated')} ·
           ${esc(job.source || '')}</p>
      </div>
      <button class="so-close" id="soClose" aria-label="Close">×</button>
    </div>

    <div class="so-tabs">
      <button class="so-tab active" data-tab="overview">Overview</button>
      <button class="so-tab" data-tab="description">Description</button>
      <button class="so-tab" data-tab="extracted">Extracted</button>
      <button class="so-tab" data-tab="research">Research</button>
      <button class="so-tab" data-tab="provenance">Provenance</button>
    </div>

    <div class="so-body" data-panel="overview">
      <div class="grid">
        <div class="kv"><span>Salary</span><strong>${salaryCell(job)}</strong></div>
        <div class="kv"><span>Arrangement</span><strong>${titleCase(job.work_arrangement)}</strong></div>
        <div class="kv"><span>Employment</span><strong>${titleCase(job.employment_type)}</strong></div>
        <div class="kv"><span>Level</span><strong>${titleCase(job.experience_level)}</strong></div>
        <div class="kv"><span>Experience</span><strong>${job.years_experience_min !== null ? job.years_experience_min + '+ yrs' : '—'}</strong></div>
        <div class="kv"><span>Education</span><strong>${titleCase(job.education_level)}</strong></div>
        <div class="kv"><span>Posted</span><strong>${ago(job.date_posted)}</strong></div>
        <div class="kv"><span>Collected</span><strong>${ago(job.date_collected)}</strong></div>
        <div class="kv"><span>Opportunity score</span><strong>${scoreBar(job.opportunity_score)}</strong></div>
      </div>
      ${job.job_url ? `<p><a class="btn" href="${esc(job.job_url)}" target="_blank" rel="noopener noreferrer">Open original posting ↗</a></p>` : ''}
      ${job.company_detail ? `
        <h3>Employer</h3>
        <div class="grid">
          <div class="kv"><span>Industry</span><strong>${esc(job.company_detail.industry || '—')}</strong></div>
          <div class="kv"><span>Open postings found</span><strong>${job.company_detail.jobs_discovered ?? 0}</strong></div>
          <div class="kv"><span>ATS platform</span><strong>${esc(job.company_detail.ats_platform || 'not detected')}</strong></div>
          <div class="kv"><span>Hiring per week (30d)</span><strong>${job.company_detail.hiring_frequency_30d ?? 0}</strong></div>
        </div>` : ''}
    </div>

    <div class="so-body" data-panel="description" hidden>
      ${job.description_is_truncated ? `
        <p class="desc-truncated">This description was truncated by the source.
           Full text arrives if the employer's ATS board is discovered.</p>` : ''}
      <div class="desc">${job.description_text
      ? esc(job.description_text).replace(/\n/g, '<br>')
      : '<span class="t-sub">No description was provided by the source.</span>'}</div>
    </div>

    <div class="so-body" data-panel="extracted" hidden>
      <h3>Required skills</h3><div class="chips">${skillChips(req)}</div>
      <h3>Preferred skills</h3><div class="chips">${skillChips(pref)}</div>
      <h3>Also mentioned</h3><div class="chips">${skillChips(other)}</div>
      <h3>Benefits</h3>
      <div class="chips">${(job.benefits || []).length
      ? job.benefits.map(b => `<span class="chip">${esc(titleCase(b))}</span>`).join('')
      : '<span class="t-sub">None detected</span>'}</div>
      <h3>Signals</h3>
      <div class="grid">
        <div class="kv"><span>Visa sponsorship</span><strong>${flagText(job.visa_sponsorship_mentioned)}</strong></div>
        <div class="kv"><span>Relocation</span><strong>${flagText(job.relocation_mentioned)}</strong></div>
        <div class="kv"><span>Urgently hiring</span><strong>${flagText(job.urgently_hiring)}</strong></div>
      </div>
      ${(job.contacts && (job.contacts.emails.length || job.contacts.phones.length)) ? `
        <h3>Contact details stated in the posting</h3>
        <div class="chips">
          ${job.contacts.emails.map(e => `<span class="chip">${esc(e)}</span>`).join('')}
          ${job.contacts.phones.map(p => `<span class="chip">${esc(p)}</span>`).join('')}
        </div>` : ''}
    </div>

    <div class="so-body" data-panel="research" hidden>
      <div class="grid">
        <label class="kv"><span>Status</span>
          <select id="r_status">
            ${['new', 'reviewed', 'relevant', 'not_relevant', 'follow_up', 'archived']
      .map(s => `<option value="${s}" ${job.research && job.research.status === s ? 'selected' : ''}>${titleCase(s)}</option>`).join('')}
          </select></label>
        <label class="kv"><span>Priority</span>
          <select id="r_priority">
            <option value="">—</option>
            ${['high', 'medium', 'low'].map(p =>
        `<option value="${p}" ${job.research && job.research.priority === p ? 'selected' : ''}>${titleCase(p)}</option>`).join('')}
          </select></label>
        <label class="kv"><span>Lead quality (1–5)</span>
          <input id="r_lead_quality" type="number" min="1" max="5"
            value="${job.research && job.research.lead_quality ? job.research.lead_quality : ''}"></label>
        <label class="kv"><span>Tags (comma separated)</span>
          <input id="r_tags" value="${esc((job.tags || []).join(', '))}"></label>
      </div>
      <label class="kv"><span>Research notes</span>
        <textarea id="r_notes" rows="6">${esc(job.research ? job.research.notes || '' : '')}</textarea></label>

      ${state.customFields.length ? `<h3>Custom fields</h3><div class="grid">
        ${state.customFields.filter(f => f.entity === 'job').map(f => customValueInput(f,
        job.custom_fields ? job.custom_fields[f.key] : null)).join('')}
      </div>` : ''}

      <div class="btn-row">
        <button class="btn primary" id="saveResearch">Save research</button>
      </div>
    </div>

    <div class="so-body" data-panel="provenance" hidden>
      <div class="grid">
        <div class="kv"><span>Source</span><strong>${esc(job.source || '—')}</strong></div>
        <div class="kv"><span>Source job ID</span><strong>${esc(job.source_job_id || '—')}</strong></div>
        <div class="kv"><span>Primary record</span><strong>${job.is_primary ? 'Yes' : 'No'}</strong></div>
        <div class="kv"><span>Canonical fingerprint</span>
          <strong class="t-sub">${esc((job.canonical_hash || '').slice(0, 16))}</strong></div>
      </div>
      ${(job.duplicates || []).length ? `
        <h3>Linked duplicate records</h3>
        <div class="table-wrap"><table>
          <thead><tr><th>Source</th><th>Collected</th><th>URL</th></tr></thead>
          <tbody>${job.duplicates.map(d => `<tr>
            <td>${esc(d.source || '—')}</td><td class="t-sub">${ago(d.date_collected)}</td>
            <td>${d.job_url ? `<a href="${esc(d.job_url)}" target="_blank" rel="noopener noreferrer">open ↗</a>` : '—'}</td>
          </tr>`).join('')}</tbody>
        </table></div>
        <p class="t-sub">Duplicates are linked rather than deleted — appearing on
           several boards is itself a signal of how hard a role is being pushed.</p>
      ` : '<p class="t-sub">No duplicate records linked to this posting.</p>'}
    </div>`;
}

function flagText(v) {
  if (v === true) return 'Yes';
  if (v === false) return 'Explicitly no';
  return 'Not mentioned';
}

function customValueInput(f, value) {
  const id = `cv_${f.key}`;
  const v = value === null || value === undefined ? '' : value;
  let input;
  if (f.field_type === 'boolean') {
    input = `<select id="${id}" data-cv="${esc(f.key)}">
      <option value="">—</option>
      <option value="true" ${v === true ? 'selected' : ''}>Yes</option>
      <option value="false" ${v === false ? 'selected' : ''}>No</option></select>`;
  } else if (f.field_type === 'dropdown') {
    input = `<select id="${id}" data-cv="${esc(f.key)}"><option value="">—</option>
      ${(f.options || []).map(o => `<option ${o === v ? 'selected' : ''}>${esc(o)}</option>`).join('')}</select>`;
  } else if (f.field_type === 'longtext') {
    input = `<textarea id="${id}" data-cv="${esc(f.key)}" rows="3">${esc(v)}</textarea>`;
  } else if (f.field_type === 'number' || f.field_type === 'score') {
    input = `<input id="${id}" data-cv="${esc(f.key)}" type="number" value="${esc(v)}">`;
  } else {
    input = `<input id="${id}" data-cv="${esc(f.key)}" value="${esc(v)}">`;
  }
  return `<label class="kv"><span>${esc(f.label)}</span>${input}</label>`;
}

function wireJobDetail(job) {
  $('#soClose').addEventListener('click', closeSlideover);

  $$('.so-tab').forEach(tab => tab.addEventListener('click', () => {
    $$('.so-tab').forEach(t => t.classList.toggle('active', t === tab));
    $$('.so-body').forEach(p =>
      p.hidden = p.dataset.panel !== tab.dataset.tab);
  }));

  $$('[data-skill]').forEach(chip => chip.addEventListener('click', () => {
    state.query = emptyQuery();
    state.query.required_skills = [chip.dataset.skill];
    closeSlideover();
    location.hash = '#/results';
    if (state.route === 'results') renderResults();
  }));

  const saveBtn = $('#saveResearch');
  if (saveBtn) saveBtn.addEventListener('click', async () => {
    const lead = $('#r_lead_quality').value;
    try {
      await api(`/research/${job.id}`, {
        method: 'PUT',
        body: {
          status: $('#r_status').value || null,
          priority: $('#r_priority').value || null,
          lead_quality: lead ? Number(lead) : null,
          notes: $('#r_notes').value || null,
          tags: $('#r_tags').value.split(',').map(s => s.trim()).filter(Boolean),
        },
      });

      for (const el of $$('[data-cv]')) {
        if (el.value === '') continue;
        const f = state.customFields.find(x => x.key === el.dataset.cv);
        let value = el.value;
        if (f.field_type === 'boolean') value = el.value === 'true';
        else if (f.field_type === 'number' || f.field_type === 'score')
          value = Number(el.value);
        await api(`/custom-fields/${encodeURIComponent(el.dataset.cv)}/value`, {
          method: 'PUT',
          body: { entity: 'job', entity_id: job.id, value },
        });
      }

      toast('Research saved.', 'ok');
      closeSlideover();
      if (state.route === 'results') renderResults();
    } catch (err) { showError(err); }
  });
}

// ==========================================================================
// Saved searches
// ==========================================================================
async function renderSaved() {
  const rows = await api('/saved-searches');
  $('#workspace').innerHTML = `
    <div class="page-head">
      <div><span class="eyebrow">Reusable</span><h1>Saved searches</h1></div>
      <a class="btn" href="#/search">New search</a>
    </div>
    <div class="panel"><div class="table-wrap"><table>
      <thead><tr><th>Name</th><th>Filters</th><th>Last run</th><th></th></tr></thead>
      <tbody>${rows.length ? rows.map(r => `
        <tr>
          <td class="t-title">${esc(r.name)}</td>
          <td class="t-sub">${esc(Object.entries(r.query || {})
      .filter(([k, v]) => v && !['page', 'page_size', 'sort', 'sort_dir'].includes(k))
      .map(([k, v]) => `${titleCase(k)}: ${Array.isArray(v) ? v.join('/') : v}`)
      .slice(0, 4).join(' · ') || 'No filters')}</td>
          <td class="t-sub">${r.last_run_at ? ago(r.last_run_at) : 'never'}</td>
          <td class="btn-row">
            <button class="btn" data-run="${r.id}">Run</button>
            <button class="btn ghost" data-del="${r.id}">Delete</button>
          </td>
        </tr>`).join('')
      : '<tr><td colspan="4" class="t-sub">No saved searches yet.</td></tr>'}
      </tbody></table></div></div>`;

  $$('[data-run]').forEach(b => b.addEventListener('click', async () => {
    const row = rows.find(r => r.id === Number(b.dataset.run));
    state.query = Object.assign(emptyQuery(), row.query);
    location.hash = '#/results';
  }));

  $$('[data-del]').forEach(b => b.addEventListener('click', async () => {
    if (!confirm('Delete this saved search?')) return;
    await api(`/saved-searches/${b.dataset.del}`, { method: 'DELETE' });
    toast('Saved search deleted.', 'ok');
    renderSaved();
  }));
}

// ==========================================================================
// Companies
// ==========================================================================
async function renderCompanies() {
  const data = await api('/companies?limit=200&sort=jobs');
  const rows = data.results;

  $('#workspace').innerHTML = `
    <div class="page-head">
      <div><span class="eyebrow">${rows.length} tracked</span><h1>Companies</h1></div>
      <button class="btn" id="btnRefreshCompanies">Recompute metrics</button>
    </div>
    <p class="note">Employers whose ATS board has been discovered are monitored
       free of charge from then on — that is what makes free-tier collection
       compound over time.</p>
    <div class="panel"><div class="table-wrap"><table>
      <thead><tr><th>Company</th><th>Industry</th><th>Postings</th>
        <th>Cadence</th><th>Per week</th><th>ATS</th><th>Score</th></tr></thead>
      <tbody>${rows.length ? rows.map(c => `
        <tr data-company="${c.id}">
          <td class="t-title">${esc(c.name)}</td>
          <td class="t-sub">${esc(c.industry || '—')}</td>
          <td>${c.jobs_discovered ?? 0}</td>
          <td class="t-sub">${c.hiring_frequency_30d > 0 ? 'active' : '—'}</td>
          <td class="t-sub">${c.hiring_frequency_30d ?? 0}</td>
          <td>${c.ats_platform
      ? `<span class="chip ok">${esc(c.ats_platform)}</span>`
      : '<span class="t-sub">—</span>'}</td>
          <td>${scoreBar(c.opportunity_score)}</td>
        </tr>`).join('')
      : '<tr><td colspan="7" class="t-sub">No companies yet.</td></tr>'}
      </tbody></table></div></div>`;

  $('#btnRefreshCompanies').addEventListener('click', async () => {
    try {
      const r = await api('/maintenance/refresh-companies', { method: 'POST' });
      toast('Company metrics recomputed.', 'ok',
        `${r.companies_updated ?? 0} companies updated`);
      renderCompanies();
    } catch (err) { showError(err); }
  });

  $$('tr[data-company]').forEach(tr => tr.addEventListener('click', async () => {
    try {
      const c = await api(`/companies/${tr.dataset.company}`);
      showCompany(c);
    } catch (err) { showError(err); }
  }));
}

function showCompany(c) {
  $('#slideover').hidden = false;
  $('#overlay').hidden = false;
  const skills = (c.top_skills || []).map(s => ({ name: s.value, count: s.count }));
  $('#slideoverBody').innerHTML = `
    <div class="so-head">
      <div><h2>${esc(c.name)}</h2>
        <p class="t-sub">${esc(c.industry || 'Industry unknown')}
          ${c.domain ? ' · ' + esc(c.domain) : ''}</p></div>
      <button class="so-close" id="soClose">×</button>
    </div>
    <div class="so-body">
      <div class="grid">
        <div class="kv"><span>Postings found</span><strong>${c.jobs_total ?? 0}</strong></div>
        <div class="kv"><span>Remote roles</span><strong>${c.jobs_remote ?? 0}</strong></div>
        <div class="kv"><span>Hiring per week</span><strong>${c.hiring_frequency_30d ?? 0}</strong></div>
        <div class="kv"><span>ATS</span><strong>${esc(c.ats_platform || 'not detected')}</strong></div>
        <div class="kv"><span>Opportunity score</span><strong>${scoreBar(c.opportunity_score)}</strong></div>
      </div>
      ${skills.length ? `<h3>Most requested skills</h3><div class="bars">
        ${skills.slice(0, 10).map(s => `<div class="bar-row">
          <span class="bar-label">${esc(s.name)}</span>
          <span class="bar-track"><span class="bar-fill"
            style="width:${Math.min(100, (s.count / skills[0].count) * 100)}%"></span></span>
          <span class="bar-val">${s.count}</span></div>`).join('')}
      </div>` : ''}
      ${(c.jobs || []).length ? `<h3>Open postings</h3>
        <div class="table-wrap"><table>
          <thead><tr><th>Title</th><th>Location</th><th>Posted</th></tr></thead>
          <tbody>${c.jobs.slice(0, 40).map(j => `<tr data-job="${j.id}">
            <td class="t-title">${esc(j.title)}</td>
            <td class="t-sub">${esc(j.location || '—')}</td>
            <td class="t-sub">${ago(j.date_posted)}</td></tr>`).join('')}
          </tbody></table></div>` : ''}
    </div>`;

  $('#soClose').addEventListener('click', closeSlideover);
  $$('#slideoverBody tr[data-job]').forEach(tr =>
    tr.addEventListener('click', () => openJob(tr.dataset.job)));
}

// ==========================================================================
// Analytics
// ==========================================================================
async function renderAnalytics() {
  const body = cleanQuery(state.query);
  const [overview, skillData] = await Promise.all([
    api('/analytics/overview', { method: 'POST', body }),
    api('/analytics/skills', { method: 'POST', body }),
  ]);

  const barBlock = (title, items, labelKey, valKey, fmt) => {
    if (!items || !items.length)
      return `<div class="panel"><div class="panel-head"><h2>${esc(title)}</h2></div>
        <div class="sect-body"><p class="t-sub">Not enough data yet.</p></div></div>`;
    const max = Math.max(...items.map(i => i[valKey]));
    return `<div class="panel"><div class="panel-head"><h2>${esc(title)}</h2></div>
      <div class="sect-body bars">
        ${items.slice(0, 12).map(i => `<div class="bar-row">
          <span class="bar-label" title="${esc(i[labelKey])}">${esc(titleCase(i[labelKey]))}</span>
          <span class="bar-track"><span class="bar-fill"
            style="width:${Math.min(100, (i[valKey] / max) * 100)}%"></span></span>
          <span class="bar-val">${fmt ? fmt(i[valKey]) : i[valKey].toLocaleString()}</span>
        </div>`).join('')}
      </div></div>`;
  };

  const arrangements = overview.by_work_arrangement || [];
  const remoteCount = (arrangements.find(a => a.label === 'remote') || {}).count || 0;
  const remoteShare = overview.total_jobs
    ? Math.round((remoteCount / overview.total_jobs) * 100) : null;
  const avgSalary = overview.salary_summary && overview.salary_summary.avg_annualized;

  $('#workspace').innerHTML = `
    <div class="page-head">
      <div><span class="eyebrow">Phase 7</span><h1>Analytics</h1></div>
      <a class="btn ghost" href="#/search">Change filters</a>
    </div>

    <p class="note"><strong>Read these as relative, not absolute.</strong>
      ${esc(overview.coverage_note || '')}</p>

    <div class="grid">
      <div class="stat"><span class="t-sub">Postings in view</span>
        <strong>${(overview.total_jobs || 0).toLocaleString()}</strong></div>
      <div class="stat"><span class="t-sub">Distinct employers</span>
        <strong>${(overview.by_company || []).length.toLocaleString()}</strong></div>
      <div class="stat"><span class="t-sub">Average salary</span>
        <strong>${avgSalary ? money(avgSalary) : '—'}</strong></div>
      <div class="stat"><span class="t-sub">Remote share</span>
        <strong>${remoteShare !== null ? remoteShare + '%' : '—'}</strong></div>
    </div>

    <div class="results-layout">
      <div class="panel">
        <div class="panel-head"><h2>Most requested skills</h2>
          <span class="t-sub">share of postings</span></div>
        <div class="sect-body bars">
          ${(skillData.skills || []).length
      ? skillData.skills.slice(0, 14).map(s => `<div class="bar-row">
              <span class="bar-label" title="${esc(s.skill)} — required in ${s.required_in}, preferred in ${s.preferred_in}">${esc(s.skill)}</span>
              <span class="bar-track"><span class="bar-fill"
                style="width:${Math.min(100, (s.jobs / skillData.skills[0].jobs) * 100)}%"></span></span>
              <span class="bar-val">${s.jobs} · ${s.share}%</span>
            </div>`).join('')
      : '<p class="t-sub">No skills extracted yet.</p>'}
        </div>
      </div>
      ${barBlock('Hiring companies', overview.by_company, 'label', 'count')}
    </div>

    <div class="results-layout">
      ${barBlock('By city', overview.by_city, 'label', 'count')}
      ${barBlock('By state', overview.by_state, 'label', 'count')}
    </div>
    <div class="results-layout">
      ${barBlock('By salary band', overview.by_salary_band, 'label', 'count')}
      ${barBlock('By experience level', overview.by_experience_level, 'label', 'count')}
    </div>
    <div class="results-layout">
      ${barBlock('By work arrangement', overview.by_work_arrangement, 'label', 'count')}
      ${barBlock('By employment type', overview.by_employment_type, 'label', 'count')}
    </div>
    <div class="results-layout">
      ${barBlock('Most common titles', overview.by_title, 'label', 'count')}
      ${barBlock('Postings by date', (overview.timeline || []).slice(-12), 'date', 'count')}
    </div>`;
}

// ==========================================================================
// Research board
// ==========================================================================
async function renderResearch() {
  const summary = await api('/research/summary/status');
  const q = Object.assign(emptyQuery(), {
    research_status: ['relevant', 'follow_up', 'reviewed'], page_size: 100,
  });
  const data = await api('/search', { method: 'POST', body: cleanQuery(q) });

  $('#workspace').innerHTML = `
    <div class="page-head">
      <div><span class="eyebrow">Phase 6</span><h1>Research</h1></div>
    </div>

    <div class="grid">
      ${Object.entries(summary).map(([status, count]) => `
        <div class="stat"><span class="t-sub">${titleCase(status)}</span>
          <strong>${count}</strong></div>`).join('')}
    </div>

    <div class="panel">
      <div class="panel-head"><h2>Active research queue</h2>
        <span class="t-sub">Reviewed, relevant and follow-up items</span></div>
      <div class="table-wrap"><table>
        <thead><tr><th>Title</th><th>Company</th><th>Status</th>
          <th>Priority</th><th>Score</th><th>Notes</th></tr></thead>
        <tbody>${data.results.length ? data.results.map(j => `
          <tr data-job="${j.id}">
            <td class="t-title">${esc(j.title)}</td>
            <td>${esc(j.company || '—')}</td>
            <td><span class="badge ${esc(j.research ? j.research.status : 'new')}">
              ${titleCase(j.research ? j.research.status : 'new')}</span></td>
            <td class="t-sub">${titleCase(j.research ? j.research.priority : null)}</td>
            <td>${scoreBar(j.opportunity_score)}</td>
            <td class="t-sub">${esc((j.research && j.research.notes || '').slice(0, 80))}</td>
          </tr>`).join('')
      : '<tr><td colspan="6" class="t-sub">Queue is empty.</td></tr>'}
        </tbody></table></div>
    </div>`;

  $$('tr[data-job]').forEach(tr =>
    tr.addEventListener('click', () => openJob(tr.dataset.job)));
}

// ==========================================================================
// Exports
// ==========================================================================
async function renderExports() {
  const rows = await api('/exports');
  $('#workspace').innerHTML = `
    <div class="page-head">
      <div><span class="eyebrow">Phase 8</span><h1>Exports</h1></div>
      <div class="btn-row">
        <button class="btn" data-fmt="csv">Export current filters (CSV)</button>
        <button class="btn" data-fmt="xlsx">XLSX</button>
        <button class="btn" data-fmt="json">JSON</button>
      </div>
    </div>
    <p class="note">Exports always respect the filters currently set on the
       Search page — not the whole database.</p>
    <div class="panel"><div class="table-wrap"><table>
      <thead><tr><th>File</th><th>Format</th><th>Rows</th><th>Created</th><th></th></tr></thead>
      <tbody>${rows.length ? rows.map(r => `
        <tr>
          <td class="t-title">${esc(r.filename || r.file_path)}</td>
          <td><span class="chip">${esc(r.format)}</span></td>
          <td>${(r.rows || 0).toLocaleString()}</td>
          <td class="t-sub">${ago(r.created_at)}</td>
          <td>${r.exists
      ? `<a class="btn ghost" href="/api/exports/download/${encodeURIComponent(r.filename)}">Download</a>`
      : '<span class="t-sub">file removed</span>'}</td>
        </tr>`).join('')
      : '<tr><td colspan="5" class="t-sub">No exports yet.</td></tr>'}
      </tbody></table></div></div>`;

  $$('[data-fmt]').forEach(b =>
    b.addEventListener('click', () => doExport(b.dataset.fmt)));
}

// ==========================================================================
// Settings
// ==========================================================================
async function renderSettings() {
  const [cfg, sources, fields, rules] = await Promise.all([
    api('/settings'),
    api('/sources'),
    api('/custom-fields'),
    api('/scoring-rules'),
  ]);
  state.customFields = fields;
  window.__rules = rules;

  $('#workspace').innerHTML = `
    <div class="page-head">
      <div><span class="eyebrow">Configuration</span><h1>Settings</h1></div>
    </div>

    <div class="panel">
      <div class="panel-head"><h2>Data sources</h2></div>
      <div class="sect-body">
        ${sources.map(s => `
          <div class="src-row">
            <label class="switch">
              <input type="checkbox" data-src="${esc(s.key)}" ${s.enabled ? 'checked' : ''}>
              <span></span>
            </label>
            <span class="src-name">${esc(s.display_name)}</span>
            <span class="chip">${esc(s.kind)}</span>
            <span class="t-sub">
              ${s.requires_credentials
      ? (s.configured ? 'credentials set' : 'needs credentials')
      : 'no credentials needed'}
              ${s.monthly_quota ? ` · ${s.calls_used}/${s.monthly_quota} calls` : ''}
              ${s.kind === 'ats' ? ` · ${s.ats_boards_known} boards known` : ''}
            </span>
            <button class="btn ghost" data-test="${esc(s.key)}">Test</button>
          </div>`).join('')}

        <h3>Credentials</h3>
        <p class="t-sub">Adzuna: free App ID and Key at developer.adzuna.com.
           USAJOBS: free key at developer.usajobs.gov.</p>
        <div class="grid">
          <label class="kv"><span>Adzuna App ID</span>
            <input id="s_adzuna_app_id" value="${esc(cfg.adzuna_app_id || '')}"></label>
          <label class="kv"><span>Adzuna App Key</span>
            <input id="s_adzuna_app_key" type="password" value="${esc(cfg.adzuna_app_key || '')}"></label>
          <label class="kv"><span>USAJOBS API key</span>
            <input id="s_usajobs_api_key" type="password" value="${esc(cfg.usajobs_api_key || '')}"></label>
          <label class="kv"><span>USAJOBS email</span>
            <input id="s_usajobs_email" value="${esc(cfg.usajobs_email || '')}"></label>
        </div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-head"><h2>Collection behaviour</h2></div>
      <div class="sect-body"><div class="grid">
        <label class="kv"><span>Max results per run</span>
          <input id="s_max_results_per_run" type="number" min="10" max="20000"
            value="${cfg.max_results_per_run ?? 500}"></label>
        <label class="kv"><span>Requests per minute</span>
          <input id="s_rate_limit_per_minute" type="number" min="1" max="120"
            value="${cfg.rate_limit_per_minute ?? 20}"></label>
        <label class="kv"><span>Cache TTL (minutes)</span>
          <input id="s_cache_ttl_minutes" type="number" min="0" max="1440"
            value="${cfg.cache_ttl_minutes ?? 60}"></label>
        <label class="kv"><span>Default page size</span>
          <input id="s_default_page_size" type="number" min="10" max="500"
            value="${cfg.default_page_size ?? 50}"></label>
        <label class="kv"><span>Duplicate title threshold</span>
          <input id="s_dupe_title_ratio" type="number" step="0.01" min="0.5" max="1"
            value="${cfg.dupe_title_ratio ?? 0.92}"></label>
        <label class="kv"><span>Duplicate description threshold</span>
          <input id="s_dupe_description_similarity" type="number" step="0.01" min="0.5" max="1"
            value="${cfg.dupe_description_similarity ?? 0.85}"></label>
      </div>
      <div class="btn-row"><button class="btn primary" id="saveSettings">Save settings</button></div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-head"><h2>Custom research fields</h2></div>
      <div class="sect-body">
        ${fields.length ? fields.map(f => `
          <div class="src-row">
            <span class="src-name">${esc(f.label)}</span>
            <span class="chip">${esc(f.field_type)}</span>
            <span class="t-sub">${esc(f.key)}${f.options && f.options.length
      ? ' · ' + esc(f.options.join(', ')) : ''}</span>
            <button class="btn ghost" data-delfield="${f.id}">Delete</button>
          </div>`).join('') : '<p class="t-sub">No custom fields yet.</p>'}

        <h3>Add a field</h3>
        <div class="grid">
          <label class="kv"><span>Key (lowercase, no spaces)</span>
            <input id="nf_key" placeholder="lead_quality"></label>
          <label class="kv"><span>Label</span>
            <input id="nf_label" placeholder="Lead Quality"></label>
          <label class="kv"><span>Type</span>
            <select id="nf_type">
              ${['text', 'longtext', 'number', 'boolean', 'date', 'dropdown',
        'multiselect', 'score'].map(t => `<option value="${t}">${titleCase(t)}</option>`).join('')}
            </select></label>
          <label class="kv"><span>Options (comma separated, for dropdowns)</span>
            <input id="nf_options" placeholder="High, Medium, Low"></label>
        </div>
        <div class="btn-row"><button class="btn" id="addField">Create field</button></div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-head"><h2>Opportunity scoring rules</h2>
        <button class="btn ghost" id="rescore">Rescore all jobs</button></div>
      <div class="sect-body">
        <p class="t-sub">The formula is data, not code. Toggle rules or change
           weights, then rescore.</p>
        ${rules.map(r => `
          <div class="src-row">
            <label class="switch">
              <input type="checkbox" data-rule="${r.id}" ${r.is_active ? 'checked' : ''}>
              <span></span></label>
            <span class="src-name">${esc(r.name)}</span>
            <span class="chip">${r.points} pts</span>
            <span class="t-sub">${esc(JSON.stringify(r.condition))}</span>
          </div>`).join('')}
      </div>
    </div>

    <div class="panel">
      <div class="panel-head"><h2>Compliance</h2></div>
      <div class="sect-body">
        <p><strong>Indeed is not available as a data source.</strong> Its Publisher
           API was deprecated in 2023 and closed to new integrations; the remaining
           APIs are employer-side only; and scraping the site would breach its terms
           and require defeating anti-bot measures. This application contains no
           functionality for bypassing CAPTCHAs, authentication, rate limits or
           access controls, and never will.</p>
        <p class="t-sub">The collection layer is modular. If partner access to
           another provider becomes available, it can be added as one adapter
           without touching the research interface.</p>
      </div>
    </div>`;

  wireSettings(cfg);
}

function wireSettings(cfg) {
  $$('[data-src]').forEach(el => el.addEventListener('change', async () => {
    try {
      await api(`/sources/${el.dataset.src}/toggle?enabled=${el.checked}`,
        { method: 'POST' });
      await refreshMeters();
      toast(`${el.dataset.src} ${el.checked ? 'enabled' : 'disabled'}.`, 'ok');
    } catch (err) { showError(err); el.checked = !el.checked; }
  }));

  $$('[data-test]').forEach(btn => btn.addEventListener('click', async () => {
    btn.disabled = true;
    btn.textContent = 'Testing…';
    try {
      const all = await api('/sources/health-check', { method: 'POST' });
      const result = all.find(r => r.key === btn.dataset.test) || {};
      toast(result.ok ? `${btn.dataset.test} is reachable.`
        : `${btn.dataset.test} is not usable.`,
        result.ok ? 'ok' : 'error', result.message || '');
    } catch (err) { showError(err); }
    btn.disabled = false;
    btn.textContent = 'Test';
    refreshMeters();
  }));

  $('#saveSettings').addEventListener('click', async () => {
    const values = {};
    ['adzuna_app_id', 'adzuna_app_key', 'usajobs_api_key', 'usajobs_email']
      .forEach(k => { values[k] = $(`#s_${k}`).value.trim(); });
    ['max_results_per_run', 'rate_limit_per_minute', 'cache_ttl_minutes',
      'default_page_size', 'dupe_title_ratio', 'dupe_description_similarity']
      .forEach(k => { values[k] = Number($(`#s_${k}`).value); });
    try {
      await api('/settings', { method: 'PUT', body: { values } });
      toast('Settings saved.', 'ok');
      await refreshMeters();
    } catch (err) { showError(err); }
  });

  $('#addField').addEventListener('click', async () => {
    const key = $('#nf_key').value.trim();
    const label = $('#nf_label').value.trim();
    if (!key || !label) {
      toast('A key and a label are both required.', 'error');
      return;
    }
    try {
      await api('/custom-fields', {
        method: 'POST',
        body: {
          key, label, entity: 'job', field_type: $('#nf_type').value,
          options: $('#nf_options').value.split(',').map(s => s.trim()).filter(Boolean),
        },
      });
      toast('Custom field created.', 'ok',
        'It is now available as a filter and on every job.');
      renderSettings();
    } catch (err) { showError(err); }
  });

  $$('[data-delfield]').forEach(b => b.addEventListener('click', async () => {
    if (!confirm('Delete this field and all values recorded in it?')) return;
    try {
      await api(`/custom-fields/${b.dataset.delfield}`, { method: 'DELETE' });
      toast('Field deleted.', 'ok');
      renderSettings();
    } catch (err) { showError(err); }
  }));

  $('#rescore').addEventListener('click', async () => {
    try {
      const r = await api('/scoring-rules/rescore', { method: 'POST' });
      toast('Rescoring complete.', 'ok', `${r.rescored ?? 0} jobs updated`);
    } catch (err) { showError(err); }
  });

  $$('[data-rule]').forEach(el => el.addEventListener('change', async () => {
    const rule = (window.__rules || []).find(r => r.id === Number(el.dataset.rule));
    if (!rule) return;
    try {
      await api('/scoring-rules', {
        method: 'POST',
        body: {
          id: rule.id, name: rule.name, entity: rule.entity,
          weight: rule.weight, points: rule.points, condition: rule.condition,
          is_active: el.checked,
        },
      });
      toast('Rule updated.', 'ok', 'Rescore to apply it to existing jobs.');
    } catch (err) { showError(err); el.checked = !el.checked; }
  }));
}

// ==========================================================================
// Boot
// ==========================================================================
window.addEventListener('hashchange', navigate);
$('#overlay').addEventListener('click', closeSlideover);
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeSlideover();
});

$('#quickSearch').addEventListener('keydown', e => {
  if (e.key !== 'Enter') return;
  const value = e.target.value.trim();
  state.query = emptyQuery();
  if (/\b(AND|OR|NOT)\b|["()]/.test(value)) state.query.boolean = value;
  else state.query.keywords = value;
  if (location.hash === '#/results') renderResults();
  else location.hash = '#/results';
});

(async function boot() {
  await refreshMeters();
  setCollecting(false);
  await navigate();
})();
