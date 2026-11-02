# stdio_utf8

Force UTF-8 on a tool's own text streams, name the encoding policy for child-process
captures, and escape a message that quotes argv or a file name. A pure library (no CLI, no
`__main__`) imported by the tools beside it. It owns the one decision every one of them needs
to make identically, so the policy lives in one place instead of a copy per tool that drifts.

## Why this exists

Python derives a text stream's encoding from the locale whenever that stream is not a
terminal. On Windows that is the legacy ANSI code page (cp1252 on a Western install), which cannot
encode most non-ASCII characters. Two consequences, in opposite directions, from the same cause.

**Write side.** A tool that prints a path it discovered on disk works when run by hand in a UTF-8
terminal and raises `UnicodeEncodeError` the moment stdout is a pipe, which is every scheduled
run, every CI run, and every run whose parent captures it. The bug class is invisible
interactively and certain in automation.

**Read side.** `subprocess.run(..., text=True)` with no explicit `encoding` decodes the child
using the same locale code page. A child emitting UTF-8 therefore decodes to mojibake. Worse, a
byte undefined in cp1252 (`0x81`, `0x8D`, `0x8F`, `0x90`, `0x9D`, reachable from Cyrillic, Greek,
and CJK names) raises `UnicodeDecodeError` on subprocess's reader thread, where it is swallowed:
`run()` returns with `stdout=None` and the caller fails later with a confusing `TypeError` that
names neither encoding nor the child.

**Coupling.** The two halves must be fixed together. A cp1252 decode followed by a cp1252 encode
restores the original bytes *by accident*, which is why a capture-and-reprint tool can look
correct today. Forcing only the output stream to UTF-8 turns that working accident into
double-encoded corruption:

| Stage | Bytes |
| --- | --- |
| Child emits (UTF-8 `→`) | `e2 86 92` |
| Parent captures, cp1252 decode | `U+00E2 U+2020 U+2019` (mojibake) |
| Reprinted to a cp1252 stream | `e2 86 92`: correct, by luck |
| Reprinted to a UTF-8 stream | `c3 a2 e2 80 a0 e2 80 99`: corrupted |

So a tool that captures a child and reprints it takes both changes, in the same commit, or
neither.

## API

```python
from stdio_utf8 import (
    CAPTURE_ERRORS, ENCODING, ESCAPE_ERRORS, EscapingArgumentParser,
    force_utf8_streams, print_escaped, printable, spell_unsafe,
)

force_utf8_streams(sys.stdout, sys.stderr)   # -> the stream names actually changed
print_escaped(f"tool: {line}", sys.stderr)    # one message line; never raises on a surrogate
EscapingArgumentParser(prog=...)             # argparse, every message escaped
printable(text)                               # the escape alone, for a message printed elsewhere
spell_unsafe(text)                            # controls and bidi marks as escapes, for a terminal
ENCODING                                      # "utf-8", both directions
CAPTURE_ERRORS                                # "replace", child captures only
ESCAPE_ERRORS                                 # "backslashreplace", stderr and messages
```

`force_utf8_streams` reconfigures each argument to UTF-8 and returns the names it changed. The
process's stderr keeps `backslashreplace`, the handler Python opened it with; every other
stream, stdout above all, is strict. The role is read by identity at the call: the stream that
*is* `sys.stderr` keeps the escape, so a tool hands over `sys.stdout, sys.stderr` as they stand,
and any other stream it passes is forced strict. One stream serving as both (`sys.stderr`
pointed at `sys.stdout`) is strict, because data is on it. It forces only the streams it is
handed, so it requires at least one, positionally: a call passing none would force nothing while
looking, at the call site, as if it forced the process's own streams. That call is a type-check
error and a `TypeError` rather than a no-op. Pass `sys.stdout, sys.stderr` explicitly.

Streams it cannot reconfigure are skipped rather than treated as an error, so adopting it can never
destabilize an existing test harness:

- `io.StringIO`, what a test substitutes for stdout, has no `reconfigure` method.
- A closed or detached stream raises `ValueError`.

### Why stdout is strict, stderr escapes, and captures `replace`

Three streams, three policies, each for its own reason.

- **stdout: strict.** It carries a tool's data (reports, paths, JSON), and a report that silently
  mangles a path is worse than one that fails loudly: a finding whose path is corrupted is
  actively misleading. A report never prints a path that is not the real path; one it cannot
  print exactly is a loud failure, on purpose.
- **stderr: `backslashreplace`.** It carries diagnostics, and the one that matters most here
  quotes what the operator typed. A refusal of a mistyped argument must print, and exit with the
  tool's own code, rather than become a traceback and exit 1. For a tool whose exit 1 means
  "found something", that traceback would read as a verdict. An escaped byte in a diagnostic
  misleads no one: it names the byte that could not be decoded. `reconfigure(encoding=...)`
  alone would reset the handler to strict, so `force_utf8_streams` names it. This replaced a
  strict-on-both rule under which every diagnostic quoting argv or a file name crashed,
  argparse's errors among them.
- **Child captures: `replace`.** A child's bytes are *not* ours to guarantee, and one stray byte
  from a third-party tool must not abort the caller's whole run.

Accepted trade-off: a child that genuinely emits cp1252 loses fidelity to `U+FFFD` where it
previously decoded. UTF-8 is what command-line tools overwhelmingly emit, so this is
net-positive for path fidelity.

### A message that quotes argv or a file name

Python decodes argv and file names with `surrogateescape`, so a byte that is not UTF-8 (a
mistyped argument, a name from a filesystem that accepts any bytes) arrives as a lone
surrogate, which a strict stream refuses. The tool's own message then becomes a
`UnicodeEncodeError` traceback and exit 1.

On stderr the stream handles it, with no change at the call site: a plain
`print(..., file=sys.stderr)`, a logging handler writing there, and argparse's usage and errors
all print the escape once `force_utf8_streams` has run.

On stdout the message itself must be escaped, because the stream stays strict for data. That is
argparse's help and `--version`, which go to stdout, and any human-facing line a tool prints
there. One boundary covers it:

- **`print_escaped(line, stream)`** for a human-facing line: a refusal, a warning, progress. The
  stream is always named, as `force_utf8_streams` takes its streams. On stderr it is harmless and
  redundant; on stdout it is what keeps the line from raising.
- **`EscapingArgumentParser`** in place of `argparse.ArgumentParser`. argparse writes usage,
  help, errors and `--version` through one method, `_print_message`, which it overrides.
  `add_subparsers` builds each sub-parser from the parent's class, so sub-parsers inherit it.
  Its errors already escape on stderr; what it adds is help and `--version` on stdout (under a
  `prog` taken from an undecodable `argv[0]`, say).
- **`printable(text)`**, the escape alone, for a message built in one place and printed in
  another. It changes only a lone surrogate, spelled as a backslash, `u` and four hex digits,
  and escaping its result again changes nothing.

A tool's data does not go through any of these: a path in a report that is not the real path
is the silent corruption the strict stdout exists to refuse. Escaping once around a single call,
or catching the `UnicodeEncodeError` there, fixes one route and leaves the next one open.

**Testing:** use a strict stream, never `io.StringIO`. `StringIO` stores text without encoding
it, so it accepts the surrogate a real stream refuses, and a test written over it passes with the
defect present. Use `io.TextIOWrapper(io.BytesIO(), encoding="utf-8")`, as
`tests/test_escaping.py` does, and pair it with a guard proving the raw write raises. To test
stderr's half, redirect that stream into `sys.stderr` before forcing, as
`tests/test_streams.py` does: the role is read by identity, so a stream handed over on its own
is forced strict.

### A name that would drive the terminal: `spell_unsafe`

`printable` answers *can UTF-8 encode this?*; `spell_unsafe(text)` answers *would a terminal
act on this?* That is a different set of characters, for a different hazard. A name or message
the tool did not write can carry an escape sequence (a clipboard write, a hyperlink, a cursor
move) that drives the terminal it is printed to, or a direction override that makes
`01 <U+202E>gpj.mp3` display as an image's name. `spell_unsafe` spells each such character
as a visible escape:

| Characters | Printed as |
| --- | --- |
| C0 controls U+0000–U+001F (tab and newline among them), DEL U+007F, C1 U+0080–U+009F | `\xNN`, lowercase hex (a tab is `\x09`, never `\t`) |
| U+061C, U+200E, U+200F, U+202A–U+202E, U+2066–U+2069, U+2028, U+2029 | `\uXXXX`, lowercase hex |

Every other character (accents, CJK, emoji, and a lone surrogate) comes back unchanged,
and a backslash is not escaped, so the result is a fixed point. The two functions therefore
compose rather than overlap: a message quoting a name from disk on a strict stream can need
both, `printable(spell_unsafe(text))`. The set is kept private; nothing outside the module
needs it. `tests/test_spell_unsafe.py` pins it code point by code point and holds a
property test of it.

## Worked example (real output)

A path whose name contains a non-ASCII character, printed with stdout piped to `cat` rather than
attached to a terminal. `PYTHONIOENCODING=cp1252` reproduces, on any platform, the stream
encoding a Windows pipe gets from a Western locale:

```bash
PYTHONIOENCODING=cp1252 python -c "print('probe/arrow→name.txt')" | cat
```

Without this library the line is lost, and the operator sees a traceback instead of the path
(CPython 3.14 on macOS; only the interpreter's install path is shortened to `...`):

```text
Traceback (most recent call last):
  File "<string>", line 1, in <module>
    print('probe/arrow\u2192name.txt')
    ~~~~~^^^^^^^^^^^^^^^^^^^^^^^^
  File ".../lib/python3.14/encodings/cp1252.py", line 19, in encode
    return codecs.charmap_encode(input,self.errors,encoding_table)[0]
           ~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
UnicodeEncodeError: 'charmap' codec can't encode character '\u2192' in position 11: character maps to <undefined>
```

With `force_utf8_streams` called first, run from the folder that holds `stdio_utf8/`, the same
piped command prints the path intact:

```bash
PYTHONIOENCODING=cp1252 python -c "import sys; sys.path.insert(0, 'stdio_utf8'); import stdio_utf8; stdio_utf8.force_utf8_streams(sys.stdout, sys.stderr); print('probe/arrow→name.txt')" | cat
```

```text
probe/arrow→name.txt
```

## How this project's tools import it

This library sits in `stdio_utf8/`, directly under the project root, beside the
`git_workspace_backup/` folder. The backup's entry point,
`git_workspace_backup/git_workspace_backup.py`, puts that folder on `sys.path` at one fixed
place, derived from its own `__file__`, so moving the checkout needs no edit. It appends the folder
rather than inserting it, so the folder can shadow neither the tool's `src/` modules nor the
standard library:

```python
sys.path += [
    str(Path(__file__).resolve().parents[1] / name)
    for name in ("stdio_utf8", "help_formatter", "os_schedule_gen")
]

import stdio_utf8
```

No `# noqa: E402` on the import: ruff reads an augmented assignment to `sys.path` as a path
modification rather than the end of the import block, so E402 does not fire after it.

It is one fixed place, not a search up the ancestors. A search would also match a same-named
folder above the checkout, such as one in a shared temporary directory. While the project's own
copy is present it usually sits nearer and wins; with that copy missing, the search would reach
past the clone and import whatever it found there. `tests/test_bootstrap.py` plants exactly such
a folder above a copy of this project and runs the backup's entry point against it
twice: as shipped, and with this library's folder removed, where it must stop at the import. A
control for each proves the planted modules do run under an ancestor search, and a structural
check reads every shipped module for a walk. mypy cannot see a runtime `sys.path` change, so
`pyproject.toml` lists this folder on `[tool.mypy] mypy_path` as well.

A missing library is not tolerated silently: the `import` raises a plain `ImportError` naming
the module. A tool that cannot establish its output encoding should fail loudly, not quietly
reintroduce the defect this library exists to remove.

Then, as the first statement in `main()`, before any logging setup, so a warning emitted during
startup is also safe:

```python
stdio_utf8.force_utf8_streams(sys.stdout, sys.stderr)
```

That call is also what makes stderr escape a lone surrogate, so a diagnostic there needs nothing
more. A tool that prints a message to stdout quoting argv or a file name also prints that
line through `stdio_utf8.print_escaped`, and builds its parser from
`stdio_utf8.EscapingArgumentParser` for help and `--version` (see "A message that quotes argv or
a file name" above).

And at every subprocess seam that decodes text, replacing `text=True` (which is redundant once
`encoding` is passed):

```python
proc = subprocess.run(
    argv,
    capture_output=True,
    encoding=stdio_utf8.ENCODING,
    errors=stdio_utf8.CAPTURE_ERRORS,
)
```

The backup does run child processes (`git`, `launchctl`, `osascript`, and the interpreter
that runs the opt-in memory snapshot), but through `bounded_run`, which decodes their output
as UTF-8 with a bad byte replaced: the same policy `ENCODING` and `CAPTURE_ERRORS` name,
applied at that one seam rather than at each call.

## Tests

`test_stdio_utf8.py` is the one entry point: its `load_tests` hook runs the grouped suites in
`tests/` (the stream policy, the escaping boundary, `spell_unsafe` and the bootstrap), and it
fails if any of them yields fewer tests than its floor, or if a file appears there that it does
not know.

```text
python stdio_utf8/test_stdio_utf8.py
```
