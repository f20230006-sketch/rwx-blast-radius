# Blast Radius selector

The image predicts affected SQLGlot tests from unified diffs and balances the selection across up to eight workers. It implements the input and JSONL output contract described in the [official scoring rules](https://www.joinbuilders.org/buildathon/tracks/blast-radius/files/SCORING.md?inline=1).

- The committed maps come from pytest on the pinned SQLGlot revision. They record Python coverage, fixture reads, and class usage, including coverage and class usage in spawned workers.
- The selector uses statement coverage for simple expression edits and function coverage for broader changes. Class usage and source references help narrow literal configuration edits. Invalid Python and unknown dependencies trigger full-suite selection.
- Equivalent Python syntax trees and identity comparisons whose operand types establish equivalent behavior can produce an empty schedule.
- The scheduler tries the permitted worker counts with longest-test-first allocation. It compares the busiest worker's milliseconds plus two seconds per worker.

## Reproduce the sample score

Download the [official sample kit](https://www.joinbuilders.org/buildathon/tracks/blast-radius/files/blast-radius-sample.tar.gz) and unpack it into a separate directory. Copy this repository's tracked files into that directory, then run `make sample` there. The scorer reports simulated seconds and misses. The leaderboard also charges CPU time.

The official 30-commit sample scored 128.566 simulated seconds with zero misses. One container run under the contest limits used 1.799 CPU seconds.

## Regenerate the dependency map

```sh
docker build --platform linux/amd64 --target maps -t blast-radius-maps .
docker run --rm --entrypoint cat blast-radius-maps /dependencies.json > dependencies.json
docker build --platform linux/amd64 --target observations -t blast-radius-observations .
docker run --rm --entrypoint cat blast-radius-observations /classes.json > classes.json
```

Coverage is predictive. For example, a function-body change could affect a test that inspects the function without executing it on the original revision.
