"""Durable, non-destructive curation over indexed YAM recordings."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class Catalog:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS episodes (
                    id TEXT PRIMARY KEY, path TEXT UNIQUE, metadata TEXT,
                    label TEXT DEFAULT 'unreviewed', note TEXT DEFAULT '',
                    deleted INTEGER DEFAULT 0, report TEXT, fingerprint TEXT);
                CREATE TABLE IF NOT EXISTS collections (id TEXT PRIMARY KEY, name TEXT);
                CREATE TABLE IF NOT EXISTS members (collection TEXT, episode TEXT,
                    PRIMARY KEY(collection, episode));
                CREATE TABLE IF NOT EXISTS audit (time REAL, action TEXT, details TEXT);
                CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, state TEXT, payload TEXT);
            """)
            db.execute("UPDATE jobs SET state='interrupted' WHERE state IN ('queued','running')")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.root / "catalog.sqlite3", timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def audit(self, db, action, details):
        db.execute("INSERT INTO audit VALUES (?,?,?)", (time.time(), action, json.dumps(details)))

    def get(self, identity):
        with self.db() as db:
            row = db.execute("SELECT * FROM episodes WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise ValueError("未找到这一集")
        value = dict(row)
        value["metadata"] = json.loads(value["metadata"])
        value["report"] = json.loads(value["report"]) if value["report"] else None
        return value

    def scan(self, root, progress=lambda *args: None):
        root = Path(root).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("请填写运行工作台的机器上的数据目录")
        imported, skipped, errors = 0, [], []
        for folder, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "exports")
            path = Path(folder)
            if (path / "meta/info.json").exists():
                skipped.append(
                    {"path": str(path), "reason": "已转换 LeRobot 数据：首版仅索引 YAM 原始集"}
                )
                dirs.clear()
                continue
            if "manifest.json" not in files:
                continue
            dirs.clear()
            try:
                manifest = json.loads((path / "manifest.json").read_text())
                if manifest.get("schema") not in ("yam_hil_v1", "yam_hil_v2"):
                    raise ValueError("不支持的 manifest schema")
                if manifest.get("outcome") == "recording":
                    raise ValueError("录制尚未结束，请保存后重新扫描")
                from ..hil.storage import episode_segments

                # Validate containment before exposing indexed media.
                list(episode_segments(path))
                identity = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:24]
                fingerprint = hashlib.sha256((path / "manifest.json").read_bytes()).hexdigest()
                with self.db() as db:
                    db.execute(
                        """INSERT INTO episodes(id,path,metadata,fingerprint) VALUES (?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata,
                        report=CASE WHEN fingerprint=excluded.fingerprint THEN report ELSE NULL END,
                        fingerprint=excluded.fingerprint""",
                        (identity, str(path.resolve()), json.dumps(manifest), fingerprint),
                    )
                imported += 1
                progress(imported, str(path))
            except Exception as exc:
                errors.append({"path": str(path), "error": str(exc)})
        with self.db() as db:
            self.audit(db, "import", {"root": str(root), "imported": imported})
        return {"imported": imported, "errors": errors, "skipped": skipped}

    def listing(self, *, view="all", query="", collection="", offset=0, limit=100):
        clauses, params = [], []
        if view == "trash":
            clauses.append("deleted=1")
        else:
            clauses.append("deleted=0")
            if view in ("failure", "success", "unreviewed"):
                clauses.append("label=?")
                params.append(view)
            elif view == "issues":
                clauses.append("report IS NOT NULL AND json_extract(report,'$.ok')=0")
        if query:
            clauses.append("(path LIKE ? OR metadata LIKE ? OR note LIKE ?)")
            params.extend(["%" + query + "%"] * 3)
        if collection:
            clauses.append("id IN (SELECT episode FROM members WHERE collection=?)")
            params.append(collection)
        where = " AND ".join(clauses)
        with self.db() as db:
            total = db.execute("SELECT count(*) FROM episodes WHERE " + where, params).fetchone()[0]
            ids = db.execute(
                "SELECT id FROM episodes WHERE " + where + " ORDER BY path LIMIT ? OFFSET ?",
                params + [min(200, max(1, limit)), max(0, offset)],
            ).fetchall()
            counts = dict(
                db.execute(
                    "SELECT label,count(*) FROM episodes WHERE deleted=0 GROUP BY label"
                ).fetchall()
            )
            collections = [
                dict(r)
                for r in db.execute(
                    "SELECT c.*,count(m.episode) AS count FROM collections c LEFT JOIN members m ON c.id=m.collection GROUP BY c.id ORDER BY c.name"
                )
            ]
        return {
            "episodes": [self.get(r["id"]) for r in ids],
            "total": total,
            "counts": counts,
            "collections": collections,
        }

    def curate(self, ids, action, value=""):
        if not ids or len(ids) > 10000:
            raise ValueError("请选择 1–10000 集")
        for identity in ids:
            self.get(identity)
        with self.db() as db:
            if action == "label" and value in ("success", "failure", "unreviewed"):
                db.executemany("UPDATE episodes SET label=? WHERE id=?", [(value, i) for i in ids])
            elif action == "note":
                db.executemany(
                    "UPDATE episodes SET note=? WHERE id=?", [(value[:4000], i) for i in ids]
                )
            elif action in ("remove", "restore"):
                db.executemany(
                    "UPDATE episodes SET deleted=? WHERE id=?",
                    [(int(action == "remove"), i) for i in ids],
                )
            elif action in ("add", "detach"):
                if db.execute("SELECT id FROM collections WHERE id=?", (value,)).fetchone() is None:
                    raise ValueError("集合不存在")
                sql = (
                    "INSERT OR IGNORE INTO members VALUES (?,?)"
                    if action == "add"
                    else "DELETE FROM members WHERE collection=? AND episode=?"
                )
                db.executemany(sql, [(value, i) for i in ids])
            else:
                raise ValueError("无效操作")
            self.audit(db, action, {"ids": ids, "value": value})

    def collection(self, name, ids):
        name = name.strip()
        if not name or len(name) > 100:
            raise ValueError("集合名称需为 1–100 个字符")
        for identity in ids:
            self.get(identity)
        identity = uuid.uuid4().hex
        with self.db() as db:
            db.execute("INSERT INTO collections VALUES (?,?)", (identity, name))
            db.executemany(
                "INSERT OR IGNORE INTO members VALUES (?,?)", [(identity, i) for i in ids]
            )
            self.audit(db, "collection", {"id": identity, "name": name, "ids": ids})
        return identity
