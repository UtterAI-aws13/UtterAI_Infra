"""Why Q&A and incident summaries get different models: run the same workloads on both gateway
aliases and compare latency, tokens and summary validity.

Local models stand in for the aliases (fast = llama3.2:1b, quality = qwen2.5:3b on Ollama).
Costs are token counts priced at the gateway's configured per-token prices, which mirror the
public list prices of a small and a mid-size hosted model. They are estimates, not bills.

Calls go through the LiteLLM proxy with the master key, because the comparison deliberately
crosses the per-service model restrictions (e.g. Q&A on `quality`).
"""
import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from slack_worker.ops import Summarizer  # noqa: E402
from slack_worker.pipeline import SYSTEM_PROMPT, OpenAICompatModel  # noqa: E402

GATEWAY = "http://127.0.0.1:4000/v1"
ARGS = None
MASTER = "sk-local-master-test-only"
# USD per million tokens, same values as gateway/litellm.yaml.
PRICE = {"fast": (1.0, 5.0), "quality": (3.0, 15.0)}

QUESTIONS = [
    "CrashLoopBackOff가 뭐야?",
    "Pod가 Pending에서 안 넘어가면 뭘 먼저 봐야 해?",
    "SQS DLQ에 메시지가 쌓이면 어떻게 확인해?",
    "Argo CD에서 Synced인데 Degraded면 무슨 뜻이야?",
    "롤백 PR은 누가 merge할 수 있어?",
    "kubectl로 이전 컨테이너 로그 보는 법 알려줘",
]

# Two evidence sets are copied from the GitOps scenario output; two are synthetic.
# `expect` is a term a grounded summary has to mention.
INCIDENTS = [
    {"name": "bad_image_crash", "title": "UtterAIPodCrashLoopBackOff", "runbook": "pod-crashloop.md",
     "expect": r"6e134f1|이미지|배포", "lines": [
         "utterai-batch/batch-worker: Pod 2/3 Ready(목표 2), 재시작 합계 1, 상태 CrashLoopBackOff, 마지막 종료 exit 1(Error), 실행 중 이미지 prod-6e134f1, prod-aa314fc",
         "이벤트 BackOff(x1): Back-off restarting failed container worker",
         "최근 배포: 10/28 16:59 이미지 prod-aa314fc → prod-6e134f1 (ops-admin, \"ci(ai): update prod image tag to prod-6e134f1\"), 알림 0분 전",
         "Argo CD: Synced / Progressing"]},
    {"name": "node_capacity_pending", "title": "UtterAIPodPendingTooLong", "runbook": None,  # no Pending runbook exists
     "expect": r"cpu|CPU|노드|리소스", "lines": [
         "utterai-ai-cpu/cpu-worker: Pod 0/1 Ready(목표 1), 재시작 합계 0, 실행 중 이미지 prod-aa314fc",
         "이벤트 FailedScheduling(x2): 0/1 nodes are available: 1 Insufficient cpu.",
         "Argo CD: Synced / Degraded"]},
    {"name": "oom_killed", "title": "UtterAIPodCrashLoopBackOff", "runbook": "pod-crashloop.md",
     "expect": r"OOM|메모리|memory", "lines": [
         "utterai-ai/analysis-worker: Pod 1/2 Ready(목표 2), 재시작 합계 6, 상태 CrashLoopBackOff, 마지막 종료 exit 137(OOMKilled), 실행 중 이미지 prod-71cbc53",
         "이벤트 BackOff(x4): Back-off restarting failed container worker",
         "최근 배포: 6일 전, 알림과 무관",
         "Argo CD: Synced / Degraded"]},
    {"name": "stalled_consumer", "title": "UtterAIQueueBacklog", "runbook": "consumer-stalled.md",
     "expect": r"소비|처리|멈|정지|consumer|워커", "lines": [
         "analysis 큐: 대기 1240, 처리 중 0, DLQ 0, 10분 전 대비 대기 +610",
         "utterai-ai/analysis-worker: Pod 2/2 Ready(목표 2), 재시작 합계 0",
         "최근 배포: 3일 전",
         "Argo CD: Synced / Healthy"]},
]


TEMPLATE = re.compile(r"두 문장 이내 요약|근거에 기반한 원인 후보")


def model(alias, system, temperature=None, max_tokens=400):
    return OpenAICompatModel(ARGS.gateway, alias, api_key=MASTER, max_tokens=max_tokens, timeout=120,
                             system=system, temperature=temperature)


def timed(fn):
    start = time.perf_counter()
    out = fn()
    return out, round(time.perf_counter() - start, 3)


def run_qa(alias, repeats):
    m, rows = model(alias, SYSTEM_PROMPT), []
    m.generate("안녕", "")  # warm-up: load the model before timing
    for _ in range(repeats):
        for q in QUESTIONS:
            out, sec = timed(lambda: m.generate(q, ""))
            rows.append({"question": q, "seconds": sec, **out["usage"], "chars": len(out["text"])})
    return rows


class Recording:
    """Wraps a model to keep the usage of the last call (Summarizer drops it)."""
    def __init__(self, inner):
        self.inner, self.usage = inner, {}

    def generate(self, text, context, user=""):
        out = self.inner.generate(text, context, user)
        self.usage = out["usage"]
        return out


def run_summary(alias, repeats):
    rec = Recording(model(alias, Summarizer.SYSTEM, temperature=0))
    s, rows = Summarizer(rec), []
    rec.generate("안녕", "")
    for _ in range(repeats):
        for inc in INCIDENTS:
            runbook = (ROOT / "examples/runbooks" / inc["runbook"]).read_text() if inc["runbook"] else ""
            try:
                out, sec = timed(lambda: s.summarize(inc["title"], inc["lines"], runbook))
                text = out["summary"] + " " + " ".join(out["causes"])
                if TEMPLATE.search(text):  # small models echo the prompt; the format example parses as JSON
                    raise ValueError("echoed format example")
                rows.append({"incident": inc["name"], "seconds": sec, "valid": True,
                             "grounded": bool(re.search(inc["expect"], text)), **rec.usage})
            except (ValueError, json.JSONDecodeError):
                rows.append({"incident": inc["name"], "seconds": None, "valid": False, "grounded": False, **rec.usage})
    return rows


def pct(values, q):
    values = sorted(v for v in values if v is not None)
    return round(values[min(len(values) - 1, int(round(q * (len(values) - 1))))], 2) if values else None


def summarize(alias, rows):
    usd_in, usd_out = PRICE[alias]
    tin = statistics.mean(r.get("inputTokens", 0) for r in rows)
    tout = statistics.mean(r.get("outputTokens", 0) for r in rows)
    out = {"calls": len(rows), "p50_seconds": pct([r["seconds"] for r in rows], 0.5),
           "p95_seconds": pct([r["seconds"] for r in rows], 0.95),
           "avg_input_tokens": round(tin), "avg_output_tokens": round(tout),
           "usd_per_1000_calls": round((tin * usd_in + tout * usd_out) / 1000, 3)}
    if "valid" in rows[0]:
        out["valid_rate"] = round(sum(r["valid"] for r in rows) / len(rows), 2)
        out["grounded_rate"] = round(sum(r["grounded"] for r in rows) / len(rows), 2)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--qa-repeats", type=int, default=3)
    ap.add_argument("--summary-repeats", type=int, default=4)
    ap.add_argument("--out", default=str(ROOT / "evidence/model-policy.json"))
    ap.add_argument("--gateway", default=GATEWAY)
    ap.add_argument("--models", default="fast=ollama_chat/llama3.2:1b,quality=ollama_chat/qwen2.5:3b",
                    help="what the aliases point at, recorded in the evidence file")
    args = ap.parse_args()
    global ARGS
    ARGS = args

    raw, result = {}, {}
    for alias in ("fast", "quality"):  # one model at a time, so timings exclude model swaps
        raw[f"qa_{alias}"] = run_qa(alias, args.qa_repeats)
        raw[f"summary_{alias}"] = run_summary(alias, args.summary_repeats)
        result[f"qa_{alias}"] = summarize(alias, raw[f"qa_{alias}"])
        result[f"summary_{alias}"] = summarize(alias, raw[f"summary_{alias}"])
        print(alias, json.dumps({k: v for k, v in result.items() if k.endswith(alias)}, ensure_ascii=False), flush=True)

    result["by_incident"] = {
        alias: {inc["name"]: {"valid": sum(r["valid"] for r in raw[f"summary_{alias}"] if r["incident"] == inc["name"]),
                              "grounded": sum(r["grounded"] for r in raw[f"summary_{alias}"] if r["incident"] == inc["name"]),
                              "of": args.summary_repeats} for inc in INCIDENTS}
        for alias in ("fast", "quality")}
    meta = {"date": time.strftime("%Y-%m-%d"), **dict(kv.split("=", 1) for kv in args.models.split(",")),
            "via": "LiteLLM proxy " + args.gateway, "price_usd_per_mtok": PRICE,
            "note": "Costs are list-price estimates from token counts. Local-model latencies are relative only."}
    Path(args.out).write_text(json.dumps({"meta": meta, "result": result, "raw": raw}, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
