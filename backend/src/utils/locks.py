"""Cross-process advisory locks around specimen erasure.

PostgreSQL uses transaction-scoped advisory locks: readers and run creation take
the shared variant, the erasure pipeline takes the exclusive variant.  A scope
scan therefore cannot miss a run that is being frozen concurrently, and a run
cannot be created from data that predates the tombstone.

SQLite is only used by the local test harness.  It has no advisory locks, so
``hold`` takes a process-wide reader/writer lock for the whole unit of work;
that reproduces the PostgreSQL ordering for the single process hosting the API
and worker under test.  Production runs PostgreSQL.
"""
import hashlib,threading
from contextlib import contextmanager
from sqlalchemy import text

GLOBAL='erasure:global'
_rw=threading.Condition(threading.Lock())
_state={'readers':0,'writer':False}
_held=threading.local()

def _is_postgres(db):
    return db.get_bind().dialect.name=='postgresql'

def key(name):
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:8],'big',signed=True)

def _take(exclusive):
    with _rw:
        if exclusive:
            while _state['writer'] or _state['readers']: _rw.wait()
            _state['writer']=True
        else:
            while _state['writer']: _rw.wait()
            _state['readers']+=1

def _drop(exclusive):
    with _rw:
        if exclusive: _state['writer']=False
        else: _state['readers']=max(0,_state['readers']-1)
        _rw.notify_all()

@contextmanager
def hold(db,exclusive=False,name=GLOBAL):
    """Hold the erasure lock for one unit of work.

    PostgreSQL: the lock follows the transaction, so a caller that commits
    part-way (the resumable purge does) must re-enter ``hold`` afterwards.
    SQLite: the process lock is only released when the outermost ``hold`` on
    this thread exits, so intermediate commits cannot admit a writer.
    """
    if _is_postgres(db):
        sql='SELECT pg_advisory_xact_lock(:k)' if exclusive else 'SELECT pg_advisory_xact_lock_shared(:k)'
        db.execute(text(sql),{'k':key(name)})
        yield
        return
    depth=getattr(_held,'depth',0)
    held_exclusive=getattr(_held,'exclusive',False)
    if depth==0:
        _take(exclusive)
    elif exclusive and not held_exclusive:
        raise RuntimeError('cannot upgrade a shared erasure lock to exclusive')
    _held.depth=depth+1
    _held.exclusive=held_exclusive or exclusive
    try:
        yield
    finally:
        _held.depth-=1
        if _held.depth==0:
            _drop(_held.exclusive)
            _held.exclusive=False

def acquire(db,name=GLOBAL):
    """Exclusive lock for the rest of the PostgreSQL transaction."""
    if _is_postgres(db):
        db.execute(text('SELECT pg_advisory_xact_lock(:k)'),{'k':key(name)})

def acquire_shared(db,name=GLOBAL):
    """Shared lock for the rest of the PostgreSQL transaction."""
    if _is_postgres(db):
        db.execute(text('SELECT pg_advisory_xact_lock_shared(:k)'),{'k':key(name)})

def try_acquire(db,name):
    if _is_postgres(db):
        return bool(db.execute(text('SELECT pg_try_advisory_xact_lock(:k)'),{'k':key(name)}).scalar())
    return True
