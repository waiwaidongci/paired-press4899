import logging
from fastapi import APIRouter,Depends,HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from src.config.database import get_db
from src.config.domain import DOMAIN
from src.middlewares.auth import current_user,writer,admin
from src.schemas.study import StudyInput
from src.schemas.observation import ObservationInput
from src.schemas.run import RunInput
from src.schemas.review import ReviewInput
from src.schemas.erasure import ErasureInput
from src.controllers import research
from src.services import runs,studies,erasures
from src.models.audit import Audit
from src.models.observation import Observation
from src.services.audits import log
from src.utils.serializers import record
router=APIRouter()
@router.get('/domain')
def domain(user=Depends(current_user)): return DOMAIN
@router.get('/overview')
def overview(db=Depends(get_db),user=Depends(current_user)): return research.overview(db)
@router.get('/studies')
def list_studies(db=Depends(get_db),user=Depends(current_user)): return studies.list_studies(db)
@router.post('/studies')
def create_study(data:StudyInput,db=Depends(get_db),user=Depends(writer)): return research.create_study(db,data,user)
@router.get('/studies/{id}/observations')
def rows(id:int,db=Depends(get_db),user=Depends(current_user)): return research.list_rows(db,id)
@router.post('/studies/{id}/observations')
def add(id:int,data:ObservationInput,db=Depends(get_db),user=Depends(writer)): return research.save_row(db,id,data,user)
@router.put('/studies/{id}/observations/{row_id}')
def update(id:int,row_id:int,data:ObservationInput,db=Depends(get_db),user=Depends(writer)): return research.save_row(db,id,data,user,row_id)
@router.delete('/studies/{id}/observations/{row_id}')
def remove(id:int,row_id:int,db=Depends(get_db),user=Depends(writer)):
    row=db.get(Observation,row_id)
    if not row or row.study_id!=id: raise HTTPException(404,'记录不存在')
    db.delete(row);log(db,user,'observation.delete',row_id);db.commit();return {'deleted':True}
@router.get('/runs')
def list_runs(db=Depends(get_db),user=Depends(current_user)): return runs.list_runs(db)
@router.post('/runs')
def create_run(data:RunInput,db=Depends(get_db),user=Depends(writer)): return research.create_run(db,data,user)
@router.get('/runs/{id}')
def get_run(id:int,db=Depends(get_db),user=Depends(current_user)): return runs.get(db,id)
@router.post('/runs/{id}/review')
def review(id:int,data:ReviewInput,db=Depends(get_db),user=Depends(admin)): return research.review_run(db,id,data,user)
@router.get('/runs/{id}/export')
def export(id:int,db=Depends(get_db),user=Depends(current_user)):
    return Response(content=runs.export(db,id,user).encode('utf-8'),media_type='text/csv',headers={'Content-Disposition':f'attachment; filename="research-{id}.csv"'})
# Withdrawal of measurement authorization.  Only the study lead may erase; the
# tombstone is written synchronously and cleanup continues per-location.
@router.post('/erasures')
def request_erasure(data:ErasureInput,db=Depends(get_db),user=Depends(admin)):
    try: obj,_=research.request_erasure(db,data,user)
    except ValueError as exc: raise HTTPException(422,str(exc))
    # Tombstone and scope are durable before any cleanup starts; reads and new
    # analyses are already blocked.  Cleanup runs here, and a failure leaves a
    # resumable record instead of a half-open withdrawal.
    try: erasures.execute(db,obj.id)
    except Exception: db.rollback()
    return research.get_erasure(db,obj.id)
@router.get('/erasures')
def list_erasures(db=Depends(get_db),user=Depends(current_user)): return research.list_erasures(db)
@router.get('/erasures/{id}')
def get_erasure(id:int,db=Depends(get_db),user=Depends(current_user)): return research.get_erasure(db,id)
@router.post('/erasures/{id}/execute')
def execute_erasure(id:int,db=Depends(get_db),user=Depends(admin)):
    try: erasures.execute(db,id)
    except LookupError: raise HTTPException(404,'清除任务不存在')
    except Exception:
        # A resumable failure is reported as progress, not as a lost request:
        # the receipt and per-location status stay visible to the operator.
        logging.exception('erasure pass failed')
    return research.get_erasure(db,id)
@router.get('/audits')
def audits(db=Depends(get_db),user=Depends(current_user)): return [record(x) for x in db.scalars(select(Audit).order_by(Audit.id.desc()).limit(100))]
