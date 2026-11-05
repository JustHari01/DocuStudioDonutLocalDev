# Dựng video có trách nhiệm cho YouTube

DocuRandom Studio giúp đa dạng nhịp dựng, thứ tự cảnh, chuyển cảnh và màu sắc. Công cụ
không thể chứng nhận một video hoặc một kênh chắc chắn tuân thủ chính sách, được kiếm
tiền hay không bị Content ID. Quyết định cuối cùng thuộc YouTube và chủ sở hữu quyền.

## Checklist trước khi bấm chạy toàn bộ

- Mở đúng workspace để media, prompt, VOICEVOX và lịch sử không lẫn với dạng video khác.
- Đợi các badge Codex, VOICEVOX và Flow cần dùng chuyển xanh. Nếu thiếu thành phần, bấm
  nút **Tải/cài** tương ứng; tool sẽ tự làm các bước có thể tự động hóa.
- Với Flow Extension, Edge yêu cầu xác nhận thủ công đúng một lần bằng **Load unpacked**.
  Tool đã mở sẵn trang extension và đúng thư mục, sau đó tự nhận biết khi kết nối xong.
- Kiểm tra số credit Flow hiển thị trên badge. Lỗi quota/giới hạn tài khoản sẽ hiện ngay
  trong tool; không nên liên tục gửi lại khi dịch vụ đã báo giới hạn.
- Đọc lại kịch bản, prompt và video render. Tính ngẫu nhiên của hiệu ứng không thay thế
  khâu biên tập, xác minh sự thật hoặc quyền sử dụng nội dung.

## Random không thay thế tính nguyên gốc

Theo chính sách hiện hành của YouTube, nội dung cốt lõi của mỗi video cần khác biệt đáng
kể và mang lại giá trị sáng tạo, giáo dục hoặc giải trí. Chỉ thay đổi filter, crop, tốc độ,
noise hay thứ tự cảnh không tự biến nội dung của người khác thành tác phẩm nguyên gốc.
Việc dùng các thao tác kỹ thuật để lách hệ thống phát hiện hoặc sản xuất hàng loạt nhiều
video gần giống nhau còn có thể vi phạm chính sách nội dung rác.

## Danh sách kiểm tra trước khi đăng

- Bạn tự tạo hoặc có giấy phép thương mại rõ ràng cho từng ảnh, video và audio.
- Bạn lưu bằng chứng mua, giấy phép, đường dẫn nguồn và yêu cầu ghi công nếu có.
- Video có câu chuyện, mục tiêu hoặc góc nhìn riêng; không chỉ là slideshow + nhạc + hiệu ứng.
- Bạn bổ sung lời bình, voice-over, phân tích, thông tin hoặc giá trị biên tập đáng kể khi phù hợp.
- Bạn xem lại toàn bộ video, sửa thứ tự/nhịp dựng nếu cảnh ghép ngẫu nhiên làm mất mạch nội dung.
- Bạn không đăng hàng loạt các biến thể chỉ khác nhau ở hiệu ứng thẩm mỹ.
- Tiêu đề, thumbnail và mô tả phản ánh đúng nội dung.

File `.docurandom.json` cạnh video lưu seed, thứ tự cảnh, trim, hiệu ứng, transition và dấu
vân tay đầu ra. Đây là hồ sơ kỹ thuật hỗ trợ kiểm tra, không phải giấy chứng nhận bản quyền
hoặc chính sách YouTube.

## Upload qua Donut Local và khóa chống đăng trùng

- DocuRandom điều khiển profile qua Donut MCP/CDP local, không dùng tọa độ chuột và không
  phụ thuộc chữ đang hiển thị bằng tiếng Việt, Nhật hay Anh. Profile có thể bị thu nhỏ;
  browser và máy vẫn phải đang chạy, không ở chế độ sleep/hibernate.
- Upload mới luôn tạo một tab nền riêng có `targetId` cố định; không điều hướng hoặc bấm
  vào tab YouTube người dùng đang treo. Tab riêng tự đóng khi upload thành công. Chỉ khi
  khôi phục một phiên đã gắn file nhưng chưa chắc chắn, tool mới dò các tab còn giữ đúng
  video theo tên và dung lượng để tiếp tục mà không gắn file lần hai.
- Trước khi gắn file, tool băm toàn bộ nội dung video bằng SHA-256 và tạo khóa theo profile.
  Đổi tên hoặc chuyển cùng video sang workspace khác không tạo được lượt upload thứ hai.
- Việc gắn file là ranh giới một lần duy nhất vì YouTube có thể tạo bản nháp ngay tại đây.
  Sau ranh giới này, lỗi proxy, logout, tab chuyển trang, cảnh báo từ YouTube, mất kết nối
  hoặc app bị đóng đều chuyển tác vụ sang **Cần xác minh**. Tool không tự gắn file/bấm Lưu
  lại, nhằm tránh tạo video trùng hoặc spam kênh.
- Trước ranh giới gắn file, lỗi được ghi `error` và có thể thử lại sau khi sửa. Sau ranh giới,
  checkpoint được giữ ở `uncertain`; hãy mở đúng profile và kiểm tra phần Nội dung/Bản nháp
  trên YouTube Studio. Không xóa checkpoint hoặc sổ upload để ép thử lại khi chưa xác minh.
- Tool chỉ ghi `complete` sau khi lấy được đường dẫn video xác nhận từ YouTube. Một video đã
  hoàn tất sẽ trả lại kết quả cũ ở mọi lần bấm sau và không thao tác trên trang lần nữa.

## Hẹn giờ workflow, upload và thumbnail

- Mỗi workspace có hai lịch riêng theo giờ Việt Nam (GMT+7): **Chạy workflow + render** và
  **Upload YouTube**. Tick chỉ dùng để chọn lịch; sau khi chọn ngày giờ phải bấm
  **Bắt đầu hẹn giờ** thì tool mới canh giờ. Nút **Hủy hẹn giờ** dừng cả bộ hẹn giờ nhưng
  giữ nguyên lựa chọn để có thể bắt đầu lại. Tool phải được mở và máy không được
  sleep/hibernate.
- Nút **Bắt đầu hẹn giờ** có màu xanh và đổi thành **Đang canh lịch** khi đã kích hoạt;
  lúc đó nút **Hủy hẹn giờ** mới hoạt động. Vì vậy có thể nhìn ngay trạng thái mà không
  cần đoán từ riêng màu sắc hoặc dấu tick.
- Không cần nhập ngày giờ bằng bàn phím. Bấm **Chọn ngày & giờ** hoặc bấm vào ô thời gian
  để mở lịch; chọn ngày, giờ và phút rồi bấm **Áp dụng**. Có nút nhanh Hôm nay, Ngày mai
  và Bây giờ; kết quả luôn được lưu theo giờ Việt Nam.
- Nếu đến giờ YouTube khi workflow/render còn chạy, lịch chỉ chuyển sang trạng thái chờ.
  Khi video và file Tổng hợp đã hoàn tất, tool mới giữ khóa và bắt đầu đúng một lượt upload.
- Ô **Ảnh thumbnail tùy chọn** nhận PNG/JPG/JPEG/WEBP. Nếu ảnh không còn tồn tại, tool ghi
  cảnh báo rồi tiếp tục đăng video mà không dùng ảnh đó; không coi đây là lý do thử lại video.

## Realtime 60 phút và dịch vụ YouTube Like

- Chọn đúng profile/kênh ở phần YouTube. Bộ đọc dùng DOM/Accessibility qua Donut MCP,
  tự lấy ID và link từng video; không bấm theo tọa độ và không cần giữ cửa sổ ở một kích thước.
- Mỗi lần kiểm tra tạo một tab Analytics nền riêng, khóa đúng `targetId` và đóng tab sau
  khi đọc. Người dùng có thể giữ tab YouTube/upload của mình hoặc làm việc ở tab khác trong
  cùng profile; monitor không điều hướng các tab đó và không cần tab nền ở foreground.
- **Kiểm tra realtime · chỉ đọc** chỉ mở trang Nội dung/Analytics và đọc số
  `Views · Last 60 minutes`. Chế độ này tuyệt đối không gọi action `add` của 1MM.
- API key 1MM được mã hóa bằng Windows DPAPI. Danh sách trong tool chỉ giữ các gói
  YouTube Like. Sau mỗi lần đọc realtime, chế độ tự động gọi lại `services` để nhận giá,
  min và max mới nhất rồi chọn gói hợp lệ có đơn giá thấp nhất; không phụ thuộc gói đang
  chọn cho đơn thủ công.
- Công thức cho từng video là một tỷ lệ ổn định ngẫu nhiên từ 5% đến 10% số view trong
  60 phút. Kết quả dưới 10 Like chỉ được ghi là đang chờ, không đặt đơn.
- Tool quét lại sau mỗi 2 phút để phát hiện video tăng view. Sau một đơn thành công, cùng
  video phải chờ đủ 30 phút; nếu realtime lúc đó vẫn đạt ngưỡng, công thức hiện tại được
  tính lại và có thể tạo đơn tiếp theo.
- Bật tự động luôn có hộp xác nhận vì có thể trừ tiền thật. Nếu phản hồi sau khi gửi không
  xác định được mã đơn, checkpoint chuyển sang trạng thái cần xác minh và tự động dừng để
  tránh đặt trùng. Không xóa checkpoint trước khi kiểm tra tài khoản 1MM.
- Tool và máy phải tiếp tục chạy, profile phải còn đăng nhập YouTube Studio. Chế độ sleep,
  mất phiên, proxy lỗi hoặc Donut MCP dừng sẽ làm lần quét hiện tại dừng an toàn.

## CTA và logo trong video

- Bấm **Xem trước & tùy chỉnh vị trí** để xem đúng canvas video thành phẩm, gồm nền mờ và
  khung media chính ở giữa. Nếu đang chọn chế độ cắt đầy khung, preview cũng hiển thị đầy khung.
- Cửa sổ preview tự co theo màn hình và luôn giữ đúng tỷ lệ video. Có thể kéo vạch chia giữa
  preview và bảng điều khiển; khi phóng to/thu nhỏ, hai bên tiếp tục scale theo tỷ lệ đã chọn.
  Nhấn `Esc` để hủy hoặc `Ctrl+Enter` để áp dụng nhanh bằng bàn phím.
- CTA nhận MP4/WebM/MOV/MKV/AVI/GIF hoặc ảnh. Chế độ tự động nhận nền xanh, nền đen hoặc
  nền trong suốt; preview và render thật dùng cùng cách xóa phông. Có thể ép chọn loại nền
  hoặc tắt xóa phông nếu nhận diện tự động không phù hợp.
- Preview CTA video phát từ đầu đến hết toàn bộ tệp rồi tự lặp, có nút **Phát/Tạm dừng**,
  thanh tiến độ và đồng hồ. Đây là luồng xem nhẹ; video xuất cuối vẫn dùng tệp CTA gốc.
- CTA và logo đều chọn được **Toàn bộ video**, **Đoạn đầu**, **Đoạn cuối** hoặc
  **Khoảng tùy chọn**. CTA có thêm **Phát một lần** để giữ cách hoạt động cũ. Các ô giây
  chỉ hiện khi chế độ tương ứng cần chúng, và mọi giá trị đều được kiểm tra trước khi lưu.
- Logo nhận PNG/JPG/JPEG/WEBP/BMP/TIFF; nên dùng PNG nền trong suốt. Người dùng có thể kéo
  CTA/logo trực tiếp trên preview, chỉnh vị trí ngang/dọc, kích thước và độ rõ của logo.
- Bấm **Áp dụng cho workspace** để lưu. Tệp được chép vào `.docurandom\overlays`, không nằm
  trong `Media`, nên việc lưu kho và dọn phiên đã upload không làm mất CTA/logo. Render tay,
  workflow lặp và workflow hẹn giờ đều dùng đúng cấu hình đã lưu.

## Telegram Bot

- Trước khi kết nối, nếu token từng xuất hiện trong tin nhắn hoặc ảnh chụp, hãy dùng
  `/revoke` trong BotFather rồi tạo token mới. Không dùng lại token đã lộ.
- Tại màn hình workspace, bấm **Telegram**, dán token mới, chọn loại thông báo rồi bấm
  **Lưu & kết nối**. Token được mã hóa bằng tài khoản Windows hiện tại và không nằm trong
  workspace, log hay gói phát hành.
- Bấm **Tạo mã ghép cặp**, mở private chat của bot và gửi `/pair <mã>`. Mã chỉ dùng một lần,
  hết hạn sau 5 phút; mặc định chỉ đúng chat đó được phép điều khiển tool.
- Đây là một bot chung cho toàn bộ DocuRandom, không phải bot riêng từng workspace. Sau khi
  ghép cặp, bấm nút **Menu** của Telegram hoặc dùng bàn phím nút nhanh; danh sách lệnh và mô
  tả tiếng Việt được tool đăng ký tự động nên không cần nhớ cú pháp.
- `/status`, `/workspaces`, `/select`, `/run`, `/continue`, `/pause`, `/cancel` và `/logs`
  thao tác trên workflow. Lệnh cũ tồn trước lúc tool mở bị bỏ qua để không chạy lại ngoài ý muốn.
- `/flow_start`, `/flow_status`, `/voicevox_start` và `/voicevox_status` cho phép chuẩn bị lại
  engine khi tool vừa mở hoặc máy đã khởi động lại. Kết quả thành công hay lỗi được gửi về
  đúng chat đã ghép cặp, không mở hộp thoại chặn màn hình đối với lệnh từ xa.
- `/schedule_workflow 08:30 daily` và `/schedule_upload 20:00 once` đặt lần kế tiếp theo giờ
  Việt Nam; cũng chấp nhận `29/08/2026 08:30`. Dùng `/schedule_start` để bắt đầu canh và
  `/schedule_cancel` để hủy nhưng giữ nguyên các mốc đã chọn.
- `/thumbnail_prompt` đọc prompt thumbnail trong file Tổng hợp của workspace. Gửi ảnh không
  caption cho workspace đang chọn, hoặc caption `/thumbnail <tên workspace>`, rồi nhập mã
  `/confirm_thumbnail` do bot trả về. `/thumbnail_current` xem ảnh đang chọn;
  `/thumbnail_delete` cũng yêu cầu mã xác nhận. Ảnh Telegram cũ được lưu vào lịch sử thay vì
  xóa vĩnh viễn; file gốc nằm ngoài tool không bao giờ bị bot xóa.
- `/errors` trả các lỗi gần nhất. Thông báo lỗi chủ động luôn ghi rõ workspace, công đoạn, mã/
  chi tiết và hướng xử lý; lỗi mạng Telegram không dừng workflow trên máy.
- Khi bật **Thông báo trạng thái**, một video đủ tiêu chí và đặt Like thành công sẽ tạo thẻ
  Telegram có workspace, tiêu đề và link video, view trong 60 phút, tỷ lệ, số Like thực đặt,
  gói rẻ nhất và mã đơn. Đơn chưa xác nhận được mã sẽ hiện cảnh báo riêng và tự dừng chống trùng.
- `/upload` chưa đăng video ngay. Bot trả mã riêng; chỉ `/confirm_upload <mã>` đúng trong
  2 phút mới chuyển một yêu cầu vào tool. Mã bị hủy ngay sau một lần nhập và lượt upload vẫn
  đi qua checkpoint, khóa nội dung video + profile và mọi hàng rào chống đăng trùng YouTube.

## Kho phiên đã đăng và workspace sạch

- Chỉ sau khi YouTube trả URL và checkpoint ghi hoàn tất, tool mới tạo thư mục
  `Uploaded\\<ngày-giờ-tạo-video>_...` trong workspace.
- Kho này giữ toàn bộ `Output`, `Media`, `VOICEVOX`, bản sao `Prompts`, checkpoint, cấu hình
  phiên và log. Video cùng file Tổng hợp được đổi tên theo giờ Việt Nam để các lần dùng lại
  cùng tên output không ghi đè lẫn nhau.
- Khi lưu kho thành công, `Output`, `Media`, `VOICEVOX` và checkpoint tác vụ hiện tại được
  làm sạch như một phiên mới. Master Prompt, thiết lập, lịch, profile YouTube, lịch sử render
  và sổ chống đăng trùng vẫn còn để lần tự động tiếp theo hoạt động.
- Nếu upload lỗi, bị `uncertain`, thiếu URL hoặc lưu kho gặp lỗi, tool giữ nguyên dữ liệu hiện
  tại và không dọn workspace. Điều này ưu tiên khả năng kiểm tra và chống đăng trùng.

## Nguồn chính thức

- [Chính sách kiếm tiền, nội dung không chân thực và nội dung được sử dụng lại](https://support.google.com/youtube/answer/1311392?hl=vi)
- [Chính sách về nội dung rác](https://support.google.com/youtube/answer/2801973?hl=vi)
- [Quyền sử dụng thương mại cần có để kiếm tiền](https://support.google.com/youtube/answer/2490020?hl=vi)
- [Bản quyền trên YouTube](https://support.google.com/youtube/answer/2797466?hl=vi)
- [Trường hợp sử dụng hợp lý](https://support.google.com/youtube/answer/9783148?hl=vi)

Tài liệu được đối chiếu ngày 20/07/2026. Chính sách có thể thay đổi; hãy kiểm tra lại các
trang chính thức trước khi xuất bản.
