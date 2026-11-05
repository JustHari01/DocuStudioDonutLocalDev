# DocuRandom Studio

Ứng dụng desktop Windows dựng video phong cách phim tài liệu từ một thư mục chứa lẫn
**ảnh và video**, có thể ghép soundtrack/voice-over và tự cân bằng timeline đúng theo độ
dài audio. Ứng dụng hỗ trợ hàng đợi đa tác vụ: có thể tiếp tục thêm video mới trong khi
các video khác đang render. **Mọi tác vụ nằm trong cùng một cửa sổ**, không mở thêm tab
hay cửa sổ render riêng.

## Một thư mục duy nhất trên máy mới

Ở lần chạy file `DocuRandomStudio.exe` đầu tiên, bộ khởi động hiện đúng một bước
**Chọn thư mục DocuRandom**. Có thể chọn ổ D, E hoặc ổ dung lượng lớn; mặc định là
`Documents\DocuRandom Studio`. Sau đó ứng dụng tự tạo:

```text
DocuRandom Studio\
├─ App\          ứng dụng, bridge, Codex CLI, FFmpeg và watermark runtime
├─ Data\         VOICEVOX Engine, profile ChatGPT, cache, update và file tạm
└─ Workspaces\   toàn bộ dự án Media, Output, Prompts, WAV/SRT và checkpoint
```

DocuRandom ghi lựa chọn theo tài khoản Windows và các lần mở/cập nhật sau tự dùng lại,
không hỏi lại và không tách dữ liệu sang `%LOCALAPPDATA%`. Màn hình workspace luôn hiện
đường dẫn đang dùng cùng nút **Mở thư mục dữ liệu**. Bộ tự cập nhật chỉ thay thư mục
`App`; `Data` và `Workspaces` được giữ nguyên. Bản phát hành đã nhúng Codex CLI chính
thức nên máy mới không phải cài Node/npm riêng để chạy cầu nối.

Microsoft Edge và profile Edge/Windows vẫn do Microsoft quản lý ở vị trí hệ thống;
DocuRandom không di chuyển dữ liệu trình duyệt đang đăng nhập. Extension nằm trong
`App`, còn ảnh Flow tải về được đưa đúng vào `Media` của workspace thuộc thư mục đã chọn.

## Telegram Bot tùy chọn

Màn hình workspace có nút **Telegram** để kết nối một bot dùng chung cho toàn bộ
DocuRandom. Tool gửi prompt ảnh Flow, lỗi có tên workspace/công đoạn/hướng phục hồi và
trạng thái hoàn tất; đồng thời điều khiển workspace, Flow, VOICEVOX, lịch GMT+7, thumbnail,
workflow và upload YouTube từ xa. Bot tự đăng ký danh sách lệnh và bàn phím nút nhanh qua
Telegram, nên có thể bấm **Menu** thay vì nhớ câu lệnh.

Lệnh `/flow_restart` khởi động lại riêng Flow Agent và phục hồi driver trên đúng một tab
Google Flow đang dùng mà không reload trang. Lệnh có khóa chống bấm lặp, không mở nhiều
cửa sổ Edge và không cắt ngang một prompt đang được gửi; bot tự báo lại khi phiên Flow
đã sẵn sàng hoặc còn lỗi.

Gửi ảnh JPG/PNG/WEBP vào private chat với caption `/thumbnail <workspace>` để chọn đúng
workspace đích. Tool kiểm tra ảnh và yêu cầu mã
`/confirm_thumbnail` dùng một lần trong 2 phút trước khi thay. Ảnh cũ do Telegram quản lý
được chuyển vào `.docurandom\telegram-thumbnails\history`; file người dùng chọn từ ngoài
workspace chỉ bị bỏ liên kết, không bị xóa. Mọi lệnh đọc/vận hành nhận trực tiếp một hoặc
nhiều số/tên workspace, ví dụ `/status 1 2 3` và `/thumbnail_prompt 1 2 3`; không cần
`/select`. Lệnh prompt chỉ trả phần **PROMPT THUMBNAIL HOÀN CHỈNH** của từng video.

Chỉ một private chat đã nhập đúng mã ghép cặp dùng một lần mới được phép điều khiển.
Các update tồn trước khi tool mở bị bỏ qua, vì vậy lệnh cũ không chạy lại sau khởi động.
Lệnh upload YouTube cần thêm mã xác nhận một lần hết hạn sau 2 phút và vẫn đi qua toàn bộ
checkpoint/khóa SHA-256 chống đăng trùng. Token đã từng bị gửi vào chat hoặc ảnh chụp phải
được `/revoke` trong BotFather rồi thay bằng token mới trước khi kết nối.

## Workspace tách biệt cho từng dạng video

Khi mở tool, màn hình đầu tiên cho phép **tạo hoặc mở workspace**. Mỗi workspace có
thư mục `Media`, `Output`, `Prompts`, `VOICEVOX` và dữ liệu cấu hình/lịch sử riêng.
Vì vậy có thể làm tin Nhật, phim tài liệu và Shorts như các dự án độc lập mà không
trộn prompt, ảnh, audio hoặc lịch sử chống lặp. Bấm tên workspace trên thanh đầu để
quay lại danh sách; ứng dụng chỉ cho đổi khi tác vụ của workspace hiện tại đã dừng an
toàn.

Mỗi card workspace có **Đổi tên**, **Xóa** và **Mở workspace**. Đổi tên thực hiện ngay
trong card và chỉ thay tên hiển thị, không đổi đường dẫn nên media/cấu hình không bị
mất liên kết. Xóa luôn hỏi xác nhận và chuyển đúng thư mục workspace vào Thùng rác
Windows để còn khôi phục; thư mục thiếu dấu nhận dạng DocuRandom sẽ bị từ chối xóa để
bảo vệ dữ liệu ngoài workspace.

## Flow + Edge trong cùng quy trình một nút

Tùy chọn **Tự tạo ảnh bằng Flow** gửi toàn bộ prompt hình do bước AI biên tập tạo ra
sang Flow Agent, ép khung ngang phù hợp YouTube (`1792×1024`) và tải ảnh trực tiếp vào
thư mục `Media` của workspace. Sau khi tải xong, tool tự quét ảnh, tạo VOICEVOX WAV +
SRT, lấy thời lượng thật và thêm tác vụ dựng video; không cần chọn lại thư mục nguồn.

- Tạo ảnh Flow được khóa ở `Nano Banana 2 Lite`, đúng 1 ảnh cho mỗi prompt; nếu giao diện chưa xác nhận được model hoặc số lượng thì tool không gửi.
- Precheck hiển thị riêng trạng thái Edge, extension, backend local và phiên Flow.
- Nút **Cập nhật/mở Flow** tự cài Edge nếu thiếu, đồng bộ/reload extension đúng thư
  mục extension, trang `edge://extensions` và Google Flow.
- Edge bắt buộc người dùng xác nhận extension unpacked một lần: bật **Developer mode**,
  bấm **Load unpacked** rồi chọn thư mục tool vừa mở sẵn. Đây là bước xác nhận bảo mật
  của trình duyệt và không thể bị ứng dụng desktop bấm thay. Tool tự kiểm tra lại sau
  mỗi 2 giây; khi kết nối thành công badge chuyển xanh.
- Nếu tài khoản hết credit/đụng giới hạn hoặc Flow trả lỗi, tác vụ dừng trước render và
  hiển thị nguyên nhân ngay trong khung trạng thái/chat để người dùng đổi tài khoản.
- Các workspace dùng một hàng chờ tạo ảnh FIFO chung: workspace vào trước được xử lý hết
  lô trước, mỗi lần gửi cách ngẫu nhiên 3–5 giây. ChatGPT, VOICEVOX, watermark và render
  của workspace khác vẫn có thể chạy song song.
- Prompt ảnh lỗi riêng lẻ, vướng policy hoặc tải về thành file hỏng được lưu vào checkpoint
  rồi bỏ qua để tiếp tục prompt sau. Video sử dụng toàn bộ ảnh hợp lệ đã có; chỉ lỗi hệ
  thống như hết lượt, mất phiên, CAPTCHA kéo dài hoặc mất kết nối mới tạm dừng cả lô.

## Quy trình AI một nút — không dùng API key

DocuRandom Studio có khung chat ngay trong cửa sổ ứng dụng và gọi trực tiếp **ChatGPT Web
Bridge 1.0.21 · Electron 43.4.0** chạy tại `127.0.0.1`. Bridge được viết mới trong source
DocuRandom, nhúng sẵn trong tool, có mã Bearer ngẫu nhiên cho các endpoint tạo nội dung và một profile
Electron tách biệt. Ứng dụng không đọc cookie Edge/Chrome, không có ô API key và không
dùng Codex Work làm model trung gian. Đây vẫn là tích hợp trình duyệt không chính thức,
vì vậy giao diện ChatGPT thay đổi có thể làm bridge cần cập nhật.

Bridge chỉ chọn đúng một ô soạn thảo đang hiển thị, xóa bản nháp cũ bằng thao tác editor,
chèn toàn bộ prompt vào cùng một tin nhắn theo khối 16.000 ký tự qua CDP, đọc ngược và
xác minh từng prefix, rồi đặt caret vào text-node cuối trước khối tiếp theo. Sau khi xác
minh nguyên văn toàn bộ prompt, bridge kích hoạt Send bằng sự kiện CDP và chỉ chuyển sang
chờ output khi DOM đã xuất hiện user turn, assistant turn hoặc trạng thái đang tạo. Nội
dung assistant được đọc lại liên tục để cập nhật realtime cho khung chat và checkpoint.
Mỗi lượt dùng trạng thái local riêng cho gửi/suy nghĩ/đang sinh/đang chốt/hoàn tất. Tool
chỉ gửi `TIẾP` sau khi thấy điều khiển cuối lượt của ChatGPT; nếu DOM thay đổi, nội dung
phải đứng yên 12 giây và không còn dấu hiệu đang sinh mới được chốt.

Khi cài bản cập nhật, ứng dụng yêu cầu Bridge đang rảnh tự đóng qua chính kênh loopback
có Bearer token; Bridge đang tạo nội dung sẽ không bị cưỡng bức tắt. Gói cập nhật đã tải
đủ và khớp chữ ký/hash được dùng lại, nên mở lại tool không tải lặp chỉ vì lần cài trước
bị gián đoạn.

1. Dán toàn bộ nội dung vào ô **Master Prompt** (nút **Nạp TXT…** chỉ là tiện ích tùy chọn), chọn
   thư mục media, **Thư mục lưu tổng tất cả**, tên
   dự án/video và số phút cần viết.
2. Bấm **KIỂM TRA & CHẠY TOÀN BỘ**. Precheck xác minh master prompt, media, quyền sử
   dụng, phiên ChatGPT Web riêng và VOICEVOX local đang hoạt động.
3. Nếu VOICEVOX chưa chạy, ứng dụng tự khởi động engine nền; lần đầu nút hiển thị
   **Tải/cài & khởi động VOICEVOX** và tự tải/cài gói engine chính thức. Nếu thiếu bridge,
   nút **Kết nối ChatGPT** mở bridge portable đã nằm sẵn trong tool sau khi kiểm tra hai
   SHA-256 của executable và mã `app.asar`. Tool tự tìm/cài Codex CLI nếu thiếu và tự tạo
   profile `docurandom-chatgpt-web` riêng; không sửa cấu hình/model Codex Work của người dùng.
   Lần đầu tool chỉ mở `chatgpt.com` để đăng nhập; các lần sau bridge tự chạy ẩn bằng profile riêng.
4. Tool gửi nguyên văn toàn bộ nội dung đã dán vào đúng một tin nhắn. Sau phản hồi đầu tiên,
   tool hỏi ChatGPT xác nhận bằng đúng `ĐÃ NẠP`; chỉ khi nhận đúng câu đó mới gửi `BẮT ĐẦU`.
   Tool tự trả lời số phút khi được hỏi và tự gửi `TIẾP` cho tới khi thu đủ các marker. Khi đã
   vào phần prompt hình ảnh, sau mỗi lượt `TIẾP` tool hỏi xác nhận: `CHƯA XONG` thì tiếp tục,
   `ĐÃ XONG` và đủ dữ liệu mới kết thúc. Toàn bộ phản hồi xuất hiện trong khung chat để
   theo dõi. Sau khi hoàn tất vẫn có thể nhập tin nhắn chỉnh sửa và gửi tiếp vào đúng phiên.
5. Tool xuất TXT tổng hợp, đưa phần thoại vào VOICEVOX, tạo WAV + SRT, lấy thời lượng WAV
   thật để cân timeline rồi tự thêm tác vụ render MP4.

Ngay khi cửa sổ mở, ứng dụng tự tìm bridge, khởi động nền và xác minh ba
bằng chứng độc lập: tiến trình bridge, chat thường đã đăng nhập và endpoint runtime
đang nhận tác vụ. Có nút **Kiểm tra lại** độc lập. Sau khi bấm **Mở / đăng nhập ChatGPT
Web**, ứng dụng chỉ đọc trạng thái nền mỗi 2 giây trong tối đa khoảng 6 phút; bước này
không điều hướng hoặc refresh trang ChatGPT. Khi bấm chạy, tool xác minh mạnh đúng một
lần; nếu bridge đang bận một tác vụ, tool tự chờ thay vì báo sai rằng phiên đã hết hạn.
Badge chỉ chuyển xanh khi cả ba bằng chứng đều đạt.

Tool hiện cố định tuyến **ChatGPT Web · GPT-5.6 Sol · High**. Trước mỗi lượt, bridge đọc
điều khiển model trên chính trang `chatgpt.com` và chỉ tiếp tục khi xác minh đang ở `High`
(mức 3/3 của giao diện hiện tại); nếu không xác minh được thì dừng an toàn, không âm thầm
chạy model khác. Codex CLI chỉ là lớp điều khiển qua profile riêng của DocuRandom: model
thực thi vẫn là phiên ChatGPT Chat trên web, không phải Codex Work hay OpenAI API.
Profile này cũng tự tắt riêng mọi MCP kế thừa từ cấu hình Codex chung vì quy trình biên tập
chỉ cần chuyển văn bản tới ChatGPT Web. Các MCP/plugin trong Codex app không bị xóa hoặc
thay đổi và vẫn hoạt động bình thường ngoài DocuRandom.

Khung **HỘI THOẠI ĐANG CHẠY** nhận output ChatGPT theo thời gian thực, thường cập nhật
khoảng mỗi 0,5 giây với nhãn **TRỢ LÝ · ĐANG VIẾT**. Khi một lượt hoàn tất, tool chốt
nội dung vào transcript/checkpoint rồi tự gửi `TIẾP` nếu master prompt còn thiếu phần.
Trong lúc nạp một Master Prompt rất lớn, bridge cập nhật thời gian đã chạy mỗi 5 giây và ghi
một mốc mới vào hộp hội thoại mỗi phút. Sau khi bấm Gửi, trạng thái phân biệt rõ ChatGPT đang
suy nghĩ hay đang trả lời, đồng thời hiển thị số ký tự đã nhận; tool không còn im lặng khiến người
dùng tưởng tác vụ bị treo.

Khi bắt đầu, tool đưa **nguyên văn toàn bộ Master Prompt đã dán** vào một tin nhắn ChatGPT,
đọc ngược để xác minh không thiếu ký tự rồi mới bấm gửi một lần. Sau phản hồi đầu tiên, tool
gửi câu hỏi xác nhận và chỉ gửi `BẮT ĐẦU` sau khi ChatGPT trả đúng `ĐÃ NẠP`. Tên người dùng nhập
chỉ là tên file đầu ra/checkpoint, không được gửi sang ChatGPT làm tiêu đề nội dung. Nếu
ChatGPT báo lỗi mạng hoặc không xử lý được khối nội dung quá lớn, tool dừng trước
`BẮT ĐẦU` thay vì tiếp tục bằng một Master Prompt thiếu.
Với khối sáu chữ số, bridge thực hiện các thao tác nhập 16.000 ký tự một lần và xác minh chính
xác từng tiền tố trước khi nối phần tiếp theo. Đây chỉ là cách nhập an toàn ở phía trình duyệt:
ChatGPT vẫn nhìn thấy đúng một tin nhắn hoàn chỉnh và nút Gửi vẫn chỉ được bấm một lần.
Tool không dựa vào một câu văn “Hoàn thành” mơ hồ. Khi tự phát hiện phần prompt hình ảnh,
tool hỏi định kỳ để ChatGPT chỉ trả `ĐÃ XONG` hoặc `CHƯA XONG`; câu trả lời lệch mẫu được coi là
chưa xong. Số prompt dự kiến được tính từ nhịp ghi trong chính Master Prompt (ví dụ một ảnh
mỗi 15 giây), không còn cố định một ảnh mỗi phút. Tool chấp nhận thiếu tối đa ba prompt nếu
ChatGPT đã xác nhận `ĐÃ XONG`, đồng thời giữ toàn bộ prompt hợp lệ nếu ChatGPT xuất nhiều hơn.
Giới hạn an toàn 500 prompt ngăn một phản hồi lỗi tạo ra lô vô hạn. Khi Flow chạy, prompt lỗi
riêng lẻ được bỏ qua và video dùng số ảnh tạo thành công thực tế.

Trong thư mục lưu tổng, với tên dự án `KHEN OTO NHẬT`, các file chính là:

```text
KHEN OTO NHẬT.txt
KHEN OTO NHẬT_CHAT.txt
KHEN OTO NHẬT_AI_CHECKPOINT.json
KHEN OTO NHẬT.wav
KHEN OTO NHẬT.srt
KHEN OTO NHẬT.voicevox.json
KHEN OTO NHẬT.mp4
```

File tổng hợp gồm đúng các mục: **Tiêu đề**, **Mô tả**, **Prompt Thumbnail**, **Thoại
VOICEVOX** liền mạch với `A:` ở đầu mỗi dòng và **Prompt hình ảnh** đánh số `Image 1`,
`Image 2`… cách nhau một dòng trống. Bản thoại gửi sang VOICEVOX tự bỏ tiền tố `A:` để
engine không đọc ký tự đó. Checkpoint và bản chat được ghi sau từng lượt để giữ phần đã
hoàn tất nếu người dùng hủy hoặc kết nối bị lỗi.

Nút **Tiếp tục** đọc checkpoint của ChatGPT, file tổng hợp, Flow, VOICEVOX/SRT, watermark
và render rồi báo điểm sẽ nối trước khi chạy. Tùy chọn **Tự sửa & tiếp tục** tự thử lại tối
đa ba lần với lỗi cục bộ có thể phục hồi; các lỗi cần thao tác người dùng như hết lượt,
đăng nhập hoặc CAPTCHA sẽ dừng và giữ nguyên backup. VOICEVOX lưu cả từng câu đã hoàn tất,
vì vậy mất điện hoặc hủy giữa chừng không bắt đầu lại toàn bộ phần thoại.

SRT được giữ làm backup và tự chèn vào MP4. Phụ đề Nhật được ngắt theo độ rộng hiển thị,
tối đa hai dòng, chữ trắng viền đen trên nền tối mờ, đặt trong vùng an toàn phía dưới để
không tràn khỏi khung hình 16:9 hoặc video dọc.

Mỗi lần chạy toàn bộ tạo một cuộc trò chuyện ChatGPT thường và giữ toàn bộ Master Prompt,
`BẮT ĐẦU`, số phút, các lượt `TIẾP` cùng câu trả lời trong lịch sử tài khoản để có thể mở lại.
DocuRandom vẫn ghi phản hồi cục bộ sau từng lượt vào TXT chat và checkpoint. Nội dung chat
chịu cài đặt lưu trữ/quyền riêng tư thông thường của tài khoản ChatGPT.

> Yêu cầu tài khoản: người dùng phải đăng nhập ChatGPT trong profile bridge ở lần thiết
> lập đầu. Các lượt này dùng giới hạn/quyền truy cập ChatGPT Web của tài khoản, không phải
> API hoặc Codex Work. Bridge là phần mềm không chính thức và không thể có mức bảo đảm
> tương đương API/OAuth chính thức của OpenAI.

## Mở ứng dụng

1. Mở một file `DocuRandomStudio.exe`; tool tự chuẩn bị runtime trong vùng riêng của
   tài khoản Windows rồi chạy ngay, không có các bước cài đặt hay yêu cầu quyền quản trị.
   Các lần sau dùng lại chính file EXE đó; runtime chỉ được bung lại khi EXE mang phiên
   bản mới hơn.
2. Tạo workspace mới hoặc mở workspace có sẵn.
3. Chọn/dán thư mục chứa ảnh/video, hoặc bật Flow để ảnh được tải thẳng vào `Media`.
4. Nếu ảnh đến từ Gemini, bật **Gỡ watermark Gemini khỏi ảnh trước khi dựng**.
5. Chọn audio nếu cần, chọn thư mục xuất và nhập **Tên dự án & tên file video**.
6. Chọn độ phân giải, FPS, mức hiệu ứng, gói chuyển cảnh và số phiên bản.
7. Giữ chế độ **Hiệu suất tự động** hoặc chọn thủ công từ 1–4 tác vụ song song.
8. Để tự viết bằng master prompt, bấm **KIỂM TRA & CHẠY TOÀN BỘ** ở tab AI. Để chỉ dựng
   từ kịch bản/audio có sẵn, bấm **KIỂM TRA & BẮT ĐẦU** ở tab Media & Render.

Bộ cài một file đã kèm FFmpeg, Node.js, Gemini Watermark Remover và DocuRandom Chat
Bridge đã khóa checksum; không cần cài Python, FFmpeg, Node.js hay model Codex.

## Tự cập nhật an toàn, không cài lại

Màn hình workspace tự kiểm tra kênh phát hành nhị phân khi mở tool. Updater chỉ tải công
khai ẩn danh: không có quy trình đăng nhập, không dùng tài khoản, không nhận/lưu token và
không mở trang web. Khi có bản mới, card chỉ hiện phiên bản, changelog, tiến trình và các
nút cập nhật hoặc để sau. Nếu máy chủ tạm lỗi, tool chỉ báo thử lại sau.

Khi có bản mới, tool tự tải nền rồi kiểm tra đồng thời chữ ký Ed25519, SHA-256, kích
thước và cấu trúc file ZIP trước khi cho phép thay thế. Trình cập nhật chạy ngoài thư
mục portable, giữ lại bản cũ, khởi động bản mới và chờ tín hiệu giao diện hoạt động.
Nếu bản mới lỗi hoặc không mở được, nó tự phục hồi và mở lại bản trước. Người dùng
không phải chạy bộ cài; chỉ cần đồng ý khởi động lại khi tool báo bản mới đã sẵn sàng.
Thông báo dùng card nổi ngay trong cửa sổ theo style Donut, có phiên bản cũ → mới,
ghi chú, tiến trình tải, **Khởi động lại ngay**, **Để sau** và nút đóng.

Mã nguồn vẫn ở kho riêng. Kênh tải công khai chỉ chứa EXE/ZIP đã build cùng manifest
và chữ ký, không chứa source. Khóa ký release riêng tư không nằm trong source, ứng dụng
hay kênh tải; ứng dụng chỉ chứa khóa công khai Ed25519 để từ chối mọi gói bị sửa hoặc giả
mạo. Bản quá cũ chưa có updater cần cài một lần bằng EXE; từ đó các bản sau tự cập nhật.

## Gỡ watermark Gemini trước khi dựng

Tùy chọn **Gỡ watermark Gemini khỏi ảnh trước khi dựng** tích hợp trực tiếp mã nguồn
`watermark-remover-main` vào hàng đợi render:

- Hỗ trợ ảnh `JPG/JPEG`, `PNG` và `WebP`; các định dạng ảnh khác vẫn được dựng nhưng
  bỏ qua bước gỡ watermark.
- Chỉ tạo bản đã xử lý trong `<thư mục đã chọn>\Data\watermark_cache`; ảnh nguồn
  không bị ghi đè hoặc di chuyển.
- Kết quả được nhận diện theo đường dẫn, kích thước, thời điểm sửa và phiên bản engine,
  nên nhiều biến thể/tác vụ có thể dùng lại cache thay vì xử lý cùng một ảnh nhiều lần.
- Mỗi manifest ghi trạng thái bật/tắt, phiên bản engine, số ảnh mới xử lý, số cache hit và
  cảnh báo của bước tiền xử lý.
- Nếu một ảnh riêng lẻ không thể xử lý, ứng dụng ghi cảnh báo và dùng ảnh gốc để tác vụ
  vẫn có thể tạo kết quả tốt nhất có thể.

Tính năng này nhắm tới watermark hiển thị của ảnh do Gemini tạo ra. Chỉ sử dụng với tư
liệu bạn có quyền chỉnh sửa.

## Tạo giọng VOICEVOX trong cùng cửa sổ

Phần **4 · LỜI DẪN VOICEVOX** nằm ngay trong bảng cài đặt cuộn bên trái; không mở
thêm tab hoặc cửa sổ VOICEVOX. Quy trình sử dụng chỉ cần một nút:

1. Dán kịch bản, mỗi dòng là một câu hoặc đoạn; chọn nhân vật/kiểu giọng nếu danh sách
   đã có, hoặc để ứng dụng tự chọn `麒ヶ島宗麟（ノーマル）` trong lần đầu.
2. Lần đầu tải gói Windows chính thức khoảng 1,9 GB; tiến trình và dung lượng đã tải
   hiển thị ngay trên thẻ tác vụ. Có thể hủy riêng và tiếp tục tải ở lần bấm sau.
3. Engine được kiểm tra kích thước và SHA-256, giải nén vào dữ liệu ứng dụng rồi chạy
   ẩn nền. Các lần sau không tải lại và không cần tìm `run.exe`.
4. Có thể chọn một preset thuyết minh dành cho `麒ヶ島宗麟（ノーマル）` rồi bấm
   **Áp dụng**, hoặc tự chỉnh tốc độ, cao độ, ngữ điệu, âm lượng, khoảng lặng,
   tần số WAV và tùy chọn lưu riêng từng dòng.
5. Bấm duy nhất **KIỂM TRA & BẮT ĐẦU**. Ứng dụng tự kiểm tra engine đã chạy chưa; nếu
   chưa thì tự khởi động, nếu chưa cài thì tự tải/cài, sau đó kiểm tra giọng, tạo cặp
   WAV + SRT, đọc thời lượng thật và mới bắt đầu dựng video.

Ngay khi mở ứng dụng, thanh **VOICEVOX** trong thẻ Smart Pipeline tự pre-check engine
local bằng phiên bản API và danh sách giọng:

- **CHƯA HOẠT ĐỘNG**: hiện nút **Khởi động VOICEVOX**; nút này dùng engine đã cài hoặc
  tự tải/cài gói chính thức nếu máy chưa có.
- **ĐANG KHỞI ĐỘNG**: khóa tạm nút render để tránh xếp tác vụ khi engine chưa sẵn sàng.
- **HOẠT ĐỘNG · N GIỌNG**: pre-check đạt, nút khởi động tự ẩn; nút **Kiểm tra lại** vẫn
  có để xác minh thủ công bất cứ lúc nào.
- Nếu người dùng không bấm khởi động trước, tác vụ có kịch bản vẫn tự thực hiện cùng
  kiểm tra này và chuẩn bị engine trong hàng đợi.

Giao diện dùng hệ **Donut Mono** được tham chiếu trực tiếp từ mã nguồn local
`donutbrowser-main`: nền đen, card than, viền mảnh, chữ trắng/xám, nút chính trắng–đen
và màu trạng thái xanh/vàng/đỏ. Bộ biểu tượng nét mảnh được đóng gói sẵn trong ứng dụng,
không tải từ Internet và được dùng thống nhất cho nguồn media, audio, VOICEVOX, kiểm tra,
render, hàng đợi, hủy và khôi phục mặc định.

Thanh cuộn dọc/ngang của bảng cài đặt, media, kịch bản, nhật ký và hàng đợi dùng cùng một
kiểu mảnh, dễ nhìn. Con lăn hoạt động trên toàn bộ vùng nội dung; khi con trỏ nằm trên ô
số hoặc danh sách chọn, nó vẫn chỉ cuộn trang và không thay đổi giá trị. Danh sách chọn
có thể mở bằng cách bấm vào bất kỳ vị trí nào trong ô, không cần nhắm đúng mũi tên. Muốn
đổi thông số, hãy bấm chọn, nhập trực tiếp hoặc dùng phím mũi tên. Cách này tránh làm tốc
độ, cao độ, FPS hay số tác vụ bị đổi ngoài ý muốn khi cuộn giao diện.

Các thông số dạng số dùng cặp nút **+ / −** rộng, tương phản rõ thay cho hai mũi tên hệ
thống nhỏ. Chiều cao, viền và khoảng đệm của nút, ô số, dropdown và ô nhập được căn theo
cùng một hệ kích thước để các hàng điều khiển không bị lệch. Nếu dùng **+ / −** ở mục
Tác vụ song song khi đang ở chế độ Tự động, ứng dụng tự chuyển sang Thủ công và áp dụng
giá trị vừa chọn.

Mục **Thư mục backup VOICEVOX · WAV + SRT** cho phép dán hoặc chọn một thư mục riêng.
Nếu để Tự động, ứng dụng dùng thư mục `VOICEVOX` bên trong thư mục xuất video. Nút
**Mở** mở thẳng nơi backup để nghe và đối chiếu sau khi render lỗi; nút **Tự động** xóa
đường dẫn riêng và quay lại vị trí mặc định.

Audio hoàn tất được ghép thành một WAV và phụ đề SRT cùng tên trong thư mục backup.
Mỗi dòng kịch bản là một cue; mốc bắt đầu/kết thúc lấy từ số frame thật của từng đoạn
WAV và có tính cả khoảng nghỉ giữa dòng, không ước lượng theo số ký tự. Timeline video
tự cân bằng theo đúng thời lượng WAV này. Nếu một lần bấm tạo nhiều biến thể, cặp WAV +
SRT được tạo một lần rồi tái sử dụng an toàn cho cả lô. Một file `.voicevox.json` đi kèm
lưu engine/style/thông số, tên subtitle, mốc cue và các dòng kịch bản để recheck.

Tùy chọn **AI tối ưu nhịp kể + cách đọc trước khi render** được bật mặc định. Tool khóa
đúng nhân vật đã chọn bằng `speakerUuid`, chỉ cho ChatGPT chọn giữa các style thuộc cùng
nhân vật và tự điều chỉnh bảy thông số VOICEVOX theo từng chức năng kể chuyện. Trước khi
tạo WAV, tool xuất project gốc, audit toàn bộ box/cách đọc tiếng Nhật, tạo file
`[Tên]_VOICEVOX_FINAL_audited.vvproj` cùng `[Tên]_content_FINAL.txt`, kiểm tra số box,
thứ tự, text, speaker và query rồi mới tổng hợp trực tiếp từ query FINAL. Vì vậy thay đổi
style, ngắt nghỉ, mora/kana và override từng câu thực sự đi vào audio; file FINAL không chỉ
là báo cáo. Nút **Audit file .vvproj bằng ChatGPT** xử lý project có sẵn theo cùng cơ chế.
Nếu tắt tùy chọn AI, tool giữ quy trình cũ: một style và bộ thông số chung cho toàn bài.

VOICEVOX không có cấp model `medium`/`high`; các tên đó trong khu vực ChatGPT là mức xử
lý của AI biên tập, không phải chất lượng giọng. CPU/GPU chỉ ảnh hưởng tốc độ tổng hợp,
không nâng chất lượng âm thanh của cùng engine/style. Whisper là mô hình nhận dạng giọng
nói (audio thành text), không phải mô hình tạo giọng; workflow mặc định không dùng Whisper
để tránh biến transcript chính xác sẵn có thành một bản chép có thể sai.

Các preset có sẵn theo từng vai trò trong video: tin chính cân bằng, mở bài gây chú ý,
phê bình nghiêm và dè dặt, chuyển ý nhìn lại vấn đề, nêu điểm mạnh Nhật Bản và kết
luận chắc/đọng. Đây là các điểm xuất phát biên tập minh bạch, không phải thông số
"tốt nhất" do VOICEVOX chứng nhận. Sau khi áp dụng, mọi ô thông số vẫn có thể chỉnh tay.

Khi chọn ưu tiên GPU, ứng dụng cài gói DirectML chính thức; nếu GPU không tương thích,
engine tự được thử lại ở chế độ CPU. Nếu bỏ chọn GPU trước lần cài đầu, ứng dụng dùng
gói CPU chính thức.
Số luồng CPU được cân bằng với các tác vụ render đang chạy. Địa chỉ API chỉ chấp nhận
engine cục bộ (`127.0.0.1`, `localhost` hoặc `::1`) để kịch bản không bị gửi ra máy khác.

> Thư mục `voicevox-main` chỉ là mã nguồn Editor. DocuRandom Studio không dùng thư mục
> này để tạo giọng; nút **KIỂM TRA & BẮT ĐẦU** tự chuẩn bị engine phát hành chính thức.
> Hãy kiểm tra điều khoản sử dụng riêng của từng nhân vật trước khi đăng hoặc kiếm tiền.

## Render nhiều tác vụ cùng lúc

- Chỉ có một cửa sổ điều khiển chung. Nếu mở ứng dụng lần thứ hai, DocuRandom Studio sẽ
  đưa cửa sổ đang chạy lên trước thay vì tạo thêm một cửa sổ mới.
- Nút **KIỂM TRA & BẮT ĐẦU** vẫn dùng được khi tác vụ khác đang chạy. Tác vụ mới được
  đưa vào hàng đợi và tự bắt đầu ngay khi còn tài nguyên.
- Có thể chọn từ 1–20 phiên bản trong một lần thêm. Mỗi phiên bản là một tác vụ độc lập,
  có seed, Render ID, tiến trình và file đầu ra riêng.
- Mỗi thẻ tác vụ hiển thị trạng thái và phần trăm hoàn thành. Thanh tiến trình tổng hợp
  phản ánh toàn bộ hàng đợi.
- Nút **Hủy** trên từng thẻ chỉ dừng tác vụ đó, không làm gián đoạn các video còn lại.
- Nút **Hủy tất cả** dừng an toàn mọi tác vụ đang chạy và đang chờ. Video đã hoàn tất
  vẫn được giữ; file dở dang được dọn tự động.
- Bạn có thể đổi thư mục nguồn, audio, hiệu ứng hoặc thông số rồi thêm tác vụ tiếp theo.
  Mỗi tác vụ giữ một **snapshot cấu hình và danh sách media tại thời điểm được thêm**,
  vì vậy thay đổi sau đó không làm biến đổi tác vụ đã chạy hoặc đang chờ.
- Nút **Khôi phục mặc định** đưa các tùy chọn dựng, hiệu ứng, audio và hiệu suất về
  cấu hình ban đầu sau một bước xác nhận. Thư mục nguồn/xuất, tệp audio, media đã quét,
  ghi chú biên tập và các tác vụ hiện có được giữ nguyên; cấu hình mới chỉ áp dụng cho
  tác vụ được thêm sau đó.

### Tự tối ưu hiệu suất

Ở chế độ tự động, ứng dụng nhận diện số CPU logic, tổng RAM và RAM khả dụng, sau đó cân
bằng số tác vụ song song với số luồng dành cho từng tác vụ. Encoder phần cứng không được
chọn chỉ vì xuất hiện trong danh sách FFmpeg: ứng dụng mã hóa thử một đoạn ngắn để chắc
chắn encoder thật sự hoạt động, theo thứ tự:

```text
NVIDIA NVENC → AMD AMF → Intel Quick Sync (QSV) → CPU / libx264
```

Nếu một encoder phần cứng không khởi tạo được, ứng dụng tự thử encoder tiếp theo và cuối
cùng dùng x264 an toàn. Cấu hình máy hiện tại được khuyến nghị mặc định **2 tác vụ đồng
thời, khoảng 3 luồng CPU cho mỗi tác vụ**. Đây là cấu hình cân bằng để giao diện và hệ điều
hành vẫn phản hồi tốt.

Chế độ thủ công cho phép chọn **1–4 tác vụ song song**. Chọn cao hơn không nhất thiết nhanh
hơn: nhiều tác vụ có thể tranh CPU, RAM, GPU và tốc độ ổ đĩa. Nếu còn chạy phần mềm nặng,
nên dùng 1–2; chỉ dùng 3–4 khi máy có nhiều tài nguyên trống. Chất lượng hình ảnh không bị
hạ theo số tác vụ; bộ điều phối giới hạn luồng của từng tác vụ để giảm tranh chấp tài nguyên.

## Khả năng của phiên bản 2

- Quét chung ảnh `JPG/JPEG/PNG/WebP/BMP/TIFF` và video
  `MP4/MOV/MKV/AVI/WebM/M4V/WMV/MPEG/TS/MTS/M2TS/GIF`.
- Có thể tiền xử lý gỡ watermark Gemini cho ảnh `JPG/PNG/WebP` ngay trong cùng tác vụ,
  không sửa file nguồn và dùng cache chung an toàn cho hàng đợi đa tác vụ.
- Sắp xếp tên tự nhiên để đọc thư mục ổn định, sau đó có thể **trộn có kiểm soát** theo
  seed riêng cho từng phiên bản. Đây không phải công thức cố định kiểu 2 ảnh + 1 video.
- Loại các file trùng byte hoàn toàn trong cùng thư mục trước khi dựng.
- Tạo chuyển động Ken Burns/pan/zoom cho ảnh lẫn video.
- Có 57 chuyển cảnh native đã kiểm tra trên FFmpeg 8.1.2, chia thành các họ fade, wipe,
  slide, smooth, cover/reveal, shape, slice, wind và creative.
- Dùng shuffle-bag và cooldown theo họ: tránh lặp transition liền nhau và hạn chế lặp
  cùng họ trong các đoạn gần nhau.
- Có 18 profile hiệu ứng hình ảnh, gồm documentary warm/cool, muted film, vintage,
  archive/sepia, monochrome/noir, teal-orange, soft focus, chromatic shift, lens, trail,
  pixel pulse và các biến thể màu điện ảnh.
- Mỗi cảnh còn random motion, crop, điểm bắt đầu video, tốc độ phát, màu, gamma, sharpen,
  hue/vibrance và cường độ hiệu ứng.
- Toàn video có seed grain riêng, vignette, flicker và fade ngẫu nhiên.
- Nếu video nguồn ngắn hơn phần được phân bổ, engine tự điều chỉnh tốc độ hoặc lặp nguồn;
  nếu dài hơn, engine chọn đoạn cắt phù hợp.
- Khi có audio, tổng timeline được khóa theo frame: số frame đích là
  `ceil(thời_lượng_audio × FPS)`. Hình và video được phân bổ lại để khớp audio; audio được
  chuẩn hóa AAC stereo 48 kHz.
- Xuất MP4 H.264, `yuv420p`, BT.709, faststart, kèm manifest `.docurandom.json`.

## Vì sao các lần render khác nhau?

Mỗi lượt dùng seed 128-bit mới. Seed điều khiển thứ tự media (khi bật trộn), transition,
thời lượng, motion, trim video, tốc độ, profile màu, thông số effect, grain và finishing.
Ứng dụng lưu lịch sử seed, chữ ký công thức, SHA-256 tệp và dấu vân tay của các frame đã
giải mã. Nếu phát hiện một đầu ra trùng hoàn toàn với lịch sử, bản đó bị xóa và tự render
lại bằng seed khác.

Việc chống trùng tuyệt đối không có nghĩa hai video luôn khác biệt đáng kể về **nội dung
cốt lõi**. Hãy xem phần lưu ý YouTube bên dưới.

## File đầu ra

Ô **Tên video xuất** là tùy chọn:

- Để trống: ứng dụng giữ tên tự động có thời gian và Render ID.
- Nhập `Video`: một phiên bản xuất thành `Video.mp4`.
- Nhập `Video` và tạo nhiều phiên bản: xuất thành `Video_1.mp4`, `Video_2.mp4`…
- Có thể nhập tên kèm `.mp4`; ứng dụng tự chuẩn hóa và không lặp phần mở rộng.
- Nếu file cùng tên đã tồn tại hoặc đang được tác vụ khác sử dụng, ứng dụng tự thêm `_2`,
  `_3`… và không ghi đè file cũ.
- Các ký tự Windows không cho phép trong tên file sẽ được báo ngay trước khi thêm tác vụ.

```text
DocuRandom_20260720_153012_a1b2c3d4e5f6.mp4
DocuRandom_20260720_153012_a1b2c3d4e5f6.docurandom.json
```

Manifest luôn đi theo tên video, ví dụ `Video_1.docurandom.json`.

Manifest lưu Render ID, seed, snapshot cài đặt, thứ tự timeline, nguồn media, trim/tốc độ,
effect, transition, thời lượng, cấu hình encoder/runtime, chữ ký công thức và hash đầu ra.
Lịch sử local nằm tại:

```text
<thư mục đã chọn>\Data\render_history.json
```

## Theo dõi realtime và YouTube Like qua 1MM

Khung **Tăng Like YouTube qua 1MM** chỉ tải các dịch vụ có mạng xã hội YouTube và
tên/danh mục Like. API key được mã hóa bằng tài khoản Windows hiện tại, không ghi vào
workspace hoặc log. Nút **Kiểm tra realtime · chỉ đọc** tự lấy link video từ đúng profile
Donut và đọc số **Views · Last 60 minutes** trong YouTube Studio; nút này không gọi API
đặt đơn. Mỗi lần đọc dùng một tab Analytics nền khóa theo `targetId` rồi tự đóng, vì vậy
không điều hướng tab YouTube hoặc tab làm việc mà người dùng đang treo trong cùng profile.

Khi người dùng chủ động bật công tắc tự động và xác nhận cảnh báo trừ tiền thật, tool quét
lại sau mỗi 2 phút. Mỗi video nhận một tỷ lệ ổn định trong khoảng 5–10% số view realtime;
ngay sau khi đọc realtime, tool tải lại toàn bộ catalog 1MM rồi mới chọn gói đang hoạt động
rẻ nhất có min/max phù hợp. Chỉ kết quả từ 10 Like mới được đặt. Cùng một video chỉ được
đặt lại sau ít nhất 30 phút nếu vẫn đạt tiêu chí. Checkpoint toàn cục khóa đơn đang gửi
hoặc phản hồi không rõ ràng để không tự gửi trùng.

Nếu Telegram đã ghép cặp và bật **Thông báo trạng thái**, mỗi đơn thành công được gửi thành
một thẻ văn bản rõ ràng có tên workspace, tiêu đề và link video, view trong 60 phút, tỷ lệ,
số Like đã đặt, gói rẻ nhất và mã đơn. Link mở trực tiếp được trong Telegram. Phản hồi 1MM
không chắc chắn được gửi dưới dạng cảnh báo riêng; tool dừng tự động và khóa video để tránh
trừ tiền hai lần.

## Lưu ý về YouTube và bản quyền

**Không phần mềm nào có thể bảo đảm 100% một video sẽ không vi phạm chính sách, không bị
Content ID hoặc luôn đủ điều kiện kiếm tiền.** YouTube đánh giá nội dung cốt lõi và cả
kênh; random filter/noise/crop không thay thế câu chuyện, lời bình, góc nhìn và giá trị
nguyên gốc. Render nhiều tác vụ đồng thời cũng không làm nội dung tự động trở thành nội
dung biên tập bởi con người.

Bạn phải có đầy đủ quyền thương mại với **mọi ảnh, video và audio**. Không dùng công cụ để
lách hệ thống phát hiện hoặc sản xuất/đăng hàng loạt các bản chỉ khác nhau đôi chút. Hãy
bổ sung lựa chọn tư liệu có chủ đích, lời bình, cấu trúc kể chuyện, phân tích và góc nhìn
riêng. Đọc [HUONG_DAN_YOUTUBE_VI.md](HUONG_DAN_YOUTUBE_VI.md) trước khi xuất bản.

## Chạy từ mã nguồn

Yêu cầu Python 3.11 trở lên:

```powershell
python -m pip install -r requirements.txt
python app.py
```

Để dùng bước gỡ watermark khi chạy từ mã nguồn, đặt `watermark-remover-main` cạnh thư
mục `DocuRandomStudio`, cài Node.js 18+ và chuẩn bị dependency một lần:

```powershell
cd ..\watermark-remover-main
pnpm install --frozen-lockfile
cd ..\DocuRandomStudio
python app.py
```

Có thể đặt đường dẫn khác qua `DOCURANDOM_WATERMARK_ROOT` và
`DOCURANDOM_NODE_PATH`.

Nếu chưa tìm thấy FFmpeg, ứng dụng sẽ tự tải Windows Essentials Build trong lần render đầu
và lưu tại `<thư mục đã chọn>\Data\tools`.

## Kiểm thử

```powershell
python -m unittest discover -s tests -v
```

Bộ test gồm toán timeline theo frame, trộn media theo seed, chống lặp transition, quét file
trùng, điều phối nhiều tác vụ, hủy riêng, nhận diện runtime và render thật ảnh → video → ảnh
với audio AAC.

## Đóng gói portable

```powershell
powershell -ExecutionPolicy Bypass -File .\build_release.ps1
```

Script mặc định lấy `watermark-remover-main` ở thư mục cạnh dự án, tạo runtime ảnh tối
giản với Sharp và đóng gói cả `node.exe`. Có thể chỉ rõ đường dẫn bằng
`-WatermarkRoot`/`-Node`; tùy chọn `-NoBundleWatermarkRemover` chỉ dành cho bản build
không cần tính năng này. Build cũng bắt buộc xác minh và nhúng runtime bridge tại
`third_party/docurandom-chat-bridge`; nếu executable hoặc `app.asar` sai SHA-256, quá trình đóng gói
dừng ngay.

Kết quả nằm tại:

```text
release\DocuRandomStudio\DocuRandomStudio.exe
release\DocuRandomStudio-portable.zip
```
