import os
import stat
from pathlib import Path


def read_secret_file(
    path_value: str,
    *,
    minimum_bytes: int = 1,
    maximum_bytes: int = 4096,
) -> bytes:
    """Read one direct-child regular file without following its final symlink."""

    configured = Path(str(path_value or "").strip())
    if not configured.is_absolute() or not configured.name:
        raise OSError("secret path is invalid")
    parent_metadata = configured.parent.lstat()
    if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(parent_metadata.st_mode):
        raise OSError("secret directory is invalid")
    parent = configured.parent.resolve(strict=True)
    candidate = parent / configured.name
    candidate_metadata = candidate.lstat()
    if (
        stat.S_ISLNK(candidate_metadata.st_mode)
        or not stat.S_ISREG(candidate_metadata.st_mode)
        or candidate_metadata.st_size > maximum_bytes
        or candidate.resolve(strict=True).parent != parent
        or (os.name != "nt" and stat.S_IMODE(candidate_metadata.st_mode) & 0o077)
        or (os.name != "nt" and candidate_metadata.st_uid != os.geteuid())
    ):
        raise OSError("secret file is invalid")

    descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_metadata.st_mode)
            or opened_metadata.st_size > maximum_bytes
            or (os.name != "nt" and stat.S_IMODE(opened_metadata.st_mode) & 0o077)
            or (os.name != "nt" and opened_metadata.st_uid != os.geteuid())
        ):
            raise OSError("secret file is invalid")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            value = handle.read(maximum_bytes + 1)
    finally:
        os.close(descriptor)
    if len(value) < minimum_bytes or len(value) > maximum_bytes:
        raise OSError("secret file is invalid")
    return value
