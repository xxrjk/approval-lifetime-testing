# The Two-Sided Lifetime of an Approval

Executable approval contracts, relation-target generation, checked scheduling,
comparison baselines, and native workflow experiments for the paper.

## Quick start

Python 3.13:

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python run.py check
.venv/bin/python run.py native
```

`check` runs 43 tests. `native` executes 48 Wagtail HTTP conditions, checks 72
completion effects, and runs the bounded schedule and certificate validation.
It uses local test clients and temporary SQLite databases.

## Code layout

- `src/`: checker, target construction, baselines, scheduler, independent verifier,
  experiment runners, and result analysis.
- `tests/`: focused tests of the method.
- `app/`: the Dart/SQLite target and its test bridge, with locked dependencies.
- `cases.json`: fault/control configurations, fields, families, and seed counts.
- `run.py`: common entry point. `requirements.txt`: pinned Python dependencies.

Experiments write raw records and summaries to `results/` and working files to
`tmp/`. Both directories are created on first use.

## Run the experiments

Use `.venv/bin/python run.py TASK [options]`:

| Task | Execution |
|---|---|
| `smoke` | Generate fresh plans and execute eight Dart/SQLite suites |
| `spec` | Full-specification comparison, 1,880 runs (Table 2) |
| `matched` | Common-search comparison, 1,880 runs (Table 3) |
| `fair` | Same-information comparison, 2,820 runs (Table 4 / Figure 3) |
| `mutants` | Mechanical source mutations (Table 5); choose Set A or B |
| `native` | Wagtail requests, persistence, and publication (Table 6) |
| `transfer` | Eight pinned Viewflow source/encoding conditions (Table 7) |

The three comparisons generate raw records and analyze them with both oracles.
Use `--phase pilot` for their smaller pilot configurations; omit it for the
reported confirmation configurations. Run `TASK --help` for runner options
(`native` and `check` take no options).

Dart experiments require Flutter 3.44.4 / Dart 3.12.2. Populate the locked packages:

```sh
cd app
/path/to/flutter pub get
cd ..
.venv/bin/python run.py smoke --flutter /path/to/flutter
.venv/bin/python run.py spec --flutter /path/to/flutter
```

The test bridge uses loopback sockets. If the sqlite3 native library is cached,
add `--sqlite-cache /path/to/sqlite3/build`, the directory containing `download-*`
folders. Otherwise that native library must be downloadable.

For mutations, run both sets and then analyze their generated records:

```sh
.venv/bin/python run.py mutants --set A --worker 0 --workers 1 --include-original --flutter /path/to/flutter
.venv/bin/python run.py mutants --set B --worker 0 --workers 1 --include-original --flutter /path/to/flutter
.venv/bin/python src/analyze_mutation.py
```

`transfer` fetches and verifies two pinned public Viewflow versions. Supply
`--existing-source /path/to/viewflow` to reuse a public Git checkout. Third-party
sources retain their licenses under `tmp/transfer_sources/`.

The comparisons use prespecified fault variants. The Viewflow experiment tests
a public regression in an author-written workflow. Results include every run,
control, and failure; elapsed times depend on the execution host.

Result labels `v2` and `v3` identify the admission-plus-preservation and complete
obligation masks. `compiled_v4` identifies relation targets in the shared model.
`case_selection_core` records the version used to select the fault cases.
