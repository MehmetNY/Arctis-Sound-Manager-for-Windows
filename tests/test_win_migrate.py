from types import SimpleNamespace

from arctis_sound_manager.win import migrate


def test_the_engine_switch_is_turned_on_once_and_then_left_alone(tmp_path):
    marks = tmp_path / "migrations.json"
    settings = SimpleNamespace(windows_audio_engine=False)
    saved = []
    assert migrate.run(path=marks, load=lambda: settings, save=saved.append) == ["engine_on"]
    assert settings.windows_audio_engine is True and saved == [settings]
    settings.windows_audio_engine = False                                        # the user switches it off on purpose later
    assert migrate.run(path=marks, load=lambda: settings, save=saved.append) == []
    assert settings.windows_audio_engine is False and len(saved) == 1


def test_an_engine_that_is_already_on_changes_nothing_but_the_mark(tmp_path):
    marks = tmp_path / "migrations.json"
    settings = SimpleNamespace(windows_audio_engine=True)
    saved = []
    assert migrate.run(path=marks, load=lambda: settings, save=saved.append) == []
    assert not saved and marks.exists()


def test_a_broken_settings_file_never_stops_anything(tmp_path):
    def boom():
        raise OSError("disk")
    assert migrate.run(path=tmp_path / "m.json", load=boom, save=lambda gs: None) == []
