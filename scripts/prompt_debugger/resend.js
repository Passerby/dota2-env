// The resend panel: a decision's system and user prompt, edited, sent through a gateway of the --config file a
// few times at once, with every reply read line by line the way the runner reads it.

import * as log from './log.js';
import { copyButton, el, renderDiff, renderLines, seconds, usd } from './ui.js';

const $ = (id) => document.getElementById(id);
const drafts = new Map(); // draftKey() -> the system prompt as edited, kept while moving between decisions
const sources = new Map(); // draftKey() -> { text, from }: the system prompt the edits are measured against
let meta = null;
let context = null; // { log, entry, info, planLength } of the decision on show
let paramsDefault = ''; // what the params editor was last filled with, which tells an edited one apart
let spent = 0;

function postJson(url, body, signal) {
  const headers = { 'Content-Type': 'application/json' };
  return fetch(url, { method: 'POST', headers, body: JSON.stringify(body), signal });
}

// The same nickname can be another hero, or have been told something else, in another transcript.
const draftKey = () => `${context.log}/${context.entry.hero.key}`;

export async function rebuildSystem(hero) {
  const response = await postJson('/api/system', { team: hero.team, nickname: hero.nickname });
  return response.json();
}

export function setup(pageMeta) {
  meta = pageMeta;
  if (!meta.config) {
    $('resend-body').classList.remove('idle');
    $('resend-body').replaceChildren(
      el('p', { class: 'muted' }, '启动时加 --config configs/<配置>.yaml 才能重发，也才能按当前模板重建 system prompt。'),
    );
    return;
  }
  $('rs-config').textContent = meta.config.split('/').at(-1);
  $('rs-config').title = meta.config;
  $('rs-gateway').replaceChildren(
    ...meta.gateways.map((gateway) => el('option', { value: gateway.name }, `${gateway.name} · ${gateway.model}`)),
  );
  $('rs-gateway').addEventListener('change', () => fillParams($('rs-gateway').value));
  $('rs-params-reset').addEventListener('click', () => fillParams($('rs-gateway').value));
  $('rs-params').addEventListener('input', refresh);
  $('rs-user').addEventListener('input', refresh);
  $('rs-system').addEventListener('input', () => {
    keepDraft();
    refresh();
  });
  $('rs-system-reset').addEventListener('click', () => {
    $('rs-system').value = sources.get(draftKey())?.text ?? '';
    keepDraft();
    refresh();
  });
  $('rs-system-rebuild').addEventListener('click', rebuildIntoEditor);
  $('rs-system-diff').addEventListener('click', () =>
    toggleChanges('rs-system-changes', sources.get(draftKey())?.text ?? '', $('rs-system').value),
  );
  $('rs-user-reset').addEventListener('click', () => {
    $('rs-user').value = context.entry.record.prompt ?? '';
    refresh();
  });
  $('rs-user-diff').addEventListener('click', () =>
    toggleChanges('rs-user-changes', context.entry.record.prompt ?? '', $('rs-user').value),
  );
  $('rs-send').addEventListener('click', sendRound);
  $('rs-clear').addEventListener('click', () => $('rs-results').replaceChildren());
}

export function show(next) {
  context = next;
  if (!meta.config) return;
  $('resend-body').classList.toggle('idle', !context);
  if (!context) return;
  const { entry, info } = context;
  // an untouched params editor follows the hero shown; an edited one stays as it is
  if ($('rs-params').value === paramsDefault) {
    const known = meta.gateways.some((gateway) => gateway.name === info.gateway);
    fillParams(known ? info.gateway : meta.gateways[0].name);
  }
  $('rs-user').value = entry.record.prompt ?? '';
  $('rs-system-changes').replaceChildren();
  $('rs-user-changes').replaceChildren();
  showSystem(entry.hero, info);
}

// The logged request of this hero when it went through that gateway, else the config's params for it.
function fillParams(name) {
  const header = context.entry.hero.header;
  const gateway = meta.gateways.find((candidate) => candidate.name === name);
  const params =
    header?.gateway === name
      ? { model: header.model, ...header.params }
      : { model: gateway.model, ...gateway.params, ...(context.info.config?.params ?? {}) };
  $('rs-gateway').value = name;
  paramsDefault = JSON.stringify(params, null, 2);
  $('rs-params').value = paramsDefault;
  refresh();
}

async function showSystem(hero, info) {
  const key = draftKey();
  if (!sources.has(key) && info.system !== null) sources.set(key, { text: info.system, from: '日志记录' });
  if (!sources.has(key) && info.config) {
    mark('rs-system-state', '按当前模板重建中…', false);
    const result = await rebuildSystem(hero);
    if (!context || draftKey() !== key) return;
    if (result.error) {
      $('rs-system').value = drafts.get(key) ?? '';
      mark('rs-system-state', `重建失败：${result.error}`, false);
      return;
    }
    sources.set(key, { text: result.system, from: '当前模板重建（日志里没有）' });
  }
  $('rs-system').value = drafts.get(key) ?? sources.get(key)?.text ?? '';
  refresh();
}

async function rebuildIntoEditor() {
  const key = draftKey();
  const result = await rebuildSystem(context.entry.hero);
  if (!context || draftKey() !== key) return;
  if (result.error) {
    mark('rs-system-state', `重建失败：${result.error}`, false);
    return;
  }
  $('rs-system').value = result.system;
  keepDraft();
  refresh();
}

function keepDraft() {
  const key = draftKey();
  if ($('rs-system').value === sources.get(key)?.text) drafts.delete(key);
  else drafts.set(key, $('rs-system').value);
}

function readParams() {
  try {
    const params = JSON.parse($('rs-params').value);
    if (params === null || typeof params !== 'object' || Array.isArray(params)) return { error: '参数得是一个 JSON 对象' };
    return { params };
  } catch (error) {
    return { error: `JSON 不对：${error.message}` };
  }
}

function mark(id, text, changed) {
  $(id).textContent = changed ? `${text} · 已修改` : text;
  $(id).classList.toggle('changed', changed);
}

function refresh() {
  if (!context) return;
  const source = sources.get(draftKey());
  mark('rs-system-state', source?.from ?? '日志和配置里都没有，自己贴', Boolean(source) && $('rs-system').value !== source.text);
  const { prompt } = context.entry.record;
  const userChanged = prompt !== undefined && $('rs-user').value !== prompt;
  mark('rs-user-state', prompt === undefined ? '日志里没有，自己贴' : '这次决策的原文', userChanged);
  const { error } = readParams();
  $('rs-params-error').textContent = error ?? '';
  $('rs-send').disabled = Boolean(error) || !$('rs-user').value.trim();
}

function toggleChanges(id, before, after) {
  if ($(id).firstChild) $(id).replaceChildren();
  else $(id).replaceChildren(renderDiff(log.diffLines(before, after)));
}

async function sendRound() {
  const { entry, planLength } = context;
  const { params } = readParams();
  const request = { gateway: $('rs-gateway').value, params, system: $('rs-system').value, user: $('rs-user').value };
  const source = sources.get(draftKey());
  const edits = [
    `system ${source && request.system === source.text ? '原样' : '已改'}`,
    `user ${request.user === (entry.record.prompt ?? '') ? '原样' : '已改'}`,
  ];
  const summary = el('div', { class: 'muted small' }, '等回复…');
  const round = el(
    'div',
    { class: 'round' },
    el(
      'div',
      {},
      el('strong', {}, `${entry.record.nickname} ${log.clock(entry.record.dota_time)}`),
      ` · ${request.gateway} · ${params.model ?? ''} · ${edits.join(' · ')}`,
    ),
    summary,
  );
  $('rs-results').prepend(round);
  const times = Number($('rs-count').value);
  const results = await Promise.all(
    Array.from({ length: times }, (_, k) => sendOne(request, planLength || meta.plan_length, entry.record.reply, round, k + 1)),
  );
  summary.textContent = summarize(results);
}

async function* readLines(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += decoder.decode(value, { stream: true });
    for (let end = buffer.indexOf('\n'); end >= 0; end = buffer.indexOf('\n')) {
      yield JSON.parse(buffer.slice(0, end));
      buffer = buffer.slice(end + 1);
    }
  }
}

function requestJson(request) {
  const messages = [
    { role: 'system', content: request.system },
    { role: 'user', content: request.user },
  ];
  return JSON.stringify({ ...request.params, messages }, null, 2);
}

async function sendOne(request, planLength, original, round, number) {
  const controller = new AbortController();
  const lines = [];
  const body = el('div', {});
  const stats = el('div', { class: 'muted small' }, '等回复…');
  const changes = el('div', {});
  const stop = el('button', { type: 'button' }, '停止');
  const compare = el('button', { type: 'button', disabled: true }, '和原回复对比');
  stop.addEventListener('click', () => controller.abort());
  round.append(
    el(
      'div',
      { class: 'card' },
      el(
        'div',
        { class: 'toolbar' },
        el('span', { class: 'muted small' }, `第 ${number} 次`),
        stop,
        compare,
        copyButton('复制请求 JSON', () => requestJson(request)),
      ),
      body,
      stats,
      changes,
    ),
  );
  let reply = null;
  let steps = 0;
  try {
    const response = await postJson('/api/send', request, controller.signal);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    for await (const event of readLines(response)) {
      if (event.reply) {
        reply = event.reply;
      } else if (event.kind === 'step') {
        steps += 1;
        lines.push(
          steps <= planLength
            ? { line: event.line, kind: 'step', n: steps, invalid: event.error }
            : { line: event.line, kind: 'extra' },
        );
      } else {
        lines.push({ line: event.line, kind: event.kind ?? 'skip' });
      }
      body.replaceChildren(renderLines(lines));
    }
  } catch (error) {
    stats.textContent = error.name === 'AbortError' ? '已停止' : `出错：${error.message}`;
  }
  stop.disabled = true;
  if (!reply) {
    if (stats.textContent === '等回复…') stats.textContent = '连接断了，没收到完整回复，看跑调试器的终端';
    return { reply, lines };
  }
  // the gateway holds back a last line that was cut off, so that line is only in the whole text
  const tail = reply.text.split('\n').filter((line) => line.trim()).at(-1);
  if (tail !== undefined && tail !== lines.at(-1)?.line) lines.push({ line: tail, kind: 'cut' });
  body.replaceChildren(renderLines(lines));
  stats.textContent =
    `延迟 ${seconds(reply.latency)} · 首行 ${seconds(reply.first_line_latency)} · ` +
    `输入 ${reply.input_tokens} / 输出 ${reply.output_tokens} token · ${usd(reply.usd)}`;
  if (reply.error) stats.append(el('div', { class: 'error-line' }, `出错：${reply.error}`));
  spent += reply.usd;
  $('rs-spent').textContent = `本页重发共花 ${usd(spent)}`;
  compare.disabled = typeof original !== 'string';
  compare.addEventListener('click', () => {
    if (changes.firstChild) changes.replaceChildren();
    else changes.replaceChildren(renderDiff(log.diffLines(original, reply.text)));
  });
  return { reply, lines };
}

function summarize(results) {
  const done = results.filter((result) => result.reply && !result.reply.error);
  const failed = results.filter((result) => result.reply?.error).length;
  const stopped = results.length - done.length - failed;
  const firsts = log.ranking(
    done.map((result) => {
      const first = result.lines.find((line) => line.kind === 'step');
      return first ? first.line.split(',')[0].trim().toUpperCase() : '没有步骤';
    }),
  );
  const invalid = done.filter((result) => result.lines.some((line) => line.invalid)).length;
  const firstLine = log.quantiles(done.map((result) => result.reply.first_line_latency).filter(Number.isFinite));
  return [
    `${results.length} 次：正常 ${done.length}`,
    failed ? `出错 ${failed}` : null,
    stopped ? `停止或断开 ${stopped}` : null,
    done.length ? `第一步 ${firsts.map(([kind, count]) => `${kind} ×${count}`).join('、')}` : null,
    done.length ? `带格式不对的步骤 ${invalid} 次` : null,
    firstLine ? `首行延迟中位 ${seconds(firstLine.median)}` : null,
  ]
    .filter(Boolean)
    .join(' · ');
}
