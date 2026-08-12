from __future__ import annotations

import os
import signal
import subprocess  # nosec B404
import threading
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import BinaryIO


class SubprocessOutputLimitError(RuntimeError):
    pass


@dataclass(frozen=True)
class BoundedProcessResultV2:
    returncode: int
    stdout: str
    stderr: str


def run_bounded(
    command: tuple[str, ...],
    *,
    input_text: str | None,
    timeout: int,
    max_output_bytes: int,
    env: Mapping[str, str],
) -> BoundedProcessResultV2:
    if timeout < 1 or max_output_bytes < 1:
        raise ValueError("bounded subprocess limits are invalid")
    process = subprocess.Popen(  # nosec B603
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=dict(env),
    )
    if process.stdin is None or process.stdout is None or process.stderr is None:
        process.kill()
        raise RuntimeError("bounded subprocess pipes are unavailable")
    buffers: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    total = 0
    overflow = threading.Event()
    lock = threading.Lock()

    def terminate_group() -> None:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)

    def drain(name: str, stream: BinaryIO) -> None:
        nonlocal total
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return
            with lock:
                remaining = max(0, max_output_bytes - total)
                buffers[name].extend(chunk[:remaining])
                total += len(chunk)
                if total > max_output_bytes:
                    overflow.set()
                    terminate_group()
                    return

    readers = (
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    )
    for reader in readers:
        reader.start()
    try:
        if input_text is not None:
            process.stdin.write(input_text.encode("utf-8"))
        process.stdin.close()
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        terminate_group()
        process.wait()
        raise
    except BrokenPipeError:
        process.stdin.close()
        returncode = process.wait(timeout=timeout)
    finally:
        for reader in readers:
            reader.join(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            with suppress(OSError):
                stream.close()
    if overflow.is_set():
        raise SubprocessOutputLimitError("subprocess output exceeded the hard byte cap")
    return BoundedProcessResultV2(
        returncode,
        bytes(buffers["stdout"]).decode("utf-8", errors="replace"),
        bytes(buffers["stderr"]).decode("utf-8", errors="replace"),
    )
