"""Bounded request/event sidecar writer and immutable raw-run publication.

Only put_nowait is called by the control owner. Finalization, hashing, episode
copying and uploads run after recording has closed, outside the control loop.
"""

from __future__ import annotations

import copy
import json
import os
import queue
import shutil
import threading
from pathlib import Path

import h5py
import numpy as np

from ..storage import atomic_json, digest, json_value, read_rows
from .config import ARMS, SCHEMA


class PartsJournal:
    def __init__(self, path, manifest, *, capacity=128):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=False)
        self.manifest = dict(manifest, schema=SCHEMA)
        atomic_json(self.path / "run.json", self.manifest)
        self.queue = queue.Queue(maxsize=capacity)
        self.error = None
        self.gaps = []
        self.stopping = threading.Event()
        self.closed = False
        atomic_json(self.path / "recording_status.json", dict(closed=False, error=None, gaps=[]))
        self.thread = threading.Thread(target=self._run, name="parts-recording", daemon=True)
        self.thread.start()

    def submit(self, kind, value):
        if self.closed or self.error:
            return False
        try:
            self.queue.put_nowait((kind, value))
            return True
        except queue.Full:
            self.error = "PARTS journal queue full"
            self.gaps.append(
                dict(reason=self.error, context=value.get("context"), tick=value.get("tick"))
            )
            return False

    def _request(self, file, value):
        item = copy.deepcopy(value)
        context = item["context"]
        group_path = f"/requests/e{context['epoch']}_r{context['request_id']}"
        group = file.create_group(group_path)
        arrays = {}

        def save(name, array):
            if array is None:
                return
            array = np.asarray(array)
            if array.dtype.kind not in "bfiu" or not np.isfinite(array).all():
                item.setdefault("missing_arrays", {})[name] = "invalid_or_nonfinite"
                return
            group.create_dataset(name, data=array)
            arrays[name] = dict(
                path=group_path + "/" + name, shape=list(array.shape), dtype=str(array.dtype)
            )

        save("actions_native", item.pop("actions_native", None))
        prefix = item.pop("committed_prefix", None)
        save("committed_prefix", prefix)
        scheduler = item["parts_request"].get("scheduler", {})
        for name in ("targets", "valid_mask", "committed_mask"):
            save(f"scheduler/{name}", scheduler.pop(name, None))
        item["prefix_base_available"] = [False] * (0 if prefix is None else len(prefix))
        reply = item.pop("parts_reply", None)
        if reply:
            for arm in ARMS:
                candidate = reply.get("candidates", {}).get(arm, {})
                for name in ("u", "B_rad", "editable_mask"):
                    save(f"{arm}/{name}", candidate.get(name))
            features = dict(reply.get("features") or {})
            save("features/z", features.pop("z", None))
            item.update(
                features=features,
                behavior_snapshot_id=reply.get("behavior_snapshot_id"),
                candidate_metadata={
                    arm: {
                        k: v
                        for k, v in reply.get("candidates", {}).get(arm, {}).items()
                        if k not in ("u", "B_rad", "editable_mask")
                    }
                    for arm in ARMS
                },
            )
        else:
            item.update(features=None, missing_candidates=True, missing_features=True)
        item.update(hdf5_file="requests.h5", hdf5_group=group_path, arrays=arrays)
        file.flush()
        return item

    def _run(self):
        streams = {}
        try:
            streams = {
                k: (self.path / (k + "s.jsonl")).open("w", encoding="utf-8")
                for k in ("request", "event", "attempt")
            }
            with h5py.File(self.path / "requests.h5", "w") as file:
                file.attrs["schema"] = SCHEMA
                while not self.stopping.is_set() or not self.queue.empty():
                    try:
                        kind, value = self.queue.get(timeout=0.05)
                    except queue.Empty:
                        continue
                    if kind == "request":
                        value = self._request(file, value)
                    streams[kind].write(
                        json.dumps(value, default=json_value, ensure_ascii=False, allow_nan=False)
                        + "\n"
                    )
                    streams[kind].flush()
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.gaps.append(dict(reason=self.error))
        finally:
            for stream in streams.values():
                stream.flush()
                os.fsync(stream.fileno())
                stream.close()
            if (self.path / "requests.h5").exists():
                with (self.path / "requests.h5").open("rb") as file:
                    os.fsync(file.fileno())
            atomic_json(
                self.path / "recording_status.json",
                dict(closed=True, error=self.error, gaps=self.gaps),
            )

    def close(self):
        self.closed = True
        self.stopping.set()
        self.thread.join(timeout=30)
        if self.thread.is_alive():
            self.error = "PARTS sidecar finalization timeout"
        return not self.error


def jsonl(path):
    with Path(path).open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def finalize(path, *, episodes=(), producer_sha, gaps=()):
    """Called by an offline/background owner only, after all writers are closed."""
    path = Path(path)
    if (path / "publication.json").exists():
        raise ValueError("PARTS run is immutable after publication")
    run = json.loads((path / "run.json").read_text())
    health = json.loads((path / "recording_status.json").read_text())
    if not health.get("closed"):
        raise ValueError("PARTS sidecar is still open; close recording before finalization")
    requests = jsonl(path / "requests.jsonl")
    attempts = jsonl(path / "attempts.jsonl")
    episode_ids = []
    for episode in episodes:
        episode = Path(episode)
        destination = path / "episodes" / episode.name
        if episode.resolve() != destination.resolve():
            shutil.copytree(episode, destination)
        manifest = json.loads((destination / "manifest.json").read_text())
        episode_ids.append(manifest["episode_id"])
        members = []
        observations = {}
        attempt_frames = {}
        for row in read_rows(destination):
            reference = dict(
                episode_id=manifest["episode_id"],
                episode_path=destination.relative_to(path).as_posix(),
                segment=Path(row["_segment"]).name,
                frame_indices=row["video_indices"],
                tick=row["tick"],
                time=row["time"],
                sync=row.get("sync"),
            )
            if row.get("observation_valid") and row.get("obs_id") is not None:
                observations.setdefault((row.get("epoch"), row["obs_id"]), reference)
            attempt_id = (row.get("parts") or {}).get("attempt_id")
            if attempt_id:
                members.append(attempt_id)
                attempt_frames.setdefault(attempt_id, []).append(reference)
        for request in requests:
            key = (request["context"]["epoch"], request["context"]["observation_id"])
            if key in observations and request.get("video_refs") is None:
                request["video_refs"] = observations[key]
        for attempt in attempts:
            refs = attempt_frames.get(attempt["attempt_id"])
            if refs:
                attempt.setdefault("episode_intervals", []).append(
                    dict(first=refs[0], last=refs[-1])
                )
        manifest["parts"] = dict(
            schema=SCHEMA,
            run_id=run["run_id"],
            contract_sha=run.get("contract_sha"),
            mode=run["mode"],
            run_relative_path="../..",
            attempts=sorted(set(members)),
        )
        atomic_json(destination / "manifest.json", manifest)
    for name, rows in (("requests.jsonl", requests), ("attempts.jsonl", attempts)):
        with (path / name).open("w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(
                    json.dumps(row, default=json_value, allow_nan=False, ensure_ascii=False) + "\n"
                )
            stream.flush()
            os.fsync(stream.fileno())
    problems = [*gaps, *health.get("gaps", [])]
    if health.get("error"):
        problems.append(dict(reason=health["error"]))
    if not episodes:
        problems.append(dict(reason="no_finalized_episodes"))
    for request in requests:
        if request.get("video_refs") is None:
            problems.append(
                dict(reason="request_video_reference_missing", context=request["context"])
            )
        if request.get("missing_arrays"):
            problems.append(
                dict(
                    reason="invalid_request_arrays",
                    context=request["context"],
                    arrays=request["missing_arrays"],
                )
            )
    files = {
        f.relative_to(path).as_posix(): dict(bytes=f.stat().st_size, sha256=digest(f))
        for f in sorted(path.rglob("*"))
        if f.is_file()
    }
    publication = dict(
        schema=SCHEMA,
        publication_id=run["run_id"],
        run_id=run["run_id"],
        client_complete=not problems,
        files=files,
        episodes=episode_ids,
        attempts=[a["attempt_id"] for a in attempts],
        gaps=problems,
        producer_sha=producer_sha,
        mock=run["mock"],
        split_role=run["split_role"],
        transfer_state="not_transferred",
        training_ready=False,
    )
    atomic_json(path / "publication.json", publication)
    errors = validate_package(path)
    if errors:
        publication["client_complete"] = False
        publication["gaps"].extend(dict(reason=e) for e in errors)
        atomic_json(path / "publication.json", publication)
    return publication


def validate_package(path):
    """File closure + physical array/provenance checks; never grants training READY."""
    path = Path(path)
    problems = []
    try:
        publication = json.loads((path / "publication.json").read_text())
        run = json.loads((path / "run.json").read_text())
        rule_config = None
        from .config import RULES_SCHEMA

        if run.get("selector_schema") == RULES_SCHEMA:
            from .config import PartsConfig

            rule_config = PartsConfig.from_dict(run["config"])
            if run.get("selector_config_sha") != rule_config.selector_config_sha:
                problems.append("selector_configuration_hash_mismatch")
        elif run.get("selector_schema") is not None:
            problems.append("unsupported_selector_version_use_original_producer")
        if publication["schema"] != SCHEMA or run["schema"] != SCHEMA:
            problems.append("schema_mismatch")
        if publication.get("training_ready") is not False:
            problems.append("client_cannot_grant_training_ready")
        actual = {
            f.relative_to(path).as_posix()
            for f in path.rglob("*")
            if f.is_file() and f.name != "publication.json"
        }
        if actual != set(publication["files"]):
            problems.append("file_index_incomplete")
        for name, expected in publication["files"].items():
            file = (path / name).resolve()
            if not file.is_relative_to(path.resolve()) or not file.is_file():
                problems.append(f"missing_or_unsafe_file:{name}")
            elif file.stat().st_size != expected["bytes"] or digest(file) != expected["sha256"]:
                problems.append(f"file_hash_mismatch:{name}")
        indexes = jsonl(path / "requests.jsonl")
        with h5py.File(path / "requests.h5", "r") as file:
            for item in indexes:
                d = (
                    len(file[item["arrays"]["committed_prefix"]["path"]])
                    if "committed_prefix" in item["arrays"]
                    else 0
                )
                for name, spec in item["arrays"].items():
                    value = file[spec["path"]][...]
                    shape = (
                        (50, 14)
                        if name in ("actions_native", "scheduler/targets")
                        else (d, 14)
                        if name == "committed_prefix"
                        else (
                            (50, 6)
                            if name.endswith("/u")
                            else (6,)
                            if name.endswith("/B_rad")
                            else (50,)
                            if name.endswith(("/editable_mask", "/valid_mask", "/committed_mask"))
                            else tuple(spec["shape"])
                        )
                    )
                    if (
                        value.shape != shape
                        or list(value.shape) != spec["shape"]
                        or str(value.dtype) != spec["dtype"]
                    ):
                        problems.append(f"array_shape_dtype:{spec['path']}")
                    if not np.isfinite(value).all():
                        problems.append(f"nonfinite_array:{spec['path']}")
                    if name.endswith("/u") and np.any(abs(value) > 1):
                        problems.append(f"candidate_range:{spec['path']}")
                    if name.endswith("/editable_mask") and (
                        value.dtype.kind != "b" or value[:d].any()
                    ):
                        problems.append(f"editable_prefix:{spec['path']}")
                sched = [
                    item["arrays"].get("scheduler/" + name)
                    for name in ("targets", "valid_mask", "committed_mask")
                ]
                if all(sched):
                    targets, valid, committed = [file[spec["path"]][...] for spec in sched]
                    if (
                        valid.dtype.kind != "b"
                        or committed.dtype.kind != "b"
                        or np.any(committed & ~valid)
                        or np.any(targets[~valid] != 0)
                    ):
                        problems.append("scheduler_mask_invalid")
                elif any(sched):
                    problems.append("scheduler_snapshot_incomplete")
                if d and len(item.get("committed_sources", [])) != d:
                    problems.append("rtc_prefix_sources_missing")
                if "actions_native" in item["arrays"] and d:
                    native = file[item["arrays"]["actions_native"]["path"]][...]
                    prefix = file[item["arrays"]["committed_prefix"]["path"]][...]
                    if not np.allclose(native[:d], prefix, atol=2e-6, rtol=0):
                        problems.append("rtc_prefix_changed")
        attempts = jsonl(path / "attempts.jsonl")
        if len({a["attempt_id"] for a in attempts}) != len(attempts):
            problems.append("duplicate_terminal_attempt")
        if set(publication["attempts"]) != {a["attempt_id"] for a in attempts}:
            problems.append("attempt_members_mismatch")
        for attempt in attempts:
            expected = (
                None if attempt["result"] == "canceled" else int(attempt["result"] == "success")
            )
            if attempt["grasp_reward"] != expected:
                problems.append("attempt_reward_mismatch")
        # This is offline work: decode to prove closure, not merely a file index.
        import av

        actual_episodes = set()
        for manifest_path in sorted((path / "episodes").glob("*/manifest.json")):
            manifest = json.loads(manifest_path.read_text())
            actual_episodes.add(manifest["episode_id"])
            rows = list(read_rows(manifest_path.parent))
            if len(rows) != manifest["steps"]:
                problems.append("episode_row_count_mismatch")
            if rule_config is not None:
                from .replay import verify_selector

                replay = verify_selector(rows, rule_config)
                if not replay["valid"]:
                    problems.append("selector_replay_mismatch:" + manifest["episode_id"])
            for segment in manifest["segments"]:
                folder = (manifest_path.parent / segment["path"]).resolve()
                if not folder.is_relative_to(path.resolve()) or segment.get("state") != "committed":
                    problems.append("episode_segment_unclosed_or_unsafe")
                    continue
                for arm in ("top", "left", "right"):
                    with av.open(str(folder / (arm + ".mp4"))) as video:
                        count = sum(1 for _ in video.decode(video=0))
                    if count != segment["steps"] or count != segment["video_frames"][arm]:
                        problems.append("episode_video_count_mismatch")
        if actual_episodes != set(publication["episodes"]):
            problems.append("episode_members_mismatch")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        problems.append(f"unreadable_package:{exc}")
    return problems
