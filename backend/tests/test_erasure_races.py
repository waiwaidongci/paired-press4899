"""Ordering checks: concurrent freeze, late calculation, and in-flight reads.

Each scenario is deterministic on SQLite because the erasure lock becomes a
process-wide reader/writer lock (see src/utils/locks.py).  PostgreSQL uses the
same shared/exclusive advisory-lock ordering.
"""
import os,sys,threading,time
from pathlib import Path

ROOT=Path(__import__('tempfile').mkdtemp(prefix='erasure-race-'))
os.environ['DATABASE_URL']='sqlite:///'+str(ROOT/'race.db')
os.environ['REDIS_URL']='redis://127.0.0.1:16399/0'
os.environ['JWT_SECRET']='test-secret'
os.environ['ARTIFACT_DIR']=str(ROOT/'artifacts')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

try:
    import fakeredis
except ImportError:
    print('SKIP: fakeredis 未安装（pip install fakeredis）');raise SystemExit(0)

from src.services import queue
queue._client=fakeredis.FakeRedis(decode_responses=True)
from src.config.database import Base,engine,Session
# Import model modules so create_all sees every table.
from src.models import user,study,observation,run,review,audit,erasure  # noqa: F401
from src.models.review import Review
Base.metadata.create_all(engine)
from src.models.study import Study
from src.models.observation import Observation
from src.models.run import Run
from src.models.user import User
from src.utils.security import hash_password
from src.services import erasures,runs
from src.services.algorithm import calculate
import src.worker as W

ok=[];bad=[]
def check(name,cond,detail=''):
    (ok if cond else bad).append(name)
    print(('PASS ' if cond else 'FAIL ')+name+((' :: '+str(detail)) if detail and not cond else ''))

ART=ROOT/'artifacts'

class D:
    def __init__(self,study_id,name='race',parameters=None):
        self.study_id=study_id;self.name=name;self.parameters=parameters or {'resamples':200}

def setup():
    with Session() as db:
        db.query(erasure.ErasureStep).delete();db.query(erasure.ErasureRun).delete()
        db.query(erasure.SpecimenErasure).delete()
        db.query(Review).delete();db.query(Run).delete()
        db.query(Observation).delete();db.query(Study).delete();db.query(User).delete()
        db.commit()
        u=User(email='a@x',role='admin',password_hash=hash_password('x'));db.add(u)
        s=Study(name='S',source_filename='f.csv',description='');db.add(s);db.commit()
        for i in range(4):
            db.add(Observation(study_id=s.id,source_key=f'K-{i}',values={'before':3.0+i,'after':2.0+i}))
        db.commit();return u.email,s.id

def completed_run(db,study_id,user_email):
    rid=runs.create(db,D(study_id,'done'),db.query(User).first())['id']
    r=db.get(Run,rid);res=calculate(r.snapshot,r.parameters)
    r.result=res;r.status='completed';db.commit()
    return rid

# 1. A run frozen concurrently with the scope scan is still captured.
setup()
created=[];errors=[]
def maker(sid):
    with Session() as db:
        try: created.append(runs.create(db,D(sid,'concurrent'),db.query(User).first())['id'])
        except Exception as exc: errors.append(str(exc))
with Session() as db: sid=db.query(Study).first().id;admin_email=db.query(User).first().email
t=threading.Thread(target=maker,args=(sid,));t.start()
with Session() as db:
    obj,_=erasures.request(db,'K-1','撤回',db.query(User).first())
    scope={x.run_id for x in db.query(erasure.ErasureRun).filter_by(erasure_id=obj.id)}
    erasures.execute(db,obj.id)
t.join()
check('concurrent run captured by scope',set(created)<=scope,(created,scope))
with Session() as db:
    check('concurrent run erased',all(db.get(Run,x).erased for x in created))

# 2. A calculation that finishes after the withdrawal must not persist.
setup()
with Session() as db:
    sid=db.query(Study).first().id;user=db.query(User).first()
    rid=runs.create(db,D(sid,'late'),user)['id']
with Session() as db:
    obj,_=erasures.request(db,'K-2','撤回',db.query(User).first());eid=obj.id
real=W.calculate
def racing(snapshot,params):
    with Session() as db: erasures.execute(db,eid)
    return real(snapshot,params)
W.calculate=racing
queue.empty();queue.enqueue(rid)
W.run_once()
W.calculate=real
with Session() as db:
    r=db.get(Run,rid)
    check('late calculation discarded',r.result is None and r.status=='erased' and r.erased)
    check('late calculation wrote no artifact',not (ART/f'run-{rid}.json').exists())

# 3. An in-flight read completes before the tombstone, a later read is refused.
setup()
with Session() as db:
    sid=db.query(Study).first().id;user=db.query(User).first()
    rid=completed_run(db,sid,user.email)
seen=[]
from src.utils.locks import hold
started=threading.Event()
def reader():
    with Session() as db:
        with hold(db):
            started.set();time.sleep(0.25)
            seen.append(runs.get(db,rid)['result'] is not None)
t=threading.Thread(target=reader);t.start()
started.wait(2)
with Session() as db:
    obj,_=erasures.request(db,'K-3','撤回',db.query(User).first())
    erasures.execute(db,obj.id)
t.join()
with Session() as db:
    check('read completed before tombstone sees data',seen==[True],seen)
    try: runs.get(db,rid);check('read after erasure refused',False)
    except Exception as exc: check('read after erasure refused',getattr(exc,'status_code',None)==410)

print('\n%d passed, %d failed'%(len(ok),len(bad)))
code=1 if bad else 0
if bad: print('FAILED:',bad)
sys.stdout.flush();os._exit(code)
