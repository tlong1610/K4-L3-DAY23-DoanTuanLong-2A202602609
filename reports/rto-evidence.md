# RTO/RPO Evidence — Lab 23

Mọi con số dưới đây lấy từ lần chạy drill của tôi ngày 2026-10-10 (giờ UTC, bare mode
`--mock`, chạy trong container Linux `devenv/`). Mốc 0 của mỗi drill là dòng `action:kill`
trong `chaos/chaos-events.jsonl`. Các offset "+giây" = `ts` của dòng evidence − `ts` của mốc 0.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-10-10T10:16:58` | chaos kill, `mode=netblock` (SIGSTOP) | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | +0.03s (measure_rto làm tròn 0.0s) | dòng `ok:false` đầu tiên sau t_outage, `ReadTimeout` sau 2008.4ms | `reports/drill-1-nodr.jsonl:17` |
| Request thành công sau đó | không có | 16/16 request sau t_outage đều `ok:false`; request cuối vẫn fail | `reports/drill-1-nodr.jsonl:32` |
| RTO | `NO_RECOVERY` | `python3 tools/measure_rto.py --loadgen reports/drill-1-nodr.jsonl --target-rto 300` → `requests_failed=16`, `rto_verdict=NO_RECOVERY` | `reports/drill-1-nodr.jsonl` |

## 2. Drill 2 — có DR

`python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300`
→ `valid=true`, `warnings=[]`, `recovered_by_region=b`, `rto_verdict=PASS`.

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0 (`2026-10-10T10:19:10`) | `action:kill`, region a, `other_alive:true` | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | +0.05s | dòng `ok:false` đầu (`ReadTimeout`, 2011.0ms) | `reports/drill-2-withdr.jsonl:25` |
| Health check phát hiện | +15.0s | `to:UNHEALTHY, region:a`, `consecutive_fails=3` | `reports/health-events.jsonl:3` |
| Snapshot restore xong | +16.43s | `step:2_restore_snapshot`, `restore_s=0.084` | `reports/failover-events.jsonl:2` |
| Region phụ ready | +22.73s | `step:4_wait_ready`, `ready:true`, `waited_s=6.29` | `reports/failover-events.jsonl:4` |
| DNS cutover | +22.73s | `step:5_dns_cutover`, a → b | `reports/failover-events.jsonl:5` |
| **RTO đo được** | **+28.3s** | dòng `ok:true` đầu sau lỗi, `served_by:b` | `reports/drill-2-withdr.jsonl:39` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | 28.3s | 300s (5 phút) | **PASS** (dư 271.7s) |
| RPO — Vector DB | 2.0s / 1 doc (`reports/failover-events.jsonl:2`) | 300s (5 phút) | **PASS** — nhưng xem lưu ý bên dưới |

> **Lưu ý về RPO:** `reports/replication.jsonl:2` cho thấy một snapshot được `put` lúc
> +12.88s, tức là SAU outage. Lý do: `netblock` chỉ SIGSTOP process serving, file SQLite của
> region A vẫn đọc được, nên `state/replicate.py` vẫn chạy tiếp. Khi mất region thật, replication cũng
> chết theo. Lúc đó snapshot dùng được sẽ là `reports/replication.jsonl:1`
> (doc mới nhất ở −17.14s), và RPO thực tế sẽ là **16.0s / 8 doc** (doc cuối trước outage ở −1.14s).
> Vẫn đạt mục tiêu 300s, nhưng lớn gấp 8 lần con số log ghi.
> Trường hợp xấu nhất với `--every 30` là RPO ≈ 30s ≈ 15 doc ở tốc độ 0.5 doc/s.

## 3. RTO của tôi gồm những gì

Tổng 4 thành phần = 15.00 + 1.44 + 6.29 + 5.53 = 28.26s ≈ **28.3s** = `rto_measured_s`.

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | 15.00s (53% RTO) | `interval_s=5.0 × threshold=3` = 15.0s, trong `reports/health-events.jsonl:3`. Lần probe fail thứ 3 rơi đúng +15.0s | Hạ interval xuống 2s (floor 6s), giữ threshold=3 để chống flap. Hoặc dùng thêm tín hiệu ngoài (lỗi 5xx ở edge) để rút ngắn bước xác nhận |
| Snapshot restore | 1.44s | Từ lúc detect (+15.0s) tới `3_scale_pool` (+16.44s), `reports/failover-events.jsonl:3`. Gồm runbook xác nhận outage xong + verify target, cộng copy snapshot thật `restore_s=0.084s` | Restore đã gần 0. Phần còn lại là độ trễ giữa detect và runbook. Có thể trigger runbook thẳng từ alert UNHEALTHY thay vì probe lại từ đầu |
| GPU pool warm-up | 6.29s | `waited_s=6.29` ở `4_wait_ready`, `reports/failover-events.jsonl:4` (WARMUP_SECONDS=6 + nhịp poll 0.5s) | Giữ region phụ ở `pool_state=full` sẵn (warm standby, tốn tiền GPU idle), hoặc dùng pool nhỏ luôn-full rồi scale dần |
| DNS/LB TTL cache | 5.53s | t_recovered (+28.26s, `reports/drill-2-withdr.jsonl:39`) − t_cutover (+22.73s). Edge cache `active_region` với `EDGE_TTL_SECONDS=5`, cộng nhịp request 0.5s của loadgen | Hạ TTL xuống 1s, hoặc để edge tự loại upstream lỗi (passive health check) thay vì chỉ chờ DNS |
