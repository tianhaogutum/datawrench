"""Publish complete conversion output with a same-directory atomic replacement."""
from contextlib import contextmanager
import os
from pathlib import Path
import stat
import tempfile

from .io import InputError


@contextmanager
def atomic_output(path, *, protected):
    """Yield a private binary file; publish only after successful conversion/close.

    protected contains stat results for the input and optional catalog. Symlinks
    and special destinations are refused, including dangling symlinks. The
    destination directory must already exist and remain under caller control.
    """
    path = Path(path).absolute()
    identities = {(s.st_dev, s.st_ino) for s in protected}

    def check_destination():
        try:
            target = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(target.st_mode):
            raise InputError("output destination must be a regular file, not a symlink or special file")
        if (target.st_dev, target.st_ino) in identities:
            raise InputError("output destination aliases input or catalog; use a different file")

    check_destination()
    temporary = None
    try:
        # Same directory means os.replace cannot cross filesystems. Creation is
        # exclusive and mode 0600; never truncate/open the destination for writing.
        with tempfile.NamedTemporaryFile(mode="wb",
                                         dir=path.parent, prefix=".tapcheck-", suffix=".tmp",
                                         delete=False) as handle:
            temporary = Path(handle.name)
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        check_destination()
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
