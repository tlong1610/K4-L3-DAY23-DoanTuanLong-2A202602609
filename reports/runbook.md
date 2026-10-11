# Runbook 1 trang — Region chính down

Runbook này phải chạy được lúc 3h sáng bởi người KHÔNG viết nó. Mỗi bước có lệnh copy-paste
được, cách biết bước đó xong, và người chịu trách nhiệm.

**Trước khi bắt đầu:** mở shell trong container lab: `.\devenv\lab.ps1` (Windows) hoặc
đứng ở gốc repo (Linux/macOS). Mọi lệnh dưới đây chạy từ thư mục gốc repo (`/lab`).

**Trigger:** health checker ghi `"to": "UNHEALTHY", "region": "a"` vào
`reports/health-events.jsonl`, hoặc user báo lỗi 503 hàng loạt qua edge `:8080`.

**Đường tắt (tự động 7 bước, khuyến nghị):**
```bash
python3 dr/runbook.py --primary a --target b --backend fs     # sẽ hỏi y/N trước khi failover
```
Bỏ `--auto`: bản chạy tay phải có người confirm (§4, tránh flapping). Nếu script dừng ở bước
nào, đọc dòng cuối `reports/runbook-run.jsonl` rồi làm tay từ bước đó theo bảng dưới.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `for i in 1 2 3; do python3 chaos/kill_region.py status; sleep 5; done` | `a.ready=false` cả 3 lần liên tiếp (cách nhau 5s) **và** `b.alive=true`. Nếu `b.alive=false` thì **DỪNG**, escalate (double outage, không failover) | On-call SRE |
| 2 | Mở incident + bấm giờ RTO | `python3 -c "import time,json;print(json.dumps({'incident':'SEV1 region-a down','t_notified':time.time()}))" \| tee -a reports/runbook-run.jsonl` | Có dòng `t_notified` trong `reports/runbook-run.jsonl`. Đã post kênh `#incident` gồm SEV1, region, link log | On-call SRE → báo Incident Commander (IC) |
| 3 | Restore state ở region phụ | `python3 state/snapshot.py get --region b --backend fs && python3 state/snapshot.py lag --backend fs` | Lệnh in `embed_model_version` = `embed-model=vi-e5-base@v3` (khớp với A). `curl -s localhost:8002/v1/state` cho `weights:true`, `count > 0`. Ghi lại `rpo_seconds` | ML Platform engineer |
| 4 | Scale pool warm→full | `printf full > state/region-b/pool_state; until curl -sf localhost:8002/readyz >/dev/null; do sleep 1; done; echo READY` | In ra `READY`, tức `/readyz` của b trả 200 (warm-up khoảng 6s). Quá 60s chưa ready thì **KHÔNG làm bước 5**, quay lại bước 3 | ML Platform engineer |
| 5 | DNS/LB cutover | `printf b > edge/active_region; sleep 6; curl -s localhost:8080/edge/state` | `active_region` là `"b"`, và `curl -s localhost:8080/v1/infer` trả `"region":"b"` (chờ hết TTL 5s) | On-call SRE (IC duyệt) |
| 6 | Verify golden signals | `for i in $(seq 10); do curl -s -o /dev/null -w "%{http_code} %{time_total}\n" localhost:8080/v1/infer; done` | 10/10 trả `200`, p95 < 500ms, error rate = 0%. Drill của tôi: p95 = 20.4ms, error 0/10 | On-call SRE |
| 7 | Đo RTO + postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `"valid": true`, `rto_verdict` = `PASS`, `warnings` rỗng. Điền `reports/rto-evidence.md` + `reports/postmortem.md` trong 48h | IC (owner postmortem) |

## Rollback (failover ngược b → a)

**Không tự động.** Full-auto hai chiều mà không có circuit breaker thì hai region sẽ flap qua lại (§4 Anti-Patterns).

**Điều kiện được phép trả traffic về A** (cần đủ cả 4):
1. Region A `ready=true` liên tục ≥ 10 phút: `python3 chaos/kill_region.py status` chạy lặp lại, health log có `to:HEALTHY, region:a`.
2. Đã biết root cause của outage A và đã fix. Không rollback chỉ vì "nó tự sống lại".
3. Dữ liệu ingest vào B trong lúc outage đã được sync ngược sang A:
   `python3 state/snapshot.py put --region b --backend fs && python3 state/snapshot.py get --region a --backend fs`.
   Nếu không sync thì rollback sẽ tạo thêm một lần RPO nữa.
4. Đang trong giờ thấp điểm, có người trực cả hai region.

**Rollback ngay lập tức** (không chờ đủ 4 điều kiện trên) nếu sau cutover region B có error rate > 5% hoặc p95 > 1s kéo dài 5 phút, **và** A đã ready lại. Khi đó chạy:
`printf a > edge/active_region`.

**Ai quyết định:** Incident Commander. On-call SRE không tự rollback. Nếu IC không liên lạc được trong 15 phút thì Engineering Manager trực quyết định. Quyết định phải ghi vào kênh `#incident` kèm timestamp.

**Kill switch của drill:** `python3 chaos/kill_region.py restore --region a --backend bare` (luôn chạy được).
