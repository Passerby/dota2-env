// The page's state and its left column: picking and following a transcript, the decision list and its filters.
// detail.js draws the middle column from this state, resend.js the right one.

import { decisionView, overview } from './detail.js';
import * as log from './log.js';
import * as resend from './resend.js';
import { el, usd } from './ui.js';

const $ = (id) => document.getElementById(id);
const POLL_MS = 2000;

const state = {
  meta: null,
  name: null,
  offset: 0,
  records: [],
  index: log.buildIndex([]),
  generation: 0, // bumped by every openLog, so a read that raced a switch drops what it got
  pulling: false,
  follow: false,
  selected: null,
  tab: 'reply',
  overview: false,
  heroes: new Set(),
  status: 'all',
  search: '',
  shown: [],
};

async function getJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url} ${response.status}`);
  return response.json();
}

const setStatus = (text) => {
  $('status').textContent = text;
};

// Reads what the transcript gained since the last read; true when that was at least one record.
async function pull() {
  const { name, offset, generation } = state;
  const response = await fetch(`/api/log?name=${encodeURIComponent(name)}&offset=${offset}`);
  if (!response.ok) throw new Error(`读不到 ${name}：${response.status}`);
  const text = await response.text();
  if (generation !== state.generation) return false;
  state.offset = Number(response.headers.get('X-Offset'));
  if (!text) return false;
  state.records = state.records.concat(log.parseChunk(text));
  state.index = log.buildIndex(state.records);
  return true;
}

// Refilled only when the names change, so an open dropdown is not pulled from under the pointer every tick.
function fillLogs(logs) {
  const label = (item) => `${item.name}  ${(item.size / 1e6).toFixed(1)} MB`;
  const options = [...$('log').options];
  if (options.length === logs.length && options.every((option, k) => option.value === logs[k].name)) {
    options.forEach((option, k) => {
      option.textContent = label(logs[k]);
    });
    return;
  }
  $('log').replaceChildren(...logs.map((item) => el('option', { value: item.name }, label(item))));
  if (state.name) $('log').value = state.name;
}

async function openLog(name, wanted = null) {
  state.generation += 1;
  Object.assign(state, { name, offset: 0, records: [], index: log.buildIndex([]), selected: null });
  state.heroes.clear();
  $('log').value = name;
  setStatus(`读取 ${name}…`);
  await pull();
  setStatus('');
  renderHeroes();
  renderTotals();
  renderList();
  select(wanted !== null && wanted < state.index.entries.length ? wanted : (state.shown[0] ?? null));
}

// Every tick keeps the log list current; only while following does it read new records or switch logs.
async function poll() {
  if (state.pulling) return;
  state.pulling = true;
  try {
    const { logs } = await getJson('/api/logs');
    fillLogs(logs);
    if (!state.name && logs.length) await openLog(logs[0].name);
    if (!state.follow) return;
    const current = logs.find((item) => item.name === state.name);
    if (logs.length && logs[0].name !== state.name && (!current || logs[0].mtime > current.mtime)) {
      await openLog(logs[0].name);
      return;
    }
    const selected = state.selected === null ? null : state.index.entries[state.selected];
    const before = { entries: state.index.entries.length, heroes: state.index.heroes.size, rejected: selected?.rejected.length };
    if (state.name && (await pull())) grow(before);
  } catch (error) {
    setStatus(String(error));
  } finally {
    state.pulling = false;
  }
}

// New records came in while following: append their rows instead of drawing the list again.
function grow(before) {
  const { entries } = state.index;
  const list = $('list');
  const atBottom = list.scrollTop + list.clientHeight >= list.scrollHeight - 8;
  // a rejected record often lands after its decision, which changes a row that is already there
  for (let i = Math.max(0, before.entries - 50); i < before.entries; i += 1) {
    const row = list.querySelector(`li[data-i="${i}"]`);
    if (row && Number(row.dataset.rejected) !== entries[i].rejected.length) row.replaceWith(rowOf(entries[i]));
  }
  for (let i = before.entries; i < entries.length; i += 1) {
    if (!passes(entries[i])) continue;
    state.shown.push(i);
    list.append(rowOf(entries[i]));
  }
  if (atBottom) list.scrollTop = list.scrollHeight;
  if (state.index.heroes.size !== before.heroes) renderHeroes();
  renderCount();
  renderTotals();
  if (state.selected === null && state.shown.length) select(state.shown[0]);
  else if (state.selected !== null && entries[state.selected].rejected.length !== before.rejected) renderDetail();
}

function passes(entry) {
  if (state.heroes.size && !state.heroes.has(entry.hero.key)) return false;
  if (state.status === 'problem' && !log.hasProblem(entry)) return false;
  if (state.status === 'error' && !entry.record.error) return false;
  if (state.status === 'rejected' && !entry.rejected.length) return false;
  return log.matches(entry, state.search);
}

function renderHeroes() {
  const chip = (key, label, title) =>
    el('button', { type: 'button', class: `chip${state.heroes.has(key) ? ' on' : ''}`, dataset: { hero: key }, title }, label);
  $('heroes').replaceChildren(
    el('button', { type: 'button', class: `chip${state.heroes.size ? '' : ' on'}`, dataset: { hero: '' } }, '全部'),
    ...[...state.index.heroes.values()].map((hero) =>
      chip(hero.key, hero.nickname, log.heroName(state.meta, log.heroInfo(hero, state.meta).hero)),
    ),
  );
}

function renderTotals() {
  const { entries, rejected } = state.index;
  const decisions = entries.filter((entry) => entry.channel === 'act').length;
  const errors = entries.filter((entry) => entry.record.error).length;
  const spent = entries.reduce((sum, entry) => sum + (entry.record.usd ?? 0), 0);
  $('totals').textContent =
    `${decisions} 次决策 · ${entries.length - decisions} 次长思考和复盘 · 出错 ${errors} · 被拒 ${rejected.length} · ${usd(spent)}`;
}

// What a row says: the reason of a decision, the plan of a long think, the lessons of a review, else the error.
function gist(entry) {
  const { record } = entry;
  if (entry.channel === 'think') return record.plan ?? record.error ?? '';
  if (entry.channel === 'review') return (record.lessons ?? []).map((lesson) => lesson.text).join('；') || record.error || '';
  return record.reason || record.error || '';
}

function rowOf(entry) {
  const { record } = entry;
  return el(
    'li',
    { dataset: { i: entry.i, rejected: entry.rejected.length }, class: entry.i === state.selected ? 'selected' : null },
    el('span', { class: 'time' }, log.clock(record.dota_time)),
    el('span', { class: 'who' }, record.nickname),
    entry.channel === 'act' ? null : el('span', { class: 'badge think' }, log.CHANNEL_NAMES[entry.channel]),
    record.error ? el('span', { class: 'badge error' }, '错') : null,
    entry.rejected.length ? el('span', { class: 'badge rejected' }, `拒×${entry.rejected.length}`) : null,
    el('span', { class: 'reason' }, gist(entry)),
  );
}

function renderCount() {
  $('count').textContent = `显示 ${state.shown.length} / ${state.index.entries.length}`;
}

function renderList() {
  const { entries } = state.index;
  state.shown = entries.filter(passes).map((entry) => entry.i);
  $('list').replaceChildren(...state.shown.map((i) => rowOf(entries[i])));
  $('list').querySelector('li.selected')?.scrollIntoView({ block: 'center' });
  renderCount();
}

function select(i) {
  state.selected = i;
  state.overview = false;
  $('overview').classList.remove('on');
  for (const row of $('list').querySelectorAll('li.selected')) row.classList.remove('selected');
  const row = i === null ? null : $('list').querySelector(`li[data-i="${i}"]`);
  row?.classList.add('selected');
  row?.scrollIntoView({ block: 'nearest' });
  history.replaceState(null, '', `#log=${encodeURIComponent(state.name)}${i === null ? '' : `&d=${i}`}`);
  renderDetail();
  const entry = i === null ? null : state.index.entries[i];
  const info = entry && log.heroInfo(entry.hero, state.meta, entry.channel);
  resend.show(entry && { log: state.name, entry, info, planLength: state.index.planLength });
}

function renderDetail() {
  const entry = state.selected === null ? null : state.index.entries[state.selected];
  if (state.overview) $('detail').replaceChildren(overview(state.index, state.meta));
  else if (!entry) $('detail').replaceChildren(el('p', { class: 'muted pad' }, '在左边选一次决策。'));
  else $('detail').replaceChildren(...decisionView(state, entry));
}

function onDetailClick(event) {
  const button = event.target.closest('button');
  if (!button) return;
  if (button.dataset.tab) {
    state.tab = button.dataset.tab;
    renderDetail();
  } else if (button.dataset.jump) {
    const entry = state.index.entries[state.selected];
    const sibling = entry.hero.entries[entry.hero.entries.indexOf(entry) + Number(button.dataset.jump)];
    if (sibling) select(sibling.i);
  } else if (button.dataset.search) {
    $('search').value = button.dataset.search;
    $('status-filter').value = button.dataset.status;
    state.search = button.dataset.search.toLowerCase();
    state.status = button.dataset.status;
    renderList();
    select(state.shown[0] ?? null);
  }
}

function wire() {
  $('list').addEventListener('click', (event) => {
    const row = event.target.closest('li');
    if (row) select(Number(row.dataset.i));
  });
  $('detail').addEventListener('click', onDetailClick);
  $('heroes').addEventListener('click', (event) => {
    const key = event.target.closest('button')?.dataset.hero;
    if (key === undefined) return;
    if (!key) state.heroes.clear();
    else if (state.heroes.has(key)) state.heroes.delete(key);
    else state.heroes.add(key);
    renderHeroes();
    renderList();
  });
  $('status-filter').addEventListener('change', () => {
    state.status = $('status-filter').value;
    renderList();
  });
  let typing = null;
  $('search').addEventListener('input', () => {
    clearTimeout(typing);
    typing = setTimeout(() => {
      state.search = $('search').value.trim().toLowerCase();
      renderList();
      renderDetail();
    }, 200);
  });
  $('log').addEventListener('change', () => {
    // picking an older transcript by hand stops following, which would switch straight back
    if ($('log').selectedIndex > 0) {
      $('follow').checked = false;
      state.follow = false;
    }
    openLog($('log').value).catch((error) => setStatus(String(error)));
  });
  $('follow').addEventListener('change', () => {
    state.follow = $('follow').checked;
    poll();
  });
  $('overview').addEventListener('click', () => {
    state.overview = !state.overview;
    $('overview').classList.toggle('on', state.overview);
    renderDetail();
  });
  $('resend-toggle').addEventListener('click', () => {
    $('resend-toggle').classList.toggle('on', !document.body.classList.toggle('no-resend'));
  });
  document.addEventListener('keydown', (event) => {
    if (event.target.closest('input, textarea, select') || event.metaKey || event.ctrlKey || event.altKey) return;
    const step = { j: 1, ArrowDown: 1, k: -1, ArrowUp: -1 }[event.key];
    if (!step || !state.shown.length) return;
    event.preventDefault();
    const at = state.shown.indexOf(state.selected);
    select(state.shown[Math.min(state.shown.length - 1, Math.max(0, at + step))]);
  });
  setInterval(poll, POLL_MS);
}

async function start() {
  const [meta, logs] = await Promise.all([getJson('/api/meta'), getJson('/api/logs')]);
  state.meta = meta;
  // a reload can bring the controls back as they were left
  state.follow = $('follow').checked;
  state.status = $('status-filter').value;
  state.search = $('search').value.trim().toLowerCase();
  resend.setup(meta);
  wire();
  fillLogs(logs.logs);
  const names = logs.logs.map((item) => item.name);
  const hash = new URLSearchParams(location.hash.slice(1));
  const name = [hash.get('log'), logs.selected, names[0]].find((candidate) => candidate && names.includes(candidate));
  if (!name) {
    setStatus('日志目录里还没有 .jsonl');
    return;
  }
  await openLog(name, hash.has('d') ? Number(hash.get('d')) : null);
}

start().catch((error) => setStatus(String(error)));
