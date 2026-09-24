"""Ability, item and hero text in English and Chinese, from Valve's dota2.com datafeed and the installed client.

    python scripts/fetch_game_text.py [--delay 0.25]

Writes dota2_env/data/items.json, abilities.json and heroes.json (fields in docs/MAP_DATA.md). The
datafeed has the resolved text but no bulk endpoint, so every item and ability costs one request per
language and every hero one more, about 3,400 in all; responses are cached under --cache, so an
interrupted run picks up where it stopped. The client's own files in game/dota/pak01_dir.vpk add what
the datafeed leaves out: which items only the Secret Shop sells, every recipe, each hero's abilities in
slot order and the names of the stats that item text abbreviates as $str and the like.
"""

import argparse
import html
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from typing import TypeAlias

from dota2_env.bridge.game import get_default_game_path, read_vpk
from dota2_env.game_text import numbers_text
from dota2_env.map_features import DATA_DIR

BASE_URL = 'https://www.dota2.com/datafeed'
LANGUAGES = {'en': 'english', 'zh': 'schinese'}  # the datafeed's language names are also the client's file suffixes
USER_AGENT = 'dota2-env scripts/fetch_game_text.py'
CLIENT_FILES = (
    'scripts/npc/items.txt',
    'scripts/npc/heroes/npc_dota_hero_',
    *(f'resource/localization/abilities_{language}.txt' for language in LANGUAGES.values()),
)
VARIABLE_PREFIX = 'dota_ability_variable_'
ABILITY_BEHAVIOR_PASSIVE = 2
# ASCII names only: \w would also match Chinese, and resolved Chinese text keeps % signs next to it.
RE_TOKEN = re.compile(r'%(\w*)%|\{s:(\w+)\}', re.ASCII)
RE_VARIABLE = re.compile(r'\$(\w+)', re.ASCII)
# tokens that name a field of the record rather than one of its special values
PROPERTY_TOKENS = {
    'abilitycastrange': 'cast_ranges',
    'abilitycastpoint': 'cast_points',
    'abilitychanneltime': 'channel_times',
    'abilitycooldown': 'cooldowns',
    'abilityduration': 'durations',
    'abilitymanacost': 'mana_costs',
}
RE_BREAK = re.compile(r'<br\s*/?>|</h1>', re.IGNORECASE)
RE_TAG = re.compile(r'<[^>]*>')
# A KeyValues token: a quoted string, a brace, or a comment or [$PLATFORM] condition to skip.
RE_KEY_VALUES = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])|//[^\n]*|\[[^\]\n]*\]')
RE_SLOT = re.compile(r'Ability(\d+)')

KeyValues: TypeAlias = dict[str, 'str | KeyValues']


def fetch(cache: str, delay: float, endpoint: str, language: str, **ids: int) -> dict[str, object]:
    """One datafeed response, read from the cache when an earlier run already has it."""
    query = '&'.join([f'language={language}'] + [f'{name}={value}' for name, value in ids.items()])
    path = os.path.join(cache, endpoint, f'{query}.json')
    if os.path.isfile(path):
        with open(path, encoding='utf-8') as handle:
            return json.load(handle)
    url = f'{BASE_URL}/{endpoint}?{query}'
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    attempts = 0
    while True:
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
            break
        except (urllib.error.URLError, TimeoutError) as error:
            attempts += 1
            if attempts == 3:
                raise
            print(f'{url}: {error}, retrying', flush=True)
            time.sleep(5 * attempts)
    time.sleep(delay)
    # Note (ruidu): list endpoints carry no status and patchnoteslist has no result envelope at all,
    # while a detail query for an id the feed does not know answers status 8 with empty data. Some
    # records answer a bare null in one language only (Tidehunter's English herodata on 7.41f).
    if payload is None:
        raise LookupError(f'{url} answered null')
    status = payload.get('result', {}).get('status', 1)
    if status != 1:
        raise RuntimeError(f'{url} answered status {status}')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False)
    return payload


def parse_key_values(text: str) -> KeyValues:
    """A KeyValues file of the client (items.txt, a hero file, a localization file) as nested dicts.

    A key seen twice keeps its first value, and a block seen twice is merged into the first.
    """
    stack: list[KeyValues] = [{}]
    key: str | None = None
    for match in RE_KEY_VALUES.finditer(text):
        string, brace = match.groups()
        if brace == '{':
            assert key is not None, 'a block without a key'
            block = stack[-1].setdefault(key, {})
            assert isinstance(block, dict), f'{key} is both a value and a block'
            stack.append(block)
            key = None
        elif brace == '}':
            stack.pop()
        elif string is None:
            continue  # a comment or a platform condition
        elif key is None:
            key = string
        else:
            stack[-1].setdefault(key, string)
            key = None
    return stack[0]


def special_values(record: dict[str, object]) -> dict[str, list[float]]:
    """{lower-case name: per-level values} of an item or ability, plus bonus_<name> for what a shard or
    scepter changes; the text asks for names in any case."""
    values = {}
    for special in record['special_values']:
        name, upgraded = special['name'].lower(), special['values_shard'] or special['values_scepter']
        # a value that only a shard or scepter switches on is 0 without it, and the text means the upgrade
        values[name] = special['values_float'] if any(special['values_float']) else upgraded or special['values_float']
        if any(special['values_float']) and upgraded:
            values[f'bonus_{name}'] = upgraded
    return values


def resolve(text: str, values: dict[str, list[float]]) -> str:
    """Datafeed markup to plain text: %name% and {s:name} become values, tags become line breaks or go."""

    def substitute(match: re.Match[str]) -> str:
        name = (match.group(1) if match.group(1) is not None else match.group(2)).lower()
        if name == '':
            return '%'  # %% is a literal percent sign
        return numbers_text(values[name]) if values.get(name) else match.group(0)

    text = RE_BREAK.sub('\n', RE_TOKEN.sub(substitute, text))
    return html.unescape(RE_TAG.sub('', text)).strip()


def text_fields(
    record: dict[str, object], values: dict[str, list[float]], variables: dict[str, str]
) -> dict[str, str | list[str]]:
    """The language-dependent part of an item or ability; empty fields are left out.

    variables names the stats that item headings abbreviate, as the client's tooltips do: $str is Strength.
    """
    known = {token: record[field] for token, field in PROPERTY_TOKENS.items()} | values
    stats = []
    for special in record['special_values']:
        numbers = values[special['name'].lower()]
        if not special['heading_loc'] or not any(numbers):
            continue  # a value the tooltip does not show, or one that only counts in a running game
        heading = resolve(RE_VARIABLE.sub(lambda match: variables[match.group(1)], special['heading_loc']), known)
        amount = f'{numbers_text(numbers)}{"%" if special["is_percentage"] else ""}'
        if heading[:1] in ('+', '-'):
            stats.append(f'{heading[0]}{amount} {heading[1:]}')  # +$str 10 reads +10 Strength in the client
        else:
            stats.append(f'{heading}{"" if heading.endswith("：") else " "}{amount}')
    fields = {
        'name': resolve(record['name_loc'], known),
        'desc': resolve(record['desc_loc'], known),
        'notes': [resolve(note, known) for note in record['notes_loc']],
        'shard': resolve(record['shard_loc'], known),
        'scepter': resolve(record['scepter_loc'], known),
        'stats': stats,
    }
    return {key: value for key, value in fields.items() if value}


def write_json_lines(path: str, document: dict[str, object]) -> None:
    """Plain JSON, but one list element per line, so that a refresh diffs line by line."""
    lines = []
    for key, value in document.items():
        if isinstance(value, list):
            body = ',\n'.join(json.dumps(item, ensure_ascii=False, separators=(',', ':')) for item in value)
            lines.append(f'{json.dumps(key)}:[\n{body}\n]')
        else:
            lines.append(f'{json.dumps(key)}:{json.dumps(value, ensure_ascii=False, separators=(",", ":"))}')
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write('{\n' + ',\n'.join(lines) + '\n}\n')


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default=DATA_DIR)
    parser.add_argument('--cache', default=os.path.join(tempfile.gettempdir(), 'dota2_env_datafeed'))
    parser.add_argument('--delay', type=float, default=0.25, help='seconds to wait after every request')
    args = parser.parse_args()

    def get(endpoint: str, language: str, **ids: int) -> dict[str, object]:
        return fetch(args.cache, args.delay, endpoint, language, **ids)

    def detail(endpoint: str, key: str, **ids: int) -> dict[str, dict[str, object]]:
        return {lang: get(endpoint, language, **ids)['result']['data'][key][0] for lang, language in LANGUAGES.items()}

    game_path = get_default_game_path()
    with open(os.path.join(game_path, 'dota', 'steam.inf'), encoding='utf-8') as handle:
        steam_inf = dict(line.strip().split('=', 1) for line in handle if '=' in line)
    client = {
        path: parse_key_values(data.decode('utf-8-sig'))
        for path, data in read_vpk(os.path.join(game_path, 'dota', 'pak01_dir.vpk'), CLIENT_FILES).items()
    }
    client_items = client['scripts/npc/items.txt']['DOTAAbilities']
    variables = {}
    for lang, language in LANGUAGES.items():
        tokens = client[f'resource/localization/abilities_{language}.txt']['lang']['Tokens']
        variables[lang] = {
            key.removeprefix(VARIABLE_PREFIX): value for key, value in tokens.items() if key.startswith(VARIABLE_PREFIX)
        }
    # A recipe names what it makes and one or more sets of parts; its scroll is a part only when the shop
    # sells it for gold. Note (ruidu): recipes of removed items stay in the file marked IsObsolete, and the
    # * the client puts after some parts and the ; ending some lists are not part of any name.
    components = {}
    for name, block in client_items.items():
        if not isinstance(block, dict) or block.get('ItemRecipe') != '1' or block.get('IsObsolete') == '1':
            continue
        scroll = [name] if int(block['ItemCost']) > 0 and block.get('ItemPurchasable') != '0' else []
        components[block['ItemResult']] = [
            [part.rstrip('*') for part in parts.split(';') if part] + scroll
            for parts in block['ItemRequirements'].values()
        ]

    patch = max(get('patchnoteslist', 'english')['patches'], key=lambda entry: entry['patch_timestamp'])
    header = {
        'patch': patch['patch_number'],
        'client_version': int(steam_inf['ClientVersion']),
        'fetched': time.strftime('%Y-%m-%d'),
        'source': f'{BASE_URL} and game/dota/pak01_dir.vpk',
    }
    print(f'patch {header["patch"]}, client {header["client_version"]}', flush=True)

    items = []
    for entry in sorted(get('itemlist', 'english')['result']['data']['itemabilities'], key=lambda e: e['name']):
        text = detail('itemdata', 'items', item_id=entry['id'])
        values = special_values(text['en'])
        block = client_items[entry['name']]
        # Note (ruidu): the client and the datafeed are separate downloads; a price they disagree on
        # means they are on different patches, and the text would describe neither.
        if int(block.get('ItemCost') or 0) != text['en']['item_cost']:
            raise RuntimeError(f'the client and the datafeed price {entry["name"]} differently: update the client')
        record = {
            'id': entry['id'],
            'name': entry['name'],
            'cost': text['en']['item_cost'],
            'quality': text['en']['item_quality'],
            'neutral_tier': entry['neutral_item_tier'] if entry['neutral_item_tier'] >= 0 else None,
            'recipe': entry['name'].startswith('item_recipe_'),
            'purchasable': block.get('ItemPurchasable') != '0' and block.get('IsObsolete') != '1',
            'components': components.get(entry['name'], []),
            'secret_shop': block.get('SecretShop') == '1',
            'initial_charges': text['en']['item_initial_charges'],
            'stock_max': text['en']['item_stock_max'],
            'cooldowns': text['en']['cooldowns'],
            'mana_costs': text['en']['mana_costs'],
            'cast_ranges': text['en']['cast_ranges'],
            'values': values,
        }
        items.append(record | {lang: text_fields(text[lang], values, variables[lang]) for lang in LANGUAGES})
    print(f'{len(items)} items, {sum(item["secret_shop"] for item in items)} from the Secret Shop', flush=True)

    abilities = {}
    for entry in get('abilitylist', 'english')['result']['data']['itemabilities']:
        # Note (ruidu): talents are taken from herodata below, because only herodata says whose a talent is
        # and holds the ability that carries its number; unnamed entries are internal abilities without text.
        if not entry['name_loc'] or entry['name'].startswith('special_bonus'):
            continue
        text = detail('abilitydata', 'abilities', ability_id=entry['id'])
        values = special_values(text['en'])
        granted_by = [upgrade for upgrade in ('scepter', 'shard') if text['en'][f'ability_is_granted_by_{upgrade}']]
        record = {
            'id': entry['id'],
            'name': entry['name'],
            'talent': False,
            'innate': text['en']['ability_is_innate'],
            'passive': bool(int(text['en']['behavior']) & ABILITY_BEHAVIOR_PASSIVE),
            'granted_by': granted_by[0] if granted_by else None,
            'max_level': text['en']['max_level'],
            'cooldowns': text['en']['cooldowns'],
            'mana_costs': text['en']['mana_costs'],
            'cast_ranges': text['en']['cast_ranges'],
            'values': values,
        }
        abilities[entry['name']] = record | {
            lang: text_fields(text[lang], values, variables[lang]) for lang in LANGUAGES
        }
    print(f'{len(abilities)} abilities', flush=True)

    # Hero and talent names come from the per-language lists, so that herodata, which holds nothing else
    # that depends on the language, is needed in one language only.
    hero_names = {lang: get('herolist', language)['result']['data']['heroes'] for lang, language in LANGUAGES.items()}
    hero_names = {lang: {hero['id']: hero['name_loc'] for hero in listed} for lang, listed in hero_names.items()}
    talent_names = {
        lang: {
            entry['id']: entry['name_loc'] for entry in get('abilitylist', language)['result']['data']['itemabilities']
        }
        for lang, language in LANGUAGES.items()
    }
    heroes = []
    for entry in sorted(get('herolist', 'english')['result']['data']['heroes'], key=lambda e: e['name']):
        try:
            hero = get('herodata', 'english', hero_id=entry['id'])['result']['data']['heroes'][0]
        except LookupError as error:
            print(f'{error}, taking the Chinese record instead', flush=True)
            hero = get('herodata', 'schinese', hero_id=entry['id'])['result']['data']['heroes'][0]
        # The client's Ability1, Ability2, ... are the slots the bot API numbers from 0, and they hold what
        # herodata leaves out (Shadowraze has three); placeholders and talents have no text of their own.
        block = client[f'scripts/npc/heroes/{hero["name"]}.txt']['DOTAHeroes'][hero['name']]
        slots = sorted((key for key in block if RE_SLOT.fullmatch(key)), key=lambda key: int(key[len('Ability') :]))
        heroes.append(
            {
                'id': hero['id'],
                'name': hero['name'],
                'primary_attr': hero['primary_attr'],
                'attack_capability': hero['attack_capability'],
                'abilities': [
                    block[key] for key in slots if block[key] in abilities and not abilities[block[key]]['talent']
                ],
                'talents': [talent['name'] for talent in hero['talents']],
            }
            | {lang: {'name': hero_names[lang][hero['id']]} for lang in LANGUAGES}
        )
        # A talent's number sits in the ability it improves, as a bonus named after the talent;
        # its text asks for it as {s:bonus_<special value name>}.
        bonuses = {}
        for ability in hero['abilities']:
            for special in ability['special_values']:
                for bonus in special['bonuses']:
                    bonuses.setdefault(bonus['name'], {}).setdefault(
                        f'bonus_{special["name"].lower()}', [bonus['value']]
                    )
        for talent in hero['talents']:
            if talent['name'] in abilities:  # generic talents such as +20 damage are shared between heroes
                continue
            values = special_values(talent) | bonuses.get(talent['name'], {})
            names = {lang: {'name': resolve(talent_names[lang][talent['id']], values)} for lang in LANGUAGES}
            abilities[talent['name']] = {'id': talent['id'], 'name': talent['name'], 'talent': True, 'values': values}
            abilities[talent['name']] |= names
    print(f'{len(heroes)} heroes, {len(abilities)} abilities with talents', flush=True)

    os.makedirs(args.out, exist_ok=True)
    for key, records in (('items', items), ('abilities', sorted(abilities.values(), key=lambda r: r['name']))):
        unresolved = sum(len(RE_TOKEN.findall(json.dumps([r['en'], r['zh']], ensure_ascii=False))) for r in records)
        print(f'{key}: {unresolved} unresolved tokens', flush=True)
        write_json_lines(os.path.join(args.out, f'{key}.json'), header | {key: records})
    write_json_lines(os.path.join(args.out, 'heroes.json'), header | {'heroes': heroes})
    print(f'written to {args.out}', flush=True)


if __name__ == '__main__':
    main()
