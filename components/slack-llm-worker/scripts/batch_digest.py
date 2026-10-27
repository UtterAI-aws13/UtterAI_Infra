"""Second gateway client: a nightly digest job that summarizes a batch of texts.

It exists so the gateway has two services with different keys, models and budgets. It calls
the same OpenAI-compatible client as the worker, with the `batch` key and `quality` alias.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from slack_worker.pipeline import OpenAICompatModel  # noqa: E402
from slack_worker.worker import ModelRejected  # noqa: E402

KEYS = ROOT / "gateway/.keys.local.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("texts", nargs="+")
    ap.add_argument("--gateway", default="http://127.0.0.1:4000/v1")
    ap.add_argument("--model", default="quality")
    args = ap.parse_args()
    model = OpenAICompatModel(args.gateway, args.model, json.loads(KEYS.read_text())["batch"], max_tokens=120)
    for i, text in enumerate(args.texts):
        try:
            result = model.generate("다음 내용을 한 문장으로 요약해줘:\n" + text, "", user=f"batch:digest:{i}")
        except ModelRejected as e:
            raise SystemExit(f"gateway refused the batch key: {e.reason}")
        print(json.dumps({"item": i, "summary": result["text"], "usage": result["usage"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
