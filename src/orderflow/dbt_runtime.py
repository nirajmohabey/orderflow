"""Pinned dbt CLI child using in-process locks instead of POSIX semaphores.

Only the finite hosted child invokes this module. dbt's model pool is already
thread based; its adapter/manifest locks and pool notification pipe need not be
process-shared. No global multiprocessing implementation is modified.
"""
from importlib.metadata import version
from multiprocessing.pool import Pool
from multiprocessing.queues import SimpleQueue
import sys
import threading

EXPECTED={"dbt-core":"1.12.7","dbt-postgres":"1.11.0","dbt-adapters":"1.24.6","dbt-common":"1.39.0"}


class ThreadOnlyContext:
    @staticmethod
    def Lock():return threading.Lock()

    @staticmethod
    def RLock():return threading.RLock()

    def SimpleQueue(self):
        # A real OS pipe with thread locks; dbt's thread-pool notification
        # queue stays inside this process and never spawns a worker process.
        return SimpleQueue(ctx=self)


def configure():
    if sys.version_info[:2]!=(3,12) or any(version(name)!=expected for name,expected in EXPECTED.items()):
        raise RuntimeError("Finite dbt runtime requires its reviewed Python3.12 and exact locked dbt versions")
    import dbt.mp_context
    context=ThreadOnlyContext()
    dbt.mp_context._MP_CONTEXT=context
    # Configure before importing dbt.cli/task modules, so Manifest's lock
    # factory and every Postgres connection manager receive this context.
    from dbt.graph import thread_pool
    if 'dbt.task.runnable' in sys.modules:
        raise RuntimeError("Finite dbt context must be configured before task imports")

    class FiniteThreadPool(thread_pool.DbtThreadPool):
        def __init__(self,processes=None,initializer=None,initargs=()):
            processes=2 if processes is None else processes
            if type(processes) is not int or not 1<=processes<=2:
                raise RuntimeError("Finite dbt model concurrency must be between one and two threads")
            # ThreadPool.__init__ omits Pool's context argument. Use the same
            # Pool initializer with ThreadPool's inherited thread/queue hooks.
            Pool.__init__(self,processes,initializer,initargs,context=context)
            self.closed=False
            self.max_threads=processes
            self.max_microbatch_models=max(1,processes//2)

    thread_pool.DbtThreadPool=FiniteThreadPool
    return context


def main():
    configure()
    from dbt.cli.main import cli
    cli()


if __name__=='__main__':main()
