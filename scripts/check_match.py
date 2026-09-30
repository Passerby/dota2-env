"""Read an LLM match transcript and report whether the run actually exercised anything.

python scripts/check_match.py logs/20260920T081225Z.jsonl
"""

import argparse
import collections
import json

HORN_DOTA_TIME = 0.0  # creeps spawn here; everything before it is the pre-game walk-around


def quantiles(values):
    """Median, p90 and worst of a small sample."""
    ordered = sorted(values)
    last = len(ordered) - 1
    return ordered[last // 2], ordered[min(last, int(len(ordered) * 0.9))], ordered[last]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('transcript')
    parser.add_argument('--reasons', type=int, default=5, help='sample this many model reasons')
    args = parser.parse_args()

    with open(args.transcript) as f:
        records = [json.loads(line) for line in f]
    decisions = [record for record in records if record['kind'] == 'decision']
    rejected = [record for record in records if record['kind'] == 'rejected']
    thinks = [record for record in records if record['kind'] == 'think']
    events = [record for record in records if record['kind'] == 'event']
    reviews = [record for record in records if record['kind'] == 'review']
    summary = next((record for record in records if record['kind'] == 'summary'), None)
    if not decisions:
        raise SystemExit('no decision records: the run never got a reply out of any gateway')

    times = [record['dota_time'] for record in decisions]
    print(
        f'{len(decisions)} decisions, {len(rejected)} rejected actions, dota_time {min(times):.0f} -> {max(times):.0f}'
    )
    if max(times) < HORN_DOTA_TIME:
        print(f'  WARNING: the whole run was pre-game, {-max(times):.0f}s short of the horn - no creeps existed yet')
    else:
        played = max(times) - max(min(times), HORN_DOTA_TIME)
        print(f'  {played:.0f} game seconds of it after the horn')

    median, p90, worst = quantiles([record['latency'] for record in decisions])
    print(f'\nlatency  median {median:.2f}s  p90 {p90:.2f}s  worst {worst:.2f}s')
    first_lines = [record['first_line_latency'] for record in decisions if record.get('first_line_latency') is not None]
    if first_lines:
        median, p90, worst = quantiles(first_lines)
        print(
            f'latency to the first line, when a hero can act  median {median:.2f}s  p90 {p90:.2f}s  worst {worst:.2f}s'
        )
    lag = [record['dota_time'] - record['decided_at'] for record in decisions if 'decided_at' in record]
    if lag:
        median, p90, worst = quantiles(lag)
        print(f'staleness when the whole reply is in  median {median:.1f}s  p90 {p90:.1f}s  worst {worst:.1f}s')

    plans = [record['plan'] for record in decisions if record.get('plan')]
    if plans:
        lengths = collections.Counter(len(plan) for plan in plans)
        print(f'\nplan length  {dict(sorted(lengths.items()))}')
        # Note (ruidu): a step is the line the model wrote; transcripts from before replies streamed hold JSON objects.
        kinds = collections.Counter(
            (step.split(',')[0] if isinstance(step, str) else str(step.get('type', '?'))).strip().upper()
            for plan in plans
            for step in plan
        )
        total = sum(kinds.values())
        print('planned actions  ' + '  '.join(f'{name} {100 * n / total:.0f}%' for name, n in kinds.most_common()))
        varied = sum(1 for plan in plans if len({json.dumps(step, sort_keys=True) for step in plan}) > 1)
        print(f'plans with more than one distinct action  {100 * varied / len(plans):.0f}%')

    if rejected:
        print('\nwhy actions were rejected:')
        for error, n in collections.Counter(record['error'] for record in rejected).most_common(6):
            print(f'  {n:5}  {error}')

    reasons = [record['reason'] for record in decisions if record.get('reason')]
    if reasons:
        print(f'\n{len(reasons)} of {len(decisions)} replies carried a reason; a sample:')
        step = max(1, len(reasons) // args.reasons)
        for reason in reasons[::step][: args.reasons]:
            print(f'  {reason}')
    else:
        print('\nno reasons in this transcript (the model never wrote a REASON line)')

    if thinks:
        print(f'\n{len(thinks)} long thinks')
        median, p90, worst = quantiles([record['latency'] for record in thinks])
        print(f'  latency  median {median:.2f}s  p90 {p90:.2f}s  worst {worst:.2f}s')
        median, p90, worst = quantiles([record['dota_time'] - record['decided_at'] for record in thinks])
        print(f'  staleness when the whole reply is in  median {median:.1f}s  p90 {p90:.1f}s  worst {worst:.1f}s')
        asked = collections.Counter(trigger for record in thinks for trigger in record['triggers'])
        print('  asked by  ' + '  '.join(f'{name} {n}' for name, n in asked.most_common()))
        failed = collections.Counter(record['error'] for record in thinks if record['error'])
        print(
            f'  replies without a plan  {sum(failed.values())}'
            + (f'  e.g. {failed.most_common(1)[0][0]}' if failed else '')
        )
        plans = [record['plan'] for record in thinks if record['plan']]
        step = max(1, len(plans) // args.reasons)
        for plan in plans[::step][: args.reasons]:
            print(f'  {plan}')
    intents = [record['intent'] for record in decisions if record.get('intent')]
    calls = [call for record in decisions + thinks for call in record.get('calls') or []]
    print(f'\nintents written {len(intents)}, calls made {len(calls)}' + (f'  e.g. {calls[0]!r}' if calls else ''))
    if events:
        happened = collections.Counter(record['event'] for record in events)
        print('events seen  ' + '  '.join(f'{name} {n}' for name, n in happened.most_common()))
    if reviews:
        lessons = [lesson['text'] for record in reviews for lesson in record['lessons']]
        print(f'{len(reviews)} reviews, {len(lessons)} lessons' + (f'  e.g. {lessons[0]!r}' if lessons else ''))

    said = [record['say'] for record in decisions if record.get('say')]
    print(f'\ntaunts sent: {len(said)}' + (f'  e.g. {said[0]!r}' if said else ''))
    print(f'prompts stored: {"yes" if any("prompt" in record for record in decisions) else "no (set log_prompts)"}')

    if summary:
        print()
        header = (
            'nickname',
            'req',
            'reply',
            'net_err',
            'rejected',
            'steps',
            'held',
            'thinks',
            'tokens',
            'usd',
            'latency',
        )
        print('{:<10}{:>5}{:>6}{:>8}{:>9}{:>7}{:>6}{:>7}{:>9}{:>9}{:>9}'.format(*header))
        for entry in summary['agents']:
            print(
                '{:<10}{:>5}{:>6}{:>8}{:>9}{:>7}{:>6}{:>7}{:>9}{:>9.4f}{:>9}'.format(
                    entry['nickname'],
                    entry['requests'],
                    entry['replies'],
                    entry['gateway_errors'],
                    entry['rejected_actions'],
                    entry.get('planned_steps', 0),
                    entry['held_frames'],
                    entry.get('thinks', 0),
                    entry['input_tokens'] + entry['output_tokens'],
                    entry['usd'],
                    entry['mean_latency'],
                )
            )
        print(f'total ${sum(entry["usd"] for entry in summary["agents"]):.4f}  winner {summary["winner"]}')
    else:
        print('\nno summary record: the run was killed before it finished')


if __name__ == '__main__':
    main()
