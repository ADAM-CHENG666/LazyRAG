# Dataset tests

Dataset tests are isolated from the legacy `tests/evo` fixture tree and are split by execution cost and dependency boundary.

| Layer | Scope | Command |
| --- | --- | --- |
| `unit` | Dataset materializers, contracts, and fake KB clients | `make test-dataset-unit` |
| `runtime` | ArtifactFlow scheduling, dynamic partitions, and worker execution | `make test-dataset-runtime` |
| `integration` | Current service seed and request contracts | `make test-dataset-integration` |

Run every dataset test with `make test-dataset`. Pytest markers are added automatically from the directory name, so `pytest tests/dataset -m dataset_runtime` is also supported.

New dataset tests belong in the narrowest layer that exercises the behavior. Do not add a dependency on `tests/evo/conftest.py`; use explicit local fakes and fixtures instead.
