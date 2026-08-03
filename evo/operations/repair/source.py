from __future__ import annotations

import hashlib
import shutil
from fnmatch import fnmatchcase
from pathlib import Path


ALGORITHM_APP = Path('algorithm/lazymind/chat/app.py')
SOURCE_FILES = ('.dockerignore', 'Dockerfile', 'config.py', 'requirements.txt')
SOURCE_IGNORE = (
    '.git', '.evo_repair_logs', '__pycache__', '*.pyc', '.pytest_cache',
    '.venv', '.venv-*', 'venv', 'Tutorial', 'docs',
)


def source_root(value: object) -> Path:
    path = Path(str(value or '').strip()).resolve()
    return next((candidate for candidate in (path, *path.parents) if (candidate / ALGORITHM_APP).exists()), path)


def source_hash(source: Path) -> str:
    """Hash exactly the source projection copied into Repair workspaces."""
    root = source.resolve()
    files = [root / name for name in SOURCE_FILES if (root / name).is_file()]
    algorithm = root / 'algorithm'
    if algorithm.is_dir():
        files.extend(path for path in algorithm.rglob('*') if path.is_file())
    digest = hashlib.sha256()
    for path in sorted(files):
        relative = path.relative_to(root)
        if _ignored(relative):
            continue
        digest.update(relative.as_posix().encode())
        digest.update(b'\0')
        digest.update(path.read_bytes())
    return digest.hexdigest()


def copy_source(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    algorithm = source / 'algorithm'
    if algorithm.is_dir():
        shutil.copytree(
            algorithm,
            destination / 'algorithm',
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(*SOURCE_IGNORE),
        )
    for name in SOURCE_FILES:
        path = source / name
        if path.is_file():
            shutil.copy2(path, destination / name)


def _ignored(path: Path) -> bool:
    return any(fnmatchcase(part, pattern) for part in path.parts for pattern in SOURCE_IGNORE)
