"""Score the agent on the hand-written eval set.

Three independent signals:
  1. citation validity  - code: every quote must appear verbatim in one turn of the cited meeting
  2. key-fact coverage  - code: regex alternatives per required fact (deterministic, no LLM)
  3. judge verdict      - LLM compares the answer with my gold answer: correct / partial / wrong
The headline is the judge score (correct=1, partial=0.5); the regex score is a cross-check
that can't be fooled by a lenient judge.

usage: python eval/run_eval.py [--ids E01,E02] [--no-judge]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from meeting_memory.agent import answer_many  # noqa: E402
from meeting_memory.llm import complete_json  # noqa: E402
from meeting_memory.quotes import verify  # noqa: E402

JUDGE_SYSTEM = "You grade answers against a reference answer. Be strict about facts, lenient about wording. Reply with JSON only."
JUDGE = """QUESTION: {q}
REFERENCE ANSWER (checked by a human against the transcripts): {gold}
CANDIDATE ANSWER: {pred}

Does the candidate get every part of the question right? Wording does not matter; facts do (options, numbers,
who, which meeting, what was final). Extra correct detail is fine; a wrong claim about a key fact is not.
Reply: {{"verdict": "correct" | "partial" | "wrong", "missing_or_wrong": "<short note>"}}"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids")
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--file", default=str(ROOT / "eval" / "my_questions.json"))
    a = ap.parse_args()
    qs = json.loads(Path(a.file).read_text(encoding="utf-8"))["questions"]
    if a.ids:
        qs = [q for q in qs if q["question_id"] in set(a.ids.split(","))]
    results = answer_many(qs)
    rows = []
    for q, r in zip(qs, results):
        cits = r["citations"]
        valid = sum(1 for c in cits if verify(c["meeting_id"], c["quote"]) is not None)
        facts = [any(re.search(p, r["answer"], re.I) for p in alts) for alts in q["key_facts"]]
        verdict, note = "n/a", ""
        if not a.no_judge:
            try:
                j, _ = complete_json("agent", JUDGE_SYSTEM, JUDGE.format(q=q["question"], gold=q["gold_answer"],
                                                                         pred=r["answer"]), purpose="judge", max_tokens=800)
                verdict, note = str(j.get("verdict", "wrong")).lower(), j.get("missing_or_wrong", "")
            except Exception as e:  # judge failure shouldn't kill the run
                verdict, note = "error", str(e)
        rows.append({"id": q["question_id"], "team": q["team"], "question": q["question"], "gold": q["gold_answer"],
                     "answer": r["answer"], "citations": cits, "citations_valid": f"{valid}/{len(cits)}",
                     "facts": f"{sum(facts)}/{len(facts)}", "fact_score": sum(facts) / len(facts),
                     "verdict": verdict, "judge_note": note, "meta": r["_meta"]})

    score = sum({"correct": 1, "partial": 0.5}.get(x["verdict"], 0) for x in rows)
    fact = sum(x["fact_score"] for x in rows) / len(rows)
    nc = sum(len(x["citations"]) for x in rows)
    nv = sum(int(x["citations_valid"].split("/")[0]) for x in rows)
    secs = sum(x["meta"]["seconds"] for x in rows) / len(rows)
    steps = sum(x["meta"]["steps"] for x in rows) / len(rows)

    out = ROOT / "eval"
    (out / "results.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    md = [f"# Eval results ({len(rows)} hand-written questions)", "",
          f"- **Judge score: {score:.1f} / {len(rows)}** (correct = 1, partial = 0.5)",
          f"- Key-fact coverage (regex, no LLM): {fact:.0%}",
          f"- Citations verified word-for-word: {nv}/{nc}",
          f"- Mean agent steps: {steps:.1f}, mean latency: {secs:.0f}s per question", "",
          "| id | team | verdict | facts | cites ok | judge note |", "|---|---|---|---|---|---|"]
    for x in rows:
        md.append(f"| {x['id']} | {x['team']} | {x['verdict']} | {x['facts']} | {x['citations_valid']} | {x['judge_note'][:140]} |")
    md += ["", "## Answers", ""]
    for x in rows:
        md += [f"### {x['id']} ({x['verdict']})", f"**Q:** {x['question']}", "", f"**Gold:** {x['gold']}", "",
               f"**Agent:** {x['answer']}", ""]
        md += [f"- `{c['meeting_id']}`: \"{c['quote']}\"" for c in x["citations"]] + [""]
    (out / "results.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md[:8 + len(rows)]))


if __name__ == "__main__":
    main()
