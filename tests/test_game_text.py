"""The item, ability and hero text of dota2_env/data, and how dota2_env.game_text renders it for prompts."""

import json
import re

from dota2_env.game_text import display_name, hero_text, item_text, load_records


def test_item_and_ability_text_is_there_in_both_languages():
    items, abilities = load_records('items'), load_records('abilities')
    assert items['item_tango']['cost'] > 0
    assert items['item_tango']['en']['name'] == 'Tango' and items['item_tango']['zh']['name'] == '树之祭祀'
    for name in ('nevermore_shadowraze1', 'nevermore_shadowraze2', 'special_bonus_unique_nevermore_7'):
        for language in ('en', 'zh'):
            text = json.dumps(abilities[name][language], ensure_ascii=False)
            assert re.search(r'%\w+%|\{s:\w+\}', text) is None, (name, language, text)


def test_stats_read_the_way_the_client_shows_them():
    records = [*load_records('items').values(), *load_records('abilities').values()]
    stats = [stat for record in records for language in ('en', 'zh') for stat in record[language].get('stats', [])]
    assert stats and not [stat for stat in stats if '$' in stat or '<' in stat]
    assert load_records('items')['item_black_king_bar']['zh']['stats'] == ['+10 力量', '+24 攻击力']


def test_every_hero_lists_its_abilities_in_slot_order():
    heroes, abilities = load_records('heroes'), load_records('abilities')
    raze = [f'nevermore_shadowraze{number}' for number in (1, 2, 3)]
    assert heroes['npc_dota_hero_nevermore']['abilities'][:4] == [*raze, 'nevermore_frenzy']
    assert all(not abilities[name]['talent'] for hero in heroes.values() for name in hero['abilities'])


def test_recipes_add_up_to_the_price_and_name_the_secret_shop():
    items = load_records('items')
    assert items['item_hyperstone']['secret_shop'] and not items['item_tango']['secret_shop']
    assert items['item_magic_wand']['components'] == [
        ['item_magic_stick', 'item_branches', 'item_branches', 'item_recipe_magic_wand']
    ]
    assert len(items['item_power_treads']['components']) == 3  # with a belt, a robe or a band, and no scroll
    for item in items.values():
        for parts in item['components'] if item['purchasable'] else []:
            assert sum(items[part]['cost'] for part in parts) == item['cost'], (item['name'], parts)


def test_an_item_comes_with_its_price_recipe_and_shop():
    assert item_text('item_magic_wand').splitlines()[:2] == [
        '- item_magic_wand 魔杖（Magic Wand）：价格 460，冷却时间 15 秒',
        '  合成：魔棒 + 铁树枝干 + 铁树枝干 + 图纸（150）',
    ]
    assert item_text('item_hyperstone').startswith(
        '- item_hyperstone 振奋宝石（Hyperstone）：价格 2000，神秘商店有售\n'
    )
    assert '  合成：圣者遗物（神秘商店） + 恶魔刀锋（神秘商店）\n' in item_text('item_rapier')
    assert '价格' not in item_text('item_aegis')  # Roshan drops it, no shop sells it
    treads = item_text('item_power_treads', 'en').splitlines()[1]
    assert treads.startswith('  Made of: Boots of Speed + Gloves of Haste + Belt of Strength, or Boots of Speed')


def test_a_hero_gives_an_ability_that_repeats_the_one_before_as_the_difference():
    chinese = hero_text('npc_dota_hero_nevermore').splitlines()
    assert '- nevermore_shadowraze2 毁灭阴影（Shadowraze）：同 nevermore_shadowraze1，距离：450' in chinese
    english = hero_text('npc_dota_hero_nevermore', 'en').splitlines()
    assert english[0] == '- nevermore_shadowraze1 Shadowraze: cooldown 9s, mana 75'
    assert '- nevermore_shadowraze2 Shadowraze: cooldown 9s, mana 75' in english  # its English text differs
    assert "  Aghanim's Scepter: Increases max souls." in english


def test_names_fall_back_to_the_internal_name():
    assert display_name('npc_dota_hero_nevermore') == '影魔' and display_name('item_tango', 'en') == 'Tango'
    assert display_name('generic_hidden') == 'generic_hidden'  # a placeholder slot has no text
