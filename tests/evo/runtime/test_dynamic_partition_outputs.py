from __future__ import annotations

import asyncio

from evo.artifact_runtime import (
    ArtifactCommit,
    ArtifactDraft,
    ArtifactKey,
    ArtifactRuntime,
    OperationResult,
    PartitionSet,
    one,
    operation,
    partitioned,
    scalar,
)


@operation(
    op_id='demo.expand_partitions',
    inputs={'spec': one('demo.spec')},
    outputs={
        'partitions': scalar('demo.requests'),
        'requests': partitioned('demo.request', over='demo.requests'),
    },
    execution='cooperative',
)
async def expand_partitions(ctx, spec):
    keys = tuple(str(item) for item in spec['keys'])
    return OperationResult({
        'partitions': PartitionSet(keys),
        'requests': {key: {'partition_key': key} for key in keys},
    })


SPEC_KEY = ArtifactKey.scalar('demo.spec')
REQUESTS_KEY = ArtifactKey.scalar('demo.requests')


async def _run_until_settled(runtime: ArtifactRuntime, run_id: str):
    await runtime.start(run_id)
    return await runtime.wait_until_settled(run_id, timeout=5.0)


def test_rerunning_dynamic_partition_producer_replaces_partition_keys(tmp_path) -> None:
    async def run() -> None:
        runtime = await ArtifactRuntime.open(tmp_path / 'runtime', (expand_partitions,))
        try:
            await runtime.create('run-1', ArtifactCommit(
                'seed',
                'user:seed',
                (ArtifactDraft(SPEC_KEY, {'keys': ['chunk-a', 'chunk-b']}),),
                {SPEC_KEY: None},
            ))
            first = await _run_until_settled(runtime, 'run-1')
            assert first.status == 'completed'
            assert first.partition_sets[REQUESTS_KEY].keys == ('chunk-a', 'chunk-b')

            spec = await runtime.head('run-1', SPEC_KEY)
            assert spec is not None
            await runtime.commit('run-1', ArtifactCommit(
                'replace-keys',
                'user:spec',
                (ArtifactDraft(SPEC_KEY, {'keys': ['chunk-b', 'chunk-c']}),),
                {SPEC_KEY: spec.ref},
            ))
            second = await runtime.wait_until_settled('run-1', timeout=5.0)
            assert second.status == 'completed', getattr(second.error, 'message', second.status)
            assert second.partition_sets[REQUESTS_KEY].keys == ('chunk-b', 'chunk-c')
        finally:
            await runtime.close()

    asyncio.run(run())
