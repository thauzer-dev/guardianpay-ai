import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from guardianpay.config import load_settings  # noqa: E402
from guardianpay.runtime import bootstrap  # noqa: E402

RAW = ROOT / "data" / "raw"


@pytest.fixture(scope="session")
def template_db(tmp_path_factory):
    """Ingere uma vez; cada teste recebe uma cópia isolada do banco."""
    p = tmp_path_factory.mktemp("tpl") / "template.db"
    rt = bootstrap(load_settings(db_path=p, raw_dir=RAW, ai_proxy_api_key=None), reingest=True)
    rt.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    rt.conn.close()
    return p


@pytest.fixture
def rt(template_db, tmp_path):
    dst = tmp_path / "gp.db"
    src = sqlite3.connect(template_db)
    out = sqlite3.connect(dst)
    src.backup(out)
    src.close(); out.close()
    r = bootstrap(load_settings(db_path=dst, raw_dir=RAW, ai_proxy_api_key=None, demo_mode=True))
    yield r
    r.conn.close()
