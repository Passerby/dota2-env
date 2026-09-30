// What the middle column shows: one decision under five tabs, or the overview of the whole transcript. Takes
// view.js's state as it is and changes nothing in it.

import * as log from './log.js';
import { rebuildSystem } from './resend.js';
import { copyButton, el, highlight, renderDiff, renderLines, seconds, usd } from './ui.js';

const TABS = [
  ['reply', '回复'],
  ['user', 'User prompt'],
  ['diff', '对比上一次'],
  ['system', 'System prompt'],
  ['json', '原始 JSON'],
];

export function decisionView(state, entry) {
  return [head(entry, state.meta), tabBar(state.tab), el('div', { id: 'tab-body' }, tabBody(state, entry))];
}

function head(entry, meta) {
  const { record, hero } = entry;
  const info = log.heroInfo(hero, meta, entry.channel);
  const who = [
    record.nickname,
    log.CHANNEL_NAMES[entry.channel],
    meta.positions[info.position],
    log.heroName(meta, info.hero),
    meta.teams[record.team],
  ];
  const when =
    record.decided_at === undefined
      ? `回复完 ${log.clock(record.dota_time, true)}`
      : `决策于 ${log.clock(record.decided_at, true)} → 回复完 ${log.clock(record.dota_time, true)}` +
        `（晚 ${(record.dota_time - record.decided_at).toFixed(1)} 游戏秒）`;
  const place = hero.entries.indexOf(entry);
  return el(
    'div',
    { class: 'head' },
    el(
      'div',
      { class: 'toolbar' },
      el('span', { class: 'who' }, who.filter(Boolean).join(' · ')),
      el('button', { type: 'button', dataset: { jump: '-1' }, disabled: place === 0 }, '‹ 上一次'),
      el('span', { class: 'muted small' }, `第 ${place + 1} / ${hero.entries.length} 次`),
      el('button', { type: 'button', dataset: { jump: '1' }, disabled: place === hero.entries.length - 1 }, '下一次 ›'),
    ),
    el(
      'div',
      { class: 'muted' },
      `${when} · 延迟 ${seconds(record.latency)} · 首行 ${seconds(record.first_line_latency)}`,
      ` · 输入 ${record.input_tokens ?? '—'} / 输出 ${record.output_tokens ?? '—'} token · ${usd(record.usd ?? 0)}`,
    ),
    record.error ? el('div', { class: 'error-line' }, `出错：${record.error}`) : null,
    (record.problems ?? []).length ? el('div', { class: 'error-line' }, `没用上的行：${record.problems.join('；')}`) : null,
  );
}

function tabBar(tab) {
  return el(
    'nav',
    { class: 'tabs' },
    TABS.map(([key, label]) => el('button', { type: 'button', class: key === tab ? 'on' : null, dataset: { tab: key } }, label)),
  );
}

function tabBody(state, entry) {
  const { record } = entry;
  if (state.tab === 'reply') return replyTab(entry, state.index.planLength, state.search);
  if (state.tab === 'user') {
    return record.prompt === undefined ? noPrompt() : promptView(record.prompt, state.search, record.input_tokens);
  }
  if (state.tab === 'diff') return diffTab(entry);
  if (state.tab === 'system') return systemTab(entry, state.meta, state.search);
  return el(
    'div',
    {},
    el('pre', {}, JSON.stringify(record, null, 2)),
    entry.rejected.length ? el('h4', {}, '归到这次决策的被拒记录') : null,
    entry.rejected.length ? el('pre', {}, entry.rejected.map((item) => JSON.stringify(item)).join('\n')) : null,
  );
}

const noPrompt = () => el('p', { class: 'muted' }, '这份日志没有记 user prompt：比赛时 log_prompts 没开。');

function replyTab(entry, planLength, term) {
  const lines = log.readReply(entry, planLength);
  if (!lines) {
    return el(
      'div',
      {},
      el('p', { class: 'muted' }, '旧格式的日志，回复按原样显示：'),
      el('pre', {}, highlight(String(entry.record.reply ?? ''), term)),
    );
  }
  const unmatched = entry.rejected.length - lines.filter((line) => line.rejected).length;
  const how =
    entry.channel === 'act'
      ? '#n 是计划的第 n 步，每帧下发一步；下一份回复的第一步一到，还没轮到的步骤就被换掉了，所以没标被拒的步骤不一定都执行了。'
      : entry.channel === 'think'
        ? 'PLAN 一到就是英雄的计划，后面的 GOAL 给它加上目标点；NOTE、FORGET、CALL 也是读到就生效。'
        : '每条 LESSON 写进 memory_dir 里这个英雄的经验文件，下一局的长思考会读到。';
  return el(
    'div',
    {},
    renderLines(lines, term),
    unmatched > 0 ? el('p', { class: 'error-line' }, `另有 ${unmatched} 条被拒记录对不上回复里的行，见「原始 JSON」。`) : null,
    el('p', { class: 'muted small' }, how),
  );
}

function promptView(text, term, inputTokens = null) {
  const size = `${text.length} 字 · 估算约 ${log.approxTokens(text)} token`;
  const actual = inputTokens ? `；这次请求实际输入 ${inputTokens} token（连同 system prompt）` : '';
  return el(
    'div',
    {},
    el('div', { class: 'toolbar' }, el('span', { class: 'muted' }, size + actual), copyButton('复制', () => text)),
    log.sections(text).map((section) => {
      const [first, ...rest] = section.lines;
      const cls = `section${section.gap ? ' gap' : ''}`;
      if (!rest.length) return el('pre', { class: cls }, highlight(first, term));
      const body = section.lines.join('\n');
      return el(
        'details',
        { class: cls, open: true },
        el(
          'summary',
          {},
          highlight(first, term),
          el('span', { class: 'size' }, `  ${section.lines.length} 行 · ${body.length} 字 · 约 ${log.approxTokens(body)} token`),
        ),
        el('pre', {}, highlight(rest.join('\n'), term)),
      );
    }),
  );
}

function diffTab(entry) {
  const { prev, record } = entry;
  if (!prev) return el('p', { class: 'muted' }, `这是 ${record.nickname} 在这份日志里的第一次决策。`);
  if (record.prompt === undefined || prev.record.prompt === undefined) return noPrompt();
  const madeAt = prev.record.decided_at ?? prev.record.dota_time;
  return el(
    'div',
    {},
    el('p', { class: 'muted' }, `和 ${record.nickname} 上一次（决策于 ${log.clock(madeAt, true)}）的 user prompt 比：`),
    renderDiff(log.diffLines(prev.record.prompt, record.prompt)),
  );
}

function systemTab(entry, meta, term) {
  const info = log.heroInfo(entry.hero, meta, entry.channel);
  const box = el('div', {});
  if (info.system !== null) {
    const changes = el('div', {});
    const compare = el('button', { type: 'button', disabled: !info.config }, '和当前模板重建的比');
    compare.addEventListener('click', async () => {
      const result = await rebuildSystem(entry.hero, entry.channel);
      changes.replaceChildren(
        result.error
          ? el('p', { class: 'error-line' }, `重建失败：${result.error}`)
          : el(
              'div',
              {},
              el('p', { class: 'muted' }, '日志记录 → 当前模板重建：'),
              renderDiff(log.diffLines(info.system, result.system)),
            ),
      );
    });
    const title = el('span', { class: 'muted' }, '日志记录：开局时发给模型的 system prompt');
    box.append(el('div', { class: 'toolbar' }, title, compare), changes, promptView(info.system, term));
    return box;
  }
  const missing = entry.channel === 'review' ? '复盘的 system prompt 不进日志' : '这份日志没有记 system prompt（旧日志）';
  if (!info.config) return el('p', { class: 'muted' }, `${missing}。启动时加 --config，就能按当前模板重建一份。`);
  box.append(el('p', { class: 'muted' }, `${missing}，下面按当前模板和 ${meta.config} 重建，可能和当时发出的不同。`));
  rebuildSystem(entry.hero, entry.channel).then((result) => {
    box.append(result.error ? el('p', { class: 'error-line' }, `重建失败：${result.error}`) : promptView(result.system, term));
  });
  return box;
}

export function overview(index, meta) {
  const cell = (value, cls = null) => el('td', { class: cls }, value);
  const rows = [...index.heroes.values()].map((hero) => {
    const info = log.heroInfo(hero, meta);
    const stats = log.heroStats(hero);
    return el(
      'tr',
      {},
      cell(hero.nickname),
      cell(log.heroName(meta, info.hero)),
      cell(meta.positions[info.position] ?? ''),
      cell([info.gateway, info.model].filter(Boolean).join(' / ')),
      cell(stats.decisions, 'num'),
      cell(stats.thinks, 'num'),
      cell(stats.errors, 'num'),
      cell(stats.rejected, 'num'),
      cell(`${stats.inputTokens} / ${stats.outputTokens}`, 'num'),
      cell(usd(stats.usd), 'num'),
      cell(stats.latency ? `${seconds(stats.latency.median)} / ${seconds(stats.latency.p90)}` : '—', 'num'),
      cell(stats.firstLine ? seconds(stats.firstLine.median) : '—', 'num'),
      cell(stats.late ? stats.late.median.toFixed(1) : '—', 'num'),
    );
  });
  const columns = ['昵称', '英雄', '位置', '网关 / 模型', '决策', '长思考', '出错', '被拒', '输入 / 输出 token', '花费'];
  const ranked = (title, texts, status) => {
    const top = log.ranking(texts).slice(0, 12);
    if (!top.length) return null;
    const items = top.map(([text, count]) =>
      el('li', {}, el('button', { type: 'button', dataset: { search: text, status } }, `${count} × ${text}`)),
    );
    return el('div', {}, el('h4', {}, title), el('ul', { class: 'ranking' }, items));
  };
  const { summary, unlinked, entries, rejected } = index;
  const winner = summary?.winner ? meta.teams[summary.winner] : '没有';
  return el(
    'div',
    { class: 'pad' },
    el(
      'table',
      {},
      el('thead', {}, el('tr', {}, [...columns, '延迟 中位 / p90', '首行延迟 中位', '晚了几游戏秒 中位'].map((name) => el('th', {}, name)))),
      el('tbody', {}, rows),
    ),
    el(
      'p',
      { class: 'muted' },
      summary ? `这局跑了 ${summary.steps} 步，胜方：${winner}。` : '没有 summary 行：这局是中途停掉的，统计都按记录现算。',
      unlinked.length ? ` ${unlinked.length} 条被拒记录没能归到某次决策。` : '',
    ),
    ranked('被拒原因（点一下筛出来）', rejected.map((record) => record.error), 'rejected'),
    ranked('出错原因', entries.map((entry) => entry.record.error), 'error'),
  );
}
