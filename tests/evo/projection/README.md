# Projection tests

Projection tests cover `ProjectionService` and its response-projection helpers. They are split by execution cost and dependency boundary.

| Layer | Scope |
| --- | --- |
| `unit` | Pure revision, page-token, filtering and page-slicing helpers. |
| `runtime` | Projection behavior that requires an ArtifactFlow runtime and persisted Artifact history. |
| `integration` | Public ProjectionService methods composed with local Flow fakes, including Dataset list-interface behavior. |

New projection tests belong in the narrowest layer that exercises the behavior. Dataset business-output tests remain under `tests/evo/dataset/`; tests for generic projection and pagination behavior belong here.
