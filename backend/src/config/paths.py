import os
from pathlib import Path

ARTIFACT_DIR=Path(os.environ.get('ARTIFACT_DIR','/app/artifacts'))
