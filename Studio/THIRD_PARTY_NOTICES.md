# Thông báo thành phần bên thứ ba

DocuRandom Studio dùng các thành phần sau khi chạy hoặc khi đóng gói:

## FFmpeg

- Trang dự án: <https://ffmpeg.org/>
- Windows build: <https://www.gyan.dev/ffmpeg/builds/>
- Mã nguồn: <https://ffmpeg.org/download.html>
- Bản Essentials Build được dùng ở đây có cấu hình GPL. Xem thông tin giấy phép
  đầy đủ tại <https://ffmpeg.org/legal.html>.

FFmpeg và FFprobe là các chương trình độc lập được gọi dưới dạng tiến trình con.
Thông báo này không thay thế nội dung giấy phép đi kèm bản build FFmpeg.

## Pillow

- Trang dự án: <https://python-pillow.github.io/>
- Giấy phép HPND: <https://github.com/python-pillow/Pillow/blob/main/LICENSE>

## Gemini Watermark Remover

- Gói: `@pilio/gemini-watermark-remover`
- Mã nguồn tích hợp: thư mục `watermark-remover-main`
- Giấy phép MIT; bản sao giấy phép được đóng gói tại `watermark-remover/LICENSE`.

DocuRandom Studio gọi CLI của thành phần này dưới dạng tiến trình con để xử lý ảnh
JPG/PNG/WebP trong máy người dùng. Ảnh không được tải lên dịch vụ từ xa.

## Node.js và Sharp

Node.js được đóng gói làm runtime cho Gemini Watermark Remover. Sharp và libvips được
dùng để giải mã/mã hóa ảnh. Xem giấy phép và thông báo của từng dự án:

- <https://github.com/nodejs/node/blob/main/LICENSE>
- <https://github.com/lovell/sharp/blob/main/LICENSE>
- <https://github.com/libvips/libvips/blob/master/LICENSE>

## Python và Tcl/Tk

- Python Software Foundation License: <https://docs.python.org/3/license.html>
- Tcl/Tk license: <https://www.tcl.tk/software/tcltk/license.html>

## PyInstaller

PyInstaller chỉ được dùng khi đóng gói. Bootloader được phân phối theo GPL với
ngoại lệ cho phép đóng gói ứng dụng. Xem <https://pyinstaller.org/en/stable/license.html>.

## VOICEVOX

DocuRandom Studio kết nối tới VOICEVOX Engine cục bộ qua API HTTP công khai
(`/speakers`, `/audio_query`, `/synthesis`). Bản portable không nhúng engine/model;
khi người dùng bấm khởi động lần đầu, ứng dụng tải gói VVPP Windows 0.25.2 trực tiếp
từ GitHub Releases chính thức của VOICEVOX Engine, kiểm tra SHA-256 rồi cài vào dữ
liệu ứng dụng. Engine chỉ chạy trên máy người dùng. VOICEVOX và từng nhân vật có
giấy phép/điều khoản sử dụng riêng:

- <https://voicevox.hiroshiba.jp/>
- <https://github.com/VOICEVOX/voicevox_engine>

## Faster Whisper và CTranslate2

DocuRandom Studio dùng Faster Whisper với model Whisper `small` để hậu kiểm có chọn
lọc một số câu tiếng Nhật rủi ro sau khi VOICEVOX render. Model chạy cục bộ bằng CPU
INT8, một worker; dữ liệu giọng không được gửi tới dịch vụ nhận dạng bên ngoài. Model
được tải một lần vào thư mục dữ liệu ứng dụng khi người dùng bật hậu kiểm.

- Faster Whisper (MIT): <https://github.com/SYSTRAN/faster-whisper>
- CTranslate2 (MIT): <https://github.com/OpenNMT/CTranslate2>
- Whisper/model weights (MIT): <https://github.com/openai/whisper>

## Flow Agent và Microsoft Edge

Theo yêu cầu của chủ dự án, bản portable chứa extension và backend từ source local
`flow-agent-main`. Thành phần này chạy bridge tại `127.0.0.1:8001` và điều khiển phiên
Google Flow đang đăng nhập trong Microsoft Edge. Source local không kèm file giấy phép;
việc sử dụng/phân phối lại chịu điều khoản của source đó, Microsoft Edge và Google Flow.
DocuRandom Studio không nhúng tài khoản Google và không cam kết quota/credit của Flow.

## ChatGPT Web Bridge

DocuRandom Chat Bridge 1.0.18 là mã clean-room nằm trong `chat_bridge/` của dự án, không
dùng source `codex-chatgpt-web`. Runtime dùng Electron 43.4.0. SHA-256 của executable
Windows x64 và `app.asar` được tạo, khóa trực tiếp từ source trong từng bản phát hành;
tool đối chiếu cả hai trước khi chạy. Bridge chỉ bind `127.0.0.1`, yêu cầu Bearer token ngẫu nhiên, chặn
quyền trình duyệt mặc định và dùng profile riêng trong
thư mục `Data\chat-bridge` do người dùng chọn lúc cài. Bridge không đọc cookie Edge/Chrome/app
ChatGPT, không sửa Codex và không có API key. Đây là browser automation không chính thức;
việc sử dụng chịu điều khoản/hạn mức ChatGPT và không thể bảo đảm 100% nếu tài khoản
Windows đã bị malware kiểm soát.

- <https://github.com/electron/electron>
- Giấy phép Electron/Chromium được kèm trong thư mục runtime bridge.

## OpenAI Codex CLI

Bản Windows nhúng executable Codex CLI x64 chính thức từ OpenAI để máy mới không phải
cài Node/npm toàn hệ thống. Asset phát hành được khóa phiên bản và SHA-256 trong quy
trình build; CLI chỉ làm transport tới ChatGPT Web Bridge local của DocuRandom.

- <https://github.com/openai/codex>
- Apache License 2.0: <https://github.com/openai/codex/blob/main/LICENSE>

## Send2Trash

Ứng dụng dùng Send2Trash để đưa workspace người dùng đã xác nhận xóa vào Thùng rác
Windows thay vì xóa vĩnh viễn ngay lập tức.

- <https://github.com/arsenetar/send2trash>

# Donut Browser

DocuRandom Studio có thể đóng kèm **Donut Local**, một bản dẫn xuất được GitHub tự động
build từ tag stable của Donut Browser với phạm vi vá tích hợp được công bố. Donut Browser
và bản dẫn xuất được phát hành theo giấy phép AGPL-3.0. Gói nhị phân, manifest có chữ ký,
SHA-256 và đường dẫn tải mã nguồn tương ứng được lưu trong
`donut-managed/donut-local-manifest.json`, `donut-managed/donut-local-manifest.sig` và
`donut-managed/SOURCE.txt` của bản đóng gói.

Donut Local giữ nguyên chính sách quyền theo tài khoản thật của upstream; bản vá không
vô hiệu hóa kiểm tra gói dịch vụ. Kho dữ liệu `%LOCALAPPDATA%\DonutBrowser` vẫn được dùng
chung để không tạo lại profile/cookie/proxy/fingerprint.

Mã nguồn: https://github.com/zhom/donutbrowser
