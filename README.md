# DocuStudioDonutLocalDev

Monorepo phát triển **Donut Local + DocuRandom Studio**, nhập từ source hiện có trên máy ngày 05/11/2026. Bao gồm các sửa đổi hiện có, Bridge, Flow, hai extension và thư viện Master. Repo dùng để làm việc với mã nguồn; ứng dụng portable hiện tại vẫn ở vị trí cũ.

## Phiên bản source

| Thành phần | Phiên bản |
|---|---|
| Donut Local | 0.36.0 |
| Studio | 2.4.339 |
| Chat Bridge | 1.0.158 |
| Donut Bridge extension | 1.1.43 |
| Flow extension | 1.3.52 |
| Master | 11.1.0-r1 — 20 chủ đề |

## Cấu trúc

```text
src/ + src-tauri/       Donut Local: giao diện và backend Rust/Tauri
donut-sync/             Sync server
sdk/                   SDK
Studio/docurandom/     Ứng dụng Studio Python
Studio/chat_bridge/   Chat Bridge Electron
Studio/flow_server/   Backend Flow
Studio/flow_engine/   Tích hợp Flow
Studio/donut-extension/  Extension nối Donut
Studio/flow-extension/   Extension Flow
Studio/tests/         Kiểm thử Studio
Studio/assets/research/  Contract và dữ liệu nghiên cứu cho Master
MasterPrompts/        20 Master TXT hoàn chỉnh
portable-dev/*.ps1   Source launcher, không kèm bộ cài/dữ liệu
docs/source-import/  Nguồn gốc, manifest hash và tài liệu cũ
```

## Bắt đầu làm việc

GitHub Actions đang tắt ở lần nhập đầu tiên để các workflow kế thừa không tự build/phát hành trước khi được rà lại cho monorepo. Source workflow vẫn được giữ; có thể cấu hình rồi bật lại trong Settings → Actions. Lần đưa source lên này chưa chạy lại bộ build/kiểm thử ứng dụng.

```powershell
git clone https://github.com/JustHari01/DocuStudioDonutLocalDev.git
cd DocuStudioDonutLocalDev
```

Donut dùng Node/pnpm và Rust/Tauri; xem [LOCAL_DEV.md](LOCAL_DEV.md), [CONTRIBUTING.md](CONTRIBUTING.md) và `package.json` cho các lệnh hiện có. Có thể bắt đầu bằng `pnpm install` rồi `pnpm dev:local`; dùng dữ liệu thử riêng. Các runtime/browser hoặc binary bên thứ ba cần cài hoặc tải lại theo script của từng thành phần.

Studio dùng Python; tạo môi trường riêng rồi cài `Studio/requirements.txt`. Xem [hướng dẫn Studio](Studio/README_VI.md), [đóng gói](Studio/build_release.ps1) và [thông báo bên thứ ba](Studio/THIRD_PARTY_NOTICES.md). Bridge dùng `Studio/chat_bridge/package.json` và lockfile riêng. Mẫu cấu hình Flow nằm trong `Studio/flow-extension/config.example.js` và `Studio/flow_engine/local_config.example.py`; cấu hình có khóa thật chỉ giữ trên máy.

Giữ nguyên bố cục thư mục để các đường dẫn tương đối Donut–Studio hoạt động như source cũ. Một số `.spec`, launcher và probe vẫn ghi đường dẫn cài đặt gốc; repo chưa được tuyên bố build/portable hoàn toàn trên máy khác. Không chạy launcher của bản clone trước khi cấu hình runtime/dữ liệu thử.

## Quy tắc dữ liệu

Không commit profile, cookie, dữ liệu tài khoản, khóa API, `.env`, file video, checkpoint, queue, cache, thư mục `Data`/`Workspaces`, executable hoặc dependency đã cài. Có `.gitignore` cho các nhóm này. Source snapshot không di chuyển hay đóng ứng dụng đang dùng, không đổi lịch hoặc gán lại workspace.

Đây là lần nhập source hiện tại, không nhập lịch sử Git cũ. Thông tin kiểm chứng và SHA-256: [SOURCE_SNAPSHOT.md](docs/source-import/SOURCE_SNAPSHOT.md) và [source-manifest.json](docs/source-import/source-manifest.json). Tài liệu cũ có thể ghi phiên bản lịch sử; bảng phiên bản ở trên lấy từ metadata source hiện có.

## Giấy phép và bên thứ ba

Donut giữ [LICENSE](LICENSE), tác giả và các thông báo upstream. Studio và các thành phần đi kèm giữ thông báo/điều kiện riêng; lần nhập này không cấp lại giấy phép cho toàn bộ monorepo. Xem [THIRD_PARTY_NOTICES.md](Studio/THIRD_PARTY_NOTICES.md). Repo Private không có nghĩa là thay đổi quyền của phần mềm bên thứ ba.
