"""Streaming portable archives: relative paths, source preservation, SHA-256 validation."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tarfile
import tempfile
import uuid
from pathlib import Path

from .formats import contained


class HashReader:
    def __init__(self, file):
        self.file, self.hash = file, hashlib.sha256()

    def read(self, size=-1):
        data = self.file.read(size)
        self.hash.update(data)
        return data


def create_package(catalog, ids, output, progress=lambda *args: None):
    output = Path(output).expanduser().resolve()
    if output.suffix != ".tar":
        raise ValueError("迁移包使用 .tar 后缀（不重复压缩MP4）")
    if output.exists() or output.with_suffix(".tar.partial").exists():
        raise ValueError("输出已存在，请使用新文件名")
    if not ids:
        raise ValueError("请选择迁移的集")
    ids = list(dict.fromkeys(ids))
    roots = []
    for identity in ids:
        entry = catalog.get(identity)
        root = entry["path"]
        if root not in roots:
            roots.append(root)
        if output.is_relative_to(Path(root).resolve()):
            raise ValueError("迁移包不能写入来源目录")
        if entry["format"] != "lerobot_v3":
            manifest = json.loads((Path(root) / "manifest.json").read_text())
            if manifest.get("outcome") == "recording":
                raise ValueError("请先结束录制再打包")
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(".tar.partial")
    with tempfile.TemporaryDirectory(prefix="yam-portable-") as temp:
        db_path = Path(temp) / "portable.sqlite3"
        db = sqlite3.connect(db_path)
        try:
            db.executescript("""
                CREATE TABLE info(version INTEGER);
                INSERT INTO info VALUES(1);
                CREATE TABLE episodes(id TEXT PRIMARY KEY,root TEXT,record TEXT);
                CREATE TABLE collections(id TEXT PRIMARY KEY,name TEXT);
                CREATE TABLE members(collection TEXT,episode TEXT);
                CREATE TABLE files(path TEXT PRIMARY KEY,bytes INTEGER,sha256 TEXT);
            """)
            names = {root: "payload/" + uuid.uuid4().hex for root in roots}
            for identity in ids:
                entry = catalog.get(identity)
                record = {k: v for k, v in entry.items() if k != "path"}
                db.execute(
                    "INSERT INTO episodes VALUES(?,?,?)",
                    (entry["id"], names[entry["path"]], json.dumps(record)),
                )
            with catalog.db() as source:
                for identity in ids:
                    for member in source.execute(
                        "SELECT c.id,c.name FROM collections c JOIN members m ON m.collection=c.id WHERE m.episode=?",
                        (identity,),
                    ):
                        db.execute(
                            "INSERT OR IGNORE INTO collections VALUES(?,?)",
                            (member["id"], member["name"]),
                        )
                        db.execute("INSERT INTO members VALUES(?,?)", (member["id"], identity))
            files_count, total = 0, 0
            with (
                partial.open("xb") as package_file,
                tarfile.open(fileobj=package_file, mode="w|") as archive,
            ):
                for root_text in roots:
                    root = Path(root_text).resolve()
                    import os

                    for folder, dirs, files in os.walk(root, followlinks=False):
                        dirs[:] = sorted(
                            d for d in dirs if not d.startswith(".") and d != "exports"
                        )
                        for directory in dirs:
                            if (Path(folder) / directory).is_symlink():
                                raise ValueError("迁移来源不允许符号链接目录")
                        for name in sorted(files):
                            if name.startswith("."):
                                continue
                            file = Path(folder) / name
                            if file.is_symlink() or not file.is_file():
                                raise ValueError("迁移来源只接受普通文件")
                            relative = names[root_text] + "/" + str(file.relative_to(root))
                            before = file.stat()
                            info = tarfile.TarInfo(relative)
                            info.size, info.mode = before.st_size, 0o644
                            with file.open("rb") as handle:
                                reader = HashReader(handle)
                                archive.addfile(info, reader)
                                archive.members.clear()  # Python 3.12 otherwise retains every TarInfo.
                            after = file.stat()
                            if (before.st_size, before.st_mtime_ns) != (
                                after.st_size,
                                after.st_mtime_ns,
                            ):
                                raise ValueError("来源在打包期间变化，请停止修改后重试")
                            db.execute(
                                "INSERT INTO files VALUES(?,?,?)",
                                (relative, info.size, reader.hash.hexdigest()),
                            )
                            files_count += 1
                            total += info.size
                            if files_count % 100 == 0:
                                db.commit()
                                progress(
                                    files_count, f"已打包 {files_count} 文件 / {total / 1e9:.2f} GB"
                                )
                db.commit()
                archive.add(db_path, arcname="portable.sqlite3", recursive=False)
            import os

            with partial.open("rb") as handle:
                os.fsync(handle.fileno())
            os.link(partial, output)
            partial.unlink()
            return {
                "path": str(output),
                "episodes": len(ids),
                "source_directories": len(roots),
                "files": files_count,
                "bytes": total,
                "layout": "LeRobot包含完整来源目录；审核成员为本次所选集；迁移包不压缩",
            }
        finally:
            db.close()


def restore_package(catalog, archive_path, destination, progress=lambda *args: None):
    destination = Path(destination).expanduser().resolve()
    archive_path = Path(archive_path).expanduser().resolve()
    staging = destination.with_name(destination.name + ".partial")
    if destination.exists() or staging.exists():
        raise ValueError("恢复目录或.partial已存在，请选择新目录")
    if archive_path.is_relative_to(destination):
        raise ValueError("归档不能位于恢复目录内")
    staging.mkdir(parents=True)
    count = 0
    with sqlite3.connect(staging / "received.sqlite3") as received:
        received.execute("CREATE TABLE files(path TEXT PRIMARY KEY,bytes INTEGER,sha256 TEXT)")
        with tarfile.open(archive_path, "r|") as archive:
            for member in archive:
                if not member.isfile() or (
                    member.name != "portable.sqlite3" and not member.name.startswith("payload/")
                ):
                    raise ValueError("归档包含不支持的成员类型或路径")
                file = contained(staging, member.name)
                file.parent.mkdir(parents=True, exist_ok=True)
                with file.open("xb") as target:
                    reader = HashReader(archive.extractfile(member))
                    shutil.copyfileobj(reader, target, length=1024 * 1024)
                if file.stat().st_size != member.size:
                    raise ValueError("归档文件长度不完整")
                if member.name != "portable.sqlite3":
                    received.execute(
                        "INSERT INTO files VALUES(?,?,?)",
                        (member.name, member.size, reader.hash.hexdigest()),
                    )
                archive.members.clear()  # Streamed members need no in-memory archive index.
                count += 1
                if count % 100 == 0:
                    received.commit()
                    progress(count, f"已恢复并校验 {count} 个文件")
        manifest = contained(staging, "portable.sqlite3")
        with sqlite3.connect(f"file:{manifest}?mode=ro", uri=True) as package:
            package.execute("PRAGMA trusted_schema=OFF")
            if package.execute("SELECT version FROM info").fetchone() != (1,):
                raise ValueError("不支持的迁移包版本")
            expected = 0
            for path, size, sha in package.execute("SELECT path,bytes,sha256 FROM files"):
                contained(staging, path)
                if received.execute(
                    "SELECT bytes,sha256 FROM files WHERE path=?", (path,)
                ).fetchone() != (size, sha):
                    raise ValueError("迁移包内容校验失败：" + path)
                expected += 1
            if received.execute("SELECT count(*) FROM files").fetchone()[0] != expected:
                raise ValueError("迁移包包含未登记数据文件")
            # Validate index paths and all identities before publishing extracted data.
            for identity, root, record in package.execute("SELECT id,root,record FROM episodes"):
                if not contained(staging, root).is_dir():
                    raise ValueError("迁移索引来源不存在")
                e = json.loads(record)
                existing = None
                try:
                    existing = catalog.get(identity)
                except ValueError:
                    pass
                if existing and existing["fingerprint"] != e["fingerprint"]:
                    raise ValueError("本地同ID数据版本不同，请使用独立目录库恢复")
    (staging / "received.sqlite3").unlink()
    staging.rename(destination)
    with sqlite3.connect(f"file:{destination / 'portable.sqlite3'}?mode=ro", uri=True) as package:
        package.execute("PRAGMA trusted_schema=OFF")
        with catalog.db() as target:
            for identity, root, record in package.execute("SELECT id,root,record FROM episodes"):
                entry = json.loads(record)
                new_path = contained(destination, root)
                actual = catalog.upsert(
                    target,
                    new_path,
                    entry["metadata"],
                    entry["fingerprint"],
                    identity=identity,
                    label=entry["label"],
                    note=entry["note"],
                    deleted=entry["deleted"],
                )
                if actual != identity:
                    raise ValueError("恢复身份与目标路径冲突")
                target.execute(
                    "UPDATE episodes SET label=?,note=?,deleted=?,report=?,check_ok=? WHERE id=?",
                    (
                        entry["label"],
                        entry["note"],
                        entry["deleted"],
                        json.dumps(entry["report"]) if entry["report"] else None,
                        entry.get("check_ok"),
                        identity,
                    ),
                )
            for identity, name in package.execute("SELECT id,name FROM collections"):
                old = target.execute(
                    "SELECT name FROM collections WHERE id=?", (identity,)
                ).fetchone()
                if old and old["name"] != name:
                    raise ValueError("集合ID冲突")
                target.execute("INSERT OR IGNORE INTO collections VALUES(?,?)", (identity, name))
            for collection, episode in package.execute("SELECT collection,episode FROM members"):
                target.execute("INSERT OR IGNORE INTO members VALUES(?,?)", (collection, episode))
            catalog.audit(
                target, "restore", {"archive": str(archive_path), "destination": str(destination)}
            )
    return {"path": str(destination), "verified_files": count - 1, "state": "complete"}
