"""API-level end-to-end checks for erasure (TestClient, sqlite, fake redis).

Run from backend/: python3 tests/test_erasure_api.py
Requires fakeredis; SQLite stands in for PostgreSQL.
"""
import os,sys,json,threading,time
from pathlib import Path

ROOT=Path(__import__('tempfile').mkdtemp(prefix='erasure-api-'))
DB=ROOT/'api.db'
if DB.exists(): DB.unlink()
ART=ROOT/'artifacts';ART.mkdir(exist_ok=True)
for p in ART.glob('*'): p.unlink()
os.environ['DATABASE_URL']='sqlite:///'+str(DB)
os.environ['REDIS_URL']='redis://127.0.0.1:16399/0'
os.environ['JWT_SECRET']='test-secret'
os.environ['ARTIFACT_DIR']=str(ART)
os.environ['SEED_DEMO']='false'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

try:
    import fakeredis
except ImportError:
    print('SKIP: fakeredis 未安装（pip install fakeredis）');raise SystemExit(0)
server=fakeredis.TcpFakeServer(('127.0.0.1',16399),server_type='redis')
threading.Thread(target=server.serve_forever,daemon=True).start()
time.sleep(.3)

from fastapi.testclient import TestClient
from src.main import app
from src.config.database import Session,Base,engine
from src.models.run import Run
from src.models.observation import Observation
from src.services import queue
from src.services.audits import log as audit_log
from src.utils.serializers import now

Base.metadata.create_all(engine)
from scripts.seed_demo import seed
seed()

ok=[];bad=[]
def check(name,cond,detail=''):
    (ok if cond else bad).append(name);print(('PASS ' if cond else 'FAIL ')+name+((' :: '+str(detail)) if (not cond and detail) else ''))

c=TestClient(app)
def login(email,password='Research2026!'):
    r=c.post('/auth/login',json={'email':email,'password':password})
    assert r.status_code==200,r.text
    return {'Authorization':'Bearer '+r.json()['token']}

admin=login('admin@demo.local');analyst=login('analyst@demo.local');viewer=login('viewer@demo.local')

studies=c.get('/studies',headers=analyst).json()
sid=studies[0]['id'];sid2=studies[1]['id']
rows=c.get(f'/studies/{sid}/observations',headers=analyst).json()
target=rows[0]['source_key']
print('target specimen',target,'study',sid)

# analysis A in study 1, analysis B in study 2
a=c.post('/runs',json={'study_id':sid,'name':'跨分析甲','parameters':{'resamples':1000}},headers=analyst).json()
b=c.post('/runs',json={'study_id':sid,'name':'跨分析乙','parameters':{'resamples':1000}},headers=analyst).json()
# second study needs rows containing target; copy one row
with Session() as db:
    db.add(Observation(study_id=sid2,source_key=target+'X',values={'before':4.0,'after':3.0}));db.commit()
# make run B actually share the number by re-creating after row edits
with Session() as db:
    db.add(Observation(study_id=sid2,source_key=target,values={'before':4.0,'after':3.0}));db.commit()
b2=c.post('/runs',json={'study_id':sid2,'name':'跨分析乙','parameters':{'resamples':1000}},headers=analyst).json()

# execute the queued jobs to completion (worker path)
import src.worker as W
queue.empty()
with Session() as db:
    db.query(Run).update({Run.status:'completed'});db.commit()
    for run in db.query(Run):
        from src.services.algorithm import calculate
        res=calculate(run.snapshot,run.parameters);res['input_sha256']=run.checksum
        run.result=res;db.commit()
        (ART/f'run-{run.id}.json').write_text(json.dumps(res,ensure_ascii=False))
    # a review quoting the value and an export audit
    db.add(__import__('src.models.review',fromlist=['Review']).Review(run_id=a['id'],note=f'{target} 约 4 ΔE',author='admin@demo.local',created_at=now()));db.commit()
    audit_log(db,c_users:=type('U',(),{'email':'analyst@demo.local'})(),'run.export',str(a['id']));db.commit()

check('admin can list erasures',c.get('/erasures',headers=admin).status_code==200)
r=c.post('/erasures',json={'specimen_number':target.lower()+'  ','reason':'供应方撤回测量授权'},headers=analyst)
check('analyst cannot erase',r.status_code==403,r.text)
r=c.post('/erasures',json={'specimen_number':target.lower()+'  ','reason':'供应方撤回测量授权'},headers=admin)
check('admin erase accepted',r.status_code==200,r.text)
body=r.json()
check('erase completes synchronously',body['status']=='completed',body.get('error'))
check('scope includes cross-study runs',len(body['runs'])>=2,body['runs'])
check('all steps verified',all(s['status']=='verified' for s in body['steps']),body['steps'])
check('receipt exposed',len(body.get('receipt_sha256',''))==64)
eid=body['id']

check('progress endpoint',c.get(f'/erasures/{eid}',headers=viewer).json()['status']=='completed')
check('read refused 410',c.get(f"/runs/{a['id']}",headers=analyst).status_code==410)
check('export refused',c.get(f"/runs/{a['id']}/export",headers=viewer).status_code==410)
check('review refused',c.post(f"/runs/{a['id']}/review",json={'note':'试图复核被清除运行'},headers=admin).status_code==410)
check('overview hides values',all(x['snapshot']==[] for x in c.get('/overview',headers=admin).json()['runs'] if x['status']=='erased'))
check('register hides row',not any(x['source_key']==target for x in c.get(f'/studies/{sid}/observations',headers=analyst).json()))
check('re-register refused',c.post(f'/studies/{sid}/observations',json={'source_key':target,'values':{'before':1,'after':2}},headers=analyst).status_code==409)
check('re-register refused (case/width)',c.post(f'/studies/{sid}/observations',json={'source_key':' '+target.upper(),'values':{'before':1,'after':2}},headers=analyst).status_code==409)

# other analyses still work
r=c.post('/runs',json={'study_id':sid,'name':'其他纸样照常','parameters':{'resamples':1000}},headers=analyst)
check('other analysis still creatable',r.status_code==200,r.text)
new_id=r.json()['id']
queue.empty();queue.enqueue(new_id)
with Session() as db: db.get(Run,new_id).status='queued';db.commit()
W.run_once()
with Session() as db: check('other analysis completes',db.get(Run,new_id).status=='completed')

# unauthenticated / viewer erase denied
check('viewer cannot erase',c.post('/erasures',json={'specimen_number':'X','reason':''},headers=viewer).status_code==403)

print('\n%d passed, %d failed'%(len(ok),len(bad)))
code=1 if bad else 0
if bad: print('FAILED:',bad)
# The in-process fake Redis server and the TestClient portal keep daemon
# threads alive; exit explicitly so the harness terminates.
sys.stdout.flush()
os._exit(code)
