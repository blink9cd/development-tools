# atomic_write

One atomic file replacement, for every tool that writes a file something else may
read. A library, not a CLI: there is nothing to run.

## What it is for

A file another process reads while its writer runs (a state file a later run
loads, a document a person or a crawler opens, a configuration a service reads at
start) is exposed half-written by a truncate-and-refill, which surfaces as an
intermittent failure that never reproduces on its own.

Writing beside the target and renaming closes that window: the old file stays
whole until one indivisible step swaps in the new one.

## Who calls it

In this repository, `git_workspace_backup` writes all of its state files and each
snapshot's documents through it, and the property list of the agent it installs.

## Why it exists rather than being re-typed

The recipe was once written out by hand in seven tools, and the copies had drifted
apart on every axis that matters:

| Axis                      | What the copies did                                         |
| ------------------------- | ----------------------------------------------------------- |
| `fsync` before the rename | two did, five did not                                       |
| the published mode        | three set one, four renamed `mkstemp`'s 0600 into place     |
| cleanup on failure        | five removed the staging file, one did not remove it at all |
| line endings              | some pinned LF, some took the host's                        |

None of those differences was a decision. Each was whatever the tool in front
of the author happened to do that day, and the one with no cleanup stranded a
`.tmp` beside committed data whenever a replace failed.

Every caller shares `STAGING_SUFFIX`. What identifies a leftover is the target's
own name, which sits in the middle of the staging name, so it still says what it
was going to _be_, just not who was writing it. A per-caller suffix would put back
into this module exactly the per-tool variation it exists to remove, and the
cleanup here is wide enough (`BaseException`, so an interrupt too) that a leftover
needs a `SIGKILL` rather than a Ctrl-C.

A tool that has to recognize a staging file derives the pattern from
`staging_name` rather than spelling it again. Passing a glob wildcard for each
part, `staging_name("*", "*")`, gives a pattern that matches every staging file,
and `staging_name(target, "*")` gives one that matches a single target's.
The pattern is published for exactly those callers: a tool that keeps staging
files out of a snapshot or a sweep derives its ignore line from here, so renaming
the staging file here moves every such pattern with it instead of leaving one
that silently stops matching.

## Using it

Put its folder on `sys.path` from one fixed place derived from your own file, as
`git_workspace_backup/src/backup_fs.py` does, appending it so it can shadow neither
your own modules nor the standard library (`stdio_utf8/stdio_utf8.md`, "How this
project's tools import it"). Then call `replace_text` or `replace_bytes`. Both take
the whole new contents; neither appends. To make a file that must never be written over,
call `create_bytes` instead. To read a file that must not be a link, call
`read_bytes_nofollow`.

The `mode` keyword defaults to `SERVED_MODE`, which is what a generated file
wants. Pass a narrower one for a file that is not served, or `PRESERVE` to keep
whatever the target already carries.

`PRESERVE` is the policy for a file whose mode is not this tool's to decide: a
rewrite keeps the bits it found, and a file that does not exist yet gets what a
plain write would have given it, the creation mode filtered by the caller's
umask. That is what a plain `Path.write_text` does by construction, so it is
what a writer replacing a plain write needs. Forcing such a file onto a flat mode
would strip an execute bit from a file that had one and ignore the umask on a
file that had none.

## A last check before the rename

Some writers must ask, as late as possible, whether the replace should still
happen: a writer that re-reads the target and compares its digest with the one
taken when it read it refuses a concurrent writer's edit rather than overwriting
it. Such a writer takes the `before_rename` keyword.

The check is handed the staging file once it holds its final bytes, carries its
final mode and has been fsynced, and it runs immediately before the rename. That
placement is the point. Anything still left to do after the check widens the
window it cannot cover, so the only step after it is the rename itself. A check
run earlier would leave the write, the mode and the fsync inside that window.

It answers with a boolean. False vetoes the replace: the staging file is
removed, the target is left as it was, and the call returns False. An exception
the check raises does the same and then propagates, so a caller that wants to
report _why_ it refused can raise its own error. Without a check, or when the
check allows it, the call returns True.

This narrows the window rather than closing it. The check and the rename are two
steps, and a writer that lands between them is still lost. Closing it needs a
lock, or, for a file that is only ever created, `create_bytes` below.

## Creating a file once: `create_bytes`

Some files must be made once and never written over: a secret generated on a
tool's first run, which a second process may already have read. A re-check before
the rename cannot make that safe, because two first runs at the same moment can
each pass the check and then each rename, and one of them goes on holding a
secret the file no longer contains.

`create_bytes(path, data, mode=...)` closes that race rather than narrowing it.
**A secret must pass `mode=0o600`:** the mode defaults to `SERVED_MODE`, `0o644`, which
every account on the machine can read, the right mode for a published file and the wrong
one for a key or a token.
It finishes the staging file exactly as `replace_bytes` does (beside the target,
the mode on the descriptor, the bytes fsynced), then hard-links it to `path`
with `os.link` and removes the staging name. A link refuses any existing entry at
its destination, a dangling symlink included, and never follows it, so of two
writers racing to make the file exactly one lands. The other gets
`FileExistsError`, with the winner's file left as it was, and its caller reads
that file instead of trusting its own bytes.

**It needs a filesystem with hard links.** FAT, exFAT and some SMB shares refuse
the link outright. There is deliberately no fallback: a plain write or a rename
could not refuse an existing file, which is the one thing this call promises, so
the link's own `OSError` surfaces, the staging file is removed, and nothing is
written. Once the link is made, though, the file is there whole: an error or an
interrupt after that point (removing the staging name, say) still raises, so a
caller that must know whether the file was created checks for it. The `mode`
keyword takes a number only; `PRESERVE` has nothing to keep on a file this call
may create.

The directory entry is not fsynced, here or in `replace_bytes`: a crash just
after the call can lose the new name, though never expose a half-written file
under it.

## Reading a file that must not be a link: `read_bytes_nofollow`

This library is the one home of careful file IO, not of the atomic write alone:
the write no reader can catch half-done, and the read that refuses a link, a
non-file, or a file of another identity put where the checked one was, where
the volume reports file numbers. A check
of that kind is short enough to retype and easy to retype one step short, so it
is written here once and called.

Some files are read in order to be acted on, from a folder something else can
write to: a record one run leaves for the next, say. A plain `open` follows
whatever is at the path, so a link planted there, or a file swapped in at the
right moment, hands the reader contents of someone else's choosing.

`read_bytes_nofollow(path, max_bytes=...)` reads only from the regular file the
path itself names. It returns a `NofollowRead`: `data`, the bytes, and `status`,
the `os.fstat` of the descriptor they came from, so a caller that needs the
file's time or size takes it from the file that was read and not from a second
look at the path. A caller that wants text decodes `data` itself.

In order, it:

1. takes the path's own status with `lstat` and refuses a symbolic link or, on
   Windows, any reparse point (a junction and a mount point are both one);
2. refuses anything that is not a regular file: a folder, a named pipe, a
   device, a socket;
3. opens read-only with `O_NOFOLLOW` and `O_NONBLOCK` where the host has them
   (and `O_NOCTTY`, and `O_BINARY` on Windows), so a link that arrives after the
   check fails the open, and a pipe that arrives after it is opened at once
   instead of waited on. The open's "this is a link" error (`ELOOP`, or
   `EMLINK` on FreeBSD, or `EFTYPE` on NetBSD) is turned into a refusal, with
   the host's error kept as its cause;
4. takes the descriptor's status with `fstat` and, on a host without
   `O_NOFOLLOW`, refuses a file when either status carries no file number
   ("cannot be checked against a swap on this volume"; see
   [A volume with no file numbers](#a-volume-with-no-file-numbers)). It then
   refuses a file whose device and file numbers are not the checked one's ("was
   replaced between its check and its open"), and last one that is not a
   regular file even under the same numbers ("is no longer a regular file"),
   because a file system may hand a removed file's number to the next thing
   created;
5. reads at most `max_bytes + 1` bytes, continuing after a short read, and
   refuses a file that holds more than `max_bytes`;
6. closes the descriptor on every path out, each refusal and each error included.

Every refusal is a `ReadRefused`, which is an `OSError`: a caller that treats
every unreadable file alike needs one `except`, and a caller that wants the
refusals apart catches `ReadRefused` first. What that class does and does not
tell a caller:

- Each step above that finds a link, a non-file, a file of another identity or
  an oversized one raises it, on every host. A link swapped in after the check
  is one of them: refused by the open in step 3 where the host has
  `O_NOFOLLOW` ("is a symbolic link, or its path loops"), and by step 4 where
  it has not. One file of another identity is outside this: another regular
  file swapped in on a volume that reports no file numbers, on a host with
  `O_NOFOLLOW`, is read (see
  [A volume with no file numbers](#a-volume-with-no-file-numbers)).
- That step 3 message names two causes because the host's `ELOOP` has two: a
  link at the path itself, or too many links met on the way down the path. The
  open's error cannot say which, so neither does the message. Both mean the
  path stopped naming a plain file after it passed the check.
- Any other failure of the check, the open or the read leaves as the host
  raised it, and is never a `ReadRefused`: a permission error, an I/O error, a
  file removed since the check. So `ReadRefused` always means the call declined
  what it found, but a plain `OSError` is not proof that nothing was tampered
  with: whoever can swap the file can also remove it or make it unreadable.

A missing file stays a `FileNotFoundError`, at the check or at the open, and a
negative `max_bytes` is a `ValueError` raised before anything is touched.
`is_link_like` and `REPARSE_POINT` are public, for a caller that applies the
same link test to a folder it is about to use.

Windows has no `O_NOFOLLOW`, which is why steps 1 and 4 run on every host
instead of leaning on step 3: there the open follows a link, and the identity
check is what refuses the file it reached. No open error is turned into a
refusal on such a host, since none of them means a link there.

**What it does not protect against.** State these plainly to anyone relying on
it:

- **A linked parent folder is followed.** `O_NOFOLLOW` and the `lstat` both look
  only at the path's last component. If a folder above the file is a link, the
  read goes wherever it points. The folder has to be one the caller already
  trusts, or has checked itself.
- **A regular file forged in place is read as given.** Whoever can write the
  folder can put an ordinary file at the path before the check, and it passes
  every step. The call says where the bytes came from, never that they are true.
- **A hard link is read like any other file.** A hard link is not a symbolic
  link: it is a second name for a regular file, with that file's own stable
  identity, so it passes every step. Whoever can write the folder can make the
  path a hard link to any regular file on the same file system that the host
  lets them link, and the read returns that file's contents if the reader may
  open it.
- **Another regular file that inherits the checked file's identity is read.**
  Step 4 compares device and file numbers, and some file systems hand a removed
  file's number to the next file created. A regular file made at the name after
  the checked one is unlinked can therefore carry the same numbers and pass.
  That is the forged file of the bullet above arriving a moment later, with
  the same reach: whoever can do it can write the folder.
- **Another regular file swapped in on a volume with no file numbers is read,
  where the host has `O_NOFOLLOW`.** When both statuses carry file number zero,
  step 4 compares the device number alone, so any regular file of that volume
  put at the name between the check and the open passes. A link put there is
  still refused, by the open. This too is the forged file arriving a moment
  later, with the same reach: whoever can do it can write the folder.
- **The contents can change after the read.** The bytes are those of one moment;
  nothing holds the file still afterwards.

### A volume with no file numbers

Some Windows network and FAT volumes answer zero for a file's number, so every
file there "matches" every other and step 4's comparison compares nothing. What
is left to catch a link swapped in after the check is the open's own refusal,
and that exists only where the host has `O_NOFOLLOW`. So:

| The host has `O_NOFOLLOW` | A status with file number zero | The read |
| --- | --- | --- |
| yes | both | goes ahead: the open itself refuses a swapped-in link, and another regular file swapped in is not refused |
| yes | one only | refused, "was replaced between its check and its open": a zero and a real number do not match |
| no | neither | goes ahead: step 4 compares real numbers |
| no | either, or both | refused, "cannot be checked against a swap on this volume" |

The last row fails closed on purpose. A caller there gets a `ReadRefused` for
every file on that volume, and does whatever it does for an unreadable file;
the alternative was a read that followed a swapped-in link and said nothing.
Numbers that are reported but inconsistent also fail as a refusal.

The first row does not fail closed, and what it lets through is narrower: with
zero on both sides the comparison reduces to the device number, so another
regular file of the same volume swapped in between the check and the open is
read. That has the same reach as a regular file forged in place before the
check, which the call never refuses (see "What it does not protect against"
above).

## What it guarantees, and why each part is not optional

- **Staged in the target's own directory.** `os.replace` is atomic only within
  one filesystem; across a mount boundary it quietly becomes a copy, which is
  the very window this closes.
- **`fsync` before the rename.** The rename's atomicity says nothing about
  durability: a crash afterward could otherwise publish an empty file under
  the target's name.
- **The mode set on the descriptor.** `mkstemp` creates at 0600 and the rename
  carries that through, so without it a published file is readable by the
  account that wrote it alone.
  Setting it on the descriptor rather than afterward leaves no window in which
  the finished bytes sit on disk under the wrong mode. Windows has no POSIX mode
  bits, so none is set there; `os.fchmod` does not exist on it below Python
  3.13, and this project's floor is 3.12.
- **Never wider on disk than it is published.** A staging file whose mode is
  set next is created `0600` and then given the wanted mode on its descriptor.
  Created at the plain-write mode instead, it sat for a moment under whatever
  the umask left, and an account that opened it then kept its descriptor after
  the mode was narrowed. The one staging file created at the
  plain-write mode is a new file under `PRESERVE`: no mode is set on it, so the
  mode it is created with is the mode it is published with.
- **Any failure removes the staging file and re-raises**, including a
  `KeyboardInterrupt`: an operator who interrupts a build is exactly the person
  who would otherwise find the leftovers.

## A note for whoever tests a mode

A test that asserts the published mode must fail when the `fchmod` is deleted,
and whether it does depends on how the staging file was created. This module
stages through a path-based `os.open`, because `PRESERVE` needs a new file
to land where a plain write would have put it, and `mkstemp` takes no mode.

A staging file with a mode to carry is created `0600`, so with the `fchmod`
deleted it is published `0600` under every umask (measured at 0022, 0077 and
0): a test asking for any other mode fails, and a test asking for `0600`
itself passes for the wrong reason. Assert a mode on each side of `0600`, as
the staging-mode suite does, never that one alone.

A new file under `PRESERVE` is the case a umask can still hide. No mode is set
on it: it is created at 0666 and the umask reduces it, which under the
usual 0022 lands on 0644, the right answer for the wrong reason.
A hardcoded mode would give the same answer there. So compare that file with a
plain write under more than one umask, as `test_atomic_write.py` does, and do
not assert a published mode from a scratch directory that has not set its
umask. The strict umask in that suite's mode tests dates from when every
staging file was created that way, and stays for the day one is again.

One property here is deliberately not covered, because no filesystem test
can see it: the mode is set on the descriptor _before_ the bytes are written,
so no window exists in which finished content sits under the wrong mode. Moving
the `fchmod` after the write leaves the same end state and survives the suite.
It is held by the comment at the call, not by an assertion.

## Not in scope

A writer that stages through a temporary path for a reason other than atomicity
is deliberately not a caller:

- one that stages its output and then decides, after validating it, whether to
  install it at all, which is a stage-then-validate-then-install flow, not
  "write this content";
- one that stages a file to verify something about it, such as that it carries
  no identity metadata, before moving it, where the staging exists for the
  verification, not for the atomicity;
- one that writes every file of a multi-file change to a staging file first,
  proves the set, and only then replaces the targets in a crash-safe order. That
  is a multi-file stage-then-verify-then-install, not one file's replace.
