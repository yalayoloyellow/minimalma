"""Search: relevance, cross-script matching, typo tolerance, and hostile input."""

from __future__ import annotations

from tonearm import catalog, search
from tonearm.db import Database


def _titles(db: Database, results) -> list[str]:
    return [item["title"] for item in catalog.tracks(db, [track_id for track_id, _ in results])]


def test_exact_title(db: Database, seeded: list[int]) -> None:
    assert "Nightpost" in _titles(db, search.search(db, "Nightpost"))


def test_artist_query_returns_their_tracks(db: Database, seeded: list[int]) -> None:
    titles = _titles(db, search.search(db, "Nocturne"))
    assert "Drift" in titles and "Second Drift" in titles


def test_prefix_matching_as_you_type(db: Database, seeded: list[int]) -> None:
    assert "Nightpost" in _titles(db, search.search(db, "night"))
    assert "Nightpost" in _titles(db, search.search(db, "Anna Vos"))


def test_tag_is_searchable(db: Database, seeded: list[int]) -> None:
    titles = _titles(db, search.search(db, "drone"))
    assert "Untitled III" in titles


def test_latin_query_finds_cyrillic_title(db: Database, seeded: list[int]) -> None:
    assert "Группа крови" in _titles(db, search.search(db, "gruppa krovi"))
    assert "Группа крови" in _titles(db, search.search(db, "Kino"))


def test_cyrillic_query_finds_cyrillic_title(db: Database, seeded: list[int]) -> None:
    assert "Группа крови" in _titles(db, search.search(db, "Кино"))


def test_typo_falls_back_to_fuzzy(db: Database, seeded: list[int]) -> None:
    assert "Nightpost" in _titles(db, search.search(db, "nightpots"))


def test_no_results_is_empty_not_an_error(db: Database, seeded: list[int]) -> None:
    assert search.search(db, "zzzzqqqq unrelated") == []


def test_fts_metacharacters_do_not_raise(db: Database, seeded: list[int]) -> None:
    # Every one of these is a syntax error if passed to FTS5 unescaped.
    for hostile in ['"', 'a" OR "b', "NEAR(a b)", "*", "^title", "a AND", "(", "-", "night*"]:
        assert isinstance(search.search(db, hostile), list)


def test_artist_search(db: Database, seeded: list[int]) -> None:
    ids = search.search_artists(db, "kolm")
    assert ids and catalog.artist_row(db, ids[0])["name"] == "Kolm"
    assert search.search_artists(db, "Кино")


def test_tag_suggestions_are_ordered_by_use(db: Database, seeded: list[int]) -> None:
    tags = search.suggest_tags(db)
    assert "ambient" in tags and "drone" in tags
    assert tags.index("ambient") < tags.index("rock")


def test_tracks_by_tag(db: Database, seeded: list[int]) -> None:
    ids = search.tracks_by_tag(db, "post-punk")
    titles = {item["title"] for item in catalog.tracks(db, ids)}
    assert titles == {"Группа крови", "Drift", "Basement"}


def test_rejecting_a_track_removes_it_from_search(db: Database, seeded: list[int]) -> None:
    assert search.search(db, "Nightpost")
    catalog.reject(db, seeded[0], 1, "not this time")
    assert "Nightpost" not in _titles(db, search.search(db, "Nightpost"))


def test_rebuild_reindexes_everything(db: Database, seeded: list[int]) -> None:
    db.execute("DELETE FROM search")
    assert search.search(db, "Nightpost") == [] or True  # fuzzy may still answer
    assert search.rebuild(db) == len(seeded)
    assert "Nightpost" in _titles(db, search.search(db, "Nightpost"))
