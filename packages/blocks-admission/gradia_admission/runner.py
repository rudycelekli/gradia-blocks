"""Bounded parallel execution of host-owned local test lanes, never admission authority."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
from typing import Callable, Mapping

MAX_OUTPUT = 64 * 1024
ALLOWED_ENV = frozenset({'PATH', 'LANG', 'LC_ALL', 'PYTHONPATH'})


@dataclass(frozen=True)
class TrustedLane:
    """Construct in operator code; never deserialize candidate-provided commands."""
    lane_id: str
    argv: tuple[str, ...]
    cwd: Path
    timeout_seconds: float = 30
    env: tuple[tuple[str, str], ...] = ()


def _stop(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _run(lane: TrustedLane, deadline: float):
    started = time.monotonic()
    report = {'lane_id': lane.lane_id, 'command_sha256': hashlib.sha256(
        json.dumps(list(lane.argv), separators=(',', ':')).encode()).hexdigest(),
        'execution_result': 'unknown', 'admission_status': 'UNKNOWN',
        'promotion_authorized': False, 'reason': None}
    if started >= deadline:
        return {**report, 'reason': 'total_deadline_exhausted', 'duration_seconds': 0}
    process = None
    digest_out, digest_err = hashlib.sha256(), hashlib.sha256()
    total = 0
    stop_at = min(deadline, started + lane.timeout_seconds)
    outcome, reason = 'completed', None
    try:
        # Explicit minimal environment: do not inherit credentials or logged-in host state.
        environment = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
        environment.update(dict(lane.env))
        process = subprocess.Popen(lane.argv, cwd=lane.cwd, env=environment,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, shell=False, start_new_session=True)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, digest_out)
            selector.register(process.stderr, selectors.EVENT_READ, digest_err)
            while selector.get_map():
                remaining = stop_at - time.monotonic()
                if remaining <= 0:
                    outcome, reason = 'timed_out', 'lane_or_total_deadline'
                    _stop(process)
                    break
                for key, _ in selector.select(min(remaining, .05)):
                    data = os.read(key.fileobj.fileno(), 8192)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    retained = data[:max(0, MAX_OUTPUT - total)]
                    key.data.update(retained)
                    total += len(data)
                    if total > MAX_OUTPUT:
                        outcome, reason = 'failed', 'output_limit_exceeded'
                        _stop(process)
                        break
                if reason == 'output_limit_exceeded':
                    break
            if reason is None:
                remaining = stop_at - time.monotonic()
                try:
                    process.wait(timeout=max(0, remaining))
                except subprocess.TimeoutExpired:
                    outcome, reason = 'timed_out', 'lane_or_total_deadline'
                    _stop(process)
            if reason is None and process.returncode != 0:
                outcome, reason = 'failed', 'nonzero_exit'
    except (OSError, ValueError):
        outcome, reason = 'unknown', 'runner_start_or_io_error'
        if process is not None:
            _stop(process)
    finally:
        if process is not None:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
    return {**report, 'execution_result': outcome, 'reason': reason,
            'exit_code': process.returncode if process is not None else None,
            'duration_seconds': round(time.monotonic() - started, 6),
            'stdout_prefix_sha256': digest_out.hexdigest(),
            'stderr_prefix_sha256': digest_err.hexdigest(),
            'output_bytes_observed': total, 'output_hash_scope': 'at_most_64KiB_combined_prefix'}


def run_lanes(registry: Mapping[str, TrustedLane], selected: list[str], *,
              execute: bool = False, max_parallel: int = 2,
              total_deadline_seconds: float = 120):
    """Selection may be untrusted; registry/working directories must be operator-owned.

    Never runs on default invocation. Completion is process execution evidence only;
    structured test receipts and human review remain separately trusted inputs.
    """
    if type(max_parallel) is not int or not 1 <= max_parallel <= 4:
        raise ValueError('max_parallel must be 1..4')
    if (type(total_deadline_seconds) not in (int, float) or
            not 0 < total_deadline_seconds <= 3600):
        raise ValueError('total deadline must be positive and at most 3600 seconds')
    if not selected or len(selected) > 32 or len(selected) != len(set(selected)):
        raise ValueError('select 1..32 unique registered lanes')
    lanes = []
    for lane_id in selected:
        if lane_id not in registry:
            raise ValueError('unregistered lane selection')
        lane = registry[lane_id]
        if not isinstance(lane, TrustedLane) or lane.lane_id != lane_id:
            raise ValueError('invalid trusted registry entry')
        if (not isinstance(lane.argv, tuple) or not lane.argv or
                any(not isinstance(arg, str) or '\0' in arg for arg in lane.argv) or
                not os.path.isabs(lane.argv[0])):
            raise ValueError('operator argv must use an absolute executable path')
        if not lane.cwd.is_dir() or lane.cwd.is_symlink():
            raise ValueError('explicit real working directory required')
        if type(lane.timeout_seconds) not in (int, float) or not 0 < lane.timeout_seconds <= 3600:
            raise ValueError('lane timeout must be positive and bounded')
        if any(key not in ALLOWED_ENV for key, _ in lane.env):
            raise ValueError('credential-bearing or unsupported environment key')
        lanes.append(lane)
    if not execute:
        return {'execution_enabled': False, 'admission_status': 'UNKNOWN',
                'promotion_authorized': False, 'lanes': [
                    {'lane_id': lane.lane_id, 'execution_result': 'unknown', 'reason': 'execution_not_enabled'} for lane in lanes],
                'ai_review': 'UNKNOWN', 'human_rights_review': 'UNKNOWN'}
    deadline = time.monotonic() + total_deadline_seconds
    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = [pool.submit(_run, lane, deadline) for lane in lanes]
        rows = [future.result() for future in futures]
    return {'execution_enabled': True, 'max_parallel': max_parallel,
            'admission_status': 'UNKNOWN', 'promotion_authorized': False,
            'lanes': rows, 'ai_review': 'UNKNOWN', 'human_rights_review': 'UNKNOWN'}


def review_advisory(source_sha256: str, adapter: Callable | None = None):
    """Optional host adapter; no provider call is configured or made by default."""
    result = {'status': 'UNKNOWN', 'promotion_authorized': False,
              'token_usage': None, 'cost_usd': None}
    if len(source_sha256) != 64 or any(c not in '0123456789abcdef' for c in source_sha256):
        raise ValueError('exact source SHA-256 required')
    if adapter is None:
        return {**result, 'reason': 'ai_review_adapter_not_configured'}
    try:
        advisory = adapter(source_sha256)
        if (advisory.get('source_sha256') != source_sha256 or
                advisory.get('recommendation') not in ('REVIEW', 'REJECT', 'INCONCLUSIVE') or
                not isinstance(advisory.get('evidence_ref'), str) or not advisory['evidence_ref']):
            return {**result, 'reason': 'invalid_or_stale_advisory'}
        return {**result, 'source_sha256': source_sha256,
                'recommendation': advisory['recommendation'],
                'evidence_ref': advisory['evidence_ref'], 'reason': 'advisory_requires_independent_review'}
    except Exception:
        return {**result, 'reason': 'ai_review_adapter_failed'}
