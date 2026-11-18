from pydantic import BaseModel,Field

class ErasureInput(BaseModel):
    specimen_number:str=Field(min_length=1,max_length=100)
    reason:str=Field(default='',max_length=2000)
