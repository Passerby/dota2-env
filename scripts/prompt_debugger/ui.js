// DOM helpers shared by the log view and the resend panel. Prompts and replies are model output, so text only
// ever goes in as text nodes, never as HTML.

import { hunks } from './log.js';

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else node.setAttribute(key, value === true ? '' : value);
  }
  node.append(...children.flat().filter((child) => child !== null && child !== undefined && child !== false));
  return node;
}

export const usd = (value) => `$${value.toFixed(value < 0.01 ? 6 : 4)}`;
export const seconds = (value) => (Number.isFinite(value) ? `${value.toFixed(2)}s` : '—');

export function highlight(text, term) {
  const fragment = document.createDocumentFragment();
  const lower = term ? text.toLowerCase() : '';
  let at = 0;
  for (let hit = term ? lower.indexOf(term) : -1; hit >= 0; hit = lower.indexOf(term, at)) {
    fragment.append(text.slice(at, hit), el('mark', {}, text.slice(hit, hit + term.length)));
    at = hit + term.length;
  }
  fragment.append(text.slice(at));
  return fragment;
}

const TAGS = { reason: '理由', say: '喊话', skip: '', extra: '多余', cut: '截断', unplanned: '未用' };
const NOTES = { extra: '超出 plan_length，没执行', cut: '最后一行没写完，没执行', unplanned: '没进计划' };

// Reply lines as log.readReply or the resend stream tell them apart; rejected is why the frame refused a step,
// invalid why a step is written like no action at all.
export function renderLines(lines, term = '') {
  return el(
    'div',
    { class: 'lines' },
    lines.map((line) => {
      const failed = line.rejected || line.invalid;
      const why = line.rejected ? `被拒：${line.rejected}` : line.invalid ? `格式不对：${line.invalid}` : NOTES[line.kind];
      return el(
        'div',
        { class: `line ${line.kind}${failed ? ' failed' : ''}` },
        el('span', { class: 'tag' }, line.kind === 'step' ? `#${line.n}` : TAGS[line.kind]),
        el('span', { class: 'text' }, highlight(line.line, term)),
        why ? el('span', { class: 'why' }, highlight(why, term)) : null,
      );
    }),
  );
}

const OP_CLASS = { '+': 'add', '-': 'del', ' ': 'same' };

export function renderDiff(ops, context = 3) {
  if (!ops) return el('p', { class: 'muted' }, '两边都太长，不逐行对比。');
  const removed = ops.filter((item) => item.op === '-').length;
  const added = ops.filter((item) => item.op === '+').length;
  if (!removed && !added) return el('p', { class: 'muted' }, '完全一样。');
  const body = el('div', { class: 'diff' });
  const toggle = el('button', { type: 'button' });
  let full = false;
  const draw = () => {
    body.replaceChildren(
      ...(full ? ops : hunks(ops, context)).map((item) =>
        item.skipped
          ? el('div', { class: 'skipped' }, `… ${item.skipped} 行相同 …`)
          : el('div', { class: OP_CLASS[item.op] }, `${item.op} ${item.line}`),
      ),
    );
    toggle.textContent = full ? '只看改动' : '显示全部';
  };
  toggle.addEventListener('click', () => {
    full = !full;
    draw();
  });
  draw();
  return el('div', {}, el('div', { class: 'toolbar' }, el('span', { class: 'muted' }, `删 ${removed} 行，加 ${added} 行`), toggle), body);
}

export function copyButton(label, text) {
  const button = el('button', { type: 'button' }, label);
  button.addEventListener('click', async () => {
    await navigator.clipboard.writeText(text());
    button.textContent = '已复制';
    setTimeout(() => {
      button.textContent = label;
    }, 1200);
  });
  return button;
}
