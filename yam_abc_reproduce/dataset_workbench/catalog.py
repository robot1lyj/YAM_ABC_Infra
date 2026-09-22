"""Durable, non-destructive curation over indexed YAM recordings."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class Catalog:
    def __init__(self, root, *, recover_jobs=True):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
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
            self.migrate(db)
            db.execute("CREATE INDEX IF NOT EXISTS episode_page ON episodes(deleted,id)")
            if recover_jobs:
                unfinished = db.execute(
                    "SELECT * FROM jobs WHERE state IN ('queued','running')"
                ).fetchall()
                for row in unfinished:
                    payload = json.loads(row["payload"])
                    payload.update(
                        error="上次数据集服务退出时任务尚未完成",
                        message="服务中断；未自动重跑，请核对已生成文件后重新提交",
                    )
                    db.execute(
                        "UPDATE jobs SET state='interrupted',payload=? WHERE id=?",
                        (json.dumps(payload), row["id"]),
                    )

    def migrate(self, db):
        if "format" in {r["name"] for r in db.execute("PRAGMA table_info(episodes)")}:
            return
        db.executescript("""
            BEGIN IMMEDIATE;
            ALTER TABLE episodes RENAME TO episodes_old;
            CREATE TABLE episodes (id TEXT PRIMARY KEY,path TEXT NOT NULL,metadata TEXT,
                label TEXT DEFAULT 'unreviewed',note TEXT DEFAULT '',deleted INTEGER DEFAULT 0,
                report TEXT,fingerprint TEXT,format TEXT,episode_key TEXT DEFAULT '',title TEXT,task TEXT,
                steps INTEGER,fps REAL,check_ok INTEGER,UNIQUE(path,episode_key));
            CREATE INDEX episode_view ON episodes(deleted,label,id);
            CREATE INDEX episode_format ON episodes(deleted,format,id);
            CREATE INDEX episode_check ON episodes(deleted,check_ok,id);
            CREATE TABLE counters(label TEXT,deleted INTEGER,count INTEGER,PRIMARY KEY(label,deleted));
            CREATE TRIGGER episode_insert AFTER INSERT ON episodes BEGIN
                INSERT INTO counters VALUES(NEW.label,NEW.deleted,1) ON CONFLICT(label,deleted) DO UPDATE SET count=count+1;
            END;
            CREATE TRIGGER episode_delete AFTER DELETE ON episodes BEGIN
                UPDATE counters SET count=count-1 WHERE label=OLD.label AND deleted=OLD.deleted;
            END;
            CREATE TRIGGER episode_update AFTER UPDATE OF label,deleted ON episodes BEGIN
                UPDATE counters SET count=count-1 WHERE label=OLD.label AND deleted=OLD.deleted;
                INSERT INTO counters VALUES(NEW.label,NEW.deleted,1) ON CONFLICT(label,deleted) DO UPDATE SET count=count+1;
            END;
        """)
        for old in db.execute("SELECT * FROM episodes_old"):
            m = json.loads(old["metadata"])
            self.upsert(
                db,
                old["path"],
                m,
                old["fingerprint"],
                identity=old["id"],
                label=old["label"],
                note=old["note"],
                deleted=old["deleted"],
            )
            report = json.loads(old["report"]) if old["report"] else None
            db.execute(
                "UPDATE episodes SET report=?,check_ok=? WHERE id=?",
                (old["report"], None if report is None else int(report["ok"]), old["id"]),
            )
        db.execute("DROP TABLE episodes_old")

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
        value["metadata"]["_format"] = value["format"]
        value["metadata"]["_episode_key"] = value["episode_key"]
        return value

    def scan(self, root, progress=lambda *args: None):
        from .formats import lerobot_metadata, raw_metadata

        root = Path(root).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("请填写运行工作台的机器上的数据目录")
        imported, errors, skipped = 0, [], []
        batch = []

        def flush():
            nonlocal batch
            with self.db() as db:
                for path, metadata, token in batch:
                    self.upsert(db, path, metadata, token)
            batch = []

        for folder, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "exports")
            path = Path(folder).resolve()
            if self.root.is_relative_to(path) and path == self.root:
                dirs.clear()
                continue
            try:
                if (path / "meta/info.json").exists():
                    dirs.clear()
                    records = lerobot_metadata(path)
                elif "manifest.json" in files:
                    dirs.clear()
                    records = [raw_metadata(path)]
                else:
                    continue
                for metadata, token in records:
                    batch.append((path, metadata, token))
                    imported += 1
                    if len(batch) >= 256:
                        flush()
                        progress(imported, str(path))
            except Exception as exc:
                if len(errors) < 1000:
                    errors.append({"path": str(path), "error": str(exc)})
        flush()
        progress(imported, f"已索引 {imported} 集")
        with self.db() as db:
            self.audit(db, "import", {"root": str(root), "imported": imported})
        return {"imported": imported, "errors": errors, "skipped": skipped}

    def upsert(
        self, db, path, metadata, token, *, identity=None, label="unreviewed", note="", deleted=0
    ):
        key = str(metadata.get("_episode_key", ""))
        fmt = metadata.get("_format", metadata.get("schema", "yam_hil_v2"))
        title = (
            metadata.get("_title")
            or (metadata.get("collection_task") or {}).get("name")
            or metadata.get("station", {}).get("task_name", "")
        )
        task = metadata.get("_task", metadata.get("station", {}).get("task_name", ""))
        # Existing path identity is preserved; a portable package explicitly carries IDs.
        old = db.execute(
            "SELECT id,fingerprint FROM episodes WHERE path=? AND episode_key=?", (str(path), key)
        ).fetchone()
        if old and old["fingerprint"] == token:
            return old["id"]
        identity = old["id"] if old else identity or uuid.uuid4().hex
        db.execute(
            """INSERT INTO episodes(id,path,metadata,fingerprint,format,episode_key,title,task,steps,fps,label,note,deleted)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            path=excluded.path,metadata=excluded.metadata,format=excluded.format,episode_key=excluded.episode_key,
            title=excluded.title,task=excluded.task,steps=excluded.steps,fps=excluded.fps,
            report=CASE WHEN fingerprint=excluded.fingerprint THEN report ELSE NULL END,
            check_ok=CASE WHEN fingerprint=excluded.fingerprint THEN check_ok ELSE NULL END,
            fingerprint=excluded.fingerprint""",
            (
                identity,
                str(path),
                json.dumps(metadata, ensure_ascii=False),
                token,
                fmt,
                key,
                title,
                task,
                metadata.get("steps", 0),
                metadata.get("fps", 30),
                label,
                note,
                deleted,
            ),
        )
        return identity

    def listing(
        self, *, view="all", query="", collection="", offset=0, limit=100, cursor="", format=""
    ):
        clauses, params = ["deleted=?"], [int(view == "trash")]
        if view in ("failure", "success", "unreviewed"):
            clauses.append("label=?")
            params.append(view)
        elif view == "issues":
            clauses.append("check_ok=0")
        if query:
            clauses.append("(title LIKE ? OR task LIKE ? OR note LIKE ?)")
            params.extend(["%" + query + "%"] * 3)
        if format:
            clauses.append("format=?")
            params.append(format)
        if collection:
            clauses.append("id IN (SELECT episode FROM members WHERE collection=?)")
            params.append(collection)
        if cursor:
            clauses.append("id>?")
            params.append(cursor)
        where = " AND ".join(clauses)
        size = min(200, max(1, limit))
        with self.db() as db:
            rows = db.execute(
                "SELECT id,path,title,task,steps,fps,format,episode_key,label,note,deleted,check_ok FROM episodes WHERE "
                + where
                + " ORDER BY id LIMIT ? OFFSET ?",
                params + [size + 1, max(0, offset) if not cursor else 0],
            ).fetchall()
            counts = dict(db.execute("SELECT label,count FROM counters WHERE deleted=0"))
            total = sum(counts.values())
            if view in ("failure", "success", "unreviewed"):
                total = counts.get(view, 0)
            elif view == "trash":
                total = db.execute(
                    "SELECT coalesce(sum(count),0) FROM counters WHERE deleted=1"
                ).fetchone()[0]
            if collection:
                total = db.execute(
                    "SELECT count(*) FROM members WHERE collection=?", (collection,)
                ).fetchone()[0]
            collections = [
                dict(r)
                for r in db.execute("SELECT id,name FROM collections ORDER BY name LIMIT 1000")
            ]
        values = []
        for row in rows[:size]:
            e = dict(row)
            e["metadata"] = {
                "_format": e["format"],
                "_title": e["title"],
                "_task": e["task"],
                "_episode_key": e["episode_key"],
                "steps": e["steps"],
                "fps": e["fps"],
                "station": {"task_name": e["task"]},
                "collection_task": {"name": e["title"]},
            }
            e["report"] = None if e["check_ok"] is None else {"ok": bool(e["check_ok"])}
            values.append(e)
        # Totals for filtered queries are intentionally not scanned on each page.
        return {
            "episodes": values,
            "total": total,
            "counts": counts,
            "collections": collections,
            "has_more": len(rows) > size,
            "next_cursor": rows[size - 1]["id"] if len(rows) > size else None,
        }

    def selected_formats(self, ids):
        formats = set()
        ids = list(dict.fromkeys(ids))
        with self.db() as db:
            for start in range(0, len(ids), 500):
                batch = ids[start : start + 500]
                rows = db.execute(
                    "SELECT id,format FROM episodes WHERE id IN ("
                    + ",".join("?" for _ in batch)
                    + ")",
                    batch,
                ).fetchall()
                if len(rows) != len(batch):
                    raise ValueError("部分选择的集不存在，请刷新")
                formats.update(r["format"] for r in rows)
        return formats

    def curate(self, ids, action, value=""):
        if not ids or len(ids) > 10000:
            raise ValueError("请选择 1–10000 集")
        self.selected_formats(ids)
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
        self.selected_formats(ids)
        identity = uuid.uuid4().hex
        with self.db() as db:
            db.execute("INSERT INTO collections VALUES (?,?)", (identity, name))
            db.executemany(
                "INSERT OR IGNORE INTO members VALUES (?,?)", [(identity, i) for i in ids]
            )
            self.audit(db, "collection", {"id": identity, "name": name, "ids": ids})
        return identity
