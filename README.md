# Mockup Forge — ChatGPT Automation

Tự động hoá: upload **template** + chọn **prompt design** → điều khiển ChatGPT web
gen **ảnh mockup** (template đã gắn design) → tải về. Chạy **đa tab / đa profile**
song song, mỗi mockup 1 phiên chat riêng.

## Cài đặt
- **Cách 1 (Nhanh nhất trên Windows)**: Nhấp đúp vào file `setup.bat`.
- **Cách 2 (Thủ công)**:
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

### Chạy song song tới đâu
**1 tab / 1 tài khoản** (`browser.tabs_per_account: 1`): mỗi tài khoản chạy đúng một
collection tại một thời điểm, song song diễn ra ở mức tài khoản. Tăng số tab thì nhanh
hơn nhưng đốt lượt của tài khoản đó nhanh tương ứng và dễ bị ChatGPT chặn, nên để 1.

Bộ điều phối là **động**: tài khoản chọn thêm giữa lượt (hoặc dự bị được gọi vào) có
worker trong vòng nửa giây, không phải chờ đội hình cũ nghỉ. Sau khi hết việc, pool
nán lại `run.idle_exit_seconds` (mặc định 90s) với Chrome vẫn mở, nên lượt gen kế
tiếp trong khoảng đó dùng lại luôn, khỏi tốn 10-30s bật lại cả đội.

### Gen lại mà không làm lại (chống hết token giữa chừng)
Ảnh và thư mục được gom theo cấu trúc phân cấp:
```
/designs
├── <design>/ (ví dụ: cute-cat/)
│   ├── <mockup>/ (ví dụ: mug/, tshirt/, tote-bag/)
│   │   └── <mockup>__<job_id>.png
```
Cấu trúc thư mục `designs/<design>/<mockup>` đã định danh duy nhất từng ảnh cần có,
nên **đĩa chính là sổ tiến độ**: thư mục nào đã có ảnh thì lượt sau bỏ qua. Chạy 50 chủ
đề mà hết token ở chủ đề thứ 23, chỉ cần bấm gen lại đúng như cũ — tool tự làm tiếp từ
chỗ dở, bộ nào đã đủ ảnh thì không mở chat lần nữa. Cách này sống sót qua cả restart
server lẫn mất điện vì không phụ thuộc sổ sách trong RAM.

Bỏ tick **"Bỏ qua ảnh đã có"** trong hộp chọn tài khoản nếu muốn gen đè lại tất cả.
Khi không còn gì để làm, tool báo luôn thay vì mở Chrome vô ích.
Khi tải file ZIP về qua nút "Tải về" / "Tải tất cả (ZIP)", file ZIP cũng giải nén ra đúng
cây thư mục `/designs/<design>/<mockup>/...`.

### Tài khoản đứng hình
Không phải lỗi nào cũng báo ra chữ. Có lúc tab mở chat xong rồi nằm im: khung soạn
trống, không đính kèm được ảnh, không gửi được gì. Nếu quá `run.stall_timeout`
(mặc định 120s) mà một collection chưa ra nổi ảnh nào, tool coi tài khoản đó là đứng
hình, **ngừng giao việc cho nó** và đẩy collection sang tài khoản khác — thay vì ngồi
thử lại đủ 4 vòng mất hàng phút.

Nguyên nhân hay gặp nhất đã xử lý riêng: trang ChatGPT có nhiều `input[type=file]`,
trong đó mấy cái `mobile-composer-*` là của khung soạn bản mobile. Nhét ảnh vào đó thì
khung desktop không nhận gì. Tool giờ thử lần lượt từng input (ưu tiên cái không phải
mobile) và **kiểm tra thumbnail có hiện ra thật không** mới đi tiếp; hỏng hết thì bấm
nút "+" rồi hứng hộp thoại chọn file.

### Hết lượt thì chuyển tài khoản
Tài khoản nào bị ChatGPT chặn vì hết lượt tạo ảnh sẽ bị đánh dấu và **ngừng nhận
việc**. Collection đang dở của nó quay lại hàng đợi để tài khoản khác trong đội hình
nhặt — nếu mọi tài khoản đang bận thì nó xếp hàng chờ. Khi cả đội hình đã chọn đều
hết lượt, pool tự **gọi tài khoản dự bị** (những tài khoản còn lại trong `config.yaml`
mà bạn không tích chọn) vào chạy tiếp. Hết sạch cả dự bị mới báo lỗi.

Collection chuyển sang tài khoản khác sẽ được **gen lại từ đầu**, vì chat cũ nằm ở
tài khoản cũ — ghép ảnh của hai chat vào một bộ là mất tính đồng nhất. Muốn giữ ảnh
đã có (nhanh hơn, đổi lại bộ ảnh pha hai hướng design) thì bật
`run.resume_partial_on_other_account: true`.

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
ảnh tải về nhiều lần thì gom theo **nội dung ảnh** (hash bytes) chứ không theo URL —
ChatGPT có thể phục vụ mọi ảnh qua cùng một đường dẫn, gom theo URL là cả loạt ảnh
khác nhau bị nhập làm một.

Hai lớp chặn để không nhận nhầm chính ảnh mình gửi lên (ChatGPT tải template về để
hiển thị trong tin nhắn, cũng là response ảnh to như thật): **hash SHA-256** của các
template vừa gửi, và mốc **"đã thấy ChatGPT bắt đầu gen"** — mọi ảnh bắt được trước
mốc đó đều bị bỏ.

Chốt ảnh cũng theo **DOM**: đủ số ảnh chưa chắc đã xong, nên tool còn đợi danh sách
ảnh đứng yên `run.settle_seconds` giây; lúc chốt, ảnh nào không còn hiển thị trong câu
trả lời thì đó là bản ChatGPT đã vẽ lại và bị loại. Chỉ khi DOM không đọc được mới
đoán bằng dung lượng.

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
