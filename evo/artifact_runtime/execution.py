from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import pickle
import signal
import socket
import subprocess
import sys
import tempfile
import threading
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Self

from .errors import DefinitionError, OperationExecutionError
from .operation import Operation, OperationContext, OperationInvocation, OperationResult
from .state import ProgressUpdate


_PROGRESS_LIMIT = 64 * 1024
_WORKER_ENTRYPOINT = 'from evo.artifact_runtime.execution import _main; _main()'


@dataclass(frozen=True, slots=True)
class _IsolatedRequest:
    module: str
    qualname: str
    run_id: str
    invocation_id: str
    partition_key: str
    inputs: tuple[tuple[str, object], ...]


@dataclass(frozen=True, slots=True)
class _IsolatedResponse:
    values: tuple[tuple[str, object], ...]

    @classmethod
    def from_result(cls, result: OperationResult) -> _IsolatedResponse:
        return cls(tuple(result.values.items()))

    def to_result(self) -> OperationResult:
        return OperationResult(dict(self.values))


class ExecutionHandle(Protocol):
    async def wait(self) -> OperationResult:
        ...

    async def terminate(self) -> None:
        ...


class _CooperativeHandle:
    def __init__(self, task: asyncio.Task[OperationResult]) -> None:
        self._task = task

    async def wait(self) -> OperationResult:
        return await asyncio.shield(self._task)

    async def terminate(self) -> None:
        self._task.cancel()
        try:
            await asyncio.shield(self._task)
        except asyncio.CancelledError:
            if not self._task.cancelled():
                raise
        except Exception:
            pass


class _IsolatedHandle:
    def __init__(self, operation: Operation, process: asyncio.subprocess.Process,
                 stdout_task: asyncio.Task[bytes], stderr_task: asyncio.Task[bytes],
                 progress_task: asyncio.Task[None], result_path: Path,
                 directory: tempfile.TemporaryDirectory[str], parent_watch: int,
                 terminate_timeout: float
                 ) -> None:
        self._operation = operation
        self._process = process
        self._stdout_task = stdout_task
        self._stderr_task = stderr_task
        self._progress_task = progress_task
        self._result_path = result_path
        self._directory = directory
        self._parent_watch = parent_watch
        self._terminate_timeout = terminate_timeout
        self._terminate_requested = False
        self._terminate_lock = asyncio.Lock()
        self._completion = asyncio.create_task(
            self._complete(),
            name=f'isolated:{operation.spec.op_id}:{process.pid}',
        )

    async def wait(self) -> OperationResult:
        result = await asyncio.shield(self._completion)
        if result is None:
            raise asyncio.CancelledError
        return result

    async def terminate(self) -> None:
        async with self._terminate_lock:
            if not self._completion.done():
                self._terminate_requested = True
                with suppress(ProcessLookupError):
                    os.killpg(self._process.pid, signal.SIGTERM)

        try:
            await asyncio.shield(self._completion)
        except asyncio.CancelledError:
            if not self._completion.cancelled():
                raise
        except OperationExecutionError:
            pass

    async def _complete(self) -> OperationResult | None:
        try:
            await _wait_process_exit(self._process)
            await _finish_process_group(
                self._process.pid,
                self._terminate_timeout,
            )
            await self._process.wait()
            stdout, stderr = await self._output()
            try:
                await self._progress_task
            except Exception as exc:
                raise OperationExecutionError(
                    f'{self._operation.spec.op_id} worker emitted invalid progress'
                ) from exc
            if self._terminate_requested:
                return None
            if self._result_path.is_file():
                try:
                    response = pickle.loads(self._result_path.read_bytes())
                    if not isinstance(response, _IsolatedResponse):
                        raise TypeError('response must be _IsolatedResponse')
                    return _validated_result(self._operation, response.to_result())
                except OperationExecutionError:
                    raise
                except Exception as exc:
                    raise OperationExecutionError(
                        f'{self._operation.spec.op_id} worker returned an invalid response'
                    ) from exc
            detail = stderr.decode(errors='replace').strip() or stdout.decode(errors='replace').strip()
            if detail:
                raise OperationExecutionError(
                    f'{self._operation.spec.op_id} worker failed: {detail}'
                )
            raise OperationExecutionError(
                f'{self._operation.spec.op_id} worker produced no result'
            )
        finally:
            self._cleanup()

    async def _output(self) -> tuple[bytes, bytes]:
        stdout, stderr = await asyncio.gather(
            self._stdout_task,
            self._stderr_task,
        )
        return stdout, stderr

    def _cleanup(self) -> None:
        try:
            os.close(self._parent_watch)
        finally:
            self._directory.cleanup()


async def start_execution(invocation: OperationInvocation, ctx: OperationContext,
                          inputs: Mapping[str, object], *, terminate_timeout: float = 1.0
                          ) -> ExecutionHandle:
    if terminate_timeout <= 0:
        raise ValueError('terminate_timeout must be positive')
    if invocation.operation.spec.execution == 'cooperative':
        task = asyncio.create_task(
            _execute_cooperative(invocation, ctx, inputs),
            name=f'cooperative:{invocation.invocation_id}',
        )
        return _CooperativeHandle(task)
    if os.name != 'posix':
        raise OperationExecutionError(
            'isolated execution requires POSIX process groups'
        )
    return await _start_isolated(invocation, ctx, inputs, terminate_timeout)


async def _execute_cooperative(invocation: OperationInvocation, ctx: OperationContext,
                               inputs: Mapping[str, object]
                               ) -> OperationResult:
    result = await invocation.operation(ctx, **dict(inputs))
    return _validated_result(invocation.operation, result)


async def _start_isolated(invocation: OperationInvocation, ctx: OperationContext,
                          inputs: Mapping[str, object], terminate_timeout: float
                          ) -> _IsolatedHandle:
    directory = tempfile.TemporaryDirectory(prefix='artifact-operation-')
    root = Path(directory.name)
    request_path = root / 'request.pkl'
    result_path = root / 'result.pkl'
    request = _IsolatedRequest(
        invocation.operation.__module__,
        invocation.operation.__qualname__,
        ctx.run_id,
        ctx.invocation_id,
        ctx.partition_key,
        tuple(inputs.items()),
    )
    request_path.write_bytes(pickle.dumps(request, protocol=pickle.HIGHEST_PROTOCOL))

    watch_reader = watch_writer = -1
    progress_reader: socket.socket | None = None
    progress_writer: socket.socket | None = None
    process: asyncio.subprocess.Process | None = None
    try:
        watch_reader, watch_writer = os.pipe()
        progress_reader, progress_writer = socket.socketpair()
        progress_reader.setblocking(False)
        progress_fd = progress_writer.fileno()
        command = _worker_command(
            'supervise',
            request_path,
            result_path,
            watch_reader,
            progress_fd,
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            pass_fds=(watch_reader, progress_fd),
        )
        os.close(watch_reader)
        watch_reader = -1
        progress_writer.close()
        progress_writer = None
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_task = asyncio.create_task(_drain_output(process.stdout))
        stderr_task = asyncio.create_task(_drain_output(process.stderr))
        progress_task = asyncio.create_task(_forward_progress(progress_reader, ctx))
        progress_reader = None
        return _IsolatedHandle(
            invocation.operation,
            process,
            stdout_task,
            stderr_task,
            progress_task,
            result_path,
            directory,
            watch_writer,
            terminate_timeout,
        )
    except BaseException:
        if watch_reader >= 0:
            os.close(watch_reader)
        if watch_writer >= 0:
            os.close(watch_writer)
        if progress_reader is not None:
            progress_reader.close()
        if progress_writer is not None:
            progress_writer.close()
        try:
            if process is not None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await asyncio.shield(process.wait())
        finally:
            directory.cleanup()
        raise


async def _drain_output(stream: asyncio.StreamReader, *, limit: int = 64 * 1024) -> bytes:
    retained = bytearray()
    while chunk := await stream.read(8192):
        retained.extend(chunk)
        if len(retained) > limit:
            del retained[:-limit]
    return bytes(retained)


async def _forward_progress(sock: socket.socket, ctx: OperationContext) -> None:
    reader, writer = await asyncio.open_connection(
        sock=sock,
        limit=_PROGRESS_LIMIT,
    )
    try:
        while line := await reader.readline():
            if not line.endswith(b'\n'):
                raise OperationExecutionError(
                    'isolated worker emitted an incomplete progress event'
                )
            data = json.loads(line)
            await ctx.report(
                str(data['phase']),
                str(data.get('message') or ''),
                current=data.get('current'),
                total=data.get('total'),
                detail=data.get('detail') or {},
            )
    finally:
        writer.close()
        await writer.wait_closed()


async def _finish_process_group(process_group: int, timeout: float) -> None:
    if not _process_group_exists(process_group):
        return
    try:
        async with asyncio.timeout(timeout):
            await _wait_process_group(process_group)
        return
    except TimeoutError:
        with suppress(ProcessLookupError):
            os.killpg(process_group, signal.SIGKILL)

    try:
        async with asyncio.timeout(timeout):
            await _wait_process_group(process_group)
    except TimeoutError as exc:
        raise OperationExecutionError(
            f'isolated process group {process_group} survived SIGKILL'
        ) from exc


async def _wait_process_exit(process: asyncio.subprocess.Process) -> None:
    while process.returncode is None:
        await asyncio.sleep(0.01)


async def _wait_process_group(process_group: int) -> None:
    while _process_group_exists(process_group):
        await asyncio.sleep(0.01)


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _validated_result(operation: Operation, result: object) -> OperationResult:
    if not isinstance(result, OperationResult):
        raise OperationExecutionError(f'{operation.spec.op_id} must return OperationResult')
    try:
        return result.validate_for(operation.spec)
    except (DefinitionError, TypeError) as exc:
        raise OperationExecutionError(
            f'{operation.spec.op_id} returned an invalid result: {exc}'
        ) from exc


def _resolve_operation(module_name: str, qualname: str) -> Operation:
    target: object = importlib.import_module(module_name)
    for part in qualname.split('.'):
        target = getattr(target, part)
    if not callable(target):
        raise TypeError(f'{module_name}.{qualname} is not callable')
    return target  # type: ignore[return-value]


class _ProgressWriter:
    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self._writer = writer

    @classmethod
    async def open(cls, progress_fd: int) -> Self:
        sock = socket.socket(fileno=progress_fd)
        sock.setblocking(False)
        try:
            _, writer = await asyncio.open_connection(sock=sock)
        except BaseException:
            sock.close()
            raise
        return cls(writer)

    async def __call__(self, update: ProgressUpdate) -> None:
        payload = json.dumps(
            {
                'phase': update.phase,
                'message': update.message,
                'current': update.current,
                'total': update.total,
                'detail': dict(update.detail),
            },
            ensure_ascii=False,
            separators=(',', ':'),
        ).encode() + b'\n'
        self._writer.write(payload)
        await self._writer.drain()

    async def close(self) -> None:
        self._writer.close()
        await self._writer.wait_closed()


async def _worker(request_path: Path, result_path: Path, progress_fd: int) -> None:
    request = pickle.loads(request_path.read_bytes())
    if not isinstance(request, _IsolatedRequest):
        raise TypeError('isolated operation request has an invalid type')
    operation = _resolve_operation(request.module, request.qualname)
    reporter = await _ProgressWriter.open(progress_fd)
    context = OperationContext(
        request.run_id,
        request.invocation_id,
        request.partition_key,
        reporter,
    )
    try:
        result = _validated_result(
            operation,
            await operation(context, **dict(request.inputs)),
        )
        response = _IsolatedResponse.from_result(result)
        temporary = result_path.with_suffix('.tmp')
        temporary.write_bytes(pickle.dumps(response, protocol=pickle.HIGHEST_PROTOCOL))
        os.replace(temporary, result_path)
    finally:
        await reporter.close()


def _watch_parent(parent_watch: int) -> None:
    def stop_with_parent() -> None:
        try:
            while os.read(parent_watch, 1):
                pass
        finally:
            os.close(parent_watch)
        with suppress(ProcessLookupError):
            os.killpg(os.getpgrp(), signal.SIGKILL)

    threading.Thread(
        target=stop_with_parent,
        name='artifact-parent-watch',
        daemon=True,
    ).start()


def _supervise(request_path: Path, result_path: Path, parent_watch: int, progress_fd: int) -> None:
    _watch_parent(parent_watch)
    child = subprocess.Popen(
        _worker_command('work', request_path, result_path, progress_fd),
        pass_fds=(progress_fd,),
    )
    os.close(progress_fd)
    returncode = child.wait()
    os.killpg(os.getpgrp(), signal.SIGKILL)
    os._exit(max(0, min(returncode, 255)))


def _worker_command(mode: str, *arguments: object) -> list[str]:
    return [
        sys.executable,
        '-c',
        _WORKER_ENTRYPOINT,
        mode,
        *(str(argument) for argument in arguments),
    ]


def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('supervise', 'work'))
    parser.add_argument('request', type=Path)
    parser.add_argument('result', type=Path)
    parser.add_argument('file_descriptors', type=int, nargs='+')
    args = parser.parse_args()
    match args.mode, args.file_descriptors:
        case 'supervise', [parent_watch, progress_fd]:
            _supervise(args.request, args.result, parent_watch, progress_fd)
        case 'work', [progress_fd]:
            asyncio.run(_worker(args.request, args.result, progress_fd))
        case _:
            parser.error(f'invalid file descriptors for {args.mode}')


__all__ = ['ExecutionHandle', 'start_execution']
