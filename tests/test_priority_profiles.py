import pytest
from src import evaluation_service as evaluation
from src import priority_profiles


def test_named_profiles_are_shared_and_newest_created_is_default(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation, 'PROJECT_ROOT', tmp_path)
    assert evaluation.list_priority_profiles() == []
    city = evaluation.save_priority_profile('City', {'quiet_sleep': 5}, 'Near public transport.')
    beach = evaluation.save_priority_profile('Beach', {'quiet_sleep': 1}, 'Near the sea.')
    assert [profile['name'] for profile in evaluation.list_priority_profiles()] == ['Beach', 'City']
    updated = evaluation.save_priority_profile('City', {'quiet_sleep': 4}, 'Walking distance to transit.')
    assert updated['created_at'] == city['created_at']
    assert [profile['name'] for profile in evaluation.list_priority_profiles()] == ['Beach', 'City']
    evaluation.use_priority_profile('search-one', 'City')
    evaluation.use_priority_profile('search-two', 'City')
    assert evaluation.load_evaluation_profile('search-two', 'City')['weights']['quiet_sleep'] == 4
    # Global preferences must not create an ancestor database that could absorb
    # resources belonging to future search workspaces.
    assert not (tmp_path / 'searches' / 'analyzer.sqlite3').exists()
    evaluation.delete_priority_profile('City')
    assert [profile['name'] for profile in evaluation.list_priority_profiles()] == ['Beach']
    assert evaluation.load_evaluation_profile('search-one', 'City')['weights']['quiet_sleep'] == 4
    evaluation.delete_priority_profile('Beach')
    assert evaluation.list_priority_profiles() == []  # Deleted names do not return from local snapshots.


def test_use_profile_reuses_unchanged_search_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation, 'PROJECT_ROOT', tmp_path)
    evaluation.save_priority_profile('Enrica', {'cleanliness': 5})
    first = evaluation.use_priority_profile('trip', 'Enrica')
    second = evaluation.use_priority_profile('trip', 'Enrica')
    assert first['revision'] == second['revision']
    assert first['updated_at'] == second['updated_at']


def test_legacy_named_profiles_are_preserved_but_neutral_default_is_not(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation, 'PROJECT_ROOT', tmp_path)
    evaluation.save_evaluation_profile('old-trip', 'Default', {key: 3 for key in evaluation.DEFAULT_WEIGHTS})
    evaluation.save_evaluation_profile('old-trip', 'Enrica', {'cleanliness': 5})
    assert [profile['name'] for profile in evaluation.list_priority_profiles()] == ['Enrica']
    assert (tmp_path / 'searches' / '.priority_profiles.json').exists()
    evaluation.delete_priority_profile('Enrica')
    assert evaluation.list_priority_profiles() == []


def test_invalid_names_and_corrupt_library_are_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation, 'PROJECT_ROOT', tmp_path)
    with pytest.raises(ValueError):
        evaluation.save_priority_profile('../bad', {'cleanliness': 5})
    path = tmp_path / 'searches' / '.priority_profiles.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{broken')
    with pytest.raises(ValueError):
        evaluation.save_priority_profile('City', {'cleanliness': 5})
    assert path.read_text() == '{broken'
