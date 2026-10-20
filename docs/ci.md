# archplan in CI

Two commands judge the code against `.archplan/baseline.json`:

- `archplan check` reads the model and the baseline. It does not run git
  or the exporter.
- `archplan ci` is check for a pull request. It runs the exporter, compares
  with the base branch, and writes a summary and SARIF. See
  [archplan ci](#archplan-ci) below.

Both fail, with exit code 1, in two cases:

- there is a new finding: one that is not in the baseline;
- a ratchet metric got worse than the baseline says.

They exit with 0 when neither happens, and with 2 on errors, such as a
missing model.

## The ratchet

The ratchet is a small set of project-level numbers in the baseline. Each
one may only get better. All of them are "lower is better".

| Metric | What it counts |
|---|---|
| `weight.<group>` | the sum of the weights of the open findings in a group, such as `weight.structure`. Open means new or accepted; dismissed findings do not count |
| `largest_tangle` | the components in the largest group of components that all reach each other; 0 without cycles |
| `cycle_edges` | the dependencies between two components of such a group |
| `upward_edges` | the dependencies from a lower layer to a higher one; only with layers in the profile |
| `propagation_cost` | the share of component pairs (a, b) where a reaches b through dependencies, in percent. A component reaches itself. This is the propagation cost of MacCormack et al., as in DV8 |

The numbers come from the model and the profile only, so every machine with
the same model gets the same numbers:

- The weights leave out churn, which comes from the git history.
- The `history` and `evolution` groups are not in the ratchet.
- The weights count only findings at or above the minimum weight
  (`--min-weight` or `"check": {"min_weight": N}`). When the minimum weight
  changes, check does not compare the weight metrics until you run
  `--loosen`.

### Commands

| Command | What it does |
|---|---|
| `archplan check` | compares the numbers now with the baseline; fails when one got worse |
| `archplan check --tighten` | writes the numbers that got better, and starts the ratchet when there is none. It never writes a worse number |
| `archplan check --update-baseline` | accepts every current finding and tightens the ratchet |
| `archplan check --loosen "REASON"` | writes every number as it is now, worse ones too. The reason goes into the log in the baseline |

When a metric got worse, check names it, with the old and the new value and
what caused it:

```
Ratchet (set 2026-10-13T05:55:57Z): 1 worse, 1 better, 10 the same.
  worse   weight.structure: 159 -> 209, weight of open structure findings
          [new, weight 93] utils and 15 other components form one dependency tangle.
          [new, weight 60] api → viewsets → api form a dependency cycle.
  better  cycle_edges: 67 -> 64, dependencies inside cycles
  Fix the cause, or run `archplan check --loosen REASON` to accept the worse values.
  Run `archplan check --tighten` to keep the better values in the baseline.
```

For the propagation cost, check lists the components that reach the most
others. With `--format github`, a worse metric becomes an `::error`
annotation. With `--json`, the output has a `ratchet` section.

### Tolerance

A tolerance lets a metric get a little worse without failing. Set it per
metric in `.archplan/config.json`:

```json
"check": {
  "min_weight": 40,
  "tolerance": {"propagation_cost": 0.5, "weight.concepts": 20}
}
```

### In the baseline

```json
"ratchet": {
  "metrics": {"weight.structure": 209, "largest_tangle": 16, "propagation_cost": 27.7},
  "min_weight": 0,
  "at": "2026-10-13T05:55:57Z",
  "log": [{"at": "...", "change": "loosen", "reason": "split billing next sprint",
           "metrics": {"propagation_cost": [27.7, 28.1]}}]
}
```

archplan treats this file as untrusted repository data. It keeps only known
metric names with numbers from 0, and at most 20 log entries.

## archplan ci

`archplan ci` uses the same findings, baseline and ratchet as check. It adds
an export step, a comparison with the base branch, and reports.

| Option | What it does |
|---|---|
| `--base REF` | compare with the merge base of REF and HEAD, such as `origin/main` |
| `--base-model PATH` | read the base's model from this file instead of exporting at the base |
| `--trust-config` | run the export command from `config.json` without `archplan trust`; `ARCHPLAN_CI_TRUST=1` does the same |
| `--no-export` | use the model as it is |
| `--summary PATH` | write a markdown summary; by default it is appended to `$GITHUB_STEP_SUMMARY` when that is set |
| `--sarif PATH` | write SARIF 2.1.0 for GitHub code scanning |
| `--format github` | `::warning` and `::error` annotations, as in check |
| `--json`, `--all`, `--min-weight N` | as in check |

The exit codes are the same as check's: 1, 0, and 2 on errors.

### The export and trust

The export command in `.archplan/config.json` comes from the repository. It
runs in a shell. So archplan runs it only when a person approved it on that
machine with `archplan trust`. Nobody does that on a CI runner, so ci needs
an explicit opt-in: `--trust-config` or `ARCHPLAN_CI_TRUST=1`.

Without the opt-in, ci works as check does. It runs the export command only
when it is approved. Otherwise it uses the model that is already in the
repository, and says so. With no model it fails with exit code 2.

The opt-in is safe in the usual setup. A CI job for a pull request already
runs the repository's own build and tests, which can run any code. The
export command adds nothing to that. On GitHub, a `pull_request` job from a
fork gets no secrets and a read-only token.

It is not safe with `pull_request_target`. That event runs with the base
repository's secrets and a token that can write. If such a job checks out
the pull request's code and runs ci with the opt-in, anyone who opens a pull
request from a fork can run code with those secrets. Do not set
`ARCHPLAN_CI_TRUST` in a `pull_request_target` job that checks out fork code.

### Against the base

With `--base REF`, ci finds the merge base of REF and HEAD. It makes a
temporary git worktree there and exports the model in it, with the same
trust rules. The worktree gets this branch's `.archplan` settings: the
profile, `config.json` and the baseline. So only the code differs between
the two sides. When the profile is not saved, the base uses the profile
inferred from this branch. The worktree is removed at the end.

With `--base-model PATH`, ci reads the base's model from a file instead,
such as a cached artifact from the base branch's last run. Then nothing
runs at the base. With `--base` too, the worktree still gives the base's
source files to the detectors that read code. Without `--base`, those
detectors read this branch's files.

A finding fails only when both are true:

- it is not in the baseline; and
- it was not there on the base.

A finding that was already on the base does not fail the pull request, even
when nobody accepted it. The pull request did not add it. The summary lists
such findings under "Already on the base". A tangle that grew since the
base counts as new. When a detector could not run on the base, the baseline
alone decides for its findings.

Without `--base` or `--base-model`, ci compares with the baseline only, as
check does.

The ratchet still compares with the baseline. The summary also shows each
metric on the base.

With `--base`, ci also lists the files changed since the merge base. A new
finding is marked "in the diff" when it names a changed file, or a component
that owns one. When the model has traces, the summary shows the blast
radius of the change: the traced entry points the changed files touch.

### The summary

The markdown summary has:

- the result, and the number of new findings and worse metrics;
- the new findings, with weight, file and line, and whether they are in the
  diff;
- the findings that the pull request fixed, and the ones already on the
  base;
- the ratchet: each metric in the baseline, on the base and now;
- the plan, when there is a target: the number of differences between the
  code and the target, now and on the base. This counts components,
  dependencies between components and types that only one side has, and
  components with another name or path. It is not the editor's task list,
  which only the editor computes;
- the blast radius, as above;
- notes, such as a skipped export.

### SARIF

`--sarif PATH` writes SARIF 2.1.0. Each finding kind, such as
`structure.cycle`, is a rule. Each failing finding is a result at the first
file and line of its evidence, with level `warning`. With `--all`, the other
new findings are results too, with level `note`. Code scanning needs a
location, so a finding without a file points at `.archplan/config.json`.

### The tighten reminder

CI does not write the baseline. When a metric got better, ci prints a
notice with the command to run, and does not fail:

```
Run `archplan check --tighten` in this branch and commit .archplan/baseline.json to keep them.
```

With `--format github`, it is a `::notice` annotation.

## A GitHub Actions workflow

Start the ratchet once, on your machine, and commit the baseline:

```sh
archplan check --update-baseline
git add .archplan/baseline.json
git commit -m "Start the archplan baseline"
```

Then check every pull request. This example is for a Go project. It needs
the full history (`fetch-depth: 0`) to find the merge base, and it caches
the Go exporter, whose version is pinned in `.archplan/config.json`.

```yaml
name: Architecture
on:
  pull_request:

permissions:
  contents: read
  security-events: write   # for the SARIF upload

jobs:
  archplan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - uses: actions/setup-go@v5
        with:
          go-version: stable
          cache: false
      - uses: actions/cache@v4
        with:
          path: |
            ~/go/pkg/mod
            ~/.cache/go-build
          key: archplan-go-${{ hashFiles('.archplan/config.json', 'go.sum') }}
      - run: pip install git+https://github.com/vloothuis/archplan
      - run: archplan ci --base origin/${{ github.base_ref }} --format github --sarif archplan.sarif
        env:
          ARCHPLAN_CI_TRUST: "1"
      - uses: github/codeql-action/upload-sarif@v3
        if: always() && hashFiles('archplan.sarif') != ''
        with:
          sarif_file: archplan.sarif
          category: archplan
```

For another language, leave out the Go steps and set up that language's
exporter instead. The step summary appears on the job's page. The
annotations show in the pull request. A pull request from a fork has a
read-only token, so the SARIF upload fails there; the check itself still
runs.

When a pull request makes a number better, run `archplan check --tighten` in
the branch and commit the baseline, so the ratchet moves with it. When a
worse number is a deliberate choice, run `archplan check --loosen "why"` and
commit that. The reason shows in the diff for the reviewer.

`archplan check` also works in CI when you run the exporter yourself:

```yaml
- run: archplan export-python -o .archplan/model.archmodel.json
- run: archplan check --format github
```

