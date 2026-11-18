from sqlalchemy import Column,Integer,String,Text,UniqueConstraint,ForeignKey
from src.config.database import Base

class SpecimenErasure(Base):
    """A permanent tombstone for a withdrawn specimen number.

    Rows are never deleted: the normalized specimen number must stay blocked so
    that re-registration and late job retries cannot resurrect the data.  The
    stored payload contains only scope metadata, never measurements.
    """
    __tablename__='specimen_erasures'
    id=Column(Integer,primary_key=True)
    specimen_key=Column(String(100),unique=True,nullable=False)
    status=Column(String(20),default='pending',nullable=False)
    actor=Column(String(120),nullable=False)
    reason=Column(Text,nullable=False,default='')
    created_at=Column(String(40),nullable=False)
    updated_at=Column(String(40),nullable=False)
    completed_at=Column(String(40),nullable=True)
    affected_observations=Column(Integer,default=0,nullable=False)
    affected_runs=Column(Integer,default=0,nullable=False)
    prior_exports=Column(Integer,default=0,nullable=False)
    receipt=Column(Text,default='',nullable=False)
    receipt_sha256=Column(String(64),default='',nullable=False)
    error=Column(Text,default='',nullable=False)

class ErasureRun(Base):
    """Run ids captured at erasure request time.

    The rows are written in the same transaction as the tombstone, so they are
    the authoritative block list for readers even before payload cleanup runs.
    Metadata is kept for the scope report; no measurement is stored here.
    """
    __tablename__='erasure_runs'
    __table_args__=(UniqueConstraint('erasure_id','run_id'),)
    id=Column(Integer,primary_key=True)
    erasure_id=Column(Integer,ForeignKey('specimen_erasures.id'),nullable=False)
    run_id=Column(Integer,nullable=False)
    study_id=Column(Integer,nullable=False)
    run_name=Column(String(120),nullable=False)
    snapshot_rows=Column(Integer,default=0,nullable=False)
    result_present=Column(Integer,default=0,nullable=False)

class ErasureStep(Base):
    """Per-location cleanup progress and verification trail."""
    __tablename__='erasure_steps'
    __table_args__=(UniqueConstraint('erasure_id','name'),)
    id=Column(Integer,primary_key=True)
    erasure_id=Column(Integer,ForeignKey('specimen_erasures.id'),nullable=False)
    name=Column(String(40),nullable=False)
    status=Column(String(20),default='pending',nullable=False)
    removed=Column(Integer,default=0,nullable=False)
    note=Column(Text,default='',nullable=False)
    verified_at=Column(String(40),nullable=True)
    updated_at=Column(String(40),nullable=False)
