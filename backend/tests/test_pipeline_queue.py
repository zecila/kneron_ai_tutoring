import sys
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pipeline_queue as pq

def test_priority_order():
    # lower priority number = served first; equal priority falls back to FIFO (seq)
    pq.enqueue_job("lesson_a", "path_a", "a.pdf", priority=10)
    pq.enqueue_job("lesson_b", "path_b", "b.pdf", priority=10)
    pq.enqueue_job("lesson_retry", "path_r", "r.pdf", priority=0)  # simulates a retry

    order = [pq._job_queue.get().lesson_id for _ in range(3)]
    assert order == ["lesson_retry", "lesson_a", "lesson_b"]

if __name__ == "__main__":
    test_priority_order()
    print("ok: retry jumped ahead of equal-priority FIFO jobs")
