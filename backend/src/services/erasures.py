import json
from sqlalchemy import select,func

from src.models.audit import Audit
from src.models.erasure import ErasureRun,ErasureStep,SpecimenErasure
from src.models.observation import Observation
from src.models.run import Run
from src.models.review import Review
from src.config.paths import ARTIFACT_DIR
from src.services import queue,specimens
from src.services.audits import log
from src.utils.locks import hold
from src.utils.serializers import checksum,now

STEP_ORDER=('tombstone','observations','queued_jobs','runs','reviews','artifacts','audits','receipt')
PURGED='purged'   # removed, awaiting the independent verification pass
STEP_LABELS={
    'tombstone':'纸样编号封存',
    'observations':'登记观测',
    'queued_jobs':'队列任务',
    'runs':'冻结输入与计算结果',
    'reviews':'业务复核',
    'artifacts':'共享产物留档',
    'audits':'审计留痕原值',
    'receipt':'清除凭据与逐处核验',
}
REDACTED='[已按纸样编号清除]'
ERASURE_ACTIONS=('erasure.request','erasure.complete','erasure.resume')

def _get(db,erasure_id):
    obj=db.get(SpecimenErasure,erasure_id)
    if not obj: raise LookupError('清除任务不存在')
    return obj

def _step(db,erasure_id,name):
    row=db.scalar(select(ErasureStep).where(ErasureStep.erasure_id==erasure_id,ErasureStep.name==name))
    if not row:
        row=ErasureStep(erasure_id=erasure_id,name=name,status='pending',updated_at=now())
        db.add(row);db.flush()
    return row

def _mark(db,erasure_id,name,status,removed=None,note=None):
    row=_step(db,erasure_id,name)
    row.status=status
    if removed is not None: row.removed=int(removed)
    if note is not None: row.note=str(note)[:2000]
    row.updated_at=now()
    if status=='verified': row.verified_at=now()
    return row

# ---- blocking predicates -------------------------------------------------

def blocked_specimen(db,key):
    return db.scalar(select(SpecimenErasure).where(SpecimenErasure.specimen_key==specimens.normalize(key)))

def all_blocked_run_ids(db):
    return {r.run_id for r in db.scalars(select(ErasureRun))}

def blocked_run_ids(db,run_ids):
    ids=[int(r) for r in run_ids]
    if not ids: return set()
    rows=db.scalars(select(ErasureRun.run_id).where(ErasureRun.run_id.in_(ids)))
    return set(rows)

def run_blocked(db,run):
    if run is None: return False
    if getattr(run,'erased',False): return True
    return bool(blocked_run_ids(db,[run.id]))

def pending_for_snapshot(db,keys):
    """Specimen numbers frozen into a new run that are withdrawn or being withdrawn."""
    normalized={specimens.normalize(k) for k in keys}
    if not normalized: return set()
    return {r.specimen_key for r in db.scalars(select(SpecimenErasure)) if r.specimen_key in normalized}

# ---- scope ---------------------------------------------------------------

def _scope(db,erasure_id):
    runs=list(db.scalars(select(ErasureRun).where(ErasureRun.erasure_id==erasure_id).order_by(ErasureRun.run_id)))
    obs_step=db.scalar(select(ErasureStep).where(ErasureStep.erasure_id==erasure_id,
                                                ErasureStep.name=='observations'))
    return {
        'observations_removed':obs_step.removed if obs_step else 0,
        'runs':[{'run_id':r.run_id,'study_id':r.study_id,'name':r.run_name,
                 'snapshot_rows':r.snapshot_rows,'result_present':bool(r.result_present)} for r in runs],
    }

def _record_scope(db,obj,key):
    """Capture every run whose frozen snapshot contains the key, across studies.

    Runs created after the tombstone commit take the shared erasure lock and are
    rejected while any erasure for one of their specimen numbers is pending, so
    this scan cannot miss a concurrently frozen analysis.  Re-scanning on resume
    heals an interruption between tombstone write and scope capture.
    """
    # Runs are scanned by key directly.  A run frozen before this feature shipped
    # has specimen_keys NULL; it is backfilled from its snapshot at the same time.
    found=set()
    for run in db.scalars(select(Run)):
        snapshot_keys=run.specimen_keys
        if snapshot_keys is None:
            snapshot_keys=sorted(specimens.keys_in_snapshot(run.snapshot))
            run.specimen_keys=snapshot_keys
        if key in {specimens.normalize(k) for k in snapshot_keys}:
            found.add(run.id)
            existing=db.scalar(select(ErasureRun).where(ErasureRun.erasure_id==obj.id,
                                                        ErasureRun.run_id==run.id))
            if not existing:
                # The display name is metadata only; if it quotes the withdrawn
                # number it is stored redacted so the scope report cannot leak
                # the association after the original run is blanked.
                stored=run.name if not specimens.contains(run.name,key) else f'运行 #{run.id}'
                db.add(ErasureRun(erasure_id=obj.id,run_id=run.id,study_id=run.study_id,
                                  run_name=stored,snapshot_rows=len(run.snapshot or []),
                                  result_present=1 if run.result else 0))
    db.flush()
    # Count from the persisted scope, not from this pass's hits: on a resumed
    # erasure the snapshots are already blanked, and the scope must not shrink.
    scoped={r.run_id for r in db.scalars(select(ErasureRun).where(ErasureRun.erasure_id==obj.id))}
    obj.affected_runs=len(scoped)
    obs_step=db.scalar(select(ErasureStep).where(ErasureStep.erasure_id==obj.id,
                                                ErasureStep.name=='observations'))
    observed=sum(1 for o in db.scalars(select(Observation))
                 if specimens.normalize(o.source_key)==key)
    obj.affected_observations=max(observed,obs_step.removed if obs_step else 0)
    obj.prior_exports=sum(1 for a in db.scalars(select(Audit).where(Audit.action=='run.export'))
                          if a.detail.strip().isdigit() and int(a.detail.strip()) in scoped)

def request(db,key,reason,user):
    """Open or resume an erasure.  Returns (object, created)."""
    norm=specimens.normalize(key)
    if not norm: raise ValueError('纸样编号不能为空')
    if len(norm)>100: raise ValueError('纸样编号过长')
    db.commit()   # start the scan from a fresh snapshot
    # Exclusive for the scope capture and tombstone commit: no run may be
    # frozen while the scan enumerates affected analyses.  The commit that
    # writes the tombstone is also the point where PostgreSQL releases the
    # transaction lock, so every later run creation sees the tombstone.
    with hold(db,exclusive=True):
        obj=db.scalar(select(SpecimenErasure).where(SpecimenErasure.specimen_key==norm))
        created=False
        if not obj:
            obj=SpecimenErasure(specimen_key=norm,status='pending',actor=user.email,
                                reason=(reason or '').strip()[:2000],created_at=now(),updated_at=now())
            db.add(obj);db.flush()
            created=True
        elif obj.status=='completed':
            db.commit()
            return obj,False
        for name in STEP_ORDER: _step(db,obj.id,name)
        _record_scope(db,obj,norm)
        obj.updated_at=now()
        if created: log(db,user,'erasure.request',str(obj.id))
        db.commit()
        return obj,created

# ---- cleanup -------------------------------------------------------------

def _leading_int(text):
    head=str(text).split(':',1)[0].strip()
    return int(head) if head.isdigit() else None

def _artifact_path(run_id):
    return ARTIFACT_DIR/f'run-{run_id}.json'

def empty_run(run,erasure_id,key):
    run.snapshot=[]
    run.parameters={}
    run.result=None
    run.error=''
    run.checksum=''
    run.specimen_keys=[]
    run.erased=True
    run.erasure_id=erasure_id
    run.status='erased'
    # A run name typed by an operator may quote the withdrawn number; the id
    # alone is enough to identify the record afterwards.
    if key and specimens.contains(run.name,key):
        run.name=f'运行 #{run.id}（纸样编号已清除）'

def _purge(db,obj):
    key=obj.specimen_key
    erasure_id=obj.id
    run_ids=[r.run_id for r in db.scalars(select(ErasureRun).where(ErasureRun.erasure_id==erasure_id))]
    for name in STEP_ORDER: _step(db,erasure_id,name)

    _mark(db,erasure_id,'tombstone',PURGED,note='纸样编号已封存；重录与重试将被拒绝')

    removed_obs=0
    for row in db.scalars(select(Observation)):
        if specimens.normalize(row.source_key)==key:
            db.delete(row);removed_obs+=1
    db.flush()
    _mark(db,erasure_id,'observations',PURGED,removed=removed_obs,note='该编号登记行已删除')

    queue_removed=queue.remove(run_ids)
    _mark(db,erasure_id,'queued_jobs',PURGED,removed=queue_removed,
          note='队列副本已移除；worker 写回前重新校验状态位')

    scrubbed=0
    for run in db.scalars(select(Run).where(Run.id.in_(run_ids))):
        if run.erased and run.snapshot==[] and run.result is None: continue
        empty_run(run,erasure_id,key);scrubbed+=1
    db.flush()
    _mark(db,erasure_id,'runs',PURGED,removed=scrubbed,
          note='冻结输入、参数、结果、错误与 SHA256 已清空')

    removed_reviews=0
    for review in db.scalars(select(Review).where(Review.run_id.in_(run_ids))):
        db.delete(review);removed_reviews+=1
    db.flush()
    _mark(db,erasure_id,'reviews',PURGED,removed=removed_reviews,
          note='复核意见可能引用原值，随运行一并删除')

    removed_artifacts=0
    for run_id in run_ids:
        path=_artifact_path(run_id)
        if path.exists():
            path.unlink();removed_artifacts+=1
    _mark(db,erasure_id,'artifacts',PURGED,removed=removed_artifacts,
          note='共享产物卷上的可还原结果已删除')

    scrubbed_audits=0
    for row in db.scalars(select(Audit)):
        if row.action in ERASURE_ACTIONS: continue
        # A review note can quote a measured value without naming the paper
        # sample, so every note attached to an affected run is redacted, not
        # only those that happen to contain the number.
        if row.action=='review.approve' and _leading_int(row.detail) in run_ids:
            row.detail=f'{_leading_int(row.detail)}: {REDACTED}';scrubbed_audits+=1
        elif specimens.contains(row.detail,key):
            row.detail=REDACTED;scrubbed_audits+=1
    db.flush()
    _mark(db,erasure_id,'audits',PURGED,removed=scrubbed_audits,
          note='审计明细中的原值已替换为清除标记')

    db.commit()
    return run_ids

# ---- verification --------------------------------------------------------

def _verify(db,obj):
    # The caller holds the exclusive lock.  _purge commits its progress for
    # resumability, so execute() re-enters the lock for this phase.
    key=obj.specimen_key
    erasure_id=obj.id
    run_ids=set(r.run_id for r in db.scalars(select(ErasureRun).where(ErasureRun.erasure_id==erasure_id)))
    problems=[]

    def check(name,ok,detail):
        _mark(db,erasure_id,name,'verified' if ok else 'failed',note=detail)
        if not ok: problems.append(f'{name}: {detail}')

    check('tombstone',bool(blocked_specimen(db,key)),'封存标记存在，重录会被拒绝')

    observations=[o for o in db.scalars(select(Observation)) if specimens.normalize(o.source_key)==key]
    check('observations',not observations,
          f'残留登记 {len(observations)} 行' if observations else '登记表无该编号')

    leaked=[]
    for run in db.scalars(select(Run).where(Run.id.in_(run_ids))):
        if not run.erased or run.snapshot or run.result is not None or run.error or run.parameters or run.checksum \
                or specimens.contains(run.name,key):
            leaked.append(run.id)
    check('runs',not leaked,f'运行 {leaked} 仍含数值' if leaked else f'{len(run_ids)} 个运行已清空')

    queued=[r for r in run_ids if queue.contains(r)]
    check('queued_jobs',not queued,f'队列残留运行 {sorted(queued)}' if queued else '队列无残留副本')

    reviews=db.scalar(select(func.count()).select_from(Review).where(Review.run_id.in_(run_ids))) or 0
    check('reviews',reviews==0,f'残留复核 {reviews} 条' if reviews else '复核记录已清除')

    files=[str(_artifact_path(r)) for r in run_ids if _artifact_path(r).exists()]
    check('artifacts',not files,f'残留产物 {files}' if files else '产物目录无残留')

    audits=[a.id for a in db.scalars(select(Audit))
            if a.action not in ERASURE_ACTIONS and
            (specimens.contains(a.detail,key) or
             (a.action=='review.approve' and _leading_int(a.detail) in run_ids))]
    check('audits',not audits,f'审计仍含原值 {audits}' if audits else '审计明细无原值')

    scope=_scope(db,erasure_id)
    if problems:
        obj.status='failed';obj.error='；'.join(problems)
        _mark(db,erasure_id,'receipt','failed',note=obj.error)
    else:
        obj.status='completed';obj.error=''
        obj.completed_at=obj.completed_at or now()
    receipt=_receipt_payload(db,obj,scope)
    obj.receipt=json.dumps(receipt,ensure_ascii=False,sort_keys=True)
    obj.receipt_sha256=checksum(receipt)
    if not problems:
        _mark(db,erasure_id,'receipt','verified',note='凭据 SHA256 '+obj.receipt_sha256)
        log(db,obj.actor,'erasure.complete',f'{obj.id}: {obj.receipt_sha256}')
    obj.updated_at=now()
    db.commit()
    return obj

def _receipt_payload(db,obj,scope):
    return {
        'erasure_id':obj.id,
        'specimen_number':obj.specimen_key,
        'status':obj.status,
        'requested_by':obj.actor,
        'reason':obj.reason,
        'created_at':obj.created_at,
        'completed_at':obj.completed_at,
        'scope':{'observations_removed':scope.get('observations_removed',0),
                 'runs':[{'run_id':r['run_id'],'study_id':r['study_id'],
                          'snapshot_rows':r['snapshot_rows'],
                          'result_present':r['result_present']} for r in scope.get('runs',[])]},
        'steps':[{'name':s.name,'status':s.status,'removed':s.removed,'verified_at':s.verified_at}
                 for s in db.scalars(select(ErasureStep).where(ErasureStep.erasure_id==obj.id).order_by(ErasureStep.id))],
    }

def execute(db,erasure_id):
    """Cleanup then per-location verification.  Idempotent and resumable."""
    with hold(db,exclusive=True):
        obj=_get(db,erasure_id)
        if obj.status=='completed': return obj
        try:
            _purge(db,obj)
        except Exception as exc:
            db.rollback()
            _record_failure(db,erasure_id,exc)
            raise
    with hold(db,exclusive=True):
        try:
            return _verify(db,_get(db,erasure_id))
        except Exception as exc:
            db.rollback()
            _record_failure(db,erasure_id,exc)
            raise

def _record_failure(db,erasure_id,exc):
    """Keep the withdrawal open and resumable instead of reporting success."""
    obj=_get(db,erasure_id)
    obj.status='failed';obj.error=str(exc)[:2000];obj.updated_at=now()
    row=_step(db,erasure_id,'receipt')
    row.status='failed';row.note='清理中断，可恢复';row.updated_at=now()
    db.commit()

def sweep_artifacts(db):
    """Delete artifacts belonging to tombstoned runs.

    A worker that crashed between writing an artifact and observing the
    tombstone leaves a derived file behind; this sweep runs at startup and on
    every resume so the file cannot outlive the withdrawal.
    """
    blocked={r.run_id for r in db.scalars(select(ErasureRun))}
    removed=0
    for run_id in blocked:
        path=_artifact_path(run_id)
        if path.exists():
            path.unlink();removed+=1
    return removed

def resume_pending(db):
    sweep_artifacts(db)
    ids=[o.id for o in db.scalars(select(SpecimenErasure).where(SpecimenErasure.status.in_(('pending','failed'))))]
    for erasure_id in ids:
        try: execute(db,erasure_id)
        except Exception: pass
    return ids

# ---- presentation --------------------------------------------------------

def current(db,erasure_id):
    # Read-only: a progress view must not create or modify step rows.
    obj=_get(db,erasure_id)
    existing={s.name:s for s in db.scalars(select(ErasureStep).where(ErasureStep.erasure_id==erasure_id))}
    steps=[existing.get(name) or ErasureStep(erasure_id=erasure_id,name=name,status='pending',
                                             removed=0,note='',updated_at=obj.updated_at)
           for name in STEP_ORDER]
    runs=list(db.scalars(select(ErasureRun).where(ErasureRun.erasure_id==erasure_id).order_by(ErasureRun.run_id)))
    return {
        'id':obj.id,'specimen_number':obj.specimen_key,'status':obj.status,'actor':obj.actor,
        'reason':obj.reason,'created_at':obj.created_at,'updated_at':obj.updated_at,
        'completed_at':obj.completed_at,'error':obj.error,
        'affected_observations':obj.affected_observations,'affected_runs':obj.affected_runs,
        'prior_exports':obj.prior_exports,
        'receipt':obj.receipt,'receipt_sha256':obj.receipt_sha256,
        'steps':[{'name':s.name,'label':STEP_LABELS[s.name],'status':s.status,'removed':s.removed,
                  'note':s.note,'verified_at':s.verified_at} for s in steps],
        'runs':[{'run_id':r.run_id,'study_id':r.study_id,'name':r.run_name,
                 'snapshot_rows':r.snapshot_rows,'result_present':bool(r.result_present)} for r in runs],
    }

def list_all(db):
    return [{'id':o.id,'specimen_number':o.specimen_key,'status':o.status,'actor':o.actor,
             'created_at':o.created_at,'completed_at':o.completed_at,
             'affected_runs':o.affected_runs,'affected_observations':o.affected_observations,
             'receipt_sha256':o.receipt_sha256}
            for o in db.scalars(select(SpecimenErasure).order_by(SpecimenErasure.id.desc()))]
