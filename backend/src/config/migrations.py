"""Additive schema upgrades for databases created by earlier versions.

The project deliberately ships no destructive migration framework; schema v1 is
created by ``Base.metadata.create_all``.  The withdrawal feature adds nullable
columns and new tables, so an existing ``runs`` table is upgraded in place with
``ADD COLUMN IF NOT EXISTS`` (PostgreSQL) or a guarded ``ALTER TABLE`` (SQLite).
No column, row or value is dropped.
"""
from sqlalchemy import inspect,text

COLUMNS={
    'runs':[
        ('specimen_keys','JSON'),
        ('erased','BOOLEAN DEFAULT FALSE NOT NULL'),
        ('erasure_id','INTEGER'),
    ],
}

def upgrade(engine):
    if engine.dialect.name=='postgresql':
        with engine.begin() as conn:
            for table,columns in COLUMNS.items():
                for name,ddl in columns:
                    conn.execute(text(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {ddl}'))
    elif engine.dialect.name=='sqlite':
        with engine.begin() as conn:
            for table,columns in COLUMNS.items():
                existing={c['name'] for c in inspect(conn).get_columns(table)}
                for name,ddl in columns:
                    if name not in existing:
                        conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))
