# Tương tác Google theo workspace

Trong màn hình chọn workspace, mỗi thẻ có **Google định kỳ** và **Thiết lập Google**.
Chức năng này dùng profile Donut mà workspace đã chọn. Lịch Google độc lập với lịch
sản xuất và đăng video. Mặc định chưa bật cho bất kỳ workspace nào.

1. Mở **Thiết lập Google** của workspace cần dùng.
2. Chọn ứng dụng, từ khóa/địa chỉ, chu kỳ và số ứng dụng mỗi lượt.
3. Tùy chọn tạo file hoặc tải thumbnail chỉ chạy khi được đánh dấu riêng.
4. Bấm **Chạy một lượt ngay** để kiểm tra, hoặc bật chạy định kỳ và lưu.

Danh mục hiện có 26 ứng dụng. Mở, đọc và lướt trang là thao tác chung. Search,
Maps, YouTube, Translate và News có nội dung tìm kiếm. Docs/Sheets/Slides/Forms
có tạo file riêng của lượt chạy; Docs có nội dung ghi chú, Sheets có bảng dữ liệu.
Drive có tải ảnh thumbnail hiện tại của workspace và xác minh tệp đã xuất hiện.
Những thao tác này không đồng nghĩa với hỗ trợ mọi chức năng của cả 26 dịch vụ.

Mỗi ứng dụng chỉ tạo tối đa một file/ngày/workspace/profile. Mỗi ảnh thumbnail chỉ
tải một lần theo mã băm nội dung. URL và biên nhận ở thư mục
`.docurandom/google-workspace/` trong workspace, cũng hiển thị ở cửa sổ thiết lập.
Lượt chưa rõ kết quả được đọc lại theo biên nhận; không tự gửi lại thao tác ghi.

Google dùng cùng bộ điều phối profile với YouTube. Khi profile đang bận, lượt
Google chờ; upload và Public được ưu tiên. Chức năng chỉ đóng tab do chính nó mở.
Đăng nhập, CAPTCHA và yêu cầu xác minh tài khoản phải được người dùng xử lý trong
đúng profile; không thay proxy, dấu vân tay, cookie hay danh tính để vượt qua.

Kết quả thực tế ngày 28/10/2026: DaiTa4 đã hoàn tất Search và Maps. Google Docs
chuyển sang đăng nhập, nên kiểm thử Docs/Sheets/Drive chưa hoàn tất. Không có file
Google nào được tạo hoặc upload trong lượt bị chặn này. Dữ liệu và kế hoạch lượt
được giữ để kiểm tra tiếp sau đăng nhập.

## Phục hồi phiên (Studio 2.4.335 / extension 1.1.42)

Tool phân biệt chọn tài khoản, cần đăng nhập, thiếu quyền và cần xác minh. Các
trạng thái này dừng tự mở lại profile; sau khi xử lý đúng tài khoản, người dùng
bấm chạy tiếp. HTTP 429 và thời gian Retry-After được giữ qua khởi động lại,
kể cả khi bấm chạy thủ công. Lượt chưa rõ kết quả chỉ dò đúng biên nhận sau
2, 5 và 15 phút; hết ngân sách thì chuyển sang cần kiểm tra.

Lệnh chạy một lượt được lưu xuống đĩa. Khởi động lại không tạo một lượt mới,
không nhập lại nội dung hoặc tải lại tệp chưa rõ kết quả. Khi tab chuyển sang
tài liệu khác, tool dừng trước khi nhập tiếp. Profile do tool mở cũng được giữ
nếu xuất hiện tab mới hoặc tab người dùng đã chuyển trang. Lịch sử chẩn đoán
giới hạn 100 bản ghi tại `activity.json`, gồm bước, số lần thử và mở/đóng profile.

Kiểm thử thật Docs/Sheets/Drive của DaiTa4 vẫn tạm chờ theo lựa chọn người dùng.
Các kiểm thử tự động về phục hồi không thay cho xác minh tạo/tải file thật.

## Lịch và nghiệm thu (Studio 2.4.336)

Thiết lập Google có ba thẻ: **Lịch & giới hạn**, **Ứng dụng & tác vụ** và
**Trạng thái & kế hoạch**. Chọn riêng ngày trong tuần, khung giờ Việt Nam,
số lượt mới tối đa/ngày (mặc định 3) và số phút mỗi phiên (mặc định 10).
Hai giờ giống nhau nghĩa là cả ngày; khung qua nửa đêm tính theo ngày bắt đầu.
Giới hạn vẫn áp dụng khi bấm chạy một lượt. Phục hồi dùng cùng mã lượt đã lưu;
không cấp thêm lượt hoặc gửi lại thao tác ghi chỉ vì đã sang ngày mới.
Hết thời gian phiên dừng tại ranh giới kiểm tra, không cắt ngang lệnh ghi chưa rõ.

**Xem kế hoạch** không chạy Google hoặc cấp mã lượt; khi còn checkpoint sẽ
hiển thị kế hoạch cũ, không thay bằng các lựa chọn mới. **Kiểm tra kết nối**
chỉ hỏi extension đã ghép, không mở profile/tab, không đăng nhập hay tạo file.
Extension kết nối được không có nghĩa cả 26 ứng dụng đã đăng nhập hoặc nghiệm thu.
Danh mục cho biết thao tác được code hỗ trợ và kết quả thực tế đã được lưu riêng.

`daily-summary.json` lưu số lượt đã bắt đầu, thời gian chạy và kết quả thành công
gần nhất theo ứng dụng. Số lượt hoàn tất/phục hồi ghi rõ phạm vi lịch sử 100 bản ghi;
dữ liệu này không phải điểm uy tín tài khoản. `diagnostics.json` lưu kiểm tra chỉ đọc.
Các lệnh local `google_preview` và `google_diagnostics` cần cùng xác thực loopback
với các lệnh bảo trì hiện có. Google định kỳ không tự bật khi cập nhật.

## Chờ công bằng (Studio 2.4.337)

Lượt Google đang chờ giữ một vị trí FIFO có heartbeat, thay vì phải tranh chỗ
với mỗi lượt đọc số liệu mới. Khi phiên đang chạy kết thúc, lượt đọc số liệu
đến sau không vượt qua lượt Google. Upload/Public vẫn được ưu tiên và có thể
yêu cầu Google nhường phiên. Vị trí chờ tự hết hạn sau 30 giây không còn heartbeat;
tắt chức năng hoặc đóng Studio sẽ hủy vị trí ngay.

Lệnh chạy thủ công đã lưu được nối sau khi khởi động lại, không bị mốc chu kỳ
cũ trì hoãn đến hôm sau. Nó vẫn tuân theo Retry-After, khung giờ và quota ngày.
