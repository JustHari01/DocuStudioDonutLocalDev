# Local developer mode

Chạy trong thư mục repo:

```powershell
pnpm install
pnpm dev:local
```

Chế độ này bật Cargo feature `local-dev` cho bản Tauri debug, bật các capability local trong UI/API/MCP và dùng thư mục dữ liệu riêng `.local-dev-data`. Dữ liệu, profile và thông tin đăng nhập của bản Donut đang cài không bị dùng; kể cả khi trỏ vào một thư mục dữ liệu có sẵn, local-dev cũng không nạp phiên cloud. Cloud sign-in, profile refresh, token refresh, proxy/usage API, remote MCP và các lệnh cloud-auth đều bị chặn. Màn hình Account chỉ còn cấu hình sync tự host.

Local REST API và local MCP chỉ nghe trên loopback. MCP dùng token local như trước; cần chấp nhận Wayfern terms trong app trước khi bật. Các chức năng dựa vào cloud như Cloud Backup, team, Agent, Cookie Bot và remote browser vẫn tắt.

Donut hiện chỉ tích hợp browser engine Wayfern. Chế độ local mở các gate nằm trong mã nguồn Donut cho automation và chỉnh fingerprint, nhưng không cấp Wayfern token hay đổi gate nằm bên trong engine. Vì vậy CDP `Runtime.evaluate`, fingerprint tùy chỉnh/cross-OS hoặc tạo fingerprint có thể vẫn bị Wayfern từ chối nếu engine yêu cầu quyền của nó.

Extension tại `C:\Users\ndinh\Documents\DocuRandom Studio\App\_internal\flow-extension` là Flow Agent riêng: nó dùng `chrome.debugger` để thao tác tab `flow.google.com` và kết nối backend Flow Agent tại `127.0.0.1:8001`. Nó không thay thế CDP tổng quát cho profile Donut.

Build/release mặc định không bật Cargo feature `local-dev`; `local_dev::enabled()` cũng luôn trả false khi không phải debug build.
