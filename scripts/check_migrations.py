"""Fail when the models have drifted from the migration chain.

Usage (CI does exactly this):

    alembic upgrade head      # against a scratch database (DATABASE_URL)
    python scripts/check_migrations.py

Compares the live schema against Base.metadata with alembic's autogenerate
engine; any diff means someone changed app/models.py without running
`alembic revision --autogenerate`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext

from app import models  # noqa: F401  (register tables on Base.metadata)
from app.db import Base, engine


def main() -> int:
    with engine.connect() as conn:
        context = MigrationContext.configure(conn)
        diffs = compare_metadata(context, Base.metadata)
    if diffs:
        print(
            "models and migrations have diverged; run "
            "`alembic revision --autogenerate -m <what changed>` and commit it:"
        )
        for diff in diffs:
            print(f"  {diff}")
        return 1
    print("migrations match models")
    return 0


if __name__ == "__main__":
    sys.exit(main())
