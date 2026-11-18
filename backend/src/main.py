import os,logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from sqlalchemy import text
from src.config.database import Base,engine,Session
from src.config.migrations import upgrade
# Importing the model modules registers their tables on Base.metadata.
from src.models import user,study,observation,run,review,audit,erasure  # noqa: F401
from src.routes import auth,research
from src.middlewares.errors import ErrorMiddleware
from src.middlewares.rate_limit import RateLimitMiddleware
from src.services import erasures,queue as job_queue

@asynccontextmanager
async def lifespan(app):
    Base.metadata.create_all(engine)
    upgrade(engine)
    if os.environ.get('SEED_DEMO')=='true':
        try:
            from scripts.seed_demo import seed
            seed()
        except Exception: logging.exception('seed failed; manual seed available')
    # Resume cleanup that was interrupted (crash, deploy, or a failed pass) so a
    # withdrawal never stays half-applied just because nobody pressed resume.
    try:
        with Session() as db: erasures.resume_pending(db)
    except Exception: logging.exception('erasure resume failed')
    try:
        job_queue.client().ping()
    except Exception: logging.exception('queue unavailable; resume endpoint can requeue')
    yield
app=FastAPI(lifespan=lifespan,title='Research Application')
app.add_middleware(ErrorMiddleware)
app.add_middleware(RateLimitMiddleware)
app.include_router(auth.router);app.include_router(research.router)
@app.get('/health')
def health():
    with engine.connect() as conn: conn.execute(text('SELECT 1'))
    queue_ping()
    return {'status':'healthy'}
def queue_ping():
    from redis import Redis
    Redis.from_url(os.environ['REDIS_URL']).ping()
