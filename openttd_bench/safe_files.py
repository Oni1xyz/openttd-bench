"""Bounded, descriptor-relative artifact I/O (POSIX, stdlib only).

Only an explicitly operator-selected root may be a symlink: it is canonicalized
once, then held open. Every descendant is opened relative to directory descriptors
with O_NOFOLLOW. This is not a sandbox against a hostile concurrent root owner
who can rename directories, mutate regular files, or create hard links.
"""
from __future__ import annotations

import json
import os
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class BoundaryError(ValueError):
    """An unsafe artifact or exhausted resource budget; never includes file contents."""


class InvalidJSON(BoundaryError):
    """Malformed JSON, distinct from a size/depth rejection."""


@dataclass(frozen=True)
class Limits:
    file_bytes: int = 8 * 1024 * 1024
    total_bytes: int = 32 * 1024 * 1024
    records: int = 20_000
    nodes: int = 200_000
    depth: int = 32
    entries: int = 8192
    files: int = 2048
    graph_nodes: int = 4096
    graph_work: int = 1_000_000
    output_bytes: int = 64 * 1024 * 1024


class Budget:
    def __init__(self, limits: Limits | None = None):
        self.limits = limits or Limits()
        self.bytes = self.records = self.nodes = self.entries = self.files = 0

    def charge(self, field: str, amount: int = 1) -> None:
        value = getattr(self, field) + amount
        limit = self.limits.total_bytes if field == "bytes" else getattr(self.limits, field)
        if value > limit:
            raise BoundaryError(f"Artifact {field} limit exceeded")
        setattr(self, field, value)


def check_depth(raw: str, maximum: int) -> None:
    """Reject nesting before invoking the recursive JSON decoder (respect strings)."""
    depth = 0
    quoted = escaped = False
    for char in raw:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > maximum:
                raise BoundaryError("Artifact depth limit exceeded")
        elif char in "]}":
            depth -= 1


def check_value(value, budget: Budget) -> None:
    """Iterative preflight before graph traversal, text rendering, or serialization."""
    stack = [(iter((value,)), 0)]
    string_bytes = 0
    while stack:
        iterator, depth = stack[-1]
        try:
            item = next(iterator)
        except StopIteration:
            stack.pop()
            continue
        budget.charge("nodes")
        if depth > budget.limits.depth:
            raise BoundaryError("Artifact depth limit exceeded")
        if isinstance(item, (dict, list)):
            if len(item) > budget.limits.nodes - budget.nodes:
                raise BoundaryError("Artifact nodes limit exceeded")
            if isinstance(item, dict):
                # Count keys as well; no list of children is allocated.
                children = (part for pair in item.items() for part in pair)
            else:
                children = iter(item)
            stack.append((children, depth + 1))
        elif isinstance(item, str):
            string_bytes += len(item)
            if string_bytes > budget.limits.total_bytes:
                raise BoundaryError("Artifact string limit exceeded")
        elif item is not None and not isinstance(item, (bool, int, float)):
            raise BoundaryError("Artifact is not plain JSON")


def decode_json(raw: str, budget: Budget):
    check_depth(raw, budget.limits.depth)
    try:
        result = json.loads(raw)
    except (ValueError, RecursionError):
        raise InvalidJSON("Invalid artifact JSON") from None
    check_value(result, budget)
    return result


def decode_records(data: bytes, budget: Budget) -> list[tuple[int, dict]]:
    records = []
    # The entire file is already byte-bounded; splitting cannot grow without bound.
    try:
        raw = data.decode("utf-8")
    except UnicodeError:
        raise BoundaryError("Invalid artifact encoding") from None
    for line_number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        budget.charge("records")
        record = decode_json(line, budget)
        if not isinstance(record, dict):
            raise BoundaryError("Artifact record must be an object")
        records.append((line_number, record))
    return records


def relative_parts(path: str | Path) -> tuple[str, ...]:
    raw = str(path)
    if raw == ".":
        return ()
    parts = raw.split("/")
    if not raw or any(p in ("", ".", "..") for p in parts) or "\\" in raw or "\x00" in raw:
        raise BoundaryError("Unsafe artifact path")
    return tuple(parts)


def _identity(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class SafeRoot:
    def __init__(self, path: Path):
        self.fd = -1
        try:
            self.path = Path(path).resolve(strict=True)
            self.fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except (OSError, RuntimeError):
            raise BoundaryError("Invalid operator-selected artifact root") from None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        os.close(self.fd)
        self.fd = -1

    @contextmanager
    def directory(self, relative: str | Path = "."):
        fd = os.dup(self.fd)
        try:
            for part in relative_parts(relative):
                before = os.stat(part, dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISDIR(before.st_mode):
                    raise BoundaryError("Unsafe artifact directory")
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                after = os.fstat(child)
                if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                    os.close(child)
                    raise BoundaryError("Artifact directory changed")
                os.close(fd)
                fd = child
            yield fd
        except FileNotFoundError:
            raise
        except OSError:
            raise BoundaryError("Unsafe artifact directory") from None
        finally:
            os.close(fd)

    def is_dir(self, relative: str | Path) -> bool:
        try:
            with self.directory(relative):
                return True
        except FileNotFoundError:
            return False

    def walk(self, budget: Budget, relative: str | Path = ".", exclude: tuple[str, ...] = ()) -> list[tuple[Path, bool]]:
        found = []

        def visit(parent: Path, depth: int):
            if depth > budget.limits.depth:
                raise BoundaryError("Artifact directory depth limit exceeded")
            with self.directory(parent) as fd:
                with os.scandir(fd) as iterator:
                    entries = []
                    for entry in iterator:
                        budget.charge("entries")
                        info = entry.stat(follow_symlinks=False)
                        directory = stat.S_ISDIR(info.st_mode)
                        if not directory and not stat.S_ISREG(info.st_mode):
                            raise BoundaryError("Nonregular artifact entry")
                        if parent == Path(".") and entry.name in exclude:
                            continue
                        if not directory:
                            budget.charge("files")
                        entries.append((entry.name, directory))
                for name, directory in sorted(entries):
                    child = parent / name
                    found.append((child, directory))
                    if directory:
                        visit(child, depth + 1)

        try:
            visit(Path(relative), 0)
        except OSError:
            raise BoundaryError("Cannot enumerate artifacts") from None
        return found

    def read(self, relative: str | Path, budget: Budget) -> bytes:
        parts = relative_parts(relative)
        if not parts:
            raise BoundaryError("Artifact file required")
        try:
            with self.directory(Path(*parts[:-1])) as parent:
                before = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(before.st_mode):
                    raise BoundaryError("Nonregular artifact file")
                if before.st_size > budget.limits.file_bytes:
                    raise BoundaryError("Artifact file bytes limit exceeded")
                budget.charge("bytes", before.st_size)
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                try:
                    opened = os.fstat(fd)
                    if not stat.S_ISREG(opened.st_mode) or _identity(before) != _identity(opened):
                        raise BoundaryError("Artifact file changed")
                    chunks = []
                    remaining = before.st_size + 1
                    while remaining:
                        chunk = os.read(fd, min(remaining, 65536))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    data = b"".join(chunks)
                    if len(data) != before.st_size or _identity(opened) != _identity(os.fstat(fd)):
                        raise BoundaryError("Artifact file changed")
                    return data
                finally:
                    os.close(fd)
        except OSError:
            raise BoundaryError("Cannot read artifact file") from None

    def validate_target(self, relative: str | Path) -> None:
        parts = relative_parts(relative)
        if not parts:
            raise BoundaryError("Artifact file required")
        try:
            with self.directory(Path(*parts[:-1])) as parent:
                info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise BoundaryError("Unsafe artifact publication target")
        except FileNotFoundError:
            return
        except OSError:
            raise BoundaryError("Cannot inspect artifact publication target") from None

    def mkdir(self, relative: str | Path) -> None:
        parts = relative_parts(relative)
        with self.directory(Path(*parts[:-1])) as parent:
            try:
                os.mkdir(parts[-1], mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
        with self.directory(relative):
            pass

    def atomic_write(self, relative: str | Path, data: bytes) -> None:
        self.validate_target(relative)
        parts = relative_parts(relative)
        with self.directory(Path(*parts[:-1])) as parent:
            temporary = ".atif-" + uuid.uuid4().hex + ".tmp"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                self.validate_target(relative)
                os.replace(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
