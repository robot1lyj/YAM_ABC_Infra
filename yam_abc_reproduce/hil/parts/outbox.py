"""Persistent publication retry owner. No policy socket or SDK dependency."""

import json
import shutil
import tempfile
import threading
from pathlib import Path

from ..storage import atomic_json, digest
from .recording import validate_package


class DirectoryTransport:
    """First transport: a mounted receiving directory, using atomic rename + ack.

    The remote destination can be a separately managed mount. A network API is
    intentionally injected by its owner rather than guessed by this client.
    """

    def __init__(self, destination):
        self.destination = Path(destination)
        self.destination.mkdir(parents=True, exist_ok=True)

    def send(self, source, publication):
        target = self.destination / publication["publication_id"]
        expected = digest(Path(source) / "publication.json")
        if target.exists():
            if digest(target / "publication.json") != expected or validate_package(target):
                raise ValueError("publication ID collision or corrupt receiver")
        else:
            stage = Path(tempfile.mkdtemp(prefix=".parts-transfer-", dir=self.destination))
            shutil.copytree(source, stage, dirs_exist_ok=True)
            if validate_package(stage):
                raise ValueError("receiver validation failed")
            stage.rename(target)
        return dict(
            publication_id=publication["publication_id"],
            publication_sha256=expected,
            received=True,
            training_ready=False,
        )


class Outbox:
    def __init__(self, path, transport):
        self.path, self.transport = Path(path), transport
        self.path.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def enqueue(self, source):
        source = Path(source).resolve()
        errors = validate_package(source)
        publication = json.loads((source / "publication.json").read_text())
        if errors or not publication["client_complete"]:
            raise ValueError("incomplete PARTS publication: " + ", ".join(errors))
        pid = publication["publication_id"]
        if not pid or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in pid
        ):
            raise ValueError("unsafe PARTS publication ID")
        file = self.path / (pid + ".json")
        expected = digest(source / "publication.json")
        with self.lock:
            if file.exists():
                if json.loads(file.read_text())["publication_sha256"] != expected:
                    raise ValueError("outbox publication ID collision")
                return pid
            atomic_json(
                file,
                dict(
                    publication_id=pid,
                    source=str(source),
                    publication_sha256=expected,
                    state="queued",
                    attempts=0,
                    ack=None,
                    error=None,
                ),
            )
        return pid

    def drain_once(self):
        # One outbox owner may run per directory; advisory process locking avoids
        # racing retries from separate CLI invocations.
        import fcntl

        with (self.path / ".owner.lock").open("a") as owner:
            fcntl.flock(owner, fcntl.LOCK_EX)
            for file in sorted(self.path.glob("*.json")):
                item = json.loads(file.read_text())
                if item["state"] == "acked":
                    continue
                item["attempts"] += 1
                try:
                    source = Path(item["source"])
                    publication = json.loads((source / "publication.json").read_text())
                    if digest(source / "publication.json") != item[
                        "publication_sha256"
                    ] or validate_package(source):
                        raise ValueError("immutable source changed")
                    ack = self.transport.send(source, publication)
                    if (
                        ack.get("publication_id") != item["publication_id"]
                        or not ack.get("received")
                        or ack.get("publication_sha256") != item["publication_sha256"]
                    ):
                        raise ValueError("outbox acknowledgement mismatch")
                    item.update(state="acked", ack=ack, error=None)
                except Exception as exc:
                    item.update(state="retry", error=f"{type(exc).__name__}: {exc}")
                atomic_json(file, item)

    def serve(self, stopping, *, retry_s=5):
        while not stopping.is_set():
            self.drain_once()
            stopping.wait(retry_s)
