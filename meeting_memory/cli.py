"""Command line entry point:  python -m meeting_memory <command> ...

  ingest              index transcripts (code only: parse, chunk, full-text index, embeddings, roles)
  extract             build the memory ledger with the LLM (incremental, per team in time order;
                      includes the end-of-meeting reconciliation pass)
  reconcile           (re)run only the end-of-meeting reconciliation pass
  ask "question"      run the agent on one question (add --as-of TS3012c to time-slice)
  answer              answer questions.json -> answers.json (keeps an existing brief)
  brief               write the pre-meeting brief (default: TS3012 before its final meeting)
  build               answer + brief -> answers.json
  validate FILE       check every citation in an answers file word-for-word
  ledger --team T     print a team's memory timeline (optionally --topic, --as-of)
  stats               token / cost / time report and projection to 1,000 meetings
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import config


def _load_questions(path: Path, ids: str | None):
    qs = json.loads(path.read_text(encoding="utf-8"))["questions"]
    if ids:
        keep = set(ids.split(","))
        qs = [q for q in qs if q["question_id"] in keep]
    return qs


def cmd_ingest(a):
    from .index import index_all
    print(json.dumps(index_all(force=a.force), indent=2))


def cmd_extract(a):
    from .extract import extract_all
    from .index import rebuild_roles
    t = time.time()
    res = extract_all(a.teams.split(",") if a.teams else None, force=a.force)
    rebuild_roles()
    print(f"extracted {len(res)} meetings in {time.time() - t:.0f}s")


def cmd_reconcile(a):
    from .reconcile import reconcile_all
    res = reconcile_all(a.teams.split(",") if a.teams else None, force=a.force)
    print(f"reconciled {len(res)} meetings: +{sum(r['links'] for r in res)} links, "
          f"+{sum(r['decisions'] for r in res)} end-of-meeting decisions")


def cmd_ask(a):
    from .agent import answer_question
    from .store import meeting_seq
    max_seq = meeting_seq(a.as_of) if a.as_of else None
    r = answer_question(a.question, a.team, max_seq=max_seq, verbose=True)
    print("\nANSWER:", r["answer"])
    for c in r["citations"]:
        print(f'  [{c["meeting_id"]}] "{c["quote"]}"')
    m = r["_meta"]
    print(f"\n({m['steps']} steps, {m['seconds']}s, grounding={m['grounding']}, trace: store/traces/adhoc.json)")


def _write_answers(path: Path, answers=None, brief=None):
    cur = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"answers": [], "brief": ""}
    if answers is not None:
        by_id = {x["question_id"]: x for x in cur.get("answers", [])}
        for x in answers:
            by_id[x["question_id"]] = {k: v for k, v in x.items() if not k.startswith("_")}
        cur["answers"] = [by_id[k] for k in sorted(by_id)]
    if brief is not None:
        cur["brief"] = brief
    path.write_text(json.dumps(cur, indent=2, ensure_ascii=False), encoding="utf-8")


def cmd_answer(a):
    from .agent import answer_many
    qs = _load_questions(Path(a.questions), a.ids)
    res = answer_many(qs)
    _write_answers(Path(a.out), answers=res)
    meta_path = config.STORE_DIR / "answers_meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    meta.update({r["question_id"]: r["_meta"] for r in res})
    meta_path.write_text(json.dumps(meta, indent=2))
    print(f"wrote {a.out}")


def cmd_brief(a):
    from .brief import make_brief
    b = make_brief(a.team, a.before)
    print(b["brief"])
    print(f"\n({b['points']} points, {b['dropped_uncited']} dropped for lack of a valid citation)")
    if a.out:
        _write_answers(Path(a.out), brief=b["brief"])
        print(f"brief written into {a.out}")


def cmd_build(a):
    cmd_answer(a)
    a.team, a.before = "TS3012", None
    cmd_brief(a)


def cmd_validate(a):
    from .quotes import MIN_WORDS, verify
    import re
    data = json.loads(Path(a.file).read_text(encoding="utf-8"))
    ok = bad = 0
    for ans in data.get("answers", []):
        n = len(ans.get("citations", []))
        if not 1 <= n <= 3:
            print(f"{ans['question_id']}: {n} citations (need 1-3)")
        for c in ans.get("citations", []):
            if verify(c["meeting_id"], c["quote"]) is not None and len(c["quote"].split()) >= MIN_WORDS:
                ok += 1
            else:
                bad += 1
                print(f"FAIL {ans['question_id']} {c['meeting_id']}: {c['quote'][:80]}")
    bq = re.findall(r'([A-Z]{2}\d{4}[a-d]): "([^"]+)"', data.get("brief", ""))
    bok = sum(1 for m, q in bq if verify(m, q) is not None)
    print(f"answers: {ok} citations verified, {bad} failed | brief: {bok}/{len(bq)} citations verified")
    sys.exit(1 if bad or bok < len(bq) else 0)


def cmd_ledger(a):
    from .ledger import timeline, state
    from .store import meeting_seq
    max_seq = meeting_seq(a.as_of) if a.as_of else None
    if a.topic:
        print(timeline(a.team, topic_query=a.topic, max_seq=max_seq, limit=200))
    else:
        print(timeline(a.team, item_ids=list(state(a.team, max_seq)), max_seq=max_seq, limit=400))


def cmd_stats(a):
    from .report import cost_report
    print(cost_report())


def main(argv=None):
    p = argparse.ArgumentParser(prog="meeting_memory", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("ingest"); s.add_argument("--force", action="store_true"); s.set_defaults(f=cmd_ingest)
    s = sub.add_parser("extract"); s.add_argument("--teams"); s.add_argument("--force", action="store_true"); s.set_defaults(f=cmd_extract)
    s = sub.add_parser("reconcile"); s.add_argument("--teams"); s.add_argument("--force", action="store_true"); s.set_defaults(f=cmd_reconcile)
    s = sub.add_parser("ask"); s.add_argument("question"); s.add_argument("--team"); s.add_argument("--as-of"); s.set_defaults(f=cmd_ask)
    for name, fn in (("answer", cmd_answer), ("build", cmd_build)):
        s = sub.add_parser(name)
        s.add_argument("--questions", default=str(config.QUESTIONS_PATH))
        s.add_argument("--ids", help="comma-separated question ids")
        s.add_argument("--out", default=str(config.ROOT / "answers.json"))
        s.set_defaults(f=fn)
    s = sub.add_parser("brief"); s.add_argument("--team", default="TS3012"); s.add_argument("--before")
    s.add_argument("--out", nargs="?", const=str(config.ROOT / "answers.json")); s.set_defaults(f=cmd_brief)
    s = sub.add_parser("validate"); s.add_argument("file", nargs="?", default=str(config.ROOT / "answers.json")); s.set_defaults(f=cmd_validate)
    s = sub.add_parser("ledger"); s.add_argument("--team", required=True); s.add_argument("--topic"); s.add_argument("--as-of"); s.set_defaults(f=cmd_ledger)
    s = sub.add_parser("stats"); s.set_defaults(f=cmd_stats)
    a = p.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    main()
