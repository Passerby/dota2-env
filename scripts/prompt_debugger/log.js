// Transcript records turned into what the page shows: the index, rejected steps tied to their decisions,
// statistics, how a logged reply was read, prompt sections and line diffs. Pure functions, no DOM.

export function parseChunk(text) {
  return text
    .split('\n')
    .filter((line) => line)
    .map((line) => JSON.parse(line));
}

// Transcripts from before replies were read line by line hold every step as a JSON object.
export const stepText = (step) => (typeof step === 'string' ? step : JSON.stringify(step));

const heroKey = (record) => `${record.team}/${record.nickname}`;

// The records of an agent's replies and the channel each comes from: the per-second channel, the long think, and
// the review after the match.
const CHANNELS = { decision: 'act', think: 'think', review: 'review' };
export const CHANNEL_NAMES = { act: '每秒', think: '长思考', review: '复盘' };

// Built again from every record loaded so far whenever more arrive, so no link is ever kept half made.
export function buildIndex(records) {
  const heroes = new Map();
  const entries = [];
  const rejected = [];
  let summary = null;
  const heroOf = (record) => {
    const key = heroKey(record);
    if (!heroes.has(key)) {
      heroes.set(key, {
        key,
        team: record.team,
        nickname: record.nickname,
        header: null,
        summary: null,
        entries: [],
        rejected: [],
        byDecidedAt: new Map(),
        last: new Map(), // channel -> the latest entry of it, the one a new entry is compared with
      });
    }
    return heroes.get(key);
  };
  for (const record of records) {
    if (record.kind === 'agent') {
      heroOf(record).header = record;
    } else if (Object.hasOwn(CHANNELS, record.kind)) {
      const hero = heroOf(record);
      const channel = CHANNELS[record.kind];
      const entry = { i: entries.length, record, hero, channel, prev: hero.last.get(channel) ?? null, rejected: [] };
      hero.last.set(channel, entry);
      hero.entries.push(entry);
      if (channel === 'act') hero.byDecidedAt.set(record.decided_at, entry);
      entries.push(entry);
    } else if (record.kind === 'rejected') {
      heroOf(record).rejected.push(record);
      rejected.push(record);
    } else if (record.kind === 'summary') {
      summary = record;
    }
  }
  // the summary has no team, and it is all a transcript without headers says about hero and position
  for (const agent of summary?.agents ?? []) {
    const hero = [...heroes.values()].find((candidate) => candidate.nickname === agent.nickname);
    if (hero) hero.summary = agent;
  }
  const unlinked = rejected.filter((record) => {
    const entry = ownerOf(record, heroes.get(heroKey(record)));
    entry?.rejected.push(record);
    return !entry;
  });
  return { heroes, entries, rejected, unlinked, summary, planLength: planLength(heroes, entries) };
}

function ownerOf(record, hero) {
  if (!hero) return null;
  if (record.decided_at !== undefined) return hero.byDecidedAt.get(record.decided_at) ?? null;
  // older transcripts: the last decision made by then whose plan holds the step
  const step = stepText(record.step);
  for (let k = hero.entries.length - 1; k >= 0; k -= 1) {
    const decision = hero.entries[k].record;
    const madeBy = decision.decided_at ?? decision.dota_time;
    if (madeBy <= record.dota_time && (decision.plan ?? []).some((planned) => stepText(planned) === step)) {
      return hero.entries[k];
    }
  }
  return null;
}

// The match's plan_length from a header, or else the longest plan the transcript holds.
function planLength(heroes, entries) {
  const header = [...heroes.values()].find((hero) => hero.header)?.header;
  if (header) return header.plan_length;
  let longest = 0;
  for (const entry of entries) {
    if (entry.channel === 'act') longest = Math.max(longest, (entry.record.plan ?? []).length);
  }
  return longest || null;
}

// What the page knows of a hero and one of its channels, from its header, else the summary, else the --config file.
// The review's system prompt is not in the transcript, only rebuilt; neither is anything of the older ones.
export function heroInfo(hero, meta, channel = 'act') {
  const config = (meta.agents ?? []).find((agent) => agent.team === hero.team && agent.nickname === hero.nickname);
  const known = hero.header ?? hero.summary ?? config ?? {};
  const thinking = channel !== 'act';
  const think = hero.header?.think ?? null;
  return {
    hero: known.hero ?? null,
    position: known.position ?? null,
    channel,
    gateway: thinking ? (think?.gateway ?? config?.think?.gateway ?? null) : (known.gateway ?? null),
    model: thinking ? (think?.model ?? null) : (hero.header?.model ?? null),
    params: thinking ? (think?.params ?? null) : (hero.header?.params ?? null),
    system: channel === 'think' ? (think?.system ?? null) : thinking ? null : (hero.header?.system ?? null),
    config: config ?? null,
  };
}

export const heroName = (meta, hero) => (hero ? (meta.heroes[hero] ?? hero) : '');

export const hasProblem = (entry) =>
  Boolean(entry.record.error) || entry.rejected.length > 0 || (entry.record.problems ?? []).length > 0;

export function matches(entry, term) {
  if (!term) return true;
  const { prompt, reply, reason, error } = entry.record;
  return [prompt, reply, reason, error, ...entry.rejected.map((record) => record.error)].some(
    (text) => typeof text === 'string' && text.toLowerCase().includes(term),
  );
}

// Median, p90 and worst, picked the way scripts/check_match.py picks them.
export function quantiles(values) {
  if (!values.length) return null;
  const ordered = [...values].sort((a, b) => a - b);
  const last = ordered.length - 1;
  return {
    median: ordered[Math.floor(last / 2)],
    p90: ordered[Math.min(last, Math.floor(ordered.length * 0.9))],
    worst: ordered[last],
  };
}

// The per-second channel's decisions; tokens and money are every channel's.
export function heroStats(hero) {
  const records = hero.entries.filter((entry) => entry.channel === 'act').map((entry) => entry.record);
  const total = (key) => hero.entries.reduce((sum, entry) => sum + (entry.record[key] ?? 0), 0);
  const finite = (values) => values.filter((value) => Number.isFinite(value));
  return {
    decisions: records.length,
    thinks: hero.entries.filter((entry) => entry.channel === 'think').length,
    errors: records.filter((record) => record.error).length,
    rejected: hero.rejected.length,
    inputTokens: total('input_tokens'),
    outputTokens: total('output_tokens'),
    usd: total('usd'),
    latency: quantiles(finite(records.map((record) => record.latency))),
    firstLine: quantiles(finite(records.map((record) => record.first_line_latency))),
    late: quantiles(finite(records.map((record) => record.dota_time - record.decided_at))),
  };
}

export function ranking(texts) {
  const counts = new Map();
  for (const text of texts) if (text) counts.set(text, (counts.get(text) ?? 0) + 1);
  return [...counts].sort((a, b) => b[1] - a[1]);
}

// The words a reply line can start with that make it no step (agent.TAGS), and those each channel acts on.
const TAG_WORDS = ['REASON', 'SAY', 'INTENT', 'CALL', 'ASK', 'PLAN', 'GOAL', 'NOTE', 'FORGET', 'LESSON'];
const CHANNEL_TAGS = {
  act: ['REASON', 'SAY', 'INTENT', 'CALL', 'ASK'],
  think: ['PLAN', 'GOAL', 'NOTE', 'FORGET', 'CALL', 'REASON'],
  review: ['LESSON', 'REASON'],
};

// What a tagged line is to the channel it came in on: its tag, or unused for a tag the channel ignores.
export function tagOf(line, channel) {
  const word = line.trim().match(/^[A-Za-z_]*/)[0].toUpperCase();
  if (!TAG_WORDS.includes(word)) return null;
  return CHANNEL_TAGS[channel].includes(word) ? word.toLowerCase() : 'unused';
}

// Every line of a logged reply, told apart the way the runner read it: the leading word decides the tags
// (agent.read_line), and the plan is the step lines in order, so the lines that are not in it were dropped.
// A long think or a review has no steps, only tags. null for the transcripts from before replies were plain lines.
export function readReply(entry, planLength) {
  const { reply, plan } = entry.record;
  if (entry.channel !== 'act') {
    if (typeof reply !== 'string') return null;
    return reply.split('\n').map((line) => {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith('```')) return { line, kind: 'skip' };
      return { line, kind: tagOf(trimmed, entry.channel) ?? 'unused' };
    });
  }
  if (typeof reply !== 'string' || !Array.isArray(plan) || !plan.every((step) => typeof step === 'string')) {
    return null;
  }
  const rejections = new Map();
  for (const record of entry.rejected) rejections.set(record.step, [...(rejections.get(record.step) ?? []), record.error]);
  let steps = 0;
  return reply.split('\n').map((line) => {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('```')) return { line, kind: 'skip' };
    const tag = tagOf(trimmed, 'act');
    if (tag) return { line, kind: tag };
    if (steps < plan.length && trimmed === plan[steps]) {
      steps += 1;
      return { line, kind: 'step', n: steps, rejected: rejections.get(trimmed)?.shift() ?? null };
    }
    if (!planLength) return { line, kind: 'unplanned' };
    return { line, kind: steps < planLength ? 'cut' : 'extra' };
  });
}

// A prompt cut at blank lines, and before every line that is neither indented nor a "- " item, which is how
// both the state block and the item notes begin a part.
export function sections(prompt) {
  const parts = [];
  let gap = false;
  for (const line of prompt.split('\n')) {
    if (!line.trim()) {
      gap = true;
      continue;
    }
    if (gap || !parts.length || !/^[\s-]/.test(line)) parts.push({ lines: [line], gap: gap && parts.length > 0 });
    else parts.at(-1).lines.push(line);
    gap = false;
  }
  return parts;
}

// examples/llm_match.py --dry-run's estimate: a token per Chinese character, one per three of anything else.
export function approxTokens(text) {
  const chinese = (text.match(/[一-鿿]/g) ?? []).length;
  return chinese + Math.floor((text.length - chinese) / 3);
}

// Line diff by longest common subsequence; null when the table would pass a million cells.
export function diffLines(before, after) {
  const a = before.split('\n');
  const b = after.split('\n');
  const width = b.length + 1;
  if (a.length * b.length > 1e6) return null;
  const common = new Uint16Array((a.length + 1) * width);
  for (let i = a.length - 1; i >= 0; i -= 1) {
    for (let j = b.length - 1; j >= 0; j -= 1) {
      common[i * width + j] =
        a[i] === b[j]
          ? common[(i + 1) * width + j + 1] + 1
          : Math.max(common[(i + 1) * width + j], common[i * width + j + 1]);
    }
  }
  const ops = [];
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      ops.push({ op: ' ', line: a[i] });
      i += 1;
      j += 1;
    } else if (common[(i + 1) * width + j] >= common[i * width + j + 1]) {
      ops.push({ op: '-', line: a[i] });
      i += 1;
    } else {
      ops.push({ op: '+', line: b[j] });
      j += 1;
    }
  }
  for (; i < a.length; i += 1) ops.push({ op: '-', line: a[i] });
  for (; j < b.length; j += 1) ops.push({ op: '+', line: b[j] });
  return ops;
}

// The changed lines with context lines around them; each longer run of unchanged lines becomes { skipped: n }.
export function hunks(ops, context) {
  const keep = ops.map(() => false);
  ops.forEach((item, k) => {
    if (item.op === ' ') return;
    for (let near = Math.max(0, k - context); near <= Math.min(ops.length - 1, k + context); near += 1) keep[near] = true;
  });
  const out = [];
  ops.forEach((item, k) => {
    if (keep[k]) out.push(item);
    else if (out.at(-1)?.skipped) out.at(-1).skipped += 1;
    else out.push({ skipped: 1 });
  });
  return out;
}

// Game time the way the client's clock and text.clock show it: -1:30 before the horn, 12:05 after.
export function clock(dotaTime, tenths = false) {
  const seconds = Math.abs(dotaTime);
  const text = `${dotaTime < 0 ? '-' : ''}${Math.floor(seconds / 60)}:${String(Math.floor(seconds) % 60).padStart(2, '0')}`;
  return tenths ? `${text}.${Math.floor(seconds * 10) % 10}` : text;
}
