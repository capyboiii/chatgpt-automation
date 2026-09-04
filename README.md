# Mockup Forge — ChatGPT Automation

Tự động hoá: upload **template** + chọn **prompt design** → điều khiển ChatGPT web
gen **ảnh mockup** (template đã gắn design) → tải về. Chạy **đa tab / đa profile**
song song, mỗi mockup 1 phiên chat riêng.

## Cài
```bash
pip install -r requirements.txt
playwright install chrome
```

## Cấu hình `config.yaml`
Mỗi profile = 1 tài khoản ChatGPT. Mỗi lượt gen chạy trên **1 tài khoản** — chọn ngay
trên UI khi bấm *Tạo Mockup*.
```yaml
browser:
  profiles:
    - name: acc1
  launch_stagger: 0.4   # giây lệch nhau giữa các lần bật Chrome
run:
  max_retries: 2
  batch_size: 6         # số ảnh đính kèm mỗi tin nhắn
```

### Vì sao cả lượt đi trong 1 chat
Mỗi cuộc hội thoại ChatGPT cho ra một hướng design khác nhau, nên chia ảnh ra nhiều
chat / nhiều tab thì **bộ mockup không đồng nhất**. Cả lượt vì thế đi trong **một
phiên chat duy nhất**:

- lô 1: `[ảnh 1..batch_size]` + prompt gốc → ChatGPT chốt hướng design
- lô 2, 3...: ảnh còn lại + `followup_prompt` ("giữ y design ở trên"), vẫn trong
  chính chat đó

Chia lô chỉ vì ChatGPT giới hạn số ảnh đính kèm mỗi tin nhắn. Nếu một vòng trả thiếu ảnh
(ChatGPT hay dính `Something went wrong` giữa chừng), tool chỉ gửi lại **đúng những
template còn thiếu** trong chính chat đó kèm `topup_prompt` — ảnh đã lấy được thì giữ
nguyên, không gen lại. Ảnh trả về được gán cho
template theo **đúng thứ tự ảnh gen ra** trong lô.

### Lấy ảnh ở tầng mạng, không mò DOM
ChatGPT bày kết quả thành 1 ảnh lớn + mấy thumbnail nhỏ (kèm `srcset`, `blob:`), nên
lọc theo kích thước hiển thị là sót ảnh ngay. Tool nghe thẳng `page.on("response")`:
response ảnh phản ánh đúng số ảnh gen ra và mang sẵn bytes gốc — khỏi fetch lại URL đã
ký. Thumbnail/icon bị loại bằng **kích thước thật** (Pillow, cạnh ngắn ≥ 400px); một
ảnh tải về nhiều lần thì gom theo URL (bỏ query) và giữ bản nặng nhất.

Hai lớp chặn để không nhận nhầm chính ảnh mình gửi lên (ChatGPT tải template về để
hiển thị trong tin nhắn, cũng là response ảnh to như thật): **hash SHA-256** của các
template vừa gửi, và mốc **"đã thấy ChatGPT bắt đầu gen"** — mọi ảnh bắt được trước
mốc đó đều bị bỏ.

Thứ tự ảnh lấy theo **DOM** chứ không theo thứ tự response: ảnh tải song song nên
response về lộn xộn, xếp theo đó là gán nhầm template (design của cốc lưu vào file
áo). DOM chỉ còn để biết "còn đang gen không", giữ thứ tự, và làm nguồn dự phòng.

## Dùng
1. Đăng nhập từng profile (1 lần):
   ```bash
   python login.py acc1
   ```
2. Mở tool: chạy `start_ui.bat` (hoặc `python server.py`) → http://127.0.0.1:8010
3. Trên UI: upload template → thêm prompt → tích template + chọn prompt → **Gen mockup** → xem/tải kết quả.

## Xử lý lỗi
- **Hết lượt tạo ảnh** (ChatGPT chặn quota): pool đọc chữ trong hộp thoại / lượt trả
  lời cuối, nhận ra thì đánh dấu **cả tài khoản** đó nghỉ, đẩy job đang dở sang tài
  khoản khác và hiện banner đỏ trên UI. Hết sạch tài khoản thì job báo failed kèm lý do.
- **Không ra ảnh** (bị từ chối, ChatGPT báo lỗi, trả lời bằng chữ): retry `max_retries`
  lần trong chat mới; hết lượt retry thì job failed, di chuột lên chữ "Lỗi" xem nguyên nhân.
- Không phải chờ hết `generation_timeout` mới biết lỗi: im lặng 8s hoặc trả lời xong
  mà không có ảnh là pool soi chữ và kết luận ngay.
- **Tab chết / Chrome sập**: tái tạo tab 1 lần, không được thì trả job về hàng đợi.

## Kiến trúc
- `chatgpt_pool.py` — pool Playwright đa tab: chat mới mỗi job → upload template →
  gửi prompt → chờ → tải ảnh. Không dùng extension (ChatGPT không chặn automation).
- `server.py` — FastAPI: template upload, prompt CRUD, chạy pool nền, poll status.
- `static/` — UI (dark/light).

> Selector ChatGPT có thể đổi theo thời gian; nếu gen lỗi (không thấy ô nhập / nút
> Send / ảnh), cập nhật các hằng `SEL_*` trong `chatgpt_pool.py`.
