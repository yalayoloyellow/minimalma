"""Ranking, discovery and mixes.

The engine is a hybrid with three signals and two hard rules.

Signals
    content     A sparse TF-IDF vector per track over curator tags, genre and
                bucketed acoustic descriptors. Works from the moment a track
                is approved, which is the only thing that matters on a
                platform whose whole point is unknown artists.
    collaborative
                Item-item co-occurrence over positive interactions with
                popularity damping, ``sim(i,j) = cooc(i,j) / (pop_i^a *
                pop_j^(1-a))``. Blended in proportionally to how much the
                listener has actually done, so it never dominates a cold
                profile.
    exposure    A UCB1-shaped bonus, ``sqrt(ln(E+2) / (e_t+1))``, that lifts
                tracks nobody has been shown yet. Without it the catalogue
                collapses onto whatever was approved first.

Hard rules
    * At most one track per artist in any single selection.
    * Every daily selection reserves one slot for the least-exposed eligible
      track, regardless of score.

Nothing here optimises for session length, skip rate or return frequency. The
ranking objective is "would this listener keep this track", approximated by
explicit saves/likes and requests. Telegram does not expose reliable playback
completion events, so the engine never treats a delivered audio message as a
completed listen.
"""

from __future__ import annotations

import json
import logging
import math
import random
import threading
import time
from collections.abc import Iterable, Sequence
from datetime import date, datetime

from . import audio, metadata
from .config import Config
from .db import Database, now

log = logging.getLogger("minimalma.recommend")

#: How much each recorded interaction says about taste. ``skip`` is negative
#: but small: a skip is weak evidence, and weighting it heavily is exactly how
#: mainstream recommenders end up optimising for stickiness.
INTERACTION_WEIGHTS = {
    "like": 1.0,
    "save": 0.8,
    "follow": 0.5,
    "play": 0.2,
    "skip": -0.25,
}
POSITIVE_KINDS = ("like", "save", "play", "follow")

#: Neighbours stored per track.
TOP_K = 40
#: Interactions considered per user when building co-occurrence.
USER_ITEM_CAP = 300
#: Rebuild cadence.
REBUILD_EVERY_EVENTS = 150
REBUILD_EVERY_SECONDS = 6 * 3600


def today(reference: float | None = None) -> str:
    """The service day, in the host's local timezone, as ``YYYY-MM-DD``."""
    stamp = datetime.fromtimestamp(reference if reference is not None else time.time())
    return stamp.date().isoformat()


class Engine:
    """Holds the caches that make ranking cheap; one instance per process."""

    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        self._lock = threading.Lock()
        self._vectors: dict[int, dict[str, float]] = {}
        self._inverted: dict[str, list[tuple[int, float]]] | None = None
        self._idf: dict[str, float] | None = None
        self._generation = 0
        self._rebuilding = False

    # ---------------------------------------------------------------- caches
    def invalidate(self) -> None:
        """Drop derived state after any change to the approved catalogue."""
        with self._lock:
            self._vectors.clear()
            self._inverted = None
            self._idf = None
            self._generation += 1

    # -------------------------------------------------------------- content
    def tokens(self, track_id: int) -> list[str]:
        """The content tokens describing one track."""
        row = self.db.one(
            "SELECT t.features, t.note, t.year, a.key AS artist_key "
            "FROM tracks t JOIN artists a ON a.id = t.artist_id WHERE t.id=?",
            (track_id,),
        )
        if row is None:
            return []
        out = [
            f"tag:{r['name']}"
            for r in self.db.query(
                "SELECT g.name FROM track_tags tt JOIN tags g ON g.id = tt.tag_id "
                "WHERE tt.track_id=?",
                (track_id,),
            )
        ]
        # The artist is a weak content token: it links a discography without
        # letting one prolific artist dominate a listener's profile.
        if row["artist_key"]:
            out.append(f"artist:{row['artist_key']}")
        if row["year"]:
            out.append(f"decade:{int(row['year']) // 10 * 10}")
        features = _load_json(row["features"])
        if features:
            out.extend(audio.feature_tokens(features))
        return out

    def idf(self) -> dict[str, float]:
        with self._lock:
            if self._idf is not None:
                return self._idf
        counts: dict[str, int] = {}
        total = 0
        for row in self.db.query("SELECT id FROM tracks WHERE status='approved'"):
            total += 1
            for token in set(self.tokens(row["id"])):
                counts[token] = counts.get(token, 0) + 1
        idf = {token: math.log(1.0 + (total or 1) / count) for token, count in counts.items()}
        with self._lock:
            self._idf = idf
        return idf

    def vector(self, track_id: int) -> dict[str, float]:
        """Unit-length TF-IDF vector for a track."""
        with self._lock:
            cached = self._vectors.get(track_id)
        if cached is not None:
            return cached
        idf = self.idf()
        raw = {token: idf.get(token, 1.0) for token in set(self.tokens(track_id))}
        norm = math.sqrt(sum(value * value for value in raw.values())) or 1.0
        vector = {token: value / norm for token, value in raw.items()}
        with self._lock:
            if len(self._vectors) > 20000:
                self._vectors.clear()
            self._vectors[track_id] = vector
        return vector

    def inverted(self) -> dict[str, list[tuple[int, float]]]:
        """Token to (track, weight) postings, so content search is not a scan."""
        with self._lock:
            if self._inverted is not None:
                return self._inverted
        index: dict[str, list[tuple[int, float]]] = {}
        for row in self.db.query("SELECT id FROM tracks WHERE status='approved'"):
            for token, weight in self.vector(row["id"]).items():
                index.setdefault(token, []).append((row["id"], weight))
        with self._lock:
            self._inverted = index
        return index

    def content_scores(
        self, query: dict[str, float], exclude: set[int] | None = None
    ) -> dict[int, float]:
        """Cosine of ``query`` against every approved track, via the postings."""
        if not query:
            return {}
        index = self.inverted()
        out: dict[int, float] = {}
        exclude = exclude or set()
        for token, weight in query.items():
            for track_id, other in index.get(token, ()):
                if track_id in exclude:
                    continue
                out[track_id] = out.get(track_id, 0.0) + weight * other
        return out

    # -------------------------------------------------------------- profile
    def profile(self, user_id: int) -> tuple[dict[str, float], float]:
        """Return ``(taste vector, positive interaction mass)`` for a listener.

        Older interactions decay with a configurable half-life so a profile
        follows a person rather than pinning them to what they liked once.
        """
        half_life = max(1.0, self.config.weights.profile_half_life_days) * 86400.0
        rows = self.db.query(
            "SELECT track_id, kind, ts, weight FROM events "
            "WHERE user_id=? AND track_id IS NOT NULL AND kind IN "
            "('like','save','play','skip') ORDER BY ts DESC LIMIT 600",
            (user_id,),
        )
        stamp = now()
        combined: dict[str, float] = {}
        mass = 0.0
        for row in rows:
            weight = float(row["weight"]) * math.pow(0.5, (stamp - int(row["ts"])) / half_life)
            if abs(weight) < 1e-4:
                continue
            if weight > 0:
                mass += weight
            for token, value in self.vector(int(row["track_id"])).items():
                combined[token] = combined.get(token, 0.0) + weight * value
        norm = math.sqrt(sum(v * v for v in combined.values()))
        if norm:
            combined = {k: v / norm for k, v in combined.items()}
        return combined, mass

    # -------------------------------------------------- collaborative filter
    def neighbours(self, track_id: int, limit: int = TOP_K) -> list[tuple[int, float]]:
        rows = self.db.query(
            "SELECT other_id, score FROM similar WHERE track_id=? ORDER BY score DESC LIMIT ?",
            (track_id, limit),
        )
        return [(int(r["other_id"]), float(r["score"])) for r in rows]

    def maybe_rebuild(self, force: bool = False) -> bool:
        """Rebuild item-item similarity if it has drifted. Cheap when it hasn't."""
        last_at = float(self.db.get_meta("similarity_at", 0) or 0)
        last_events = int(self.db.get_meta("similarity_events", 0) or 0)
        events = int(self.db.scalar("SELECT COUNT(*) FROM events", default=0))
        stale = (
            force
            or events - last_events >= REBUILD_EVERY_EVENTS
            or (events > 0 and time.time() - last_at >= REBUILD_EVERY_SECONDS)
        )
        if not stale:
            return False
        with self._lock:
            if self._rebuilding:
                return False
            self._rebuilding = True
        try:
            self.rebuild_similarity()
            self.db.set_meta("similarity_at", time.time())
            self.db.set_meta("similarity_events", events)
            return True
        finally:
            with self._lock:
                self._rebuilding = False

    def rebuild_similarity(self, alpha: float | None = None, top_k: int = TOP_K) -> int:
        """Recompute the whole neighbourhood table.

        O(sum over users of n_u^2) with ``n_u`` capped, which for a curated
        catalogue is milliseconds to seconds. There is no incremental variant
        because at this size there does not need to be one.
        """
        alpha = self.config.weights.popularity_alpha if alpha is None else alpha
        rows = self.db.query(
            "SELECT user_id, track_id, SUM(weight) AS w FROM events "
            "WHERE track_id IS NOT NULL AND kind IN "
            "('like','save','play') "
            "GROUP BY user_id, track_id HAVING w > 0"
        )
        by_user: dict[int, list[tuple[int, float]]] = {}
        popularity: dict[int, float] = {}
        for row in rows:
            weight = min(1.0, float(row["w"]))
            by_user.setdefault(int(row["user_id"]), []).append((int(row["track_id"]), weight))
            popularity[int(row["track_id"])] = popularity.get(int(row["track_id"]), 0.0) + weight

        cooccurrence: dict[int, dict[int, float]] = {}
        for items in by_user.values():
            if len(items) < 2:
                continue
            if len(items) > USER_ITEM_CAP:
                items = sorted(items, key=lambda item: item[1], reverse=True)[:USER_ITEM_CAP]
            for index, (left, wl) in enumerate(items):
                bucket = cooccurrence.setdefault(left, {})
                for right, wr in items[index + 1 :]:
                    product = wl * wr
                    bucket[right] = bucket.get(right, 0.0) + product
                    mirror = cooccurrence.setdefault(right, {})
                    mirror[left] = mirror.get(left, 0.0) + product

        payload: list[tuple[int, int, float]] = []
        for left, bucket in cooccurrence.items():
            pop_left = popularity.get(left, 1.0) or 1.0
            scored: list[tuple[int, float]] = []
            for right, count in bucket.items():
                pop_right = popularity.get(right, 1.0) or 1.0
                denominator = math.pow(pop_left, alpha) * math.pow(pop_right, 1.0 - alpha)
                if denominator > 0:
                    scored.append((right, count / denominator))
            scored.sort(key=lambda item: item[1], reverse=True)
            payload.extend((left, right, score) for right, score in scored[:top_k])

        with self.db.transaction() as conn:
            conn.execute("DELETE FROM similar")
            conn.executemany(
                "INSERT INTO similar(track_id, other_id, score) VALUES(?,?,?)", payload
            )
        log.info("similarity rebuilt: %d edges over %d users", len(payload), len(by_user))
        return len(payload)

    # --------------------------------------------------------------- ranking
    def heard(self, user_id: int) -> set[int]:
        """Tracks the listener has already been served."""
        rows = self.db.query(
            "SELECT DISTINCT track_id FROM events WHERE user_id=? AND track_id IS NOT NULL "
            "AND kind IN ('play','skip','like','save')",
            (user_id,),
        )
        return {int(row["track_id"]) for row in rows}

    def eligible(self, exclude: set[int] | None = None, limit: int = 4000) -> list[int]:
        rows = self.db.query(
            "SELECT id FROM tracks WHERE status='approved' ORDER BY published_at DESC LIMIT ?",
            (limit,),
        )
        exclude = exclude or set()
        return [int(row["id"]) for row in rows if int(row["id"]) not in exclude]

    def rank(
        self,
        user_id: int,
        candidates: Sequence[int],
        seed_boost: dict[int, float] | None = None,
    ) -> list[tuple[int, float]]:
        """Score candidates for one listener, best first."""
        if not candidates:
            return []
        weights = self.config.weights
        pool = set(candidates)
        profile, mass = self.profile(user_id)
        confidence = mass / (mass + max(1e-6, weights.cf_confidence_k))

        content = {
            track_id: score
            for track_id, score in self.content_scores(profile).items()
            if track_id in pool
        }

        collaborative: dict[int, float] = dict(seed_boost or {})
        seeds = self.db.query(
            "SELECT track_id, SUM(weight) AS w FROM events "
            "WHERE user_id=? AND track_id IS NOT NULL AND kind IN ('like','save') "
            "GROUP BY track_id ORDER BY w DESC LIMIT 60",
            (user_id,),
        )
        for row in seeds:
            weight = min(1.0, float(row["w"]))
            for other, score in self.neighbours(int(row["track_id"])):
                if other in pool:
                    collaborative[other] = collaborative.get(other, 0.0) + weight * score

        stamp = now()
        total_exposure = float(
            self.db.scalar(
                "SELECT COALESCE(SUM(exposures), 0) FROM tracks WHERE status='approved'",
                default=0,
            )
        )
        exposure_scale = math.sqrt(math.log(total_exposure + 2.0)) or 1.0

        meta = {
            int(row["id"]): row
            for row in self.db.query(
                "SELECT id, published_at, exposures, note, artist_id FROM tracks "
                f"WHERE id IN ({_placeholders(len(pool))})",
                tuple(pool),
            )
        }
        tag_counts = {
            int(row["track_id"]): int(row["n"])
            for row in self.db.query(
                f"SELECT track_id, COUNT(*) AS n FROM track_tags "
                f"WHERE track_id IN ({_placeholders(len(pool))}) GROUP BY track_id",
                tuple(pool),
            )
        }

        cf_max = max(collaborative.values(), default=0.0) or 1.0
        cb_max = max(content.values(), default=0.0) or 1.0

        scored: list[tuple[int, float]] = []
        for track_id in pool:
            row = meta.get(track_id)
            if row is None:
                continue
            age_days = max(0.0, (stamp - int(row["published_at"] or stamp)) / 86400.0)
            freshness = math.exp(-age_days / 30.0)
            curator = 0.5 * (1.0 if (row["note"] or "").strip() else 0.0) + 0.5 * min(
                1.0, tag_counts.get(track_id, 0) / 3.0
            )
            exposure = (
                math.sqrt(math.log(total_exposure + 2.0) / (float(row["exposures"] or 0) + 1.0))
                / exposure_scale
            )
            score = (
                weights.collaborative * confidence * (collaborative.get(track_id, 0.0) / cf_max)
                + weights.content * (1.0 - 0.5 * confidence) * (content.get(track_id, 0.0) / cb_max)
                + weights.freshness * freshness
                + weights.curator * curator
                + weights.exploration * min(1.0, exposure)
            )
            scored.append((track_id, score))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored

    def diversify(
        self,
        scored: Sequence[tuple[int, float]],
        count: int,
        one_per_artist: bool = True,
    ) -> list[int]:
        """Maximal Marginal Relevance with a hard one-track-per-artist rule.

        ``MMR = argmax [ lambda * score(t) - (1 - lambda) * max_sim(t, chosen) ]``
        """
        if not scored:
            return []
        lam = self.config.weights.mmr_lambda
        pool = list(scored[: max(count * 8, 40)])
        artists = {
            int(row["id"]): int(row["artist_id"])
            for row in self.db.query(
                f"SELECT id, artist_id FROM tracks WHERE id IN ({_placeholders(len(pool))})",
                tuple(track_id for track_id, _ in pool),
            )
        }
        chosen: list[int] = []
        used_artists: set[int] = set()
        best_score = max(score for _, score in pool) or 1.0

        while pool and len(chosen) < count:
            best_index = -1
            best_value = -1e9
            for index, (track_id, score) in enumerate(pool):
                artist = artists.get(track_id)
                if one_per_artist and artist is not None and artist in used_artists:
                    continue
                penalty = 0.0
                if chosen:
                    vector = self.vector(track_id)
                    penalty = max(_cosine(vector, self.vector(other)) for other in chosen)
                value = lam * (score / best_score) - (1.0 - lam) * penalty
                if value > best_value:
                    best_value = value
                    best_index = index
            if best_index < 0:
                # Every remaining candidate is blocked by the artist rule.
                if not one_per_artist:
                    break
                one_per_artist = False
                continue
            track_id, _ = pool.pop(best_index)
            chosen.append(track_id)
            artist = artists.get(track_id)
            if artist is not None:
                used_artists.add(artist)
        return chosen

    # ------------------------------------------------------------ selections
    def daily_selection(self, user_id: int, day: str | None = None) -> list[int]:
        """The finite, stable selection for one listener on one day.

        Written once and never regenerated: re-rolling a selection is the
        slot-machine mechanic this project exists to avoid.
        """
        day = day or today()
        row = self.db.one("SELECT ids FROM daily WHERE user_id=? AND day=?", (user_id, day))
        if row is not None:
            return [int(x) for x in json.loads(row["ids"])]

        count = max(1, self.config.limits.daily_selection)
        heard = self.heard(user_id)
        candidates = self.eligible(heard)
        if not candidates:
            # Nothing new: fall back to the listener's own library so the
            # screen is never simply broken.
            candidates = self.eligible()
            if not candidates:
                return []
        scored = self.rank(user_id, candidates)

        # A stable per-user, per-day jitter keeps two listeners with identical
        # histories from receiving identical selections, without making the
        # selection re-rollable.
        rng = random.Random(f"{user_id}:{day}")
        scored = [(track_id, score + rng.random() * 0.03) for track_id, score in scored]
        scored.sort(key=lambda item: item[1], reverse=True)

        picks = self.diversify(scored, count)
        newcomer = self._least_exposed(set(picks) | heard)
        if newcomer is not None and len(picks) >= 2:
            picks[-1] = newcomer

        self.db.execute(
            "INSERT OR REPLACE INTO daily(user_id, day, ids, served) VALUES(?,?,?,0)",
            (user_id, day, json.dumps(picks)),
        )
        return picks

    def _least_exposed(self, exclude: set[int]) -> int | None:
        """The approved track that has been shown to the fewest people."""
        rows = self.db.query(
            "SELECT id FROM tracks WHERE status='approved' "
            "ORDER BY exposures ASC, published_at DESC LIMIT 40"
        )
        for row in rows:
            if int(row["id"]) not in exclude:
                return int(row["id"])
        return None

    def discover(self, user_id: int, count: int | None = None) -> list[int]:
        """One explicit discovery batch. Finite by construction."""
        count = count or self.config.limits.discover_batch
        heard = self.heard(user_id)
        day = today()
        row = self.db.one("SELECT ids FROM daily WHERE user_id=? AND day=?", (user_id, day))
        if row is not None:
            heard |= {int(x) for x in json.loads(row["ids"])}
        candidates = self.eligible(heard)
        if not candidates:
            return []
        scored = self.rank(user_id, candidates)
        return self.diversify(scored, count)

    def new_releases(self, user_id: int, days: int = 7, count: int = 8) -> list[int]:
        """Recent approved tracks, diversified and lightly personalized.

        The window is intentional: Telegram users need a stable shelf they can
        return to, while a niche catalogue still needs a place where genuinely
        new work is visible before it accumulates interactions.
        """
        cutoff = int(time.time()) - max(1, days) * 86400
        rows = self.db.query(
            "SELECT id FROM tracks WHERE status='approved' AND published_at>=? "
            "ORDER BY published_at DESC, id DESC LIMIT 400",
            (cutoff,),
        )
        candidates = [int(row["id"]) for row in rows]
        if not candidates:
            candidates = [
                int(row["id"])
                for row in self.db.query(
                    "SELECT id FROM tracks WHERE status='approved' "
                    "ORDER BY published_at DESC, id DESC LIMIT 400"
                )
            ]
        if not candidates:
            return []
        return self.diversify(self.rank(user_id, candidates), count)

    def popular(self, user_id: int, days: int = 30, count: int = 8) -> list[int]:
        """Recent listener response, damped by exposure and artist concentration."""
        cutoff = int(time.time()) - max(1, days) * 86400
        rows = self.db.query(
            "SELECT t.id, COALESCE(SUM(e.weight), 0) AS signal, t.exposures "
            "FROM tracks t LEFT JOIN events e ON e.track_id=t.id AND e.ts>=? "
            "AND e.kind IN ('play','skip','like','save') "
            "WHERE t.status='approved' GROUP BY t.id "
            "ORDER BY signal DESC, t.published_at DESC LIMIT 400",
            (cutoff,),
        )
        ranked = {
            int(row["id"]): math.log1p(max(0.0, float(row["signal"])))
            / math.sqrt(float(row["exposures"] or 0) + 1.0)
            for row in rows
        }
        candidates = list(ranked)
        if not candidates:
            return []
        return self.diversify(sorted(ranked.items(), key=lambda item: item[1], reverse=True), count)

    def following_new(self, user_id: int, days: int = 30, count: int = 8) -> list[int]:
        cutoff = int(time.time()) - max(1, days) * 86400
        rows = self.db.query(
            "SELECT t.id FROM tracks t JOIN follows f ON f.artist_id=t.artist_id "
            "WHERE f.user_id=? AND t.status='approved' AND t.published_at>=? "
            "ORDER BY t.published_at DESC LIMIT 400",
            (user_id, cutoff),
        )
        return [int(row["id"]) for row in rows[:count]]

    def similar_to_saved(self, user_id: int, count: int = 8) -> list[int]:
        saved = [int(row["id"]) for row in self.db.query(
            "SELECT track_id AS id FROM likes WHERE user_id=? ORDER BY ts DESC LIMIT 20",
            (user_id,),
        )]
        if not saved:
            return self.new_releases(user_id, count=count)
        out: list[int] = []
        seen = set(saved)
        for track_id in saved:
            for candidate in self.similar_to(track_id, count=count):
                if candidate not in seen:
                    out.append(candidate)
                    seen.add(candidate)
                    if len(out) >= count:
                        return out
        return out

    def similar_to(self, track_id: int, count: int = 8) -> list[int]:
        """Neighbours of a track: collaborative first, content to fill in."""
        out: list[int] = []
        seen = {track_id}
        for other, _ in self.neighbours(track_id, count * 2):
            if other not in seen and self._is_approved(other):
                out.append(other)
                seen.add(other)
        if len(out) < count:
            scores = self.content_scores(self.vector(track_id), exclude=seen)
            for other, _ in sorted(scores.items(), key=lambda item: item[1], reverse=True):
                if len(out) >= count:
                    break
                out.append(other)
                seen.add(other)
        return out[:count]

    def mix(self, user_id: int, seed: int | None = None, length: int | None = None) -> list[int]:
        """A short, ordered sequence that walks rather than jumps.

        Greedy nearest-neighbour over combined similarity with a tempo-jump
        penalty, so a mix has a shape instead of being a shuffled bag.
        """
        length = length or self.config.limits.mix_length
        heard = self.heard(user_id)
        candidates = self.eligible()
        if not candidates:
            return []
        if seed is None:
            scored = self.rank(user_id, [c for c in candidates if c not in heard] or candidates)
            if not scored:
                return []
            seed = scored[0][0]

        remaining = {c for c in candidates if c != seed}
        order = [seed]
        tempos = {
            int(row["id"]): _tempo_of(row["features"])
            for row in self.db.query("SELECT id, features FROM tracks WHERE status='approved'")
        }
        artists = {
            int(row["id"]): int(row["artist_id"])
            for row in self.db.query("SELECT id, artist_id FROM tracks WHERE status='approved'")
        }
        used_artists = {artists.get(seed)}

        while remaining and len(order) < length:
            current = order[-1]
            current_vector = self.vector(current)
            cf = dict(self.neighbours(current, 60))
            cf_max = max(cf.values(), default=0.0) or 1.0
            content = self.content_scores(current_vector, exclude=set(order))
            best: int | None = None
            best_value = -1e9
            for track_id in remaining:
                value = 0.6 * content.get(track_id, 0.0) + 0.4 * (cf.get(track_id, 0.0) / cf_max)
                left, right = tempos.get(current), tempos.get(track_id)
                if left and right:
                    value -= min(0.3, abs(left - right) / 200.0)
                artist = artists.get(track_id)
                if artist is not None and artist == artists.get(current):
                    value -= 10.0
                elif artist in used_artists:
                    value -= 0.3
                if track_id in heard:
                    value -= 0.15
                if value > best_value:
                    best_value = value
                    best = track_id
            if best is None:
                break
            order.append(best)
            remaining.discard(best)
            used_artists.add(artists.get(best))
        return order

    def _is_approved(self, track_id: int) -> bool:
        return bool(
            self.db.scalar("SELECT 1 FROM tracks WHERE id=? AND status='approved'", (track_id,))
        )


# --------------------------------------------------------------------------
# Interaction recording
# --------------------------------------------------------------------------


def record(db: Database, user_id: int, track_id: int | None, kind: str) -> None:
    """Append one interaction. This is the only writer of ``events``."""
    # Telegram cannot report playback completion. Unknown/legacy events are
    # ignored rather than turning an unobservable signal into a fake metric.
    if kind not in INTERACTION_WEIGHTS:
        return
    weight = INTERACTION_WEIGHTS[kind]
    db.execute(
        "INSERT INTO events(user_id, track_id, kind, ts, weight) VALUES(?,?,?,?,?)",
        (user_id, track_id, kind, now(), weight),
    )
    if track_id is None:
        return
    column = {"play": "plays"}.get(kind)
    if column:
        if kind == "play":
            db.execute(
                "UPDATE tracks SET plays=plays+1, requests=requests+1, "
                "last_requested_at=? WHERE id=?",
                (now(), track_id),
            )
        else:
            db.execute(f"UPDATE tracks SET {column} = {column} + 1 WHERE id=?", (track_id,))


def record_exposure(db: Database, track_ids: Iterable[int]) -> None:
    """Count a track as *shown*, which is the denominator of the fairness bonus."""
    ids = [(int(track_id),) for track_id in track_ids]
    if ids:
        db.executemany("UPDATE tracks SET exposures = exposures + 1 WHERE id=?", ids)


def prune_events(db: Database, keep_days: int = 180) -> int:
    """Bound interaction history while retaining enough taste signal."""
    cutoff = now() - max(30, int(keep_days)) * 86400
    cursor = db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
    return int(cursor.rowcount)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(token, 0.0) for token, value in left.items())


def _placeholders(count: int) -> str:
    return ",".join("?" * max(1, count))


def _load_json(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _tempo_of(raw: str | None) -> float | None:
    features = _load_json(raw)
    tempo = features.get("tempo")
    return float(tempo) if tempo else None


def day_before(day: str, days: int = 1) -> str:
    parsed = date.fromisoformat(day)
    return date.fromordinal(parsed.toordinal() - days).isoformat()


__all__ = [
    "Engine",
    "INTERACTION_WEIGHTS",
    "record",
    "record_exposure",
    "today",
    "day_before",
    "metadata",
]
