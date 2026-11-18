"""Transactional checkpoint for a single MAPF-Fuzz solver/repeat job.

Only the main process opens this database. Serialized values come from this
program's own output directory and must not be loaded from untrusted files.
"""

from __future__ import annotations

import json
import os
import pickle
import shutil
import sqlite3
import zlib
import fcntl
from pathlib import Path
from typing import Any


def pack(value: Any) -> bytes:
    return zlib.compress(pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL), 3)


def unpack(blob: bytes) -> Any:
    return pickle.loads(zlib.decompress(blob))


class RunJournal:
    def __init__(self, directory: Path, manifest: dict, resume: bool,
                 initial_rng_state: object | None = None) -> None:
        self.directory = directory
        self.path = directory / "checkpoint.sqlite3"
        if resume and not self.path.is_file():
            raise FileNotFoundError(f"No checkpoint to resume: {self.path}")
        if not resume and self.path.exists():
            raise FileExistsError(f"Checkpoint already exists; use --resume: {self.path}")
        # forked solver workers inherit this descriptor. If only the parent is
        # killed, their open descriptors keep the lock until they exit.
        self.lock = (directory / "checkpoint.lock").open("a+")
        try:
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lock.close()
            raise RuntimeError(f"Another run or its workers still use {directory}") from exc
        try:
            self.conn = sqlite3.connect(self.path)
            self._initialize(manifest, resume, initial_rng_state)
        except BaseException:
            if hasattr(self, "conn"):
                self.conn.close()
            self.lock.close()
            raise

    def _initialize(self, manifest: dict, resume: bool,
                    initial_rng_state: object | None) -> None:
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS pending (
                case_id INTEGER PRIMARY KEY, parent_case INTEGER, task BLOB NOT NULL,
                map_source TEXT NOT NULL, mutation TEXT NOT NULL, case_seed INTEGER NOT NULL,
                mutation_seconds REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS results (
                case_id INTEGER PRIMARY KEY, completion_seq INTEGER UNIQUE NOT NULL,
                row_json TEXT NOT NULL, keep_files INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS seeds (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER UNIQUE NOT NULL,
                task BLOB NOT NULL, feedback BLOB NOT NULL, energy REAL NOT NULL,
                features BLOB NOT NULL, map_source TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS features (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, vector BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS neighbor_history (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, distance REAL NOT NULL
            );
        """)
        saved_manifest = self.meta("manifest", None)
        if resume:
            if saved_manifest != manifest:
                changed = (sorted(key for key in set(saved_manifest or {}) | set(manifest)
                                  if (saved_manifest or {}).get(key) != manifest.get(key)))
                raise ValueError("Resume configuration differs from checkpoint manifest: "
                                 + ", ".join(changed))
        else:
            if initial_rng_state is None:
                raise ValueError("New checkpoint needs the initial RNG state")
            with self.conn:
                self._put("manifest", manifest)
                self._put("rng_state", initial_rng_state)
                self._put("next_case", 0)
                self._put("init_completed", 0)
                self._put("phase_started", False)
                self._put("annoy_indexed_count", 0)
                self._put("fuzz_elapsed", 0.0)
                self._put("counts", {"completeness_failure": 0, "cost_gap": 0,
                                     "all_unsolved": 0, "invalid_or_error": 0,
                                     "other": 0})
                self._put("cf_count", 0)
                self._put("of_count", 0)
                self._put("mr_violations", {})
                self._put("final_status", None)

    def _put(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                          (key, pack(value)))

    def meta(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return unpack(row[0]) if row else default

    def load_seed_pool(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT case_id, task, feedback, energy, features, map_source "
            "FROM seeds ORDER BY seq")
        return [{"case": case_id, "task": unpack(task), "feedback": unpack(feedback),
                 "energy": energy, "features": unpack(features), "map_source": source}
                for case_id, task, feedback, energy, features, source in rows]

    def load_features(self) -> list[tuple[float, ...]]:
        return [unpack(blob) for (blob,) in self.conn.execute(
            "SELECT vector FROM features ORDER BY seq")]

    def load_history(self) -> list[float]:
        return sorted(value for (value,) in self.conn.execute(
            "SELECT distance FROM neighbor_history ORDER BY seq"))

    def load_pending(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT case_id, parent_case, task, map_source, mutation, case_seed, mutation_seconds "
            "FROM pending ORDER BY case_id")
        return [{"case": case, "parent_case": parent, "task": unpack(task),
                 "map_source": source, "mutation": mutation, "case_seed": seed,
                 "mutation_seconds": mutation_seconds}
                for case, parent, task, source, mutation, seed, mutation_seconds in rows]

    def completed_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM results").fetchone()[0]

    def dispatch(self, case: int, parent_case: int | None, task: Any,
                 map_source: str, mutation: str, case_seed: int,
                 rng_state: object, selected: dict | None,
                 fuzz_elapsed: float, mutation_seconds: float) -> None:
        with self.conn:
            if selected is not None:
                self.conn.execute("UPDATE seeds SET energy=? WHERE case_id=?",
                                  (selected["energy"], selected["case"]))
            self.conn.execute(
                "INSERT INTO pending VALUES (?, ?, ?, ?, ?, ?, ?)",
                (case, parent_case, pack(task), map_source, mutation, case_seed,
                 mutation_seconds))
            self._put("next_case", case + 1)
            self._put("rng_state", rng_state)
            self._put("fuzz_elapsed", fuzz_elapsed)

    def start_fuzzing(self, annoy_indexed_count: int) -> None:
        with self.conn:
            self._put("phase_started", True)
            self._put("fuzz_elapsed", 0.0)
            self._put("annoy_indexed_count", annoy_indexed_count)

    def update_elapsed(self, seconds: float) -> None:
        with self.conn:
            self._put("fuzz_elapsed", seconds)

    def complete(self, case: int, row: dict, keep_files: bool,
                 task: Any, feedback: dict, feature: tuple[float, ...] | None,
                 feature_eligible: bool, pool_eligible: bool,
                 second_distance: float | None, counts: dict, cf_count: int,
                 of_count: int, mr_violations: dict, init_completed: int,
                 fuzz_elapsed: float, annoy_indexed_count: int = 0) -> None:
        with self.conn:
            completion_seq = self.completed_count() + 1
            self.conn.execute("INSERT INTO results VALUES (?, ?, ?, ?)",
                              (case, completion_seq,
                               json.dumps(row, ensure_ascii=False), int(keep_files)))
            if feature_eligible and feature is not None:
                self.conn.execute("INSERT INTO features(vector) VALUES (?)", (pack(feature),))
            if second_distance is not None:
                self.conn.execute("INSERT INTO neighbor_history(distance) VALUES (?)",
                                  (second_distance,))
            if pool_eligible:
                self.conn.execute(
                    "INSERT INTO seeds(case_id, task, feedback, energy, features, map_source) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (case, pack(task), pack(feedback), row["energy"],
                     pack(feature), row["map_source"]))
            self.conn.execute("DELETE FROM pending WHERE case_id=?", (case,))
            self._put("counts", counts)
            self._put("cf_count", cf_count)
            self._put("of_count", of_count)
            self._put("mr_violations", mr_violations)
            self._put("init_completed", init_completed)
            self._put("fuzz_elapsed", fuzz_elapsed)
            self._put("annoy_indexed_count", annoy_indexed_count)

    def finish(self, reason: str, fuzz_elapsed: float, summary: dict) -> None:
        with self.conn:
            self._put("final_status", reason)
            self._put("fuzz_elapsed", fuzz_elapsed)
            self._put("final_summary", summary)

    def sync_outputs(self) -> None:
        """Repair JSONL and post-commit cleanup after a previous interruption."""
        target = self.directory / "results.jsonl"
        temporary = self.directory / "results.jsonl.tmp"
        with temporary.open("w") as stream:
            for (row_json,) in self.conn.execute(
                    "SELECT row_json FROM results ORDER BY completion_seq"):
                stream.write(row_json + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        for (case,) in self.conn.execute("SELECT case_id FROM results WHERE keep_files=0"):
            case_dir = self.directory / f"case_{case:05d}"
            if case_dir.exists():
                shutil.rmtree(case_dir)

    def close(self) -> None:
        self.conn.close()
        self.lock.close()
