# pgchecker on Chameleon Cloud

A reproducible HPC experiment: it reserves a bare-metal cluster on
**Chameleon Cloud**, runs the MPI benchmark **pgchecker** (from
[`hunsa/reprompi`](https://github.com/hunsa/reprompi/tree/main/pgchecker))
across the nodes, collects the results, and releases the cluster again.

## Quick start

1. Click **Launch on Chameleon** on the Trovi artifact page. This opens the
   files in your Chameleon JupyterHub.
2. Open `main.ipynb`.
3. Edit the **Settings** section at the top: choose the site (`CHI@UC` or
   `CHI@TACC`). This is the only section you need to edit.
4. Run all cells top to bottom.

Results are written to `results/<experiment_id>/`, one folder per allocation
(`k*/`) and per run (`k*/i*/`). `AnalyzeBestWorstRuns.ipynb` condenses them into
best/worst overhead tables per collective and message size.

Headless, from a Chameleon-authenticated shell:

```bash
pip install -r requirements.txt
python run.py --config configuration-uc.yaml     # or configuration-tacc.yaml
```

## Requirements

- A Chameleon project with bare-metal allocation at the chosen site.
- The public pgchecker images `tuwien-pgchecker-uc-compute_cascadelake_r`
  (CHI@UC) and `tuwien-pgchecker-tacc-compute_cascadelake_r` (CHI@TACC).

## Experiment configuration

The chosen site selects its configuration file, `configuration-uc.yaml` or
`configuration-tacc.yaml` (2 × `compute_cascadelake_r`). It defines the image,
node type and count, processes per node, lease duration and the repetition
matrix:

- `experiment.outer_repeats` (**k**): number of allocations; the machines are
  reserved `k` times, each set up and torn down on its own.
- `experiment.inner_repeats` (**i**): how often the experiment command runs per
  allocation.

## Costs and cleanup

Bare-metal leases consume your project's allocation. The workflow releases the
servers and its own lease after every allocation, also when a step fails.
