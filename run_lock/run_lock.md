# run_lock.py

An exclusive lock on a file, held for a process's lifetime and released by the kernel when the
process dies, **SIGKILL included**, plus a probe that asks whether it is held without taking it.

## Why it exists

A job that must never run twice, and a reader that must tell a RUNNING job from a DEAD one, need
the same primitive. With a kernel-released lock, "the record says in progress and the lock is
free" means the holder died, with no process-ID guesswork. A recorded process ID proves little: IDs
recycle fast on a busy machine.

In this repository, `git_workspace_backup` holds one, with `private=True`, for each backup
run, and probes it before install and uninstall.

## Public surface

| Name | What it does |
| --- | --- |
| `acquire(path, *, mode=None, private=False) -> Lock \| None` | Takes the lock without waiting. Returns `None` when another holder has it. Creates `path` and its parents, the file with `mode` (`0o644` when omitted, less the umask; an existing file keeps its own). Any other failure raises `OSError`. `private=True`: see below. |
| `Lock.release()` | Drops the lock if still held; a second call is a no-op. |
| `with lock:` | Releases on exit, whether or not the block raised. |
| `is_held(path, *, private=False) -> bool` | Probes without waiting and without creating the file. An absent file is free. An existing file that cannot be opened raises `OSError`. `private=True`: see below. |

## `private=True`: a lock for private state

For a caller whose state belongs to the running user alone, such as a backup's run lock. On
both `acquire` and `is_held`:

- **A symlink at the lock path is refused, not followed.** The open adds `O_NOFOLLOW`, so a
  symlink planted where the lock belongs raises `OSError`, whether or not what it names exists,
  and that target is neither created nor opened. The errno is the platform's: Linux and macOS
  report `ELOOP`, and FreeBSD, for one, reports `EMLINK`, so check for `OSError`, not an errno.
  Only the path's last component is guarded; a symlinked parent directory that already exists
  is still followed.
- **An existing lock file must be this user's own.** After the open, the descriptor is
  `fstat`ed, and a file that is not a regular file, is owned by another user, or has more than
  one link raises `OSError` (`EPERM`, the message naming which) with the descriptor closed.
  Without it, another user could plant the file, or a hard link to one they can open, and hold
  the lock on it forever, so `acquire` would return `None` for good. `is_held` refuses the same
  files, so a probe never reports such a stranger's lock as a run.
- **What `acquire` creates is private.** The lock file is created `0o600`, and each missing
  parent directory `0o700`, one at a time (both less the umask). A parent that already exists
  keeps its mode: it is never chmodded, so a shared directory is not narrowed under its other
  users. An existing lock file keeps its own mode too.
- **A parent planted in the gap is refused.** A parent found missing is made without accepting
  an existing one blindly: if something appears there between the check and the `mkdir`, it is
  `lstat`ed, and anything other than a real directory owned by this user with no group or other
  permission bits raises `OSError` (`EPERM`). A symlink, another user's directory, or a
  directory open to others planted there is refused; the same user's own concurrent run making
  that parent is accepted. A parent that existed before the call is not inspected at all: it
  is the caller's choice of location, which `private` does not second-guess.
- **`mode=` and `private=True` do not combine.** `private=True` sets the file mode itself, so
  passing any `mode` beside it, `0o644` included, raises `ValueError`, before anything is
  created. The default is `None` precisely so that an explicit `0o644` is told apart from an
  omitted one, rather than silently tightened to `0o600`.
- **POSIX only.** Windows has no `O_NOFOLLOW`, so there `private=True` raises `OSError`
  (`ENOSYS`) from `acquire` and `is_held` before anything is created or opened, rather than
  quietly taking a lock with less protection than was asked for.

Without `private`, behavior is unchanged: a symlink at the lock path is followed, and the file
and its parents get the default modes.

## Behavior worth knowing

- **Per open file, not per process.** `flock(2)` on POSIX and `msvcrt.locking` on Windows both
  lock an open file. A second `acquire` of the same path is refused even inside the process that
  holds the first, which is what lets the tests exercise contention in one process.
- **The lock file is never deleted.** Unlinking it races a concurrent opener: the second process
  can lock a file the first has just removed, and two holders then each own "the" lock.
- **Windows frees a dead holder's lock a moment late.** It releases a terminated process's
  byte-range locks after the process exits rather than as it exits. "Free" still proves the holder
  died; only "held" can briefly outlive it, so a reader can see a just-killed job as running once.
- **The probe can briefly contend.** `is_held` takes the lock and lets it go at once. A real
  `acquire` landing in that instant returns `None`, the same answer as losing to any other holder.
  A caller that retries on a schedule loses one turn, never a lock.

## Using it

Put its folder on `sys.path` from one fixed place derived from your own file, as
`git_workspace_backup/src/backup_run.py` does, appending it so it can shadow neither
your own modules nor the standard library (`stdio_utf8/stdio_utf8.md`, "How this
project's tools import it"). Then take the lock, and treat `None` as another run
holding it:

```python
import run_lock

lock = run_lock.acquire(state_dir / "run.lock")
if lock is None:
    raise SystemExit(0)  # another run holds it
with lock:
    ...
```

## Tests

`test_run_lock.py` covers contention, release, probing held and free, the mode a new lock file is
opened with, the descriptor closed on a refused acquire, on a non-contention error and on an
unlock that raises (after which the lock can be taken again), and a live boundary test: a second process
takes the lock, is SIGKILLed, and the probe then reads the lock free (on Windows, polled for
until it does, for the late release above). It also runs the grouped suites in `tests/`
through `load_tests`, and fails if any of them goes missing from the run.

`tests/test_private_symlink.py` covers `private=True`'s symlink and mode rules: a symlink at
the lock path naming a real, separate file (missing, then present with a backdated mtime) is
refused by both calls and the file left as it was, while the default still follows it; the
file and folder modes under the usual umask, an existing parent's mode kept; and, on a
simulated win32, `ENOSYS` before any `os.open`. A self-loop symlink would not do for the first:
it fails with `ELOOP` with or without `O_NOFOLLOW`, so it proves nothing.

`tests/test_private_refusals.py` covers the refusals: a parent planted between the check and
the `mkdir` (a symlink to a directory, a directory open to others, another user's directory,
while the same user's private one is accepted), through an `os.mkdir` stand-in that plants it
first; an existing lock file that is a hard link, a FIFO, a directory (to the probe) or another
user's, each refused by both calls with its descriptor closed; and the `ValueError` for any
mode beside `private=True`, the default value included. Another user is simulated by making
`_euid` report a different user; the hard link and the FIFO are real.
