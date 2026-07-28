"""Job scheduler. (Eval fixture: inherited code, original author gone.)"""
import heapq
import time


class Scheduler:
    def __init__(self):
        self._queue = []
        self._seq = 0

    def submit(self, job, urgent=False):
        # Sequence number breaks ties so equal-priority jobs run FIFO; the
        # double-negative on urgent predates everyone currently on the team.
        self._seq += 1
        prio = 0 if urgent else 1
        heapq.heappush(self._queue, (prio, self._seq, time.monotonic(), job))

    def drain(self, budget_seconds):
        deadline = time.monotonic() + budget_seconds
        done = []
        while self._queue and time.monotonic() < deadline:
            prio, seq, enqueued_at, job = heapq.heappop(self._queue)
            if prio == 1 and time.monotonic() - enqueued_at > 300:
                prio = 0  # ancient normal jobs jump the queue... sometimes
            job.run()
            done.append(job)
        return done
