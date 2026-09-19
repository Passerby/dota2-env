import json
import os

import pytest

from dota2_env.bridge.constants import TEAM_DIRE, TEAM_RADIANT
from dota2_env.bridge.game import DotaGame


@pytest.fixture
def game(tmp_path):
    dota_path = tmp_path / 'game'
    (dota_path / 'dota' / 'scripts' / 'vscripts').mkdir(parents=True)
    game = DotaGame(
        dota_path=str(dota_path),
        session_root=str(tmp_path / 'sessions'),
        heroes={TEAM_RADIANT: 'npc_dota_hero_nevermore', TEAM_DIRE: 'npc_dota_hero_sniper'},
    )
    yield game
    game.remove_bot_symlink()


def lua_string_payload(path):
    with open(path, encoding='utf-8') as handle:
        text = handle.read()
    assert text.startswith("return '") and text.endswith("'")
    # undo the lua escaping the same way lua would
    return text[len("return '") : -1].replace("\\'", "'").replace('\\\\', '\\')


def test_session_folder_layout(game):
    assert os.path.islink(game.dota_bot_path)
    files = set(os.listdir(game.bot_path))
    assert {'bot_nevermore.lua', 'bot_sniper.lua', 'hero_selection.lua', 'config_auto.lua', 'actions'} <= files
    config = json.loads(lua_string_payload(os.path.join(game.bot_path, 'config_auto.lua')))
    assert config['heroes'] == {'2': 'npc_dota_hero_nevermore', '3': 'npc_dota_hero_sniper'}
    assert config['control'] == {'2': 'agent', '3': 'idle'}


def test_action_file_escaping_roundtrip(game):
    data = {'dotaTime': 1.5, 'actions': [{'actionType': 'ACTION_CHAT', 'chat': {'message': 'it\'s a "test" \\ ok'}}]}
    game.write_action(data, TEAM_RADIANT)
    path = os.path.join(game.bot_path, 'actions_t2.lua')
    assert json.loads(lua_string_payload(path)) == data
    assert not os.path.exists(path + '.tmp')


def test_refuses_to_replace_a_real_bots_directory(tmp_path):
    dota_path = tmp_path / 'game'
    (dota_path / 'dota' / 'scripts' / 'vscripts' / 'bots').mkdir(parents=True)
    with pytest.raises(ValueError):
        DotaGame(dota_path=str(dota_path), session_root=str(tmp_path))
