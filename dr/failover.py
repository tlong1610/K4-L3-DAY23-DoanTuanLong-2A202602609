"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")
ACTIVE = pathlib.Path("edge/active_region")


def emit(**kw):
    """Append 1 dòng JSONL có ts + iso vào LOG, và print ra stdout."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), **kw}
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("FAILOVER", json.dumps(rec))
    return rec


def state_of(region: str) -> dict:
    """/v1/state của 1 region; không raise — lỗi mạng trả về dict có key `error`."""
    try:
        return httpx.get(f"{URL[region]}/v1/state", timeout=2.0).json()
    except Exception as e:
        return {"region": region, "error": type(e).__name__}


def wait_ready(region: str, wait: float, every: float = 0.5) -> tuple[bool, float, dict]:
    """Poll /readyz tới khi 200 hoặc hết `wait` giây -> (ready, waited_s, body cuối)."""
    t0, body = time.time(), {}
    while True:
        try:
            r = httpx.get(f"{URL[region]}/readyz", timeout=2.0)
            body = r.json()
            if r.status_code == 200:
                return True, time.time() - t0, body
        except Exception as e:
            body = {"error": type(e).__name__}
        if time.time() - t0 >= wait:
            return False, time.time() - t0, body
        time.sleep(every)


def _version(region: str):
    f = pathlib.Path(f"state/region-{region}/weights/VERSION")
    return f.read_text().strip() if f.exists() else None


def failover(target: str, backend: str, wait: float) -> dict:
    """5 bước, đúng thứ tự. Bước nào hỏng -> ghi `abort` và KHÔNG cutover."""
    source = "b" if target == "a" else "a"
    t_start = time.time()
    steps = {}

    def abort(at, reason, **kw):
        steps["abort"] = emit(step="abort", failed_at=at, target=target, reason=reason,
                              cutover=False, **kw)
        return {"ok": False, "target": target, "failed_at": at, "reason": reason,
                "steps": steps, "elapsed_s": round(time.time() - t_start, 2)}

    # 1 — region đích có process sống không, đang thiếu gì
    st = state_of(target)
    steps["1_verify_target"] = emit(step="1_verify_target", target=target, source=source,
                                    state=st)
    if "error" in st:
        return abort("1_verify_target", f"region-{target} khong phan hoi: {st['error']}")

    # 2 — restore vector DB + weights từ object store, đo RPO thật (giây + số doc)
    t = time.time()
    try:
        meta = snapshot.get(target, backend)
    except (Exception, SystemExit) as e:  # snapshot.get báo "chưa có put" bằng SystemExit
        return abort("2_restore_snapshot", str(e))
    r = snapshot.rpo(pathlib.Path(f"state/region-{source}/vectors.sqlite"),
                     pathlib.Path(f"state/region-{target}/vectors.sqlite"))
    src_ver = _version(source)
    steps["2_restore_snapshot"] = emit(
        step="2_restore_snapshot", target=target, backend=backend,
        restore_s=round(time.time() - t, 3),
        snapshot_at=meta.get("snapshot_at"),
        snapshot_age_s=None if meta.get("snapshot_at") is None
        else round(time.time() - meta["snapshot_at"], 2),
        rpo_seconds=r["rpo_seconds"], docs_lost=r["docs_lost"],
        primary_latest_doc_ts=r["primary_latest_doc_ts"],
        restored_latest_doc_ts=r["restored_latest_doc_ts"],
        embed_model_version=meta.get("embed_model_version"),
        embed_model_match=None if src_ver is None else src_ver == meta.get("embed_model_version"))

    # 3 — warm -> full (đây là lúc GPU pool bắt đầu warm-up)
    pool = pathlib.Path(f"state/region-{target}/pool_state")
    prev = pool.read_text().strip() if pool.exists() else None
    pool.parent.mkdir(parents=True, exist_ok=True)
    pool.write_text("full")
    steps["3_scale_pool"] = emit(step="3_scale_pool", target=target, pool_from=prev,
                                 pool_to="full")

    # 4 — chờ readiness THẬT; timeout -> abort, tuyệt đối không cutover
    ok, waited, body = wait_ready(target, wait)
    steps["4_wait_ready"] = emit(step="4_wait_ready", target=target, ready=ok,
                                 waited_s=round(waited, 2), wait_limit_s=wait, readyz=body)
    if not ok:
        return abort("4_wait_ready", f"region-{target} chua ready sau {wait}s", readyz=body)

    # 5 — chỉ bây giờ mới đổi "DNS"
    prev_active = ACTIVE.read_text().strip() if ACTIVE.exists() else None
    ACTIVE.write_text(target)
    steps["5_dns_cutover"] = emit(step="5_dns_cutover", from_region=prev_active,
                                  to_region=target, active_region_file=str(ACTIVE))

    return {"ok": True, "target": target, "source": source, "steps": steps,
            "target_state": state_of(target), "rpo_seconds": r["rpo_seconds"],
            "docs_lost": r["docs_lost"], "elapsed_s": round(time.time() - t_start, 2)}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
