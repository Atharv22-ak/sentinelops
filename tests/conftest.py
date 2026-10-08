import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in ("services/common", "services/processor", "services/ingest_api", "services/alert_service"):
    sys.path.insert(0, str(ROOT / p))
