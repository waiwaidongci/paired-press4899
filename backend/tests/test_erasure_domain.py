"""End-to-end checks for irreversible specimen erasure (local harness).

Run from backend/: python3 tests/test_erasure_domain.py
Requires fakeredis; SQLite stands in for PostgreSQL.
"""
import os,sys,json
from pathlib import Path

ROOT=Path(__import__('tempfile').mkdtemp(prefix='erasure-harness-'))
DB=ROOT/'test.db'
if DB.exists(): DB.unlink()
ART=ROOT/'artifacts'
for p in ART.glob('*'): p.unlink()
os.environ['DATABASE_URL']='sqlite:///'+str(DB)

os.environ['JWT_SECRET']='test-secret'
# The fake client below is used directly; this URL only satisfies module import.
os.environ['REDIS_URL']='redis://127.0.0.1:16399/0'
os.environ['ARTIFACT_DIR']=str(ART)
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

try:
    import fakeredis
except ImportError:
    print('SKIP: fakeredis 未安装（pip install fakeredis）');raise SystemExit(0)
from src.services import queue
queue._client=fakeredis.FakeRedis(decode_responses=True)

from src.config.database import Base,engine,Session
from src.models.user import User
from src.models.study import Study
from src.models.observation import Observation
from src.models.run import Run
from src.models.review import Review
from src.models.audit import Audit
from src.models import erasure
from src.services import erasures,runs,studies
from src.utils.security import hash_password
from src.services.algorithm import calculate
from src.utils.serializers import now

Base.metadata.create_all(engine)

def mkuser(db,email,role):
    u=db.query(User).filter_by(email=email).first()
    if not u:
        u=User(email=email,role=role,password_hash=hash_password('x'));db.add(u);db.commit()
    return u

def seed(db):
    admin=mkuser(db,'admin@demo.local','admin');analyst=mkuser(db,'analyst@demo.local','analyst');viewer=mkuser(db,'viewer@demo.local','viewer')
    s=Study(name='S1',source_filename='a.csv',description='');s2=Study(name='S2',source_filename='b.csv',description='')
    db.add_all([s,s2]);db.commit()
    for study in (s,s2):
        for i,(key,b,a) in enumerate([('P-1',4.0,3.5),('P-2',5.0,4.4),('P-3',3.0,3.2)]):
            db.add(Observation(study_id=study.id,source_key=key,values={'before':b,'after':a}))
    db.commit()
    return admin,analyst,viewer,s,s2

def make_run(db,study,user,name='R1',execute=True):
    class D:
        study_id=study.id
        name='run-name'
        parameters={'resamples':1000}
    d=D();d.name=name;d.study_id=study.id
    r=runs.create(db,d,user)
    run=db.get(Run,r['id'])
    if execute:
        result=calculate(run.snapshot,run.parameters);result['input_sha256']=run.checksum
        run.result=result;run.status='completed';db.commit()
        ART.mkdir(exist_ok=True)
        (ART/f"run-{run.id}.json").write_text(json.dumps(result,ensure_ascii=False))
    return run

def fresh():
    return Session()

ok=[];bad=[]
def check(name,cond,detail=''):
    (ok if cond else bad).append(name)
    print(('PASS ' if cond else 'FAIL ')+name+((' :: '+str(detail)) if detail and not cond else ''))

with fresh() as db:
    Base.metadata.drop_all(engine);Base.metadata.create_all(engine)
    admin,analyst,viewer,s,s2=seed(db)

    # Two analyses share P-1, in different studies.
    r1=make_run(db,s,analyst,'跨分析A');r2=make_run(db,s2,analyst,'跨分析B')
    # reviews + an export record whose audit detail carries the specimen number
    db.add(Review(run_id=r1.id,note='P-1 数值大概 3.5',author=admin.email,created_at=now()));db.commit()
    db.add(Audit(actor=analyst.email,action='observation.save',detail=f'{s.id}/P-1',created_at=now()))
    db.add(Audit(actor=analyst.email,action='observation.save',detail=f'{s.id}/P-10',created_at=now()))
    db.add(Audit(actor=analyst.email,action='run.export',detail=str(r1.id),created_at=now()));db.commit()
    from src.services.audits import log as audit_log
    audit_log(db,analyst,'run.export',str(r1.id));db.commit()
    r1id,r2id=r1.id,r2.id

with fresh() as db:
    obj,created=erasures.request(db,'  p-1 ','供应方撤回授权',admin)
    check('request creates pending tombstone',created and obj.status=='pending')
    check('scope captures both studies',obj.affected_runs==2,obj.affected_runs)
    check('scope counts shared observations',obj.affected_observations==2,obj.affected_observations)
    check('prior export counted',obj.prior_exports>=1,obj.prior_exports)
    eid=obj.id
    # A new analysis is still possible on the remaining samples, but the
    # withdrawn specimen must not appear in its frozen snapshot.
    nr=make_run(db,db.get(Study,s.id),analyst,'其余样本',execute=False)
    check('new run excludes withdrawn specimen','p-1' not in nr.specimen_keys,nr.specimen_keys)
    check('new run keeps other samples',{'p-2','p-3'}<=set(nr.specimen_keys),nr.specimen_keys)
    # Re-register (letter case / padding / full-width) is refused.
    from fastapi import HTTPException
    for variant in ('P-1','p-1 ','Ｐ-１'):
        try:
            studies.save_observation(db,s.id,type('D',(),{'source_key':variant,'values':{'before':1.0,'after':2.0}})(),analyst,row_id=db.query(Observation).filter_by(study_id=s.id,source_key='P-2').first().id)
            check('re-register blocked '+repr(variant),False)
        except HTTPException as exc:
            check('re-register blocked '+repr(variant),exc.status_code==409,exc.detail)

with fresh() as db:
    # Reads are stopped as soon as the tombstone exists.
    try:
        runs.get(db,r1id);check('read refused before cleanup',False)
    except Exception as exc:
        check('read refused before cleanup','410' in str(exc) or '清除' in str(exc),exc)
    listed={r['id']:r for r in runs.list_runs(db)}
    check('list hides erased analysis',listed[r1id]['status']=='erased' and listed[r1id]['snapshot']==[])
    try:
        runs.export(db,r1id,viewer);check('export refused before cleanup',False)
    except Exception as exc:
        check('export refused before cleanup','清除' in str(exc))

with fresh() as db:
    e=erasures.execute(db,eid)
    check('erasure completes',e.status=='completed',e.error)
    check('receipt sha present',len(e.receipt_sha256)==64)
    rec=json.loads(e.receipt)
    check('receipt omits values',not any(isinstance(v,(int,float)) and k in ('before','after') for row in rec.get('steps',[]) for k,v in row.items()))
    check('all steps verified',all(x['status']=='verified' for x in erasures.current(db,eid)['steps']),
          erasures.current(db,eid)['steps'])
    for rid in (r1id,r2id):
        run=db.get(Run,rid)
        check(f'run {rid} payload blanked',run.snapshot==[] and run.result is None and run.parameters=={} and run.checksum=='' and run.status=='erased')
        check(f'run {rid} artifact removed',not (ART/f'run-{rid}.json').exists())
    check('observations with key removed',not any(o.source_key=='P-1' for o in db.query(Observation)))
    check('unrelated specimens intact',db.query(Observation).filter_by(source_key='P-2').count()==2)
    check('reviews removed',db.query(Review).filter_by(run_id=r1id).count()==0)
    from src.services.specimens import contains as token_contains
    check('audit original value redacted',not any(token_contains(a.detail,'P-1') for a in db.query(Audit)))
    check('audit original value redacted (case-insens)',not any(token_contains(a.detail.lower(),'p-1') for a in db.query(Audit)))
    check('adjacent specimen P-10 audit untouched',db.query(Audit).filter(Audit.detail.like('%/P-10')).count()==1)
    check('receipt audit kept',db.query(Audit).filter_by(action='erasure.complete').count()==1)
    check('review note audit redacted',not any('P-1 数值大概' in a.detail for a in db.query(Audit)))

with fresh() as db:
    # Reads/exports stay closed after completion.
    for rid in (r1id,r2id):
        try: runs.get(db,rid);check(f'read refused after completion {rid}',False)
        except Exception as exc: check(f'read refused after completion {rid}','清除' in str(exc))
        try: runs.get(db,rid);
        except Exception: pass
    try: runs.export(db,r1id,viewer);check('export refused after completion',False)
    except Exception as exc: check('export refused after completion','清除' in str(exc))
    # Old task retry does not revive.
    queue.empty()
    queue.enqueue(r1id)
    import src.worker as W
    W.run_once()
    check('retry leaves run erased',db.get(Run,r1id).result is None and db.get(Run,r1id).status=='erased')
    check('retry does not requeue',not queue.contains(r1id))
    # Re-register after completion still refused.
    try:
        studies.save_observation(db,s.id,type('D',(),{'source_key':'P-1','values':{'before':1.0,'after':2.0}})(),analyst)
        check('re-register blocked after completion',False)
    except Exception as exc:
        check('re-register blocked after completion','撤回' in str(exc) or '清除' in str(exc))
    check('second request idempotent',erasures.request(db,'P-1','again',admin)[0].status=='completed')

# Late calculation write-back: calculate under way, erasure completes meanwhile.
with fresh() as db:
    r3=make_run(db,s,analyst,'晚到计算',execute=False)
    r3id=r3.id
with fresh() as db:
    r3row=db.get(Run,r3id)
    obj,_=erasures.request(db,'P-2','撤回',admin)
    eid2=obj.id
import src.worker as W
real=W.calculate
def racing(snapshot,params):
    with fresh() as db: erasures.execute(db,eid2)
    return real(snapshot,params)
W.calculate=racing
queue.empty()
with fresh() as db: queue.enqueue(r3id)
W.run_once()
W.calculate=real
with fresh() as db:
    r=db.get(Run,r3id)
    check('late calculation does not persist result',r.result is None and r.status=='erased')
    check('late calculation leaves no artifact',not (ART/f'run-{r3id}.json').exists())

# Interrupted cleanup resumes and only completes after verification.
with fresh() as db:
    s3=Study(name='S3',source_filename='c.csv',description='');db.add(s3);db.commit()
    db.add(Observation(study_id=s3.id,source_key='P-9',values={'before':2.0,'after':1.0}))
    db.add(Observation(study_id=s3.id,source_key='P-10',values={'before':3.0,'after':2.0}));db.commit()
    r4=make_run(db,s3,analyst,'中断恢复')
    obj,_=erasures.request(db,'P-9','撤回',admin);eid3=obj.id
    r4id=r4.id
import src.services.erasures as E
real_artifact=E._artifact_path
def boom(rid): raise RuntimeError('磁盘中断')
E._artifact_path=boom
try:
    with fresh() as db: erasures.execute(db,eid3)
except Exception:
    pass
E._artifact_path=real_artifact
with fresh() as db:
    obj=db.get(erasure.SpecimenErasure,eid3)
    check('interrupted erasure stays failed/resumable',obj.status=='failed',obj.status)
    check('interrupted erasure not marked complete',obj.completed_at is None)
with fresh() as db:
    erasures.resume_pending(db)
with fresh() as db:
    obj=db.get(erasure.SpecimenErasure,eid3)
    check('resume completes after verification',obj.status=='completed',obj.error)
    check('resumed run blanked',db.get(Run,r4id).result is None)

print('\n%d passed, %d failed'%(len(ok),len(bad)))
code=1 if bad else 0
if bad: print('FAILED:',bad)
sys.stdout.flush()
os._exit(code)
