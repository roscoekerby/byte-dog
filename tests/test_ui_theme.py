import pytest

import ui_prefs
import ui_theme
from ui_theme import DARK, LIGHT, mix, resolve, value_map, windows_prefers_dark
from ui_widgets import nice_ceiling


def test_palettes_have_the_same_keys():
    assert set(DARK) == set(LIGHT)


def test_switching_either_way_is_unambiguous():
    assert value_map(DARK, LIGHT)['#1b1d23'] == LIGHT['bg']
    assert value_map(LIGHT, DARK)['#ffffff'] == DARK['surface']


def test_ambiguous_palette_is_refused():
    with pytest.raises(ValueError):
        value_map({'a': '#000000', 'b': '#000000'}, {'a': '#111111', 'b': '#222222'})


def test_system_follows_windows_app_mode():
    assert windows_prefers_dark(lambda: 0) is True
    assert windows_prefers_dark(lambda: 1) is False
    assert windows_prefers_dark(lambda: None) is True
    assert resolve('system', lambda: False) is LIGHT
    assert resolve('dark') is DARK and resolve('light') is LIGHT
    with pytest.raises(ValueError):
        resolve('purple')


def test_mix_blends_hex_colours():
    assert mix('#000000', '#ffffff', 0.5) == '#808080'
    assert mix('#123456', '#ffffff', 0) == '#123456'


def test_nice_ceiling_rounds_axis_tops():
    assert nice_ceiling(0) == 1
    assert nice_ceiling(730) == 1000
    assert nice_ceiling(1_400_000) == 2_000_000
    assert nice_ceiling(4.2) == 5


def test_prefs_round_trip_and_survive_garbage(tmp_path):
    path = tmp_path / 'ui.json'
    assert ui_prefs.load(path) == ui_prefs.UiPrefs()
    ui_prefs.save(ui_prefs.UiPrefs(theme='light', see_through=True), path)
    assert ui_prefs.load(path) == ui_prefs.UiPrefs('light', True)
    path.write_text('{"theme": "neon", "see_through": "yes"}', encoding='utf-8')
    assert ui_prefs.load(path) == ui_prefs.UiPrefs()
    path.write_text('not json', encoding='utf-8')
    assert ui_prefs.load(path) == ui_prefs.UiPrefs()
