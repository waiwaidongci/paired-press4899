from fastapi import HTTPException
from sqlalchemy import select,func
from src.models.study import Study
from src.models.observation import Observation
from src.models.erasure import SpecimenErasure
from src.services.domain_validation import validate_values
from src.services.audits import log
from src.utils.locks import hold
from src.utils.serializers import record
from src.services import specimens

def unseen_specimens(db):
    """Specimen numbers whose authorization is withdrawn (or being withdrawn).

    Read filtering uses the tombstone, not the cleanup progress, so a register
    read is blocked the moment the withdrawal is recorded even if the purge has
    not finished or was interrupted.
    """
    return {row.specimen_key for row in db.scalars(select(SpecimenErasure))}

def list_studies(db):
    with hold(db):
        hidden=unseen_specimens(db)
        data=[]
        for s in db.scalars(select(Study).order_by(Study.id)):
            x=record(s)
            x['row_count']=sum(1 for o in db.scalars(select(Observation).where(Observation.study_id==s.id))
                               if specimens.normalize(o.source_key) not in hidden)
            data.append(x)
        return data

def create(db,data,user):
    if db.scalar(select(Study).where(Study.name==data.name)): raise HTTPException(409,'批次名称已存在')
    s=Study(**data.model_dump());db.add(s);log(db,user,'study.create',data.name);db.commit();return record(s)

def require(db,id):
    s=db.get(Study,id)
    if not s: raise HTTPException(404,'业务批次不存在')
    return s

def observations(db,id):
    # A source-register read cannot be produced from a payload that a
    # concurrent withdrawal is about to purge, and rows for a withdrawn
    # specimen are hidden immediately even before cleanup runs.
    with hold(db):
        require(db,id)
        hidden=unseen_specimens(db)
        return [record(x) for x in db.scalars(select(Observation).where(Observation.study_id==id).order_by(Observation.id))
                if specimens.normalize(x.source_key) not in hidden]

def withdrawn(db,source_key):
    """A specimen number withdrawn by its supplier can never be registered again.

    Matching is by the normalized number, so padding, full-width forms and letter
    case cannot smuggle the same paper sample back in.  Erasures still being
    cleaned also block, which prevents a concurrent re-entry racing the purge.
    """
    return db.scalar(select(SpecimenErasure).where(SpecimenErasure.specimen_key==specimens.normalize(source_key)))

def save_observation(db,id,data,user,row_id=None):
    with hold(db):
        require(db,id)
        try: values=validate_values(data.values)
        except (ValueError,TypeError) as e: raise HTTPException(422,str(e))
        if withdrawn(db,data.source_key): raise HTTPException(409,'该纸样编号已按供应方撤回授权清除，不能重新登记')
        other=db.scalar(select(Observation).where(Observation.study_id==id,Observation.source_key==data.source_key))
        if other and other.id!=row_id: raise HTTPException(409,'源记录编号重复')
        if row_id:
            row=db.get(Observation,row_id)
            if not row or row.study_id!=id: raise HTTPException(404,'记录不存在')
            row.source_key=data.source_key;row.values=values
        else:
            existing=db.scalar(select(func.count()).select_from(Observation).where(Observation.study_id==id)) or 0
            if existing>=5000: raise HTTPException(422,'小型研究最多5000行')
            row=Observation(study_id=id,source_key=data.source_key,values=values);db.add(row)
        log(db,user,'observation.save',f'{id}/{data.source_key}');db.commit();return record(row)
