"""The ranking engine, including the rules that exist for ethical reasons."""

from __future__ import annotations

from tonearm import recommend
from tonearm.config import Config
from tonearm.db import Database

from .conftest import LISTENER, OTHER


def _by_title(db: Database) -> dict[str, int]:
    return {
        row["title"]: int(row["id"])
        for row in db.query("SELECT id, title FROM tracks WHERE status='approved'")
    }


class TestDailySelection:
    def test_size_and_stability(
        self, db: Database, engine: recommend.Engine, seeded: list[int]
    ) -> None:
        first = engine.daily_selection(LISTENER)
        assert len(first) == engine.config.limits.daily_selection
        # Re-asking must return the same thing: a re-rollable selection is a
        # slot machine.
        assert engine.daily_selection(LISTENER) == first

    def test_survives_a_restart(self, db: Database, config: Config, seeded: list[int]) -> None:
        first = recommend.Engine(db, config).daily_selection(LISTENER)
        assert recommend.Engine(db, config).daily_selection(LISTENER) == first

    def test_one_track_per_artist(
        self, db: Database, engine: recommend.Engine, seeded: list[int]
    ) -> None:
        picks = engine.daily_selection(LISTENER)
        artists = [
            int(db.scalar("SELECT artist_id FROM tracks WHERE id=?", (track_id,)))
            for track_id in picks
        ]
        assert len(set(artists)) == len(artists)

    def test_the_catalogue_rotates_as_tracks_get_exposure(
        self, db: Database, engine: recommend.Engine, seeded: list[int]
    ) -> None:
        """Two cold listeners are ranked identically until exposure moves.

        Fairness here comes from the exploration term, not from randomness:
        once a track has been shown, its bonus drops and the next listener is
        steered towards something else.
        """
        first = engine.daily_selection(LISTENER)
        recommend.record_exposure(db, first * 40)
        second = engine.daily_selection(OTHER)
        assert set(second) - set(first)

    def test_empty_catalogue_is_not_a_crash(self, db: Database, engine: recommend.Engine) -> None:
        assert engine.daily_selection(LISTENER) == []

    def test_reserves_a_slot_for_the_least_exposed_track(
        self, db: Database, engine: recommend.Engine, seeded: list[int]
    ) -> None:
        # Everything has been shown a lot except one deliberately buried track.
        db.execute("UPDATE tracks SET exposures = 500")
        buried = _by_title(db)["Foghorn"]
        db.execute("UPDATE tracks SET exposures = 0 WHERE id=?", (buried,))
        assert buried in engine.daily_selection(LISTENER)


class TestExposureFairness:
    def test_unheard_tracks_outrank_saturated_ones(
        self, db: Database, engine: recommend.Engine, seeded: list[int]
    ) -> None:
        titles = _by_title(db)
        db.execute("UPDATE tracks SET exposures = 1000")
        fresh = titles["Loop Two"]
        db.execute("UPDATE tracks SET exposures = 0 WHERE id=?", (fresh,))
        ranked = engine.rank(LISTENER, seeded)
        order = [track_id for track_id, _ in ranked]
        assert order.index(fresh) < len(order) // 2

    def test_exposure_is_counted_only_when_a_track_is_sent(
        self, db: Database, seeded: list[int]
    ) -> None:
        before = int(db.scalar("SELECT exposures FROM tracks WHERE id=?", (seeded[0],)))
        recommend.record_exposure(db, [seeded[0]])
        after = int(db.scalar("SELECT exposures FROM tracks WHERE id=?", (seeded[0],)))
        assert after == before + 1


class TestContentSignal:
    def test_taste_profile_pulls_similar_tags_up(
        self, db: Database, engine: recommend.Engine, seeded: list[int]
    ) -> None:
        titles = _by_title(db)
        for title in ("Nightpost", "Untitled III"):
            recommend.record(db, LISTENER, titles[title], "like")
        engine.invalidate()

        candidates = [t for t in seeded if t not in (titles["Nightpost"], titles["Untitled III"])]
        ranked = [track_id for track_id, _ in engine.rank(LISTENER, candidates)]
        drone_or_ambient = {titles["Stairwell"], titles["Untitled IV"]}
        loud_rock = {titles["Basement"], titles["Группа крови"]}
        best_matching = min(ranked.index(t) for t in drone_or_ambient)
        best_unrelated = min(ranked.index(t) for t in loud_rock)
        assert best_matching < best_unrelated

    def test_cold_user_still_gets_a_full_ranking(
        self, db: Database, engine: recommend.Engine, seeded: list[int]
    ) -> None:
        ranked = engine.rank(LISTENER, seeded)
        assert len(ranked) == len(seeded)
        assert all(score > 0 for _, score in ranked)

    def test_vector_is_unit_length(
        self, db: Database, engine: recommend.Engine, seeded: list[int]
    ) -> None:
        vector = engine.vector(seeded[0])
        assert abs(sum(v * v for v in vector.values()) - 1.0) < 1e-9


class TestCollaborative:
    def test_co_liked_tracks_become_neighbours(
        self, db: Database, engine: recommend.Engine, seeded: list[int]
    ) -> None:
        titles = _by_title(db)
        pair = (titles["Loop One"], titles["Basement"])  # deliberately dissimilar content
        for user_id in range(950000, 950010):
            for track_id in pair:
                recommend.record(db, user_id, track_id, "like")
        engine.rebuild_similarity()
        neighbours = dict(engine.neighbours(pair[0]))
        assert pair[1] in neighbours and neighbours[pair[1]] > 0

    def test_popularity_damping_limits_a_blockbuster(
        self, db: Database, engine: recommend.Engine, seeded: list[int]
    ) -> None:
        titles = _by_title(db)
        hit = titles["Basement"]
        niche_a, niche_b = titles["Foghorn"], titles["Harbour"]
        # Everyone likes the hit; only two people like both niche tracks.
        for user_id in range(960000, 960040):
            recommend.record(db, user_id, hit, "like")
        for user_id in range(960000, 960002):
            recommend.record(db, user_id, niche_a, "like")
            recommend.record(db, user_id, niche_b, "like")
        engine.rebuild_similarity()
        niche_link = dict(engine.neighbours(niche_a)).get(niche_b, 0.0)
        hit_link = dict(engine.neighbours(niche_a)).get(hit, 0.0)
        assert niche_link > hit_link

    def test_rebuild_is_scheduled_not_constant(
        self, db: Database, engine: recommend.Engine, seeded: list[int]
    ) -> None:
        assert engine.maybe_rebuild() is False
        for user_id in range(970000, 970100):
            recommend.record(db, user_id, seeded[0], "like")
            recommend.record(db, user_id, seeded[1], "like")
        assert engine.maybe_rebuild() is True
        assert engine.maybe_rebuild() is False


class TestDiscovery:
    def test_excludes_what_you_already_heard(
        self, db: Database, engine: recommend.Engine, seeded: list[int]
    ) -> None:
        daily = engine.daily_selection(LISTENER)
        for track_id in daily:
            recommend.record(db, LISTENER, track_id, "play")
        picks = engine.discover(LISTENER)
        assert picks and not set(picks) & set(daily)

    def test_returns_nothing_once_the_catalogue_is_exhausted(
        self, db: Database, engine: recommend.Engine, seeded: list[int]
    ) -> None:
        for track_id in seeded:
            recommend.record(db, LISTENER, track_id, "play")
        assert engine.discover(LISTENER) == []


class TestMix:
    def test_is_finite_and_ordered(
        self, db: Database, engine: recommend.Engine, seeded: list[int]
    ) -> None:
        order = engine.mix(LISTENER, length=6)
        assert 1 < len(order) <= 6
        assert len(set(order)) == len(order)

    def test_avoids_two_tracks_by_the_same_artist_in_a_row(
        self, db: Database, engine: recommend.Engine, seeded: list[int]
    ) -> None:
        order = engine.mix(LISTENER, length=10)
        artists = [
            int(db.scalar("SELECT artist_id FROM tracks WHERE id=?", (track_id,)))
            for track_id in order
        ]
        assert all(a != b for a, b in zip(artists, artists[1:]))

    def test_walks_rather_than_jumps(
        self, db: Database, engine: recommend.Engine, seeded: list[int]
    ) -> None:
        titles = _by_title(db)
        order = engine.mix(LISTENER, seed=titles["Nightpost"], length=4)
        # The neighbour of an ambient seed should not be the loudest thing here.
        assert order[1] != titles["Basement"]


class TestSimilar:
    def test_content_neighbours_without_any_interactions(
        self, db: Database, engine: recommend.Engine, seeded: list[int]
    ) -> None:
        titles = _by_title(db)
        similar = engine.similar_to(titles["Drift"], count=3)
        assert titles["Second Drift"] in similar

    def test_never_returns_the_source(
        self, db: Database, engine: recommend.Engine, seeded: list[int]
    ) -> None:
        assert seeded[0] not in engine.similar_to(seeded[0])


class TestInteractionWeights:
    def test_a_skip_is_weak_evidence(self) -> None:
        assert abs(recommend.INTERACTION_WEIGHTS["skip"]) < recommend.INTERACTION_WEIGHTS["like"]

    def test_counters_move_with_events(self, db: Database, seeded: list[int]) -> None:
        recommend.record(db, LISTENER, seeded[0], "play")
        recommend.record(db, LISTENER, seeded[0], "complete")
        row = db.one("SELECT plays, completes FROM tracks WHERE id=?", (seeded[0],))
        assert row["plays"] == 1 and row["completes"] == 1
