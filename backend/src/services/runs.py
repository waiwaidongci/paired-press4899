import json,csv,io
from sqlalchemy import select
from fastapi import HTTPException
from src.models.run import Run
from src.models.observation import Observation
from src.models.review import Review
from src.services import studies,erasures,specimens,queue
from src.services.domain_validation import validate_parameters
from src.services.audits import log
from src.utils.locks import hold
from src.utils.serializers import record,now,checksum

def create(db,data,user):
    # Shared erasure lock: the snapshot cannot be frozen from a study view that
    # predates a tombstone, and an erasure scope scan cannot miss this run.
    with hold(db):
        studies.require(db,data.study_id)
        rows=studies.observations(db,data.study_id)
        if not rows:
            remaining=db.scalar(select(Observation.id).where(Observation.study_id==data.study_id))
            if remaining: raise HTTPException(409,'该批次的纸样编号均已撤回授权，无法据此创建分析')
            raise HTTPException(422,'先录入至少一条业务观测')
        try: params=validate_parameters(data.parameters)
        except (ValueError,TypeError) as e: raise HTTPException(422,str(e))
        snapshot=[{'source_key':x['source_key'],'values':x['values']} for x in rows]
        keys=sorted(specimens.keys_in_snapshot(snapshot))
        withdrawn=erasures.pending_for_snapshot(db,keys)
        if withdrawn: raise HTTPException(409,'快照含已撤回授权的纸样编号，无法创建分析')
        run=Run(study_id=data.study_id,name=data.name,parameters=params,snapshot=snapshot,
                checksum=checksum(snapshot),created_at=now(),status='queued',specimen_keys=keys)
        db.add(run);db.flush();log(db,user,'run.queue',str(run.id));db.commit()
    queue.enqueue(run.id)
    return record(run)

def get(db,id):
    # A read either completes before the tombstone is written or observes it and
    # is refused, so an old response can never be produced after the erasure.
    with hold(db):
        run=db.get(Run,id)
        if not run: raise HTTPException(404,'运行不存在')
        if erasures.run_blocked(db,run): raise HTTPException(410,'该运行涉及已撤回授权的纸样，数值已不可恢复地清除')
        result=record(run);review=db.scalar(select(Review).where(Review.run_id==id))
        result['review']=record(review) if review else None
        return result

def list_runs(db):
    with hold(db):
        data=[]
        for run in db.scalars(select(Run).order_by(Run.id.desc())):
            if erasures.run_blocked(db,run):
                mapping=db.scalar(select(erasures.ErasureRun.erasure_id).where(erasures.ErasureRun.run_id==run.id))
                data.append({'id':run.id,'study_id':run.study_id,'name':run.name,'status':'erased',
                             'parameters':{},'snapshot':[],'checksum':'','result':None,'error':'',
                             'created_at':run.created_at,'review':None,'erased':True,
                             'erasure_id':run.erasure_id or mapping})
            else:
                result=record(run);review=db.scalar(select(Review).where(Review.run_id==run.id))
                result['review']=record(review) if review else None
                data.append(result)
        return data

def approve(db,id,data,user):
    with hold(db):
        run=db.get(Run,id)
        if not run: raise HTTPException(404,'运行不存在')
        if erasures.run_blocked(db,run): raise HTTPException(410,'该运行已被清除，不能复核')
        if run.status!='completed': raise HTTPException(409,'只能复核已完成运行')
        if db.scalar(select(Review).where(Review.run_id==id)): raise HTTPException(409,'已复核，记录不可覆盖')
        obj=Review(run_id=id,note=data.note,author=user.email,created_at=now());db.add(obj);log(db,user,'review.approve',f'{id}: {data.note}');db.commit();return record(obj)

def export(db,id,user):
    with hold(db):
        r=get(db,id)
        if r['status']!='completed': raise HTTPException(409,'尚无完成结果')
        if user.role=='viewer' and not r['review']: raise HTTPException(403,'观察员只能导出已复核结果')
        stream=io.StringIO();w=csv.writer(stream);w.writerow(['运行编号','输入校验SHA256','指标','值'])
        for k,v in r['result']['metrics'].items(): w.writerow([id,r['checksum'],k,json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v])
        w.writerow([id,r['checksum'],'解释',r['result']['interpretation']]);log(db,user,'run.export',id);db.commit()
        return '﻿'+stream.getvalue()
