# pipeline_queue.py
import itertools
import queue
import threading
from dataclasses import dataclass, field

@dataclass(order=True)
class Job:
    priority: int
    seq: int
    lesson_id: str = field(compare=False)
    file_path: str = field(compare=False)
    original_filename: str = field(compare=False)
    resume_from: str | None = field(compare=False, default=None)

_job_queue: queue.PriorityQueue[Job] = queue.PriorityQueue()
_seq_counter = itertools.count()  # tiebreaker so equal priorities stay FIFO, not compared by Job fields

def enqueue_job(lesson_id, file_path, original_filename, resume_from=None, priority=10):
    _job_queue.put(Job(priority, next(_seq_counter), lesson_id, file_path, original_filename, resume_from))

def start_workers(run_pipeline_fn, num_workers=3):
    def worker_loop():
        while True:
            job = _job_queue.get()
            try:
                run_pipeline_fn(job.lesson_id, job.file_path, job.original_filename, job.resume_from)
            finally:
                _job_queue.task_done()
    for _ in range(num_workers):
        threading.Thread(target=worker_loop, daemon=True).start()