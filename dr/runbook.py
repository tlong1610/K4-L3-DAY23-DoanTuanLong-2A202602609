"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import math
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
CHAOS = pathlib.Path("chaos/chaos-events.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
P95_LIMIT_MS = 500.0
MAX_ERROR_RATE = 0.0


def step(n, name, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
           "step": n, "name": name, **kw}
    with LOG.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("RUNBOOK", json.dumps(rec, ensure_ascii=False))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N (mặc định N)."""
    if auto:
        return True
    try:
        ans = input(f"{msg} [y/N] ")
    except EOFError:  # không có TTY -> không ai confirm -> không failover
        return False
    return ans.strip().lower() in ("y", "yes")


def alive(region: str, timeout: float = 2.0) -> bool:
    """/healthz: process region còn trả lời không (region phụ chưa ready là bình thường)."""
    try:
        return httpx.get(f"{URL[region]}/healthz", timeout=timeout).status_code == 200
    except Exception:
        return False


def t_outage_of(region: str):
    """Mốc outage thật (từ chaos log) để tính độ trễ thông báo; None nếu outage tự nhiên."""
    if not CHAOS.exists():
        return None
    kills = [json.loads(l) for l in CHAOS.read_text().splitlines() if l.strip()]
    kills = [e for e in kills if e.get("action") == "kill" and e.get("region") == region]
    return kills[-1]["ts"] if kills else None


def golden_signals(region: str, n: int = 10) -> dict:
    lat, errors = [], 0
    with httpx.Client(timeout=3.0) as c:
        for i in range(n):
            t = time.time()
            try:
                r = c.get(f"{URL[region]}/v1/infer", params={"q": f"hoa don thang {i % 12 + 1}"})
                ok = r.status_code == 200 and "error" not in r.json()
            except Exception:
                ok = False
            lat.append((time.time() - t) * 1000)
            errors += not ok
    p95 = sorted(lat)[max(0, math.ceil(0.95 * n) - 1)]
    rate = errors / n
    return {"requests": n, "errors": errors, "error_rate": rate,
            "p50_ms": round(sorted(lat)[n // 2], 1), "p95_ms": round(p95, 1),
            "p95_limit_ms": P95_LIMIT_MS, "max_error_rate": MAX_ERROR_RATE,
            "pass": p95 < P95_LIMIT_MS and rate <= MAX_ERROR_RATE}


def run(primary: str, target: str, backend: str, auto: bool,
        confirm_interval: float = 5.0, confirm_threshold: int = 3, wait: float = 60) -> dict:
    t_start = time.time()

    def done(ok, **kw):
        step(7, "post_incident", ok=ok, elapsed_s=round(time.time() - t_start, 2),
             measure_cmd="python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl "
                         "--target-rto 300", **kw)
        return {"ok": ok, "elapsed_s": round(time.time() - t_start, 2), **kw}

    # 1 — một lần fail không phải outage: cần `confirm_threshold` lần fail liên tiếp
    probes = []
    for i in range(confirm_threshold):
        if i:
            time.sleep(confirm_interval)
        ok, reason = hc.probe(primary, 2.0)
        probes.append({"ts": time.time(), "ready": ok, "reason": reason})
        if ok:
            break
    confirmed = len(probes) == confirm_threshold and not any(p["ready"] for p in probes)
    target_alive = alive(target)
    step(1, "xac_nhan_outage", primary=primary, target=target, confirmed=confirmed,
         probes=probes, confirm_interval_s=confirm_interval,
         confirm_threshold=confirm_threshold, target_alive=target_alive)
    if not confirmed:
        return done(False, reason=f"region-{primary} van tra loi /readyz -> khong failover")
    if not target_alive:
        return done(False, reason=f"region-{target} cung khong song -> double outage, escalate")

    # 2 — operator biết tin LUÔN sau t_outage; ghi cả 2 mốc để tính độ trễ thông báo
    t_out, t_notified = t_outage_of(primary), time.time()
    step(2, "thong_bao_incident", severity="SEV1",
         summary=f"region-{primary} down, failover sang region-{target}",
         t_outage=t_out, t_notified=t_notified,
         notify_delay_s=None if t_out is None else round(t_notified - t_out, 2))

    if not confirm(auto, f"Failover region-{primary} -> region-{target}?"):
        step(3, "scale_gpu_pool", skipped=True, reason="operator_khong_confirm")
        return done(False, reason="operator_khong_confirm")

    # 3 — gọi failover ĐÚNG MỘT LẦN; nó tự làm verify/restore/scale/wait/cutover
    res = fo.failover(target, backend, wait)
    step(3, "scale_gpu_pool", confirm_mode="auto" if auto else "manual", ok=res["ok"],
         failed_at=res.get("failed_at"), failover_elapsed_s=res["elapsed_s"])
    if not res["ok"]:
        return done(False, reason=f"failover abort o {res.get('failed_at')}: {res.get('reason')}")

    # 4, 5 — chỉ ĐỌC kết quả của bước 3
    s = res["steps"]
    restore, ts = s["2_restore_snapshot"], res.get("target_state", {})
    step(4, "verify_state_replica", target=target, vectors=ts.get("count"),
         weights=ts.get("weights"), pool_state=ts.get("pool_state"),
         embed_model_version=restore.get("embed_model_version"),
         rpo_seconds=restore.get("rpo_seconds"), docs_lost=restore.get("docs_lost"))
    cut = s["5_dns_cutover"]
    step(5, "dns_cutover", ok=cut.get("to_region") == target, from_region=cut.get("from_region"),
         to_region=cut.get("to_region"), t_cutover=cut["ts"])

    # 6 — 10 request thật vào region phụ
    gs = golden_signals(target)
    step(6, "verify_golden_signals", target=target, **gs)

    # 7
    return done(gs["pass"], primary=primary, target=target,
                rpo_seconds=res["rpo_seconds"], docs_lost=res["docs_lost"],
                golden_signals=gs,
                since_outage_s=None if t_out is None else round(time.time() - t_out, 2))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    p.add_argument("--confirm-interval", type=float, default=5.0)
    p.add_argument("--confirm-threshold", type=int, default=3)
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto,
                         a.confirm_interval, a.confirm_threshold, a.wait), indent=2))
