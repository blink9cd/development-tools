# pyproject_config.py

Shared library (not a CLI) owning the one way a tool reads **its own**
`[tool.<name>]` table out of the nearest `pyproject.toml` — and what the paths it
finds there mean.

## Why it exists

A tool that checks a tree, a gate, usually carries standing config in a
`[tool.<name>]` table: exclude lists, allow lists, exempt lists. Reading that correctly is four separate
decisions, and each one has a silent failure mode:

| Decision | Get it wrong and… |
| --- | --- |
| Look in the nearest pyproject **above the target**, not above the cwd | the same files pass from one directory and fail from another |
| Absence — no file, no table, no key — means **empty** | a tool that raised on a missing table could not run outside a configured repo |
| Malformed means **stop** | treating unparseable config as empty silently shrinks what is checked, and still prints PASS |
| Entries resolve against **the pyproject's own directory** | an entry means a different folder depending on who asked |

That is a policy, not a snippet, and the third row is the one that matters: every
other kind of bug here makes a gate noisy, but that one makes it quietly stop
checking things.

## Who should not use it

Each of these is a reason for a tool to keep its own reader, not a gap to close here.
A library that quietly changed one caller's behavior would be worse than a second
reader.

| The caller… | Why it cannot use `read_path_lists` |
| --- | --- |
| reads a key whose entries are **not paths** (linter check IDs, package names, glob patterns, `table.key` identifiers) | every entry would be resolved into a bogus absolute path, so the exemption silently stops matching |
| reads a **scalar** (`corpus_dir`, `offline`, a word budget) or a **table** | outside `read_path_lists`' contract — such a caller takes `read_tool_table` and validates the raw value itself; see *What it owns* below |
| **degrades** malformed config to empty instead of raising | this module stops the gate by design; a readiness *report* that must survive a config typo cannot use it |
| yields a **non-empty default** when config is absent | absence here means `[]`, never a computed fallback |
| raises its own error class (`GateError`, `TargetError`) that its callers catch | conversion turns a clean exit 2 into an uncaught traceback unless the call is wrapped |
| needs to know **which** pyproject won, or keeps climbing past one that lacks its table | this module returns values, not provenance, and stops at the first file it finds |
| parses pyproject files as the **subject** of its check | that is a different concern that happens to share a parser |

## What it owns, and what it deliberately does not

It owns **path-list keys**. Given a table name and some key names, it returns each
key's value as resolved `Path` objects. Both the table name and the key names are
**parameters**: there is nothing about any one consumer baked in. Its suite pins that
by reading two different tables out of one document and requiring different answers —
an implementation that ignored `table=` and always read the first consumer's table
would satisfy a single-table test forever.

It also owns what a resolved entry **means**, which is the half that drifts quietly
because each tool re-decides it in passing:

- `is_under` — an entry names a **subtree**, not one path. A tool comparing for
  equality would walk into everything below a directory its config excluded and still
  report PASS.
- `dead_paths` — an entry that no longer resolves is **stale config that gates**,
  unless it was declared prospective. Without it an allow/exempt list keeps excusing
  folders that no longer exist, so an exclusion outlives its reason with nothing to
  say so.

The one thing it owns *below* that contract is the **walk**, published as
`read_tool_table`. A consumer whose table holds sub-tables rather than path lists
cannot use `read_path_lists`, but it needs exactly the same four decisions above — so
it takes the walk and applies its own shape rules to the raw values. `read_path_lists`
is itself built on it, which is what stops the two from drifting over where config
comes from: a walk that stopped at a different file would still return a perfectly
well-formed answer, so nothing else would notice.

It does **not** own:

- **Validating a raw table's values.** `read_tool_table` hands back whatever
  `tomllib` parsed. Only the consumer knows what shape its own keys should have, which
  is the same boundary drawn below — this module owns path lists, and nothing else.
- **Scalar config.** A boolean marker key is read by whoever cares.
  Generalizing to "any value of any type" would trade a precise contract for a dict of
  `object` the caller must re-validate anyway.
- **The consumer's resolved shape.** A gate's own `Config` dataclass names *its* keys
  (`allow` / `exempt` / `paths`) and stays with that gate. This module hands back a
  plain mapping and takes no view on what the keys mean.

An honestly narrow library beats one that claims to be generic and hardcodes a table
name, so that boundary is the contract, not an apology for it.

## Public surface

| Name | Purpose |
| --- | --- |
| `read_path_lists(start, *, table, keys, is_file, read_text)` | Every requested key from `[tool.<table>]` in the nearest pyproject above `start`, resolved. Absent keys come back as `[]`, so a caller can index the result directly |
| `read_tool_table(start, *, table, is_file, read_text)` | The same walk, raw: `(pyproject, table)` for the nearest pyproject above `start`, or `None` when there is none. For a consumer whose keys are not path lists |
| `nearest(start, *, is_file)` | Which `pyproject.toml` answers for `start`, without reading it — the walk with the parse taken off, so a consumer can tell one source from another by identity |
| `chain(start, *, stop, is_file)` | Every `pyproject.toml` from `start` up to and including `stop`, nearest first, for config that **accumulates** down a tree rather than being answered by one file. A `start` outside `stop` yields `()` — the bound keeps anything above the checkout from changing a verdict, and what to do about a subject reached from outside it is the caller's policy |
| `is_under(path, roots)` | Is `path` one of `roots`, or beneath one? The subtree rule behind every exclude list |
| `dead_paths(configured, prospective, *, exists)` | Split entries that no longer resolve into `(gating, absent)` |
| `ConfigError` | Exit-2-class failure: config that could not be **trusted**. Never raised for merely *absent* config. Its message names the `pyproject.toml` relative to the directory the walk starts in (the target `start`, or a file target's own directory), never absolutely, because consumers print it on stderr |
| `IsFileFn` · `ReadTextFn` · `ExistsFn` | The injected filesystem seam types |
| `default_is_file` · `default_is_dir` · `default_read_text` | The real implementations of those seams |

`default_read_text` decodes UTF-8 explicitly. That is the reason it lives here rather
than in each consumer: falling back to the platform default makes a pyproject holding
a non-ASCII string readable on one machine and a `UnicodeDecodeError` on another —
and because that error is caught and re-raised as `ConfigError`, the difference would
surface as a gate that mysteriously exits `2` on one developer's box.

## Usage

A consumer resolves this folder from its own location (never a hardcoded path) and
maps the returned lists onto its own config type:

```python
sys.path.append(str(Path(__file__).resolve().parent.parent / "pyproject_config"))
import pyproject_config

def read_config(start, *, is_file, read_text) -> Config:
    lists = pyproject_config.read_path_lists(
        start, table="widget_gate", keys=("allow", "exclude"),
        is_file=is_file, read_text=read_text,
    )
    return Config(allow=frozenset(lists["allow"]), exclude=tuple(lists["exclude"]))
```

Keep the seams injected all the way down from the entry point, passing
`default_is_file` / `default_read_text` in `main()`. That is what lets a consumer's
own tests drive the unreadable-file and malformed-value branches without writing a
real file, and it is why this module performs no I/O of its own.

## Notes

- **Python 3.11+**, for the stdlib `tomllib`; this project's floor, 3.12, clears
  it.
- **Pure and stateless.** No I/O of its own, no config of its own, no import-time
  failure mode.
- **One walk, one parse.** All requested keys are read from the first pyproject
  found, so asking for three keys does not open the file three times.

## Testing pyproject_config itself

```bash
python pyproject_config/test_pyproject_config.py
```

It ships no `samples/` directory: it transforms no input into an output. Because it
is imported through a runtime `sys.path` insert that mypy cannot see, it is also on
`[tool.mypy].mypy_path`.
