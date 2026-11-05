# Kích hoạt bộ não mô hình riêng cho GKC-AI

Mã nguồn đã có adapter `gkc_brain.py`. Adapter này kết nối tới máy chủ chạy mô hình mã nguồn mở do bạn tự triển khai, qua API tương thích OpenAI Chat Completions.

## Trạng thái hiện tại

- Bot Discord, bộ nhớ SQLite, kho Q&A và tìm kiếm web vẫn chạy theo cấu hình cũ.
- GKC Brain đã được tích hợp vào luồng trả lời.
- Repository **không chứa trọng số mô hình** và Wispbyte 516 MB RAM không đủ để chạy một LLM lớn.
- Vì vậy, GKC chưa trở thành mô hình độc lập hoàn chỉnh cho tới khi có máy chủ AI riêng đang chạy và điền các biến bên dưới.

## Biến môi trường cần thiết

Đặt trong Environment/Variables của host Discord:

- `GKC_MODEL_URL`: địa chỉ máy chủ mô hình, ví dụ `https://ai.example.com` (không dùng ví dụ này như địa chỉ thật).
- `GKC_MODEL_NAME`: tên model đúng như máy chủ yêu cầu.
- `GKC_MODEL_API_KEY`: tùy chọn, chỉ điền nếu máy chủ AI riêng có bật xác thực.
- `GKC_MODEL_TIMEOUT`: tùy chọn, mặc định 75 giây.
- `GKC_MAX_OUTPUT_TOKENS`: tùy chọn, mặc định 900.

Adapter tự chuẩn hóa URL gốc, URL kết thúc bằng `/v1` hoặc endpoint `/v1/chat/completions`. Lệnh Discord `/gkcstatus` kiểm tra `/health`, sau đó thử `/v1/models` để xác nhận máy chủ có phản hồi.

## Yêu cầu máy chủ AI

Máy chủ cần hỗ trợ endpoint OpenAI-compatible `POST /v1/chat/completions`, nhận `model`, `messages` và trả về `choices[0].message.content`. Có thể dùng llama.cpp server, vLLM hoặc LocalAI. Nên dùng HTTPS nếu máy chủ truy cập qua Internet.

Không đưa Discord token hoặc API key vào GitHub hay tin nhắn Discord. Không có máy chủ mô hình đang chạy thì các biến trên không thể tự tạo ra AI; bot sẽ dùng chế độ kiến thức/tìm kiếm dự phòng. Khi đã cấu hình máy chủ, câu hỏi kiến thức, lập trình và phân tích sẽ được gửi tới mô hình trước; phép tính dạng biểu thức và lời chào/cảm ơn đơn giản vẫn xử lý offline.
