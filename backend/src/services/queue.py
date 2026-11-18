import os
from redis import Redis

QUEUE='analysis-queue'
_client=None

def client():
    global _client
    if _client is None:
        _client=Redis.from_url(os.environ['REDIS_URL'],decode_responses=True)
    return _client

def reset():
    """Drop the cached client (tests and configuration reloads)."""
    global _client
    _client=None

def enqueue(run_id):
    client().rpush(QUEUE,str(int(run_id)))

def dequeue(timeout=2):
    item=client().blpop(QUEUE,timeout=timeout)
    return item[1] if item else None

def remove(run_ids):
    removed=0
    for run_id in run_ids:
        removed+=int(client().lrem(QUEUE,0,str(int(run_id))) or 0)
    return removed

def contains(run_id):
    return client().lpos(QUEUE,str(int(run_id))) is not None

def empty():
    client().delete(QUEUE)
