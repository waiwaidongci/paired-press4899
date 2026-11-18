import os,time,json,logging
from pathlib import Path
from sqlalchemy import select
from src.config.database import Session
from src.config.paths import ARTIFACT_DIR
from src.models.run import Run
# Importing the model modules registers their tables on Base.metadata.
from src.models import user,study,observation,review,audit,erasure  # noqa: F401
from src.services import erasures,queue
from src.services.algorithm import calculate
from src.utils.locks import hold,try_acquire
from src.utils.serializers import checksum

HEARTBEAT=Path(os.environ.get('WORKER_HEARTBEAT','/tmp/worker-heartbeat'))
RECOVER_LOCK='worker:recover'

def _recover():
    """Requeue jobs interrupted after dequeue.

    Exactly one worker performs recovery; a second worker started for failover
    must not duplicate every queued id.  Erased runs are never requeued.
    """
    with Session() as db:
        if not try_acquire(db,RECOVER_LOCK):
            db.rollback();return
        blocked=erasures.all_blocked_run_ids(db)
        pending=list(db.scalars(select(Run).where(Run.status.in_(['queued','running']))))
        for run in pending:
            if run.erased or run.id in blocked: continue
            if not queue.contains(run.id): queue.enqueue(run.id)
        db.commit()

def _erasure_for(db,run_id):
    return db.scalar(select(erasure.ErasureRun.erasure_id).where(erasure.ErasureRun.run_id==run_id))

def _process(run_id):
    resume=None
    with Session() as db:
        with hold(db):
            run=db.get(Run,run_id)
            if not run: db.rollback();return
            # A retry that arrives after a withdrawal must not revive the data.
            if erasures.run_blocked(db,run):
                resume=_erasure_for(db,run_id)
                db.rollback()
            else:
                if run.status in ('completed','failed'): db.rollback();return
                snapshot=list(run.snapshot);parameters=dict(run.parameters);run_checksum=run.checksum
                run.status='running';db.commit()
    if resume is not None:
        # Finish the interrupted withdrawal rather than compute anything.
        with Session() as sweep: erasures.execute(sweep,resume)
        return

    # Calculation runs without holding a lock so an erasure is never delayed.
    # The inputs live only in this process's memory; nothing is written yet.
    try:
        if not snapshot: raise ValueError('输入快照已清除')
        if checksum(snapshot)!=run_checksum: raise ValueError('输入快照校验失败')
        result=calculate(snapshot,parameters)
        result['input_sha256']=run_checksum
        failure=None
    except Exception as e:
        logging.exception('analysis failed');result=None;failure=str(e)

    with Session() as db:
        with hold(db):
            run=db.get(Run,run_id)
            if not run: db.rollback();return
            # Re-check inside the transaction that writes back.  A withdrawal
            # may have completed while this job was calculating, or its queue
            # entry may have been removed after the job was already claimed.
            blocked=erasures.run_blocked(db,run)
            if blocked or run.erased:
                erasures.empty_run(run,min(blocked) if blocked else run.erasure_id,'')
                db.commit();return
            if failure is not None:
                run.status='failed';run.error=failure;db.commit();return
            # The artifact is written under the shared lock so a purge either
            # sees and deletes it, or waits until this transaction ends; a
            # calculation that loses the race never writes the file at all.
            ARTIFACT_DIR.mkdir(exist_ok=True)
            (ARTIFACT_DIR/f'run-{run_id}.json').write_text(json.dumps(result,ensure_ascii=False,allow_nan=False))
            run.result=result;run.status='completed'
            db.commit()

def run_once(timeout=2):
    job=queue.dequeue(timeout=timeout)
    if job is None: return False
    _process(int(job))
    return True

def main():
    ARTIFACT_DIR.mkdir(exist_ok=True)
    _recover()
    while True:
        HEARTBEAT.touch()
        try:
            run_once()
        except Exception:
            logging.exception('worker loop error')
            time.sleep(1)

if __name__=='__main__': main()
