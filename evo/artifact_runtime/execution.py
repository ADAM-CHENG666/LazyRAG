from __future__ import annotations

import argparse
import asyncio
import importlib
import os
import pickle
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .artifact import CollectionResult
from .errors import OperationExecutionError
from .operation import Operation, OperationContext, OperationInvocation, OperationResult


@dataclass(frozen=True)
class _IsolatedRequest:
    module: str
    qualname: str
    context: OperationContext
    inputs: tuple[tuple[str, object], ...]


@dataclass(frozen=True)
class _IsolatedResponse:
    scalars: tuple[tuple[str, object], ...]
    collections: tuple[tuple[str, tuple[tuple[str, object], ...]], ...]

    @classmethod
    def from_result(cls, result: OperationResult) -> _IsolatedResponse:
        return cls(
            tuple(result.scalars.items()),
            tuple(
                (name, tuple(collection.items.items()))
                for name, collection in result.collections.items()
            ),
        )

    def to_result(self) -> OperationResult:
        return OperationResult(
            scalars=dict(self.scalars),
            collections={
                name: CollectionResult(dict(items))
                for name, items in self.collections
            },
        )


async def execute(
    invocation: OperationInvocation, ctx: OperationContext, inputs: Mapping[str, object], *,
    terminate_timeout: float = 1.0,
) -> OperationResult:
    if invocation.operation.spec.execution == 'async':
        result = await invocation.operation(ctx, **dict(inputs))
        return _validated_result(invocation.operation, result)
    return await _execute_isolated(invocation, ctx, inputs, terminate_timeout)


async def _execute_isolated(
    invocation: OperationInvocation, ctx: OperationContext, inputs: Mapping[str, object],
    terminate_timeout: float,
) -> OperationResult:
    if terminate_timeout <= 0:
        raise ValueError('terminate_timeout must be positive')
    with tempfile.TemporaryDirectory(prefix='artifact-operation-') as directory:
        root = Path(directory)
        request_path = root / 'request.pkl'
        result_path = root / 'result.pkl'
        request = _IsolatedRequest(
            invocation.operation.__module__,
            invocation.operation.__qualname__,
            ctx,
            tuple(inputs.items()),
        )
        request_path.write_bytes(pickle.dumps(request, protocol=pickle.HIGHEST_PROTOCOL))
        worker_watch, parent_watch = os.pipe() if os.name == 'posix' else (-1, -1)
        try:
            command = [
                sys.executable,
                '-c',
                'from evo.artifact_runtime.execution import _main; _main()',
                'supervise',
                str(request_path),
                str(result_path),
                str(worker_watch),
            ]
            options = {
                'stdout': asyncio.subprocess.PIPE,
                'stderr': asyncio.subprocess.PIPE,
            }
            if os.name == 'posix':
                options.update(start_new_session=True, pass_fds=(worker_watch,))
            process = await asyncio.create_subprocess_exec(*command, **options)
            if worker_watch >= 0:
                os.close(worker_watch)
                worker_watch = -1
            communication = asyncio.create_task(process.communicate())
            try:
                stdout, stderr = await asyncio.shield(communication)
            except asyncio.CancelledError:
                cleanup = asyncio.create_task(_terminate(process, communication, terminate_timeout))
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                await cleanup
                raise
        finally:
            if worker_watch >= 0:
                os.close(worker_watch)
            if parent_watch >= 0:
                os.close(parent_watch)
        if result_path.is_file():
            response = pickle.loads(result_path.read_bytes())
            if not isinstance(response, _IsolatedResponse):
                raise OperationExecutionError(
                    f'{invocation.operation.spec.op_id} worker returned an invalid response'
                )
            return _validated_result(invocation.operation, response.to_result())
        if process.returncode:
            detail = stderr.decode(errors='replace').strip() or stdout.decode(errors='replace').strip()
            if detail:
                raise OperationExecutionError(
                    f'{invocation.operation.spec.op_id} worker failed: {detail}'
                )
        raise OperationExecutionError(f'{invocation.operation.spec.op_id} worker produced no result')


async def _terminate(
    process: asyncio.subprocess.Process, communication: asyncio.Task[tuple[bytes, bytes]], timeout: float,
) -> None:
    if os.name == 'posix' or process.returncode is None:
        try:
            _signal_process(process, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        async with asyncio.timeout(timeout):
            await _wait_for_exit(process, communication)
    except TimeoutError:
        if os.name == 'posix' or process.returncode is None:
            try:
                _signal_process(process, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await asyncio.shield(communication)


async def _wait_for_exit(
    process: asyncio.subprocess.Process, communication: asyncio.Task[tuple[bytes, bytes]],
) -> tuple[bytes, bytes]:
    if os.name == 'posix':
        while _process_group_exists(process.pid):
            await asyncio.sleep(0.01)
    return await asyncio.shield(communication)


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    return True


def _signal_process(process: asyncio.subprocess.Process, value: signal.Signals) -> None:
    if os.name == 'posix':
        os.killpg(process.pid, value)
    elif value == signal.SIGTERM:
        process.terminate()
    else:
        process.kill()


def _validated_result(operation: Operation, result: object) -> OperationResult:
    if not isinstance(result, OperationResult):
        raise OperationExecutionError(f'{operation.spec.op_id} must return OperationResult')
    return result.validate_for(operation.spec)


def _resolve_operation(module_name: str, qualname: str) -> Operation:
    target: object = importlib.import_module(module_name)
    for part in qualname.split('.'):
        target = getattr(target, part)
    if not callable(target):
        raise TypeError(f'{module_name}.{qualname} is not callable')
    return target  # type: ignore[return-value]


async def _worker(request_path: Path, result_path: Path) -> None:
    request = pickle.loads(request_path.read_bytes())
    if not isinstance(request, _IsolatedRequest):
        raise TypeError('isolated operation request has an invalid type')
    operation = _resolve_operation(request.module, request.qualname)
    result = _validated_result(
        operation,
        await operation(request.context, **dict(request.inputs)),
    )
    response = _IsolatedResponse.from_result(result)
    temporary = result_path.with_suffix('.tmp')
    temporary.write_bytes(pickle.dumps(response, protocol=pickle.HIGHEST_PROTOCOL))
    os.replace(temporary, result_path)


def _watch_parent(parent_watch: int) -> None:
    if parent_watch < 0:
        return

    def stop_with_parent() -> None:
        try:
            while os.read(parent_watch, 1):
                pass
        finally:
            os.close(parent_watch)
        try:
            os.killpg(os.getpgrp(), signal.SIGKILL)
        except ProcessLookupError:
            pass

    threading.Thread(
        target=stop_with_parent,
        name='artifact-parent-watch',
        daemon=True,
    ).start()


def _supervise(request_path: Path, result_path: Path, parent_watch: int) -> None:
    _watch_parent(parent_watch)
    child = subprocess.Popen([
        sys.executable,
        '-c',
        'from evo.artifact_runtime.execution import _main; _main()',
        'work',
        str(request_path),
        str(result_path),
    ])
    returncode = child.wait()
    if os.name == 'posix':
        os.killpg(os.getpgrp(), signal.SIGKILL)
    os._exit(max(0, min(returncode, 255)))


def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('supervise', 'work'))
    parser.add_argument('request', type=Path)
    parser.add_argument('result', type=Path)
    parser.add_argument('parent_watch', type=int, nargs='?', default=-1)
    args = parser.parse_args()
    if args.mode == 'supervise':
        _supervise(args.request, args.result, args.parent_watch)
    else:
        asyncio.run(_worker(args.request, args.result))


__all__ = ['execute']
