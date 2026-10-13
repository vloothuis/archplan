# archplan check in CI

`archplan check` reads the model and `.archplan/baseline.json`. It does not
run git. It fails, with exit code 1, in two cases:

- there is a new finding: one that is not in the baseline;
- a ratchet metric got worse than the baseline says.

It exits with 0 when neither happens, and with 2 on errors, such as a
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

## A GitHub Actions workflow

Start the ratchet once, on your machine, and commit the baseline:

```sh
archplan check --update-baseline
git add .archplan/baseline.json
git commit -m "Start the archplan baseline"
```

Then check every pull request. This example is for a Python project; use the
exporter for your language.

```yaml
name: Architecture
on:
  pull_request:
  push:
    branches: [main]

jobs:
  archplan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install git+https://github.com/vloothuis/archplan
      - run: archplan export-python -o .archplan/model.archmodel.json
      - run: archplan check --format github
```

The job fails on a new finding or a worse metric, and the annotations show
in the pull request. CI does not write the baseline. When a pull request
makes a number better, run `archplan check --tighten` in the branch and
commit the baseline, so the ratchet moves with it. When a worse number is a
deliberate choice, run `archplan check --loosen "why"` and commit that; the
reason shows in the diff for the reviewer.

Run the exporter in CI yourself, as above. `archplan check --export` runs
only an export command that a person approved with `archplan trust`.
