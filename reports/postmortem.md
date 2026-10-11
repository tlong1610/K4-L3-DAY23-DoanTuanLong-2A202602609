# Postmortem — DR Drill Lab 23 (region-a netblock, 2026-10-10)

Blameless: câu hỏi là "hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".
Mọi giờ là UTC. Offset tính từ t_outage = `2026-10-10T10:19:10.92`.

## 1. Timeline (mọi dòng có evidence path:line)

| ISO time | +s | Sự kiện | Evidence |
|---|---:|---|---|
| 10:19:08.96 | −1.97 | (trước outage) health checker đã flag region **b** UNHEALTHY vì standby đang cold: `pool_state=warm, model_weights_missing, vector_db_empty`. Đây là trạng thái thiết kế, không phải sự cố | `reports/health-events.jsonl:2` |
| 10:19:10.92 | 0 | outage bắt đầu: SIGSTOP region-a (`mode=netblock`) | `chaos/chaos-events.jsonl:3` |
| 10:19:10.97 | +0.05 | user đầu tiên bị ảnh hưởng: 503 `ReadTimeout` sau 2011ms | `reports/drill-2-withdr.jsonl:25` |
| 10:19:25.93 | +15.0 | health check alert: region-a UNHEALTHY, 3 lần fail liên tiếp | `reports/health-events.jsonl:3` |
| 10:19:27.24 | +16.32 | runbook xác nhận outage (3 probe cách 5s) + thông báo incident (`notify_delay_s=16.33`) | `reports/runbook-run.jsonl:1-2` (log local, không commit) |
| 10:19:27.27 | +16.35 | operator confirm cutover (`--auto`), failover bắt đầu `1_verify_target` | `reports/failover-events.jsonl:1` |
| 10:19:27.36 | +16.43 | snapshot restore xong: `rpo_seconds=2.0`, `docs_lost=1`, model `vi-e5-base@v3` | `reports/failover-events.jsonl:2` |
| 10:19:33.65 | +22.73 | region-b ready sau warm-up 6.29s | `reports/failover-events.jsonl:4` |
| 10:19:33.66 | +22.73 | DNS cutover a → b | `reports/failover-events.jsonl:5` |
| 10:19:39.18 | +28.26 | **resolved:** request đầu tiên OK, `served_by=b` | `reports/drill-2-withdr.jsonl:39` |

Trong cửa sổ outage có 14 request fail (`reports/drill-2-withdr.jsonl:25` → `:38`). Từ
`reports/drill-2-withdr.jsonl:39` tới hết file (dòng 158) không còn request nào fail.

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- **RTO** mục tiêu: 300s · đo được: **28.3s** · gap: −271.7s (đạt, dư 90%).
- **RPO** mục tiêu: 300s · đo được: **2.0s** (**1** doc bị mất) · gap: −298.0s (đạt).
  Con số 2.0s lạc quan hơn thực tế. Snapshot dùng để restore được `put` lúc +12.88s, *sau*
  outage (`reports/replication.jsonl:2`), vì `netblock` chỉ đóng băng process serving chứ
  không làm mất đĩa của A. Nếu mất region thật, replication dừng ở `reports/replication.jsonl:1`,
  và RPO thực tế là **16.0s / 8 doc**. Trường hợp xấu nhất với `--every 30` là ≈ 30s / 15 doc.
- **Bước tốn nhiều giây nhất:** health-check detect floor, **15.0s = 53% RTO**. Lý do:
  `interval=5s × threshold=3` buộc phải thấy 3 lần fail liên tiếp mới báo động. Đây là cái giá
  cố ý trả để không failover vì một lần timeout lẻ. Đứng thứ hai là GPU warm-up 6.29s (22%),
  thứ ba là DNS TTL 5.53s (20%). Restore snapshot thật chỉ mất 0.084s.

## 3. Root cause (5 whys)

Câu hỏi: *nếu đây là outage thật, bước nào trong runbook của tôi sẽ thất bại?*

1. **Vì sao user nhận 503 suốt 28s?** Vì edge vẫn trỏ về region-a cho tới khi có cutover, và
   region phụ không serve được ngay.
2. **Vì sao region phụ không serve được ngay?** Vì standby là *cold*: không có weights, vector DB
   rỗng, pool `warm` (`reports/health-events.jsonl:2`). Phải restore + warm-up mới dùng được.
3. **Vì sao restore lại thành công?** Vì trong drill, replication vẫn đọc được đĩa của A sau
   outage. **Đây là bước sẽ thất bại khi outage thật:** `replicate.py` chạy *pull từ region
   chính*, nên khi region chính mất thì replication chết cùng lúc. Cả RPO thật lẫn việc
   "snapshot có tồn tại hay không" đều phụ thuộc vào lần `put` cuối trước sự cố.
4. **Vì sao không có cảnh báo khi replication lag?** Vì không có gì giám sát
   `state/snapshot.py lag`. Nếu `replicate.py` chết từ trước (process crash, hết quota bucket)
   thì failover sẽ restore một snapshot cũ hàng giờ, hoặc chết ở `2_restore_snapshot` vì
   chưa từng có `put`, và không ai biết trước.
5. **Vì sao ingest vẫn ghi vào A trong lúc outage (66 doc sau t_outage)?** Vì pipeline ingest
   ghi thẳng vào region-a, không đi qua `edge/active_region`. Sau cutover, dữ liệu mới không
   tới được B. Khi outage thật, 66 doc này sẽ mất hẳn hoặc kẹt ở A.

**Root cause hệ thống:** thiết kế DR chỉ cutover lớp *serving*. Lớp *state* (replication +
ingest) vẫn gắn cứng vào region chính và không có giám sát độ tươi của snapshot.

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Alert khi `snapshot.py lag` > 60s hoặc không có `put` mới trong 2 chu kỳ. Thêm cron kiểm tra `MANIFEST.json` | ML Platform | 2026-10-17 | Không giảm RTO. Chặn trường hợp RPO = hàng giờ (cap RPO ở ≤ 60s) |
| 2 | Chuyển replication sang *push/stream* (WAL/CDC) ngay khi ghi, thay vì snapshot 30s. Tạm thời hạ `--every` xuống 10s | ML Platform | 2026-10-31 | RPO xấu nhất từ 30s xuống ≈ 10s (−20s) |
| 3 | Pipeline ingest ghi theo `edge/active_region` (hoặc dual-write qua queue) để sau cutover doc mới vào B | Data Eng | 2026-10-31 | Tránh mất 0.5 doc/s × thời gian outage (66 doc trong drill) |
| 4 | Health checker interval 5s → 2s, giữ threshold 3. Runbook trigger thẳng từ alert UNHEALTHY thay vì probe lại 3 lần | SRE on-call lead | 2026-10-17 | Detect 15.0s → 6s (−9s), bỏ 1.3s xác nhận lặp |
| 5 | Warm standby: giữ region-b `pool_state=full` với pool tối thiểu, restore snapshot định kỳ vào B | ML Platform + FinOps duyệt chi phí | 2026-11-15 | −6.29s warm-up, −0.08s restore |
| 6 | Edge TTL 5s → 1s + passive health check (loại upstream timeout) | Network/SRE | 2026-10-24 | −4s đến −5s TTL |

Nếu làm cả #4, #5, #6 thì RTO ước tính ≈ 28.3s − 9s − 1.3s − 6.4s − 4.5s ≈ **7s**.

## 5. Ba câu hỏi bắt buộc trả lời

1. **`interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?**
   5s × 3 = **15.0s**. Detect đo được +15.0s (`reports/health-events.jsonl:3`), chiếm
   15.0 / 28.3 = **53%** RTO. Với RTO mục tiêu 5 phút thì về lý thuyết interval có thể lên tới
   ≈ (300 − 1.4 − 6.3 − 5.5) / 3 ≈ 95s. Nhưng nên giữ ≤ 10s, để còn buffer cho restore chậm
   (snapshot vài GB) và cho thao tác tay của operator.

2. **Nếu hạ interval xuống 1s, RTO giảm mấy giây, và bạn trả giá gì?**
   Detect floor 15s → 3s, nên RTO giảm khoảng **12s** (≈ 28.3s → 16s). Nếu runbook
   cũng xác nhận ở 1s thì giảm thêm khoảng 1.3s. Cái giá phải trả:
   - Probe nhiều gấp 5 lần.
   - Quan trọng hơn: chỉ cần 3s chập chờn là đủ trigger failover (GC pause, deploy rolling,
     một đợt timeout 2s vượt quá interval 1s khiến các probe chồng lên nhau). Mỗi lần failover
     nhầm tốn ≈ 13s 503 (restore + warm-up + TTL), mất dữ liệu ngang RPO (tới 30s), và rủi ro
     **flapping** a ↔ b nếu rollback cũng tự động (§4).
   Vì vậy nên hạ xuống 2s chứ không phải 1s, giữ threshold 3, và rollback luôn cần người duyệt.

3. **Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` của bạn có nghĩa gì với khách hàng?**
   `docs_lost=1` trong drill là con số lạc quan, vì replication vẫn chạy sau outage. Với mất
   region thật:
   - **8 ticket** khách gửi trong 16s cuối trước sự cố (RPO thật 16.0s; tệ nhất ≈ 15 ticket / 30s)
     biến mất khỏi vector DB. Hệ thống trả lời như thể các ticket đó chưa từng tồn tại.
   - Nếu không làm action item #3, **toàn bộ 6 giờ** ingest vẫn đổ vào region đã chết:
     0.5 doc/s × 21,600s ≈ **10,800 ticket** không tìm thấy được.
   Với khách hàng, điều này nghĩa là câu trả lời sai hoặc thiếu ngữ cảnh về hoá đơn họ vừa hỏi.
   Phải re-ingest từ source of truth (hệ thống ticket gốc). `docs_lost` cho biết chính xác cần
   replay khoảng thời gian nào: từ `restored_latest_doc_ts` tới t_outage
   (`reports/failover-events.jsonl:2`).
