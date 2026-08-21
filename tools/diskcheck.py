"""One-shot disk/SQLite probe for the Wispbyte container.

Run instead of the bot when ``connect()`` dies with ``disk I/O error``:
point the panel's ``PY_FILE`` at ``tools/diskcheck.py``, restart, copy the
output, then point it back at ``bot.py``. Touches nothing destructive -- the
only file it creates is a probe it deletes again.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB = DATA / "deezee.db"


def say(label: str, value: object) -> None:
    print(f"{label:<28} {value}")


def rule(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 46 - len(title)))


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


rule("environment")
say("python", sys.version.split()[0])
say("cwd", Path.cwd())
say("root", ROOT)
say("data dir exists", DATA.is_dir())

rule("filesystem")
target = str(DATA if DATA.is_dir() else ROOT)
if hasattr(os, "statvfs"):  # absent on Windows; the container is Linux
    try:
        st = os.statvfs(target)
        say("free space", human(st.f_bavail * st.f_frsize))
        say("total space", human(st.f_blocks * st.f_frsize))
        # A container can sit far below its byte quota and still be unable to
        # create a file: inodes are a separate, smaller budget.
        say("free inodes", st.f_favail if st.f_favail >= 0 else "n/a")
        say("total inodes", st.f_files if st.f_files >= 0 else "n/a")
        say("mounted read-only", bool(st.f_flag & os.ST_RDONLY))
    except OSError as exc:
        say("statvfs failed", exc)
else:
    usage = shutil.disk_usage(target)
    say("free space", human(usage.free))
    say("total space", human(usage.total))
    say("inodes", "n/a on this platform")

rule("data/ contents")
if DATA.is_dir():
    for entry in sorted(DATA.iterdir()):
        try:
            info = entry.stat()
            say(entry.name, f"{human(info.st_size):>10}  mode={oct(info.st_mode)[-3:]}")
        except OSError as exc:
            say(entry.name, f"stat failed: {exc}")
else:
    say("data/", "MISSING")

rule("write probe (directory level)")
probe = DATA / ".diskcheck_probe"
try:
    with probe.open("wb") as fh:
        fh.write(b"x" * 4096)
        fh.flush()
        # fsync, not just flush: a quota or read-only mount often only bites
        # when the page cache is forced out to the real filesystem.
        os.fsync(fh.fileno())
    say("create+fsync in data/", "OK")
    probe.unlink()
    say("delete probe", "OK")
except OSError as exc:
    say("create in data/", f"FAILED: {exc}")

rule("sqlite probe (fresh file)")
fresh = DATA / ".diskcheck_fresh.db"
try:
    con = sqlite3.connect(str(fresh))
    mode = con.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    con.execute("CREATE TABLE t (a INTEGER)")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    con.close()
    say("new db + WAL", f"OK (journal_mode={mode})")
except sqlite3.Error as exc:
    say("new db + WAL", f"FAILED: {exc}")
finally:
    for suffix in ("", "-wal", "-shm"):
        Path(str(fresh) + suffix).unlink(missing_ok=True)

rule("sqlite probe (real database)")
if not DB.exists():
    say("deezee.db", "MISSING")
else:
    try:
        ro = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        say("open read-only", "OK")
        say("integrity_check", ro.execute("PRAGMA quick_check").fetchone()[0])
        say("journal_mode", ro.execute("PRAGMA journal_mode").fetchone()[0])
        say("page_count", ro.execute("PRAGMA page_count").fetchone()[0])
        ro.close()
    except sqlite3.Error as exc:
        say("read-only open", f"FAILED: {exc}")

    try:
        rw = sqlite3.connect(str(DB))
        mode = rw.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        say("set WAL (the failing step)", f"OK (journal_mode={mode})")
        rw.close()
    except sqlite3.Error as exc:
        say("set WAL (the failing step)", f"FAILED: {exc}")

print("\ndone -- set PY_FILE back to bot.py before restarting.")
