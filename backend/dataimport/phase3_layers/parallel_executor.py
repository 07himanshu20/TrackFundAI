"""
Parallel executor — submits every Phase 3 call (Flavor A layers + Flavor B
chunks) to one shared ThreadPoolExecutor. Wall time = MAX(individual call
durations), not SUM.

Supports MULTI-PASS execution: a job's runner may return a result with
'_resubmit' = [job_dicts]. The executor collects those into a follow-up
batch and runs the next pass at the same parallelism. This lets the
orchestrator's split-and-retry (C7) reuse the shared pool instead of
recursing inside one worker thread, restoring full parallelism for
recovery from truncation.
"""

import os
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

logger = logging.getLogger(__name__)

_MAX_WORKERS = int(os.environ.get('PHASE3_MAX_PARALLEL_WORKERS', '8'))
_MAX_PASSES = int(os.environ.get('PHASE3_MAX_EXECUTOR_PASSES', '6'))


def _one_pass(jobs: list[dict], runner: Callable[[dict], dict],
              progress_cb, completed_counter, total_for_progress) -> list[dict]:
    """Run one parallelism pass. Returns list of result dicts."""
    n = len(jobs)
    if n == 0:
        return []
    workers = min(_MAX_WORKERS, n)
    results: list[dict] = [None] * n

    def _wrapped(idx: int, job: dict) -> tuple[int, dict]:
        start = time.monotonic()
        try:
            r = runner(job)
            r['_duration_s'] = round(time.monotonic() - start, 2)
            r['_ok'] = True
        except Exception as e:
            logger.exception(f'[phase3.executor] job {job.get("chunk_id")} failed')
            r = {
                'chunk_id': job.get('chunk_id'),
                'layer': job.get('layer'),
                'data': {},
                '_ok': False,
                '_error': f'{type(e).__name__}: {e}',
                '_duration_s': round(time.monotonic() - start, 2),
            }
        return idx, r

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='phase3') as pool:
        futures = {pool.submit(_wrapped, i, job): i for i, job in enumerate(jobs)}
        for fut in as_completed(futures):
            idx, r = fut.result()
            results[idx] = r
            completed_counter[0] += 1
            if progress_cb and total_for_progress:
                progress_cb(
                    r.get('chunk_id') or f'job-{idx}',
                    round(completed_counter[0] / total_for_progress * 100, 1),
                )
    return results


def run_in_parallel(jobs: list[dict], runner: Callable[[dict], dict],
                    progress_cb: Callable[[str, float], None] = None) -> list[dict]:
    """Execute every job concurrently. Returns list of final result dicts.

    Multi-pass: if a runner returns {'_resubmit': [sub_job, ...]} the executor
    re-runs those sub-jobs in the next pass. Final results EXCLUDE the
    resubmit envelope (only terminal results — _ok=True data or _ok=False
    error — are kept).
    """
    if not jobs:
        return []

    started_at = time.monotonic()
    completed_counter = [0]
    # Total work for the progress meter — grows as splits happen.
    total_for_progress = [len(jobs)]

    queue = list(jobs)
    final_results: list[dict] = []
    pass_num = 0

    while queue:
        pass_num += 1
        if pass_num > _MAX_PASSES:
            logger.error(
                f'[phase3.executor] exceeded {_MAX_PASSES} passes — bailing out. '
                f'Pending: {len(queue)} jobs ({[j.get("chunk_id") for j in queue]})'
            )
            for j in queue:
                final_results.append({
                    'chunk_id': j.get('chunk_id'),
                    'layer': j.get('layer'),
                    'data': {},
                    '_ok': False,
                    '_error': 'executor_max_passes_exceeded',
                })
            break

        logger.info(
            f'[phase3.executor] pass {pass_num}: {len(queue)} job(s), '
            f'workers={min(_MAX_WORKERS, len(queue))}'
        )

        def _scaled_progress(chunk_id: str, _local_pct: float):
            if progress_cb:
                overall = round(completed_counter[0] / max(total_for_progress[0], 1) * 100, 1)
                progress_cb(chunk_id, overall)

        results = _one_pass(queue, runner, _scaled_progress,
                            completed_counter, total_for_progress[0])

        new_queue: list[dict] = []
        for r in results:
            if r is None:
                continue
            resubmit = r.get('_resubmit') if isinstance(r, dict) else None
            if resubmit:
                logger.info(
                    f'[phase3.executor] {r.get("chunk_id")} requested '
                    f'{len(resubmit)} sub-job(s) (pass {pass_num + 1})'
                )
                new_queue.extend(resubmit)
                total_for_progress[0] += len(resubmit)
            else:
                final_results.append(r)
        queue = new_queue

    total = round(time.monotonic() - started_at, 2)
    durations = [r.get('_duration_s', 0) for r in final_results if r]
    max_single = max(durations, default=0)
    sum_durations = sum(durations)
    par_ratio = round(sum_durations / max(total, 0.01), 1)
    logger.info(
        f'[phase3.executor] {len(final_results)} terminal results in {total}s '
        f'({pass_num} pass(es), MAX single call {max_single}s, '
        f'parallelism ratio {par_ratio}x)'
    )
    return final_results
