from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from string import hexdigits
from tempfile import NamedTemporaryFile
from threading import Lock
from time import time

from PIL import Image


ICON_TTL = 7 * 24 * 60 * 60
MAX_ICONS = 256
MAX_ICON_BYTES = 20 * 1024


class IconCache:
    def __init__(self, folder: Path):
        self.folder = folder
        self.load = lru_cache(maxsize=512)(self._load)
        self._lock = Lock()

    def _load(self, url, download):
        path = self.folder / (sha256(url.encode()).hexdigest() + ".png")
        icon = self._read(path)
        if icon is None:
            icon = download(url)
            self._save(path, icon)
        return icon

    def _read(self, path: Path):
        try:
            with path.open("rb") as file:
                stat = path.stat()
                if stat.st_size > MAX_ICON_BYTES or time() - stat.st_mtime >= ICON_TTL:
                    return None
                with Image.open(file, formats=("PNG",)) as image:
                    if 0 < image.width <= 64 and 0 < image.height <= 64:
                        return image.convert("RGBA")
        except (OSError, SyntaxError, ValueError, Image.DecompressionBombError):
            pass
        return None

    def _save(self, path: Path, icon: Image.Image):
        temporary = None
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=self.folder, suffix=".tmp", delete=False) as file:
                temporary = Path(file.name)
                icon.save(file, format="PNG", compress_level=2)
            with self._lock:
                temporary.replace(path)
                temporary = None
                self._prune()
        except (OSError, ValueError):
            # An unavailable disk cache must not prevent a downloaded icon from being used.
            pass
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _prune(self):
        oldest = time() - ICON_TTL
        entries = []
        for path in self.folder.glob("*.png"):
            if len(path.stem) != 64 or any(char not in hexdigits for char in path.stem):
                continue
            try:
                saved = path.stat().st_mtime
                if saved <= oldest:
                    path.unlink(missing_ok=True)
                else:
                    entries.append((saved, path))
            except OSError:
                continue
        for _, path in sorted(entries)[:max(0, len(entries) - MAX_ICONS)]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
