"""Stable read-only camera endpoints shared with the runtime across connections."""


class CameraSlot:
    def __init__(self, role):
        self.role = self.name = role
        self.worker = None

    def read(self):
        worker = self.worker
        return None if worker is None else worker.read()

    def history(self):
        worker = self.worker
        return [] if worker is None else worker.history()
