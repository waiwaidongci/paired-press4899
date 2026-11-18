from sqlalchemy import Boolean,Column,Integer,String,JSON,Text,ForeignKey
from src.config.database import Base
class PairedRun(Base):
    __tablename__='runs'
    id=Column(Integer,primary_key=True)
    study_id=Column(Integer,ForeignKey('studies.id'),nullable=False)
    name=Column(String(120),nullable=False)
    status=Column(String(20),default='queued')
    parameters=Column(JSON,nullable=False)
    snapshot=Column(JSON,nullable=False)
    checksum=Column(String(64),nullable=False)
    result=Column(JSON,nullable=True)
    error=Column(Text,default='')
    created_at=Column(String(40),nullable=False)
    # Normalized specimen numbers frozen into the snapshot. Used to find every
    # analysis touched by a withdrawal without scanning historical payloads.
    specimen_keys=Column(JSON,nullable=True)
    # Irreversible-erasure tombstone marker. Reads and exports are refused when set.
    erased=Column(Boolean,default=False,nullable=False)
    erasure_id=Column(Integer,nullable=True)
Run=PairedRun
