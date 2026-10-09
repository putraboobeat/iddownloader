"""Persisted retention for dedicated VPS job directories; never scans arbitrary folders."""
import json
import shutil
import threading
import time
import uuid
from pathlib import Path


class Retention:
    def __init__(self, root, seconds=3600, log=lambda message: None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.seconds = seconds
        self.log = log
        self.active = set()
        self.lock = threading.RLock()

    def _write(self, folder, deadline):
        temp = folder / '.expiry.tmp'
        temp.write_text(json.dumps({'delete_at': deadline}))
        temp.replace(folder / '.expiry.json')

    def create(self, name):
        with self.lock:
            # Unique namespace avoids adopting/deleting existing personal folders.
            folder = self.root / ('job-' + uuid.uuid4().hex + '-' + Path(name).name)
            folder.mkdir()
            self._write(folder, time.time() + self.seconds)
            self.active.add(folder)
            return folder

    def finish(self, folder):
        with self.lock:
            deadline = time.time() + self.seconds
            self._write(folder, deadline)
            self.active.discard(folder)
            return deadline

    def sweep(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            for folder in self.root.glob('job-*'):
                if folder in self.active or folder.is_symlink() or not folder.is_dir():
                    continue
                marker = folder / '.expiry.json'
                if marker.is_symlink():
                    continue
                try:
                    deadline = json.loads(marker.read_text())['delete_at']
                    if now >= float(deadline):
                        shutil.rmtree(folder)
                        self.log('Pembersihan otomatis: hasil kedaluwarsa telah dihapus.')
                except FileNotFoundError:
                    continue
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    self.log('Pembersihan perlu diperiksa: ' + str(exc))
