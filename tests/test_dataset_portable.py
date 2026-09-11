import io
import tarfile

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tests.test_offline_conversion import episode
from yam_abc_reproduce.dataset_workbench.catalog import Catalog
from yam_abc_reproduce.dataset_workbench.operations import export_selected
from yam_abc_reproduce.dataset_workbench.portable import create_package, restore_package
from yam_abc_reproduce.dataset_workbench.reading import CACHE, check_lerobot, preview_entry
from yam_abc_reproduce.hil.video_copy import remux_segments


def shared_dataset(tmp_path):
    raw = tmp_path / "raw"
    episode(raw / "a")
    episode(raw / "b")
    c = Catalog(tmp_path / "catalog")
    c.scan(raw)
    output = tmp_path / "lerobot"
    export_selected(sorted(c.listing()["episodes"], key=lambda e: e["path"]), output)
    files = [output / f"data/chunk-000/file-{i:03d}.parquet" for i in range(2)]
    tables = [pq.read_table(file) for file in files]
    pq.write_table(pa.concat_tables(tables), files[0], row_group_size=3)
    files[1].unlink()
    meta_file = output / "meta/episodes/chunk-000/file-000.parquet"
    meta = pq.read_table(meta_file).to_pylist()
    for role in ("top", "left", "right"):
        folder = output / f"videos/observation.images.{role}_rgb/chunk-000"
        remux_segments(
            [folder / "file-000.mp4", folder / "file-001.mp4"], folder / "merged.mp4", 30
        )
        (folder / "merged.mp4").replace(folder / "file-000.mp4")
        (folder / "file-001.mp4").unlink()
        prefix = f"videos/observation.images.{role}_rgb"
        for i, e in enumerate(meta):
            e[prefix + "/file_index"] = 0
            e[prefix + "/from_timestamp"] = i * 0.1
            e[prefix + "/to_timestamp"] = (i + 1) * 0.1
    for e in meta:
        e["data/file_index"] = 0
    pq.write_table(pa.Table.from_pylist(meta), meta_file)
    return c, output


def test_shared_lerobot_import_preview_check_without_torch(tmp_path):
    catalog, root = shared_dataset(tmp_path)
    result = catalog.scan(root)
    assert result["imported"] == 2 and not result["errors"]
    entries = catalog.listing(format="lerobot_v3")["episodes"]
    assert len(entries) == 2
    for entry in entries:
        full = catalog.get(entry["id"])
        assert full["metadata"]["_format"] == "lerobot_v3"
        assert check_lerobot(full, deep=True)["ok"]
        assert preview_entry(full, "observation.images.top_rgb", 2).startswith(b"\xff\xd8")
    with pytest.raises(ValueError, match="转换仅接受"):
        export_selected(entries, tmp_path / "invalid")


def test_package_relocation_preserves_identity_labels_collections_and_pixels(tmp_path):
    c, lr = shared_dataset(tmp_path)
    c.scan(lr)
    entries = c.listing()["episodes"]
    identity = next(e["id"] for e in entries if e["format"] == "lerobot_v3")
    c.curate([identity], "label", "failure")
    c.curate([identity], "note", "积木掉落")
    collection = c.collection("迁移检查", [identity])
    before = preview_entry(c.get(identity), "observation.images.top_rgb", 2)
    archive = tmp_path / "transfer.tar"
    result = create_package(c, [e["id"] for e in entries], archive)
    assert result["episodes"] == 4
    other = Catalog(tmp_path / "server/catalog")
    restore_package(other, archive, tmp_path / "server/datasets")
    restored = other.get(identity)
    assert restored["label"] == "failure" and restored["note"] == "积木掉落"
    assert str(tmp_path / "server/datasets") in restored["path"]
    assert other.listing(collection=collection)["episodes"][0]["id"] == identity
    assert preview_entry(restored, "observation.images.top_rgb", 2) == before
    assert check_lerobot(restored, deep=True)["ok"]
    with pytest.raises(ValueError, match="已存在"):
        restore_package(other, archive, tmp_path / "server/datasets")


def test_archive_rejects_traversal_and_corruption(tmp_path):
    c = Catalog(tmp_path / "catalog")
    archive = tmp_path / "bad.tar"
    with tarfile.open(archive, "w") as tar:
        info = tarfile.TarInfo("payload/../../escaped")
        info.size = 3
        tar.addfile(info, io.BytesIO(b"bad"))
    with pytest.raises(ValueError, match="路径"):
        restore_package(c, archive, tmp_path / "restore")
    assert not (tmp_path / "escaped").exists()
    episode(tmp_path / "raw/a")
    c.scan(tmp_path / "raw")
    create_package(c, [c.listing()["episodes"][0]["id"]], tmp_path / "good.tar")
    with (
        tarfile.open(tmp_path / "good.tar", "r") as src,
        tarfile.open(tmp_path / "corrupt.tar", "w") as target,
    ):
        for member in src:
            content = src.extractfile(member).read()
            if member.name.endswith("top.mp4"):
                content = bytes([content[0] ^ 1]) + content[1:]
            target.addfile(member, io.BytesIO(content))
    with pytest.raises(ValueError, match="校验失败"):
        restore_package(
            Catalog(tmp_path / "other"), tmp_path / "corrupt.tar", tmp_path / "corrupt-output"
        )
    assert not (tmp_path / "corrupt-output").exists()


def test_summary_cursor_avoids_loading_metadata_and_repeat_import_preserves_ids(
    tmp_path, monkeypatch
):
    c = Catalog(tmp_path / "catalog")
    with c.db() as db:
        for i in range(250):
            c.upsert(
                db,
                tmp_path / f"raw/{i}",
                dict(schema="yam_hil_v2", steps=108000, fps=30, station={"task_name": "sort"}),
                "same",
                identity=f"{i:08d}",
            )
    monkeypatch.setattr(c, "get", lambda *a: pytest.fail("listing loaded full metadata"))
    first = c.listing()
    second = c.listing(cursor=first["next_cursor"])
    assert len(first["episodes"]) == len(second["episodes"]) == 100
    assert not ({e["id"] for e in first["episodes"]} & {e["id"] for e in second["episodes"]})
    assert "segments" not in first["episodes"][0]["metadata"]
    assert first["total"] == 250


def test_preview_reads_single_hdf_row_and_cache_is_bounded(tmp_path, monkeypatch):
    episode(tmp_path / "a")
    c = Catalog(tmp_path / "catalog")
    c.scan(tmp_path / "a")
    e = c.get(c.listing()["episodes"][0]["id"])
    import yam_abc_reproduce.dataset_workbench.reading as reading

    monkeypatch.setattr(reading, "read_rows", lambda *a: pytest.fail("scanned HDF rows"))
    first = reading.preview_entry(e, "top", 2)
    monkeypatch.setattr(reading, "jpeg_at", lambda *a: pytest.fail("cache miss"))
    assert reading.preview_entry(e, "top", 2) == first
    assert CACHE.size <= CACHE.max_bytes
