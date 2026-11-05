# Phục hồi thumbnail từ 2.4.288 / Bridge 1.0.136

Biên nhận của mỗi video gắn với đúng chat, SHA-256 của toàn bộ prompt ảnh và user message. Lấy mốc DOM mới sau khi nhập draft, ngay trước Send; số lượng/key DOM dựng lại không đủ để xác nhận gửi. Khi thử cùng prompt lần nữa, phải nhìn thấy một user message mới và composer đã tiêu thụ draft. Nút Send phải hiện, đúng ưu tiên selector và không bị che.

| Trạng thái | Hành động |
|---|---|
| Ảnh đã xuất hiện | Đọc ảnh thuộc đúng prompt/chat, kiểm tra bytes và SHA-256, lưu trước khi hỏi xác nhận; không tạo ảnh mới. |
| Đã phát thao tác gửi nhưng chưa có user echo | Lưu trạng thái chưa rõ kết quả ngay từ checkpoint gửi; dùng ngân sách đối soát, không quay lại gửi mới sau 30 giây. |
| Có POST thực tế nhưng không có DOM mới, kể cả HTTP 200 rồi net::ERR_FAILED | Không được coi là chưa gửi. Gắn thời gian/HTTP/mã lỗi mạng với biên nhận đúng window và giữ qua khởi động; không cấp quyền resend bằng vắng DOM. Chỉ đọc lại kết quả từ đúng chat. |
| Bridge chứng minh chưa gửi, nhưng lần resend truyền của client cũng lỗi | Giữ bằng chứng đúng thread/toàn bộ SHA prompt về Studio. Sau 60 giây, chỉ cấp một lần phục hồi truyền cho video này; nếu vẫn thất bại thì dừng tự gửi, không lặp qua khởi động lại. Thiếu bằng chứng hoặc lượt đã nhận thì không áp dụng. |
| Nút Send thay vị trí trong lúc lấy mốc/hash | Chọn lại nút chuẩn, kiểm tra draft/focus/điểm bị che và lấy tọa độ mới ngay trước thao tác. Phát pointer move rồi một click; không dùng tọa độ trước reflow. |
| Đã nhận bytes nhưng lượt xác nhận lỗi hoặc hết ngân sách dò | Kiểm tra ảnh trong `thumbnail-candidates`: đúng đường dẫn quản lý, SHA-256 bytes, prompt hiện tại, thread hiện tại và ảnh giải mã được. Nếu tất cả khớp, cài ngay ảnh đã nhận, không gửi thêm xác nhận hay tạo ảnh. Thiếu/hỏng/sai ảnh thì vẫn chặn. |
| Khởi động lại hoặc Bridge chưa sẵn sàng, nhưng đã có ảnh hợp lệ | Phục hồi ảnh đã lưu trước kiểm tra Bridge; không cần mở browser, lấy pool hay dùng hạn mức ChatGPT. |
| Lượt xác nhận che mất ảnh ở phía trước | Dò user message đúng toàn bộ SHA-256 prompt và chỉ lấy ảnh trước user message kế tiếp. Không lấy ảnh nguồn, ảnh của video khác hoặc ảnh cũ khi có prompt trùng mới hơn. |
| Prompt dài bị bọc bởi “Show more” | Dùng phần prompt đầy đủ trong bubble khi tính SHA, không dùng chữ của nút/ellipsis bên ngoài. |
| Biên nhận đã được xóa sau phản hồi hoàn tất | Đọc lại kết quả/ảnh đúng prompt; không gọi là checkpoint đổi và không coi thiếu biên nhận là quyền gửi mới. |
| Lượt ảnh quá 30 phút, cùng prompt, không có ảnh/phản hồi mới | Đọc hai snapshot ổn định. Nếu còn Stop, quan sát ít nhất 90 giây qua các lần kiểm tra; chỉ dừng đúng lượt ảnh đó một lần. |
| Stop đã mất và trạng thái ổn định | Lưu nguyên biên nhận cũ vào nhật ký Bridge, cấp quyền cho một lượt ảnh thay thế trong cùng chat. |
| Biên nhận bản cũ xác nhận nhầm do DOM dựng lại | Chỉ đối soát khi lịch sử trước gửi vẫn khớp, không có generation/ảnh, draft còn nguyên hoặc biên nhận cũ đã quá một giờ. Giữ biên nhận gốc và ghi rõ lý do thay thế. |
| Ảnh đến muộn, người dùng nhập thêm hoặc chat/prompt đổi | Giữ lượt đang có, không dừng/gửi ảnh mới. |
| HTTP 429 | Tôn trọng Retry-After; không dừng lượt cũ, không đổi sang Instant và không tiêu hao lượt đối soát. |
| Đăng nhập/quyền truy cập không hợp lệ | Giữ cookie/checkpoint; cần khôi phục đăng nhập/quyền, không giả lập thành công. |
| API diagnostic | Đọc snapshot mặc định hoặc hash/độ dài composer theo tùy chọn; không trả plaintext draft và không gửi prompt. Khi owner đang chạy, trả trạng thái trì hoãn để tránh tranh chấp renderer. |

Mỗi lượt có tối đa bốn lần đối soát chủ động. Sau đó chỉ đọc ảnh đến muộn tối đa bốn lần, cách ít nhất 30 phút. Mỗi prompt video có tối đa ba lượt ảnh thay thế; hết giới hạn thì giữ nguyên nguyên liệu và báo cần xử lý. Bộ đếm và quyền phục hồi được lưu qua khởi động, không đặt lại chỉ vì lỗi đọc hoặc sửa metadata.

Biên nhận mới được xác minh có bộ đếm đọc riêng, không kế thừa ngân sách đọc của biên nhận trước. Cùng một mã biên nhận không được đặt lại ngân sách. Khi ngân sách tạo ảnh đã cạn nhưng còn biên nhận mới, chỉ recover kết quả; không Stop hay cấp ảnh mới. HTTP của POST tạo lượt được ghi ngay khi nhận headers và giữ riêng khỏi các yêu cầu sidebar, không lưu URL/body/header/token/cookie.

Thông báo tự phục hồi cùng lỗi/chat/prompt/lượt tạo ảnh được gộp trong 12 giờ và lưu ở `.docurandom/workflow-notifications.json`. Lỗi hạn mức, đăng nhập, video mới, lượt ảnh mới hoặc chuyển sang cần xử lý vẫn được báo ngay. Toàn bộ exception vẫn ghi trong `diagnostic.log` và `application.log`.

Chỉ trạng thái `complete` với file ảnh thật đã kiểm tra mới được sử dụng làm thumbnail. Điều kiện đầy đủ metadata/thumbnail trước upload hoặc Public được giữ nguyên. Lịch tắt, profile, cookie, queue Public/Unlisted và nguyên liệu hiện có không được reset bởi phục hồi ảnh.

Các kiểm tra liên quan: `test_stalled_thumbnail_repair.py`, `test_persistent_workflow_notifications.py`, `stalled-image-recovery.test.cjs`, `electron-stalled-image-recovery-smoke.cjs`, `electron-submission-baseline-smoke.cjs`, `electron-submission-hold-smoke.cjs`, `electron-owned-image-smoke.cjs` và `electron-diagnostic-route-smoke.cjs` (phiên thử riêng, không có yêu cầu ra Internet).
