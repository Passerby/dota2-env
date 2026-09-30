"""Hero, ability and item text of dota2_env/data, rendered the way LLM prompts show it.

The files are made by scripts/fetch_game_text.py (docs/MAP_DATA.md) and hold the client's own names
and tooltips in English (en) and Simplified Chinese (zh).
"""

import functools
import json
import os
from typing import Literal

from dota2_env.map_features import DATA_DIR

Language = Literal['zh', 'en']
Kind = Literal['heroes', 'abilities', 'items']

# Note (ruidu): the client's own words, looked up in dota_schinese.txt / dota_english.txt and the
# abilities_ files of 6934: DOTA_InnateAbilityTag, DOTA_ToolTip_Ability_Passive, DOTA_AbilityTooltip_Aghs_*,
# DOTA_Shop_Recipe, DOTA_ItemPrice, DOTA_GameItems_AvailableAtSecretShop and the tooltip headings for
# cooldown, mana cost and cast range.
LABELS: dict[Language, dict[str, str]] = {
    'zh': {
        'cooldown': '冷却时间 {} 秒',
        'mana': '魔法消耗 {}',
        'range': '施法距离 {}',
        'innate': '先天技能',
        'passive': '被动',
        'scepter': '阿哈利姆神杖',
        'shard': '阿哈利姆魔晶',
        'granted': '需要{}',
        'same': '同 {}',
        'notes': '提示：',
        'cost': '价格 {}',
        'recipe': '图纸（{}）',
        'secret_shop': '神秘商店有售',
        'secret_part': '（神秘商店）',
        'made_of': '合成：',
        'or': '，或 ',
        'colon': '：',
        'comma': '，',
        'semicolon': '；',
    },
    'en': {
        'cooldown': 'cooldown {}s',
        'mana': 'mana {}',
        'range': 'cast range {}',
        'innate': 'innate',
        'passive': 'passive',
        'scepter': "Aghanim's Scepter",
        'shard': "Aghanim's Shard",
        'granted': 'needs {}',
        'same': 'as {}',
        'notes': 'Notes: ',
        'cost': 'cost {}',
        'recipe': 'Recipe ({})',
        'secret_shop': 'sold at the Secret Shop',
        'secret_part': ' (Secret Shop)',
        'made_of': 'Made of: ',
        'or': ', or ',
        'colon': ': ',
        'comma': ', ',
        'semicolon': '; ',
    },
}


@functools.cache
def load_records(kind: Kind) -> dict[str, dict[str, object]]:
    """{name: record} of heroes.json, abilities.json or items.json, read once per process."""
    with open(os.path.join(DATA_DIR, f'{kind}.json'), encoding='utf-8') as handle:
        return {record['name']: record for record in json.load(handle)[kind]}


@functools.cache
def item_names() -> dict[int, str]:
    """{item id: name} of items.json: the world state gives an item's id only."""
    return {record['id']: name for name, record in load_records('items').items()}


@functools.cache
def hero_names() -> dict[int, str]:
    """{hero id: unit name} of heroes.json: a world state's players carry the hero id only."""
    return {record['id']: name for name, record in load_records('heroes').items()}


def numbers_text(numbers: list[float]) -> str:
    """'60 / 80 / 100', or a single number when every level has the same value."""
    return ' / '.join(f'{number:g}' for number in (numbers if len(set(numbers)) > 1 else numbers[:1]))


def display_name(name: str, language: Language = 'zh') -> str:
    """The client's name for a hero, ability or item; the internal name for what the data has no text for,
    such as a recipe the shop does not list or a placeholder ability."""
    for kind in ('heroes', 'abilities', 'items'):
        record = load_records(kind).get(name)
        if record is not None:
            return record[language].get('name', name)
    return name


def entry_lines(name: str, language: Language, facts: list[str]) -> list[str]:
    """An ability or item as prompt lines: its name and facts, then description, numbers and notes.

    A Chinese name is followed by the English one, which is what models have mostly read about.
    """
    labels = LABELS[language]
    text = load_records('items' if name.startswith('item_') else 'abilities')[name][language]
    title = display_name(name, language) + (f'（{display_name(name, "en")}）' if language == 'zh' else '')
    lines = [f'- {name} {title}' + (labels['colon'] + labels['comma'].join(facts) if facts else '')]
    lines += [f'  {line.strip()}' for line in text.get('desc', '').split('\n') if line.strip()]
    if text.get('stats'):
        lines.append('  ' + labels['semicolon'].join(text['stats']))
    if text.get('notes'):
        lines.append('  ' + labels['notes'] + ' '.join(text['notes']))
    return lines


def costs(record: dict[str, object], labels: dict[str, str]) -> list[str]:
    """Cooldown, mana cost and cast range, per level, where they are not zero."""
    facts = []
    if any(record['cooldowns']):
        facts.append(labels['cooldown'].format(numbers_text(record['cooldowns'])))
    if any(record['mana_costs']):
        facts.append(labels['mana'].format(numbers_text(record['mana_costs'])))
    if any(record['cast_ranges']) and not record.get('passive'):  # a passive's range is its aura's radius
        facts.append(labels['range'].format(numbers_text(record['cast_ranges'])))
    return facts


def hero_text(hero: str, language: Language = 'zh') -> str:
    """Every ability of hero in slot order, with what Aghanim's Scepter and Shard add to it.

    An ability that repeats the one before it (Shadowraze 2 and 3) is given as the difference only.
    """
    labels, abilities = LABELS[language], load_records('abilities')
    lines = []
    previous = None
    for name in load_records('heroes')[hero]['abilities']:
        record = abilities[name]
        text = record[language]
        if previous is not None and (text['name'], text.get('desc')) == (previous[1]['name'], previous[1].get('desc')):
            changed = [stat for stat in text.get('stats', []) if stat not in previous[1].get('stats', [])]
            lines += entry_lines(name, language, [labels['same'].format(previous[0]), *changed])[:1]
            continue
        facts = [labels[flag] for flag in ('innate', 'passive') if record[flag]]
        if record['granted_by'] is not None:
            facts.append(labels['granted'].format(labels[record['granted_by']]))
        lines += entry_lines(name, language, facts + costs(record, labels))
        lines += [
            f'  {labels[upgrade]}{labels["colon"]}{text[upgrade]}'
            for upgrade in ('shard', 'scepter')
            if upgrade in text
        ]
        previous = (name, text)
    return '\n'.join(lines)


def item_text(name: str, language: Language = 'zh') -> str:
    """An item with its price, where it is sold, what it is made of and what it does."""
    labels, items = LABELS[language], load_records('items')
    record = items[name]
    facts = [labels['cost'].format(record['cost'])] if record['purchasable'] and record['cost'] else []
    if record['secret_shop']:
        facts.append(labels['secret_shop'])
    lines = entry_lines(name, language, facts + costs(record, labels))

    def part(component: str) -> str:
        if items[component]['recipe']:
            return labels['recipe'].format(items[component]['cost'])
        return display_name(component, language) + (labels['secret_part'] if items[component]['secret_shop'] else '')

    if record['components']:
        options = [' + '.join(part(component) for component in parts) for parts in record['components']]
        lines.insert(1, '  ' + labels['made_of'] + labels['or'].join(options))
    return '\n'.join(lines)
