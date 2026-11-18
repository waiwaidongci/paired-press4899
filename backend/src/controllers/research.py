from src.services import studies,runs,erasures
# Controllers keep endpoint contracts separate from persistence and calculation.
def overview(db): return {'studies':studies.list_studies(db),'runs':runs.list_runs(db),'erasures':erasures.list_all(db)}
def create_study(db,data,user): return studies.create(db,data,user)
def list_rows(db,id): return studies.observations(db,id)
def save_row(db,id,data,user,row_id=None): return studies.save_observation(db,id,data,user,row_id)
def create_run(db,data,user): return runs.create(db,data,user)
def review_run(db,id,data,user): return runs.approve(db,id,data,user)
def request_erasure(db,data,user): return erasures.request(db,data.specimen_number,data.reason,user)
def execute_erasure(db,id): return erasures.execute(db,id)
def get_erasure(db,id): return erasures.current(db,id)
def list_erasures(db): return erasures.list_all(db)
