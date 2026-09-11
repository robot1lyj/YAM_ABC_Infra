import json
import time

import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from tests.test_offline_conversion import episode
from yam_abc_reproduce.dataset_workbench.catalog import Catalog
from yam_abc_reproduce.dataset_workbench.operations import check_episode, export_selected, preview
from yam_abc_reproduce.dataset_workbench.web import create_app


def test_catalog_curation_is_durable_and_non_destructive(tmp_path):
    raw = tmp_path / "raw"
    episode(raw / "one")
    catalog = Catalog(tmp_path / "catalog")
    assert catalog.scan(raw)["imported"] == 1
    catalog.scan(raw)
    assert catalog.listing()["total"] == 1
    entry = catalog.listing()["episodes"][0]
    identity = entry["id"]
    original = (raw / "one/manifest.json").read_bytes()
    catalog.curate([identity], "label", "failure")
    collection = catalog.collection("失败原因复核", [identity])
    assert catalog.listing(view="failure")["total"] == 1
    assert catalog.listing(collection=collection)["total"] == 1
    catalog.curate([identity], "remove")
    assert catalog.listing()["total"] == 0
    assert catalog.listing(view="trash")["total"] == 1
    catalog.curate([identity], "restore")
    catalog.curate([identity], "note", "颜色放错了")
    assert Catalog(tmp_path / "catalog").get(identity)["note"] == "颜色放错了"
    assert (raw / "one/manifest.json").read_bytes() == original


def test_quality_preview_and_multi_task_merge(tmp_path):
    raw = tmp_path / "raw"
    episode(raw / "a")
    episode(raw / "b")
    manifest = raw / "b/manifest.json"
    data = json.loads(manifest.read_text())
    data["station"]["task_name"] = "put blocks away"
    manifest.write_text(json.dumps(data))
    assert check_episode(raw / "a", deep=True)["ok"]
    assert preview(raw / "a", "top", 1).startswith(b"\xff\xd8")
    catalog = Catalog(tmp_path / "catalog")
    catalog.scan(raw)
    entries = sorted(catalog.listing()["episodes"], key=lambda e: e["path"])
    catalog.curate([entries[1]["id"]], "label", "failure")
    entries = sorted(catalog.listing()["episodes"], key=lambda e: e["path"])
    result = export_selected(entries, tmp_path / "out")
    assert result["frames"] == 6 and result["tasks"] == 2
    assert result["packet_copy_episodes"] == 2
    assert pq.read_table(tmp_path / "out/data/chunk-000/file-001.parquet")[
        "task_index"
    ].to_pylist() == [1, 1, 1]
    assert pq.read_table(tmp_path / "out/meta/tasks.parquet").num_rows == 2
    assert result["sources"][1]["label"] == "failure"
    with pytest.raises(ValueError, match="已存在"):
        export_selected(entries, tmp_path / "out")
    (raw / "a/segment_000000/top.mp4").unlink()
    assert not check_episode(raw / "a")["ok"]


def test_api_import_jobs_and_origin_protection(tmp_path):
    episode(tmp_path / "raw/a")
    app = create_app(tmp_path / "catalog")
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.post("/api/import", json={"path": str(tmp_path / "raw")}).status_code == 403
        headers = {"x-yam-control": "1"}
        assert (
            client.post(
                "/api/import", headers={**headers, "origin": "https://evil.test"}, json={}
            ).status_code
            == 403
        )
        response = client.post("/api/import", headers=headers, json={"path": str(tmp_path / "raw")})
        assert response.status_code == 200
        for _ in range(100):
            jobs = client.get("/api/jobs").json()
            if jobs[0]["state"] == "complete":
                break
            time.sleep(0.02)
        assert jobs[0]["state"] == "complete"
        entries = client.get("/api/episodes").json()["episodes"]
        identity = entries[0]["id"]
        assert client.get(f"/api/episodes/{identity}/preview/top?frame=1").status_code == 200
        assert (
            client.post(
                "/api/curate",
                headers=headers,
                json={"ids": [identity], "action": "label", "value": "failure"},
            ).status_code
            == 200
        )
        assert client.get("/api/episodes?view=failure").json()["total"] == 1


def test_recording_skipped_and_media_escape_rejected(tmp_path):
    episode(tmp_path / "raw/a")
    manifest = tmp_path / "raw/a/manifest.json"
    data = json.loads(manifest.read_text())
    data["outcome"] = "recording"
    manifest.write_text(json.dumps(data))
    catalog = Catalog(tmp_path / "catalog")
    assert catalog.scan(tmp_path / "raw")["imported"] == 0
    data["outcome"] = "success"
    manifest.write_text(json.dumps(data))
    media = tmp_path / "raw/a/segment_000000/top.mp4"
    external = tmp_path / "external.mp4"
    media.rename(external)
    media.symlink_to(external)
    with pytest.raises(ValueError, match="越出"):
        preview(tmp_path / "raw/a", "top", 0)
