"""python -m processor.cli migrate|seed|serve"""
from __future__ import annotations

import sys
from pathlib import Path

from .config import Settings
from .database import connect


def main(argv: list[str]) -> int:
    s = Settings.from_env()
    cmd = argv[1] if len(argv) > 1 else "serve"
    if cmd == "migrate":
        from . import migrate
        print(migrate.run(s.database_url, approved=s.approved_migrations))
    elif cmd == "seed":
        from . import seed
        with connect(s.database_url) as c:
            print(seed.seed(c, Path(s.seed_dir)))
            c.commit()
    elif cmd == "serve":
        import uvicorn
        from .app import create_app
        uvicorn.run(create_app(s), host="0.0.0.0", port=8080, log_level="info")
    else:
        print("usage: python -m processor.cli [migrate|seed|serve]")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
