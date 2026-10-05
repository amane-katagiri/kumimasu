from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

import yaml

from .errors import StepError

PRIVATE_FILE = 0o600
PRIVATE_DIR = 0o700


def private_dir(path: Path) -> Path:
    path = Path(path)
    try:
        path.mkdir(mode=PRIVATE_DIR, parents=True)
        return path
    except FileExistsError:
        pass
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise StepError(f"{path} はディレクトリではありません（シンボリックリンクも使いません）")
    if st.st_uid != os.getuid():
        raise StepError(f"{path} は別のユーザーのものなので使いません")
    if st.st_mode & 0o022:
        raise StepError(f"{path} はほかのユーザーも書き込めるので使いません（chmod 700 にしてください）")
    return path


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(mode=PRIVATE_DIR, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def append_jsonl(path: Path, row: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, PRIVATE_FILE)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def create_new(path: Path, data: bytes, mode: int = PRIVATE_FILE) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def dump_yaml(data) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000)
