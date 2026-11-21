# Blast Radius selector

The image predicts affected SQLGlot tests from unified diffs and balances the selection across up to eight workers. It implements the input and JSONL output contract described in the [official scoring rules](https://www.joinbuilders.org/buildathon/tracks/blast-radius/files/SCORING.md?inline=1).

- The committed dependency map comes from pytest on the pinned SQLGlot revision. It records Python coverage in the test process and its spawned workers, plus fixture reads in the test process.
- Function-body edits select tests that executed the original body. Changes to import-time structure, invalid patched Python, and unknown dependencies trigger full-suite selection. Tests missing from the baseline coverage map are included when selecting by coverage or fixture reads.
- The scheduler tries the permitted worker counts with longest-test-first allocation. It compares the busiest worker's milliseconds plus two seconds per worker.

## Reproduce the sample score

Download the [official sample kit](https://www.joinbuilders.org/buildathon/tracks/blast-radius/files/blast-radius-sample.tar.gz), unpack it in a separate directory, and copy this repository's `Dockerfile`, `build_map.py`, `dependencies.json`, and `selector.py` into that directory. Run `make sample` there.

The official local scorer reported 368.163 simulated seconds and zero misses across 30 commits. The starter scored 1,210.470 simulated seconds on the same batch. The leaderboard additionally charges selector CPU time.

## Regenerate the dependency map

```sh
docker build --platform linux/amd64 --target maps -t blast-radius-maps .
docker run --rm --entrypoint cat blast-radius-maps /dependencies.json > dependencies.json
```

Coverage is predictive. For example, a function-body change could affect a test that inspects the function without executing it on the original revision.
