"""Transcript parsing. Pure code: the file format is regular, so no model is involved."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

TURN_HEADER = re.compile(r"^(\d{2}):(\d{2}):(\d{2}) Speaker ([A-Z])\s*$")
MEETING_ID = re.compile(r"^([A-Z]{2}\d{4})([a-z])$")


@dataclass
class Turn:
    idx: int
    t_sec: int
    t_str: str
    speaker: str
    text: str

    @property
    def n_words(self) -> int:
        return len(self.text.split())


@dataclass
class Meeting:
    meeting_id: str
    team: str
    letter: str
    seq: int
    path: str
    sha1: str
    turns: list[Turn] = field(default_factory=list)

    @property
    def n_words(self) -> int:
        return sum(t.n_words for t in self.turns)


def parse_meeting_id(meeting_id: str) -> tuple[str, str, int]:
    m = MEETING_ID.match(meeting_id)
    if not m:
        raise ValueError(f"bad meeting id {meeting_id!r}")
    team, letter = m.group(1), m.group(2)
    return team, letter, ord(letter) - ord("a") + 1


def parse_transcript(path: Path) -> Meeting:
    raw = path.read_text(encoding="utf-8")
    meeting_id = path.stem
    team, letter, seq = parse_meeting_id(meeting_id)
    meeting = Meeting(meeting_id, team, letter, seq, str(path), hashlib.sha1(raw.encode()).hexdigest())

    cur: dict | None = None
    lines: list[str] = []

    def flush():
        if cur is not None:
            text = " ".join(l.strip() for l in lines if l.strip())
            if text:
                meeting.turns.append(Turn(len(meeting.turns), cur["t_sec"], cur["t_str"], cur["speaker"], text))

    for line in raw.splitlines():
        m = TURN_HEADER.match(line)
        if m:
            flush()
            h, mi, s, spk = m.groups()
            cur = {"t_sec": int(h) * 3600 + int(mi) * 60 + int(s), "t_str": f"{h}:{mi}:{s}", "speaker": spk}
            lines = []
        elif cur is not None:
            lines.append(line)
    flush()
    return meeting


def iter_transcripts(directory: Path):
    for p in sorted(directory.glob("*.txt")):
        if MEETING_ID.match(p.stem):
            yield p
