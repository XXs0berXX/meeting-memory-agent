"""Helper used while hand-writing the eval set: print turns of a team matching a regex."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from meeting_memory.store import connect  # noqa: E402

team, pat = sys.argv[1], re.compile(sys.argv[2], re.I)
width = int(sys.argv[3]) if len(sys.argv) > 3 else 260
for mid, idx, spk, text in connect().execute(
        "SELECT meeting_id, idx, speaker, text FROM turns WHERE meeting_id LIKE ? ORDER BY meeting_id, idx", (team + "%",)):
    m = pat.search(text)
    if m:
        s = max(0, m.start() - width // 2)
        print(f"{mid}#{idx} {spk}: ...{text[s:s + width]}...")
