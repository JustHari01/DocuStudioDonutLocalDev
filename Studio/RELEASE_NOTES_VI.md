## 2.4.339

- Google Docs/Sheets nhấp ô tiêu đề/địa chỉ ô bằng input của trình duyệt sau khi kiểm tra phần tử duy nhất, hiển thị, không bị che, ổn định và nhận focus. Nhập Unicode và phím tắt tiếp tục kiểm tra đúng tài liệu; nhả phím/nút chuột khi mất phản hồi để tránh giữ trạng thái nhấn.
- Lượt mới có yêu cầu tạo file mở thẳng trình soạn thảo, bỏ lượt mở trang danh sách dư. Các file cùng ngày đã có biên nhận hoàn tất được dùng lại trước khi xin profile; trạng thái ghi rõ dùng kết quả cũ, không giả định vừa kiểm tra lại máy chủ hoặc tính thành lượt làm mới.
- Chỉ nhận lại quyền đóng profile khi PID hợp lệ và khớp cả phiên trình duyệt đã lưu. Giải phóng xong xóa metadata quyền sở hữu cũ; profile đang mở thủ công hoặc đổi phiên được giữ.
- Giữ nguyên kế hoạch đang dở, biên nhận, giới hạn và cách nhường profile cho upload/Public. Kiểm thử input thực hiện trên Chromium riêng với trang thử nghiệm cục bộ; không tạo hoạt động Gmail/YouTube ngẫu nhiên.

## 2.4.324

- Hoàn thành 20 Master độc lập theo bản đồ nghiên cứu: giữ nguyên sáu file 11.1.0-r1 đã có, bổ sung 14 charter ngành với 10 family đề tài, nguồn/vật chứng, thuật ngữ Nhật, điều kiện so sánh, thumbnail và biên ngách riêng.
- Danh mục chủ đề dùng chung cho bộ sinh và lựa chọn trong tool; thêm mã R theo nhóm nghiên cứu để không gán nhầm mã G cũ. Nạp TXT và chọn thư viện đều nhận đúng định danh R, giữ cổng output/kiểm ảnh cho các ngách mới.
- Mỗi workspace tự chọn chủ đề cho lượt mới; không tự gán ngách, bật lịch, đổi model, reset ngân sách hoặc thay Master của lượt dở. Không đưa dữ liệu học G-13 sang kênh có ngách khác.

## 2.4.323

- Thư viện sáu Master hoàn chỉnh 11.1.0-r1: G-13 máy công cụ, G-10 đường sắt, G-09 ô tô/pin, G-12 bán dẫn, G-11 logistics và G-16 linh kiện. Mỗi file độc lập, có phạm vi, family đề tài, thuật ngữ, điều kiện đối chứng và thiết kế hình riêng; giữ Master 11.0.0-r1 để nối lượt cũ.
- Thêm “Chọn Master theo ngách…” cho từng workspace. Lưu lựa chọn cho video mới, kiểm hash/domain/version trước khi nạp. Tiếp tục giữ nguyên draft/hội thoại cũ; nạp TXT hoặc tự sửa Master chuyển về lựa chọn thủ công.
- Phân tích bối cảnh và dữ liệu kênh trước chọn đề tài/title, hồ sơ story-context có nguồn và giới hạn; giữ marker, TIẾP, prompt ảnh mỗi 30 giây, ba dòng màu thumbnail và file tổng hợp. Chat tạo nội dung không được tự gen ảnh thật thay prompt thumbnail.
- Bộ nhớ kênh chỉ đưa vào Master mới khi đúng ngách, không đưa charter G-13 sang kịch bản đường sắt/ô tô. Không tự thay lịch, model, queue hoặc video Public. Bản ghi dữ liệu nguồn đi kèm; số CPM/view ước lượng trong báo cáo không phải bằng chứng hiệu quả của Master.
- Master mới có cổng output do code kiểm: mã lượt/JSON/domain/hash, title winner, số prompt ảnh, cue 40 ký tự và thiết kế thumbnail. Sửa trong cùng ngân sách nội dung bền qua restart, giữ phần đã hợp lệ; lượt chưa rõ kết quả vẫn chỉ thu hồi biên nhận. Gói cũ không bị áp schema mới.
- Khôi phục nhận đúng phần bộ nhớ kênh ghép vào Master, tránh coi nó là Master khác rồi nạp lại; giữ ổn định xuống dòng Windows khi đọc/ghi lại gói đã chấp nhận.

## 2.4.322

- Chặn nhánh thumbnail cũ tiếp tục dò nguồn vô hạn sau ba lần dò thất bại, kể cả callback khi mở lại tool. Giữ nguyên biên nhận, URL và số lần đã dùng; ảnh đã thu hồi vẫn được kiểm tra tiếp.
- Bridge 1.0.156 nhận diện đúng phản hồi kết thúc “Xin lỗi, công cụ tạo ảnh hiện không thực hiện được yêu cầu trong lượt này.” Vẫn phải xác minh đúng prompt, lượt đã kết thúc và không có ảnh cũ trước khi cho phép phục hồi.

## 2.4.321

- Lịch kiểm ảnh đã lưu tôn trọng cùng mốc 2/5/15 phút và ngân sách qua khởi động lại; callback giao diện không mở worker liên tục trong thời gian chờ hoặc sau khi hết ngân sách.
- Tạm giữ cả lượt đọc số liệu tự động khi đúng profile đã được xác nhận mất đăng nhập; giữ nguyên cấu hình theo dõi và cho phép kiểm tra thủ công.

## 2.4.320

- OCR không còn kết luận sai khi bộ nhận dạng tách dấu ba chấm thành các chấm ảnh; vẫn kiểm đủ chữ, số, màu và biên ảnh. Bước đọc ảnh độc lập tiếp tục bắt buộc khớp nguyên câu gồm dấu câu trước khi chấp nhận thumbnail.

## 2.4.319

- Khôi phục hữu hạn: phân loại đăng nhập, 429, đang xử lý, chưa rõ kết quả và sai cấu trúc; lưu ngân sách và biên nhận qua khởi động lại. Giữ hàng đợi khi YouTube cần đăng nhập.
- DaiTa14: bộ phân tích thumbnail dùng chung dữ liệu chữ/màu; kiểm OCR và ảnh thật trước upload/Public. Ảnh đã lưu được kiểm trong worker, không chặn giao diện hoặc gửi thêm câu xác nhận ĐÃ TẠO. Lưu URL và hash của chat kiểm ảnh.
- Sửa backup VOICEVOX legacy, JSON UTF-8 và biên nhận audit. Đối chiếu cảnh báo ASR bằng âm vị và âm thanh gốc có ngữ cảnh; giữ nguyên báo cáo thô và lời thoại, không tự coi cảnh báo là phát âm sai.
- Lịch sử phân tích kênh lớn được gửi đủ bằng tệp JSON có hash, giữ nguyên dữ liệu thiếu và bài học trước đây.
- Master nghiên cứu 11.0.0-r1 chỉ dành cho lượt mới của DaiTa14: hồ sơ bối cảnh, đa dạng cấu trúc kể, chống trùng cơ chế sự cố, phụ đề tối đa 40 ký tự; giữ marker, nhịp ảnh 30 giây và lịch thử hiện có. Không tự A/B video Public.
- Bridge 1.0.155 được đối chiếu với source; không đổi Donut, profile, cookie hoặc lịch tắt. Build/test không được xem là bằng chứng chuỗi Public → Unlisted → lượt mới đã hoàn tất.

## 2.4.315

- Thử nghiệm riêng DaiTa14: lưu số liệu Studio theo channel ID, bộ nhớ có phiên bản, phân tích ChatGPT hằng ngày/tuần và cung cấp lịch sử cho lượt nội dung mới.
- Thêm xử lý clip dọc giữ trọn hình, màu nguồn; tư liệu minh họa tùy chọn có nguồn/giấy phép, lịch sử chống lặp, vị trí theo lời thoại và credit trước upload.
- Audit VOICEVOX chia theo dung lượng UTF-8, dùng chat riêng có biên nhận; hỗ trợ kiểm tra toàn bộ box bằng Whisper large-v3 trên ổ D và giữ checkpoint khi chưa đạt.
- Giữ nguyên lịch đăng, queue Public/Unlisted và cấu hình profile. A/B tự động chưa kích hoạt trong pilot.

## 2.4.314

- Không gửi lại tin “VOICEVOX đã sẵn sàng” sau mỗi lượt precheck tự động. Chỉ gửi khi engine thực sự được khởi động hoặc người dùng yêu cầu khởi động qua Telegram.
- Giữ xác nhận cho lệnh Telegram đang chờ lượt kiểm tra trước kết thúc. Các lỗi workflow, audit và checkpoint tiếp tục được xử lý độc lập.

## 2.4.311

- Bridge 1.0.151 nhận diện câu báo lỗi tạo ảnh đã quan sát ở DaiTa8 khi phản hồi bắt đầu bằng “Công cụ tạo ảnh… không xuất được thumbnail”. Chỉ xác nhận không có ảnh sau khi đọc đúng prompt trong nguồn hội thoại đã hoàn tất; giữ kiểm tra ảnh cũ, biên nhận và ngân sách chat phục hồi.
- Tách HTTP 429 của danh sách lịch sử bên cạnh khỏi giới hạn của yêu cầu gửi/đọc đúng hội thoại. Lịch sử không tải được vẫn đóng trang đọc và chờ; yêu cầu gửi bị giới hạn vẫn giữ checkpoint và dừng đúng hạn.

## 2.4.310

- Bridge 1.0.150 đọc thanh nút của phản hồi khi ChatGPT đặt thanh này cạnh nút nội dung. Giới hạn vùng tìm theo đúng phản hồi; không lấy nút từ tin nhắn trước, ô soạn thảo, nội dung mã hoặc phản hồi còn đang sinh.
- Bộ kiểm thử Electron dùng HTTPS cục bộ để kiểm tra hash prompt với Web Crypto giống môi trường thực.
- Giữ dữ liệu profile, cookie, URL hội thoại, model đang chọn và lịch Public.

## 2.4.293

- Bộ thu hồi ảnh mở phần chữ prompt thumbnail bị thu gọn trước khi so hash. Dò đúng vùng cuộn ở giao diện ChatGPT mới để tải lịch sử cũ; khi lịch sử còn đang tải không kết luận đã đọc hết.
- Các thao tác xem ảnh vẫn bị khóa theo đúng chat, không gửi prompt mới, không bấm Retry.

## 2.4.292

- Thêm kiểm tra lịch sử ảnh trong đúng chat đã lưu: đọc các tin nhắn thu gọn và điều khiển xem ảnh, chụp đúng renderer để xác minh ảnh cũ. API chỉ cho phép các nút hiển thị đã quan sát; không có quyền gửi prompt, Retry, sửa hoặc xóa ảnh.
- Chế độ thu hồi thumbnail theo yêu cầu người dùng giữ nguyên biên nhận và số lượt tạo ảnh, khóa gửi thêm trong đúng chat; video khác vẫn dùng cơ chế bình thường.
- Giữ nguyên dữ liệu profile, nguyên liệu render và lịch Public.

## 2.4.291

- Bridge 1.0.137 đọc lịch sử ảnh có giới hạn sau khi lượt thumbnail gần nhất kết thúc không có ảnh. Nhánh này chỉ thu hồi ảnh gắn với đúng hash prompt của video, kể cả ảnh ở lượt trước; không gửi thêm prompt.
- Khôi phục vị trí cuộn và kiểm tra lại hội thoại sau khi đọc. Đổi chat, đang sinh hoặc lịch sử chưa đọc hết sẽ giữ checkpoint, không suy ra quyền gửi lại.
- Giữ nguyên dữ liệu profile, model ChatGPT và lịch Public.

## 2.4.290

- Skip cục bộ có thể gắn expected_thread_id với đúng lượt lỗi đã được người dùng chọn. Hủy worker phục hồi đang xếp hàng bằng đường native có token barrier; từ chối nếu checkpoint đã chuyển sang lượt mới.
- Giữ chặn Skip khi video đã render hoàn tất hoặc có checkpoint upload; hàng đợi Unlisted/Public nằm ngoài archive của lượt đang lỗi. Lệnh cũ không có thread vẫn chỉ nhận lượt lỗi đang rảnh.
- Không đổi Bridge 1.0.136, model ChatGPT, dữ liệu profile/cookie hoặc lịch các workspace.
- Kiểm tra chat trước khi thử lại lượt thumbnail đã kết thúc dùng mã tin nhắn và hash nội dung thay cho độ dài HTML/đếm wrapper Markdown. Vẫn chặn nội dung/mã tin đổi, đang sinh ảnh, phiên bận, sai chat hoặc thiếu đăng nhập.

## 2.4.289

- Lệnh Skip cục bộ nhận lượt lỗi đang rảnh dù workspace đang chờ tự phục hồi; không còn phụ thuộc riêng cờ khóa thủ công.
- Giữ chặn Skip khi worker đang chạy, render đã hoàn tất hoặc đã có checkpoint upload. Skip tiếp tục dùng đường hủy/archive hiện có, giữ hàng đợi Unlisted và lịch Public.
- Giữ nguyên Bridge 1.0.136 và toàn bộ sửa an toàn trước đó; không đổi dữ liệu profile, cookie hoặc model ChatGPT.

## 2.4.288

- Gắn sự kiện POST thực tế của ChatGPT vào đúng biên nhận trước Send, từ lúc request bắt đầu đến headers/lỗi stream. Chỉ giữ mã request nội bộ, thời gian, HTTP và mã net::ERR; không giữ URL/body/header/cookie.
- Không dùng “không có DOM mới” để kết luận chưa gửi nếu đã thấy request đến provider. HTTP 200 rồi stream ERR_FAILED giữ trạng thái chưa rõ kết quả và khóa resend; dữ kiện sống qua khởi động và qua callback DOM ack.
- Client giữ lỗi transport hiện tại từ bước recover thay vì che bằng lỗi chờ DOM cũ. Không cấp lượt ảnh thay thế cho biên nhận chưa xác nhận có transport thật.
- Giữ các sửa kênh Send, ngân sách đọc, ảnh đã lưu và điều kiện đủ nguyên liệu trước upload/Public từ 2.4.287.

## 2.4.287

- Chọn lại đúng nút Send và tọa độ sau bước lấy mốc lịch sử/hash; thêm pointer move trước click, không bấm tọa độ cũ sau React reflow.
- Giữ bằng chứng Bridge xác minh lượt ảnh chưa gửi khi một lần resend truyền vẫn thất bại. Studio cho phép đúng một lần phục hồi truyền theo thread/SHA hiện tại, rồi dừng nếu vẫn lỗi; không gửi lại lượt đã nhận/chưa rõ.
- Biên nhận mới có ngân sách đọc riêng; generation budget vẫn giữ. Khi đã hết lượt tạo ảnh, chỉ recover kết quả đang có, không Stop/cấp ảnh thay thế.
- Ghi riêng kết quả HTTP của POST tạo lượt ngay khi nhận headers, để yêu cầu sidebar không xóa mất dấu vết lỗi gửi. Không ghi URL, body, token hay cookie.

## 2.4.286

- Sửa API diagnostic của Bridge dùng sai biến khi đọc tùy chọn kiểm tra composer, gây lỗi HTTP 500 cả với yêu cầu mặc định.
- Kiểm thử API diagnostic trên Electron thật: mặc định, hash composer không lộ nội dung, trì hoãn khi có owner đang chạy và từ chối thread thiếu. Không gửi prompt, không đổi draft/cookie.
- Giữ toàn bộ cơ chế đối soát lượt ảnh, ảnh đã lưu, model đã chọn, hạn mức thử lại và điều kiện metadata/thumbnail trước upload/Public của 2.4.285.

## 2.4.285

- Lấy lại mốc user/assistant/key ngay trước thao tác gửi ảnh, sau khi lịch sử chat đã dựng; không dùng mốc cũ trước lúc nhập draft để nhận nhầm user echo.
- Lượt ảnh phải có đúng prompt mới và composer đã tiêu thụ draft. Thiếu key ở chat /c đã lưu sẽ dừng trước Send; lịch sử dựng lại và click không thực sự gửi không được coi là đã nhận.
- Ưu tiên nút Send chuẩn theo selector, loại controls ẩn/đang bị che, tránh bấm Send feedback trước composer.
- Dùng ngay ảnh đã nhận khi bytes/SHA-256/prompt/chat khớp, theo hợp đồng đọc ảnh đến muộn đã có. Không cần gửi lại câu xác nhận hay phụ thuộc hạn mức model; ảnh thiếu, hỏng hoặc sai chủ sở hữu vẫn chặn upload/Public.
- Ảnh cache vượt kiểm tra được nối sau khởi động và không bị ngân sách dò DOM đã cạn chặn lại. Giữ bộ đếm cũ, không tạo thêm ảnh hoặc mở profile để xử lý ảnh sẵn có.

## 2.4.284

- Bộ đọc ảnh lịch sử dùng phần prompt đầy đủ trong bubble, loại ellipsis/Show more; luôn dò ảnh của đúng prompt trước, kể cả khi lượt cuối đã là xác nhận bằng văn bản.
- Biên nhận đã được xóa sau phản hồi hoàn tất được đối soát bằng đọc kết quả, không báo nhầm checkpoint đổi và không tự cấp quyền gửi mới.

## 2.4.283

- Ghi trạng thái chưa rõ kết quả ngay trước thao tác gửi thumbnail; lỗi không có echo không còn quay lại nhánh gửi mới mỗi 30 giây. Đối soát vẫn có ngân sách bốn lượt và giữ qua khởi động.
- Kiểm tra HTTP 429 trước Send và khi chờ echo; giữ biên nhận nếu đã gửi, tôn trọng Retry-After và không tải lại/gửi lại để chữa hạn mức.
- Lưu bytes ảnh đúng prompt/chat trước khi hỏi xác nhận. Nếu xác nhận lỗi, nối lại lượt xác nhận cũ trước; giữ file và chỉ dùng ảnh sau khi xác nhận đạt yêu cầu, không tạo lại ảnh.
- Lượt xác nhận phủ định được kiểm tra thêm một lần bằng chính file ảnh đính kèm trong cùng chat. Ảnh chờ xác nhận có ngân sách đối soát, không được tự Public.
- Thu hồi ảnh thuộc đúng SHA-256 prompt ở trước lượt xác nhận, loại ảnh của video sau và ảnh nguồn đính kèm. Lượt prompt trùng mới hơn không được lấy ảnh của lượt cũ hơn.

## 2.4.282

- Xác nhận lượt ảnh bằng toàn bộ SHA-256 của user prompt mới; key/count DOM cũ dựng lại không còn được coi là biên nhận gửi thành công. Lượt thử cùng prompt phải có user message mới.
- Đối soát ảnh kẹt bằng đúng chat, prompt và biên nhận: giữ ảnh đến muộn, tôn trọng đăng nhập/429, quan sát Stop không tiến triển tối thiểu 90 giây sau khi lượt đã quá 30 phút; chỉ dừng đúng lượt ảnh đó một lần. Chỉ cấp lượt thay thế sau hai lần xác minh ổn định và lưu biên nhận cũ.
- Khôi phục biên nhận sai từ bản cũ khi echo không khớp và lịch sử trước gửi không đổi; không gọi lượt cũ là chưa gửi. Quyền thử lại có thể nối sau khởi động và không dùng cho prompt/video khác.
- Mỗi lượt ảnh có bốn lần đối soát chủ động, tối đa ba lượt ảnh thay thế; sau đó chỉ đọc ảnh đến muộn có giới hạn. Giới hạn tài khoản không tiêu hao lượt đối soát.
- Lưu chống cảnh báo phục hồi trùng qua khởi động trong 12 giờ, vẫn báo ngay lỗi hạn mức, lỗi cần thao tác, video mới hoặc lượt tạo ảnh mới. Mọi traceback tiếp tục ghi đầy đủ vào log.

## 2.4.281

- Giữ lịch tự thử lại thumbnail khi Bridge kiểm tra xong trước lúc hết thời gian chờ của lượt đã xác nhận không tạo ảnh. Nhánh gọi lại phải đăng ký lại timer thay vì rơi vào trạng thái chờ ảnh thủ công.
- Dùng nguyên giới hạn ba lần thử và thời gian chờ hiện có; giữ biên nhận, checkpoint, video đã render và điều kiện bắt buộc có thumbnail trước upload/Public. Lượt chưa rõ kết quả hoặc hết hạn mức vẫn được giữ theo cơ chế cũ.

## 2.4.280

- Tự dò tối đa ba chat chưa thuộc workspace khác khi lượt nạp Master đầu tiên đã được nhận nhưng mất URL. Xác minh bytes/SHA của tệp đã staging và mã riêng trong tên tệp, rồi đọc hai lần ổn định trước khi nối lại đúng chat và thu hồi phản hồi.
- Giữ nguyên biên nhận đã gửi và checkpoint khi chưa tìm được. Nhánh dò không gửi lại Master, không dừng lượt ChatGPT và không áp dụng cho các lượt nội dung về sau.

## 2.4.279

- Đặt thời gian dò lại DOM/gián đoạn kết nối của lượt phục hồi trước hạn nhường slot 2 phút, để có thời gian tải lại đúng hội thoại và thu hồi phản hồi; không bấm Stop hoặc gửi lại prompt.
- Nút Copy của phản hồi cũ không ngăn bước dò phản hồi mới đang thiếu; giữ yêu cầu đúng lượt và đúng hội thoại trước khi nhận kết quả.
- Khi nút Send vẫn thiếu/khóa và giao diện báo Connection interrupted sau lần reload trước khi gửi, nối vào cơ chế thay reader lỗi để mở lại đúng hội thoại thêm một lần có cooldown. Giữ draft/checkpoint và phiên đăng nhập.
- Chỉ nhánh chưa có thao tác Send mới được thử gửi ở reader đã phục hồi. Biên nhận đã gửi/chưa xác nhận tiếp tục khóa gửi lại; hạn mức hoặc lỗi đăng nhập từ máy chủ vẫn chờ theo cơ chế hiện có.
- Bỏ lời nhắc buộc mở/cập nhật Bridge trong thông báo DOM tạm thời; giữ chi tiết lỗi và trạng thái checkpoint để phân biệt lỗi tự phục hồi với lỗi cần thao tác.

## 2.4.278

- Lượt mới đã được ChatGPT nhận nhưng không ra nội dung/ảnh mới trong 5 phút sẽ nhường slot cho workspace khác; nút Stop và heartbeat không kéo dài slot vô hạn. Khi có chữ mới thực sự, thời gian chờ được gia hạn.
- Chỉ nhường lượt đọc cục bộ, giữ nguyên biên nhận, chat và model; không bấm Stop của nhà cung cấp hoặc gửi lại prompt. Lần sau chỉ nối đúng lượt đang chờ bằng cơ chế phục hồi hiện có.
- Hủy timer khi lượt hoàn tất hoặc chuyển sang một lần thử ảnh đã được xác minh thất bại, tránh timer cũ ảnh hưởng lượt tiếp theo.

## 2.4.277

- Tự dò lại đúng lượt nội dung sau khi lỗi tải chat/đọc phản hồi, kể cả ca Public đã hoãn; thêm hai lượt dò có thời gian chờ vào ngân sách bền vững hiện có. Không tiêu ngân sách trong thời gian tài khoản bị giới hạn.
- Khi hai lần kiểm tra xác nhận chat rảnh, đúng phản hồi đã lưu và kịch bản đủ, tự yêu cầu bổ sung phần thiếu đúng một lần trong cùng hội thoại. Giữ lượt TIẾP mơ hồ để đối chiếu; không gửi lại lượt đó, không đổi chủ đề hoặc model.
- Lượt bổ sung được lưu trước khi gửi, nối tiếp đúng mã lượt sau khởi động lại; kiểm tra kịch bản và tiêu đề đã có trước khi đi tiếp. Các khóa upload/thumbnail và yêu cầu đủ nguyên liệu vẫn được giữ.

## 2.4.276

- Phân biệt lỗi tải lịch sử ở thanh bên với lỗi tải hội thoại trong vùng chat chính. Home có ô nhập hợp lệ vẫn hoạt động khi thanh bên báo Unable to load history.
- Giữ kiểm tra lỗi thực của hội thoại, trang chưa tải và đăng nhập; không bỏ qua các khóa biên nhận hay thay đổi model đã chọn.

## 2.4.275

- Không tính câu hỏi giao diện về personality thành tin nhắn người dùng; phản hồi đúng lượt vẫn được đọc. Câu tương tự trong tin nhắn thật vẫn được giữ.
- Đối soát lượt Master Prompt đính kèm bằng SHA-256, độ dài và nguyên văn caption đúng tệp, cùng toàn bộ phản hồi đã checkpoint. Chỉ nối prompt kế tiếp sau hai lần chứng minh chưa gửi; không gửi lại tệp/nội dung đã nhận.
- Khi trang chat chưa tải, phục hồi đúng trang đọc trước khi đối soát; giữ cookie, tệp đính kèm và biên nhận, tuân theo cooldown/giới hạn hiện có.

## 2.4.274

- Ca Public đang đến không chạy lại precheck/Flow khi video hiện tại chờ thumbnail giữa các lượt dò; timer thumbnail giữ checkpoint. Video FIFO đã upload vẫn có thể Public đúng ca.
- Video đã Public nhưng thiếu khai báo quảng cáo/metadata được bổ sung và xác minh trên đúng mã video, giữ Public; lỗi lưu vẫn khóa xác nhận và giữ hàng đợi, không upload lại tệp.

## 2.4.273

- Hỗ trợ nhóm Có/Không mới cho khai báo quảng cáo trả tiền trong YouTube Studio. Giữ khai báo đã chọn; khi nhóm còn trống, dùng mặc định không có tài trợ của video tự tạo.
- Kiểm tra khai báo này trong lần đọc lại metadata trước khi xác nhận upload/Public, giữ nguyên mã video và checkpoint.

## 2.4.272

- Đọc nguyên văn prompt trong bubble người dùng, không đưa dấu ba chấm và Show more vào mã đối chiếu.
- Khi lượt thumbnail đã đứng ít nhất 15 phút, hai lần đọc đúng prompt xác nhận không có phản hồi/ảnh và không sinh, Bridge lưu biên nhận cũ vào nhật ký trước khi nới khóa cho lần thử lại có giới hạn trong cùng chat.
- Không nới khóa các lượt nội dung, lượt đang sinh, prompt khác, ảnh vừa xuất hiện hoặc biên nhận đã đổi; vẫn giữ checkpoint khi mất xác nhận.

## 2.4.271

- Đối chiếu URL chat phục hồi bỏ chữ nút giao diện khỏi nội dung prompt; vẫn yêu cầu khớp toàn bộ prompt đầu tiên và giữ biên nhận gốc.
- Chẩn đoán trả lý do candidate không khớp để phân biệt khác nội dung với lịch sử chưa tải xong.

## 2.4.270

- Nối checkpoint còn xếp hàng bằng nguyên văn lượt đã hoàn tất ngay cả khi ChatGPT đặt nút hành động ngoài khối phản hồi.
- Thêm dò liên kết hội thoại trong chẩn đoán; phục hồi URL bị thiếu chỉ khi liên kết thuộc lịch sử tài khoản và hai lần đọc khớp đúng toàn bộ prompt đầu tiên. Giữ biên nhận, không gửi thêm prompt.

## 2.4.269

- Checkpoint phân biệt lượt ChatGPT còn xếp hàng với lượt đã bắt đầu truyền; lưu trạng thái trước HTTP để restart không làm mất lượt chưa gửi.
- Với checkpoint cũ thiếu biên nhận, Bridge 1.0.120 chỉ xác nhận chưa gửi khi hai lần đọc cùng chat khớp toàn bộ lượt đã hoàn tất trước đó; giữ khóa khi prompt giống lượt trước, DOM chưa hoàn chỉnh hoặc còn đang sinh.
- Không coi lại phản hồi TIẾP cũ là phản hồi mới khi prompt lặp nhưng biên nhận bị thiếu.

## 2.4.268

- Bridge 1.0.119: ô tải tạm pending-home-input không còn được nhận là composer sẵn sàng; chờ ChatGPT dựng composer thật trước khi nhập/gửi hoặc xác nhận đăng nhập.
- Skip hủy worker thumbnail và callback chờ Bridge; kết quả/callback cũ không ghi đè checkpoint hay ảnh của video thay thế.

## 2.4.267

- Bridge 1.0.118: tải lại đúng hội thoại khi cảnh báo Connection interrupted đứng yên 2 phút dù nút Stop còn hiện; không áp thời gian chờ này cho High đang suy nghĩ bình thường.
- Thay cửa sổ đọc thumbnail bị treo và giữ cookie, URL hội thoại, checkpoint và biên nhận gửi. Không gửi lại prompt trong đường phục hồi này.

## 2.4.266

- Nhánh thay/đóng trang đọc ChatGPT lỗi dùng cùng bộ kiểm tra URL với nhánh lưu hội thoại. Không ghi đè link hội thoại máy chủ bằng địa chỉ tạm local-chatgpt; giữ biên nhận gửi và cookie để phục hồi đúng lượt.

## 2.4.265

- Lệnh bảo trì cục bộ Skip gọi đúng cơ chế dừng worker, lưu chẩn đoán và bỏ phiên hiện tại của launcher. Chỉ nhận workspace cụ thể đang khóa chờ xử lý; không Skip lại video mới. Giữ hàng đợi Unlisted, lịch và video đã Public.

## 2.4.264

- Tiếp tục nội dung không bị chặn bởi Checkview đang chạy nền. Render, upload và các worker đang xử lý cùng video vẫn chặn lượt tiếp tục trùng; đóng/cập nhật tool vẫn chờ cả Checkview dừng.

## 2.4.263

- Nhường hàng đợi ChatGPT sau từng phản hồi hoặc lượt đọc phục hồi, thay vì giữ suất cho toàn bộ chuỗi master prompt/TIẾP. Giữ nguyên hội thoại, checkpoint và lựa chọn model.
- Đóng trang đọc lịch sử đã lỗi sau khi công đoạn thất bại để trang đó không tiếp tục gọi lịch sử trong thời gian chờ. Giữ cookie, tệp đính kèm và biên nhận đã gửi; tránh tải hai lần khi vừa tạo một trang đọc ảnh mới.
- Nếu mất biên nhận Bridge, chỉ nối lại câu trả lời đã hoàn tất khi toàn bộ tin nhắn khớp SHA-256 với checkpoint trong đúng hội thoại và hai lần kiểm tra DOM ổn định. Không dùng trường hợp mất biên nhận để cho phép gửi lại.
- Tôn trọng thời gian Retry-After hợp lệ của máy chủ; cập nhật nhãn lịch khi workflow thực sự được tiếp tục. Vẫn chặn upload/Public khi thiếu nội dung hợp lệ, thumbnail hoặc metadata.

## 2.4.258

- Khôi phục batch ảnh Flow hoàn tất từ đĩa trước khi kiểm tra Edge/Flow hoặc vào hàng đợi. Xác minh workspace, signature, từng prompt và từng tệp ảnh; batch thiếu/hỏng vẫn dùng cơ chế phục hồi hiện có. Không gửi lại prompt của batch đã hoàn tất.
- Giới hạn mỗi lượt đọc phục hồi ChatGPT đã gửi ở 2 phút, nhường phiên nhưng giữ hội thoại và biên nhận. Bridge 1.0.113 không bấm Stop phía ChatGPT hoặc tự gửi lại lượt đang chưa rõ kết quả.
- Lỗi tải hội thoại/thiếu bằng chứng phản hồi dùng cùng ngân sách dò chỉ đọc bền vững, tối đa 3 lần; không đặt lại ngân sách sau restart.
- Worker nội dung, thumbnail và VOICEVOX ghi traceback cục bộ; giao diện gửi một cảnh báo sau khi quyết định tự phục hồi hay dừng. Cảnh báo giống hệt của cùng video/hội thoại chờ 15 phút; lỗi thay đổi hoặc video mới được báo ngay.
- Giữ lựa chọn model và chặn upload/Public nếu chưa đủ nguyên liệu, thumbnail hoặc metadata.

## 2.4.255

- Giữ trạng thái chưa xác minh khi Bridge báo còn lượt ChatGPT chưa thu hồi. Không biến lỗi đó thành lượt chưa gửi và thử tạo thumbnail mới mỗi 30 giây; tự nối lại đúng checkpoint sau thời gian chờ, giữ biên nhận cũ.
- Đối chiếu lại các trạng thái thumbnail của bản trước có cùng lỗi khi mở workspace, giữ nguyên hội thoại, prompt, ngân sách thử và mốc thời gian.

## 2.4.254

- Khi render đã xong nhưng thiếu thumbnail, giữ trạng thái chờ nguyên liệu; không lặp đối chiếu upload cũ mỗi 5 giây và không tiêu thụ ca Public.
- Mọi lượt dò ảnh chưa đủ bằng chứng trước khi đặt lượt tạo mới đều cập nhật thời gian chờ bền vững, kể cả hội thoại đã tải nhưng ảnh chưa được xác nhận. Giữ nguyên giới hạn tạo lại, mã hội thoại và checkpoint.

## 2.4.253

- Khóa gửi lại vẫn được giữ nếu hội thoại lỗi, composer chưa sẵn sàng hoặc lịch sử tải ít hơn số lượt đã có trước khi bấm Gửi. Chỉ xác nhận lượt chưa gửi sau khi tải đúng hội thoại, đủ baseline và phiên đăng nhập/composer sẵn sàng.
- Trả lỗi tải hội thoại ngay ở bước đối soát checkpoint để không chờ phản hồi trên trang lỗi. Giữ bằng chứng lỗi của từng cửa sổ khi trang khác phát nhiều yêu cầu nền.

## 2.4.252

- Bổ sung chẩn đoán yêu cầu tải tài khoản/lịch sử và trang hội thoại ChatGPT khi website đổi đường dẫn hoặc trả lỗi ngay trong trang. Chỉ lưu nhóm đường dẫn đã rút gọn và mã phản hồi; không suy diễn HTTP 200 của trang HTML thành tải hội thoại thành công.

## 2.4.251

- Khi tải đúng hội thoại ChatGPT vẫn lỗi sau reload, thay riêng trang đọc một lần và giữ nguyên link, tệp, phiên đăng nhập và checkpoint. Nếu đã bấm Gửi, chỉ thu hồi phản hồi cũ; không gửi lại prompt.
- Ghi nhận mã HTTP/lỗi kết nối của yêu cầu tải hội thoại để phân biệt lỗi mạng, máy chủ, quyền truy cập và hội thoại không còn khả dụng. Không ghi token, cookie, header hoặc nội dung yêu cầu.
- Không lặp thay trang với lỗi 401/403/404/429 đã xác minh; lần thay trang khác có thời gian chờ 15 phút để tránh vòng phục hồi liên tục.

## 2.4.250

- Nhận diện thông báo kiểm tra độ ổn định ChatGPT và chờ đúng giờ mở lại; giữ model Web đang chọn và checkpoint đã gửi.
- Luồng tạo nội dung chính dùng trực tiếp Bridge để tránh lệnh compaction của CLI lọt vào hội thoại. Master prompt dài được gửi bằng tệp UTF-8 có hash, giữ nguyên byte gốc.
- Nhận diện thẻ tệp ChatGPT hiện chỉ có tên tệp. Chỉ nối lại tệp chưa gửi sau khi hash khớp và hai lần kiểm tra đúng hội thoại xác nhận thẻ tệp ổn định, không có lượt đang tạo.
- Phân biệt CRLF/LF khi đối chiếu nội dung master prompt, giữ nguyên tệp và không nhận nhầm lỗi đính kèm của lượt cũ.
- Dừng SEO tags trước các khối prompt đánh dấu để không trộn prompt ảnh vào metadata YouTube. Upload và Public vẫn yêu cầu đủ thumbnail và metadata.
- Lỗi kiểm tra tài khoản 1MM từ monitor nền ghi log và thử lại sau hai phút, không mở nhiều hộp thoại chặn cửa sổ. Khi đóng tool không mở lại hộp thoại hoặc monitor.

## 2.4.235

- Tách project VOICEVOX lớn thành các lượt audit tối đa 500 box, kèm ngữ cảnh toàn kịch bản. Kiểm tra đủ mọi câu, giữ nguyên text/speaker/thứ tự và nối phần đã audit từ checkpoint, không gọi lại phần đã xác minh.
- Thay riêng renderer ChatGPT khi probe DOM bị treo; giữ link hội thoại, trạng thái tệp và khóa lượt đã gửi. Đọc ảnh có giới hạn IPC và thời gian tải dữ liệu để không giữ hàng đợi vô hạn.
- Giữ bằng chứng lượt thumbnail đã kết thúc không có ảnh để chuyển sang nhánh thử lại có kiểm tra. Workspace khác đang chạy không chặn việc kiểm tra hội thoại thumbnail đã rảnh.
- Lượt sửa JSON VOICEVOX nhận đúng lỗi validator cần bổ sung; vẫn chặn dữ liệu không hợp lệ trước render.

## 2.4.234

- Giới hạn thời gian của lệnh nhập văn bản ChatGPT bị treo; đóng riêng renderer không phản hồi trước khi trả lượt cho hàng đợi. Không chuyển kênh nhập hoặc bấm Gửi khi kết quả lệnh nhập chưa rõ.
- Cho phép hủy người chờ thao tác ChatGPT mà vẫn giữ thứ tự và khóa của phiên đang thao tác. Người chờ không chiếm workspace vô hạn.
- Phân biệt tín hiệu còn sống với số ký tự đã được xác minh. Watchdog phát hiện bước nhập/chờ thao tác không tiến triển, giữ checkpoint và giải phóng lượt; không áp thời hạn này cho model đang suy nghĩ.

## 2.4.233

- Khi xóa workspace, đóng phiên và hủy các lịch/monitor của đúng workspace trước khi chuyển thư mục vào Thùng rác. Các phiên cũ không được lưu lại hoặc tự thêm workspace đã xóa.
- Ghi nhận workspace đã chủ động xóa để bước phục hồi thư mục/nhập dữ liệu từ bản cũ không tự khôi phục lại. Giữ khả năng phục hồi khi mất registry và cho phép tạo workspace mới cùng tên.

## 2.4.232

- Phân biệt cảnh báo giảm năng lực/chất lượng với lỗi giới hạn gửi thật. Cảnh báo không còn tạo mã 429 hoặc khóa toàn bộ hàng đợi; giữ model đang chọn trên Web và không bấm lựa chọn High bị vô hiệu hóa.
- Đối soát thời gian nghỉ dài của bản cũ bằng bước đọc trạng thái các trang đang rảnh; chỉ gỡ khi xác minh đăng nhập, composer sẵn sàng và không có lỗi giới hạn thật. Giữ các giới hạn thật qua khởi động lại; nối hàng đợi từng lượt sau khi đối soát.
- Khi lỡ ca Public và khởi động lại, tiếp tục sản xuất video chưa xong từ checkpoint ngay. Giữ nguyên ca Public, video đang dở và khóa chống gửi/upload trùng.

## 2.4.231

- Khi thumbnail nối lại trước khi Bridge kiểm tra khởi động xong, giữ nguyên checkpoint và chờ; tự nối ngay khi Bridge sẵn sàng, không ghi lỗi giả hoặc đặt lại thời gian chờ của lượt đã gửi.
- Giữ khóa checkpoint `recovering` sau khởi động lại; không cấp nhầm một lượt tạo ảnh mới khi lượt cũ còn trong thời gian chờ kiểm tra. Trạng thái hàng đợi phân biệt lượt thumbnail đã gửi với lượt chưa gửi.

## 2.4.230

- Khi ChatGPT đã xác nhận “ĐÃ XONG” nhưng bản tổng hợp thiếu tiêu đề, mô tả, prompt thumbnail, thoại hoặc prompt ảnh, tự yêu cầu đúng phần thiếu và kiểm tra lại trước khi tiếp tục. Dùng chung giới hạn 3 lần bổ sung, lưu số lần vào checkpoint và giữ nguyên nội dung hợp lệ đã có.
- Nối checkpoint cũ bằng bản tổng hợp đã lưu; không gửi lại export hoặc TIẾP chỉ vì thiếu dữ liệu. Nếu lượt bổ sung bị ngắt, thu hồi đúng phản hồi đang chờ trước khi gửi thêm; không để bản export ngắn hơn làm mất prompt ảnh đã phục hồi.

## 2.4.229

- Bỏ các dòng phân cách trang trí trong kịch bản VOICEVOX trước khi phân tích/tạo giọng. Khắc phục lỗi `Box 156 thiếu accentPhrases` khi ChatGPT đặt dòng `━━━━━━━━━━━━━━━━━━━━` sau 155 câu thoại; giữ nguyên câu thoại và dấu câu trong nội dung.

## 2.4.228

- Chặn mọi lượt upload mới nếu thiếu thumbnail hợp lệ, tiêu đề, mô tả hoặc SEO tags; kiểm tra lại sau khi chờ hàng đợi. Giữ nguyên cơ chế nối đúng bản nháp đã upload và chốt xác minh đủ metadata/thumbnail trước Public.

## 2.4.227

- Khi nối thumbnail sau khi mở lại, đọc ảnh đã tạo trong đúng hội thoại trước khi xử lý receipt không còn trong Bridge. Giữ khóa gửi lại nếu chưa có ảnh hoặc bằng chứng cho phép; không tạo yêu cầu ảnh mới chỉ vì receipt bị thiếu.

## 2.4.226

- Khôi phục thumbnail đã xác nhận không tạo ảnh khi hội thoại sau đó có lượt audit VOICEVOX. Chỉ đi tiếp với bằng chứng không có ảnh, hai lần xác minh đúng hội thoại đã rảnh và checkpoint cấp đúng một lượt thử riêng.

## 2.4.225

- Đồng bộ kiểm tra tương thích Studio với Chat Bridge 1.0.98 để nối các checkpoint sau cập nhật. Giữ bản sửa import master prompt và thử lại thumbnail của 2.4.224.

- Bản `2.4.223` thu hồi thumbnail tạo thủ công trong đúng chat bằng vị trí ảnh sau prompt mới nhất, kể cả phản hồi chỉ có ảnh và không có wrapper văn bản. Loại ảnh cũ/ảnh đính kèm và tiến trình ẩn; không gửi lại khi lượt mới chưa rõ kết quả.
- Khi công cụ ảnh xác nhận lỗi khởi tạo/nhầm sang chỉnh sửa cần ảnh nguồn, Bridge thử thêm đúng một lượt text-to-image trong cùng chat và giữ model Web đang chọn. Lỗi quota/chính sách hoặc lượt chưa xác nhận vẫn giữ checkpoint. Public tiếp tục đợi thumbnail hợp lệ và metadata được lưu, tải lại, xác minh.

- Bản `2.4.217` khi Public hoàn tất trong lúc Skip đang chờ tạo video thay thế, nối đúng lượt thay thế và xóa cờ Skip; không nhận nhầm ca sản xuất ngày sau hoặc khóa tiếp tục của video mới.
- Bản `2.4.216` tách ca Public của video đã upload xác nhận khỏi lỗi tạo nội dung video kế tiếp: vẫn chuyển đúng video Unlisted cũ nhất trong cửa sổ 2 tiếng, mỗi ca một video. Sau Public, giữ nguyên khóa/checkpoint của lượt tạo nội dung lỗi, không gửi lại prompt hoặc upload tệp mới; lỗi đối soát YouTube chưa rõ vẫn chặn Public.
- Bản `2.4.215` phân biệt URL ChatGPT tạm với hội thoại đã lưu; nhận diện lỗi không tải được tài khoản, chỉ tự tải lại trước khi gửi và giữ khóa gửi trùng cho lượt đã gửi.
- Bản `2.4.214` giới hạn Public trong ba cửa sổ theo giờ Việt Nam: 04:30–06:30, 08:30–10:30 và 13:30–15:30. Mỗi ca chỉ chuyển video Unlisted cũ nhất sang Public một lần; xác nhận A Public rồi mới upload B Unlisted, xác nhận upload B rồi mới render C. Lượt Public chưa hoàn tất khi cửa sổ đóng được giữ checkpoint để chuyển sang ca kế tiếp, không đăng bù dồn hoặc upload lại file chưa rõ kết quả.
- Bản `2.4.213` tự dọn media trong kho Uploaded/Skipped để tránh đầy ổ đĩa; bảo vệ video hiện tại, video Unlisted đang chờ và bản upload được xác nhận mới nhất. Sau khi video kế tiếp đã upload được xác nhận, media của video Public trước đó có thể được dọn. Giữ State, Prompts, lịch sử và dữ liệu profile; không dọn cookies, proxy hoặc fingerprint.
- Bản `2.4.185` tự đối soát trạng thái `youtube-retry` bị mồ côi bằng sổ chống đăng trùng và kho Uploaded; tiếp tục render sau khi upload cũ được xác nhận, còn trạng thái mơ hồ thì giữ khóa và dừng một lần thay vì lặp log mỗi 5 giây. YouTube Studio đôi khi không đổi trạng thái nút Save dù đã lưu; tool giờ tải lại đúng video và chỉ hoàn tất khi metadata đọc lại khớp.
- Bản `2.4.184` cố định việc tự đồng bộ các ca Public theo múi giờ Việt Nam trên mọi máy portable; giữ nguyên queue FIFO, checkpoint và không upload lại video.
- Bản `2.4.183` tự đồng bộ lại ngày của các mốc Public hằng ngày khi hàng đợi Unlisted còn video mà lịch bị trượt quá xa; giữ nguyên khung giờ, thứ tự FIFO và checkpoint, không upload lại video.
- Bản `2.4.179` tại mốc Public: nếu hàng đợi đã được đọc thành công và không còn video cũ đủ điều kiện, video render hiện tại được upload Public; khi còn video cũ, tiếp tục chuyển video cũ nhất lên Public rồi upload video kế tiếp Unlisted. Lỗi đọc queue và mọi lượt sau 15:00 vẫn không được tự đoán để đăng Public.
- Bản `2.4.177` đánh thức renderer tab Flow bằng CDP trước khi chờ document tải xong trong cửa sổ Edge thu nhỏ, đồng thời tự khôi phục checkpoint thiếu project ID nếu hash prompt, workspace và trạng thái xác nhận đủ 0 prompt đã gửi/0 ảnh hoàn tất. Chỉ lúc đó tool mới xóa lỗi gửi chưa thành công và tạo project Flow mới; nếu có ảnh, prompt đã gửi hoặc trạng thái mơ hồ thì vẫn chặn để tránh tạo trùng.
- Bản `2.4.172` ghi rõ trạng thái quyền riêng tư đã xác nhận trong thông báo YouTube: Không công khai (Unlisted), Công khai (Public) hoặc Riêng tư (Private). Video chờ ca Public được ghi rõ là Unlisted; bản ghi cũ thiếu bằng chứng không bị đoán trạng thái.
- Bản `2.4.171` đưa video mới vào hàng chờ Unlisted cho ca Public; sau khi xác nhận chuyển Public video cũ nhất, nếu video kế tiếp đã render xong thì tự upload Unlisted rồi bắt đầu render video sau.
- Bản `2.4.170` tự chờ Flow xử lý xong rồi đối chiếu đầy đủ đúng project/checkpoint trước khi tiếp tục; tự thu hồi đúng lượt thumbnail ChatGPT, chỉ mở một lượt mới khi thread, prompt, số lượt và trạng thái đứng yên khớp chính xác. Giữ nguyên checkpoint và không gửi lặp khi còn mơ hồ.
- Bản `2.4.169` giữ lại lỗi gốc và xác nhận hủy của extension Flow khi tab không trả lời. Checkpoint cũ chỉ được mở khóa gửi lại sau khi quét đầy đủ đúng project, không thấy prompt và xác nhận Flow đang rảnh; nếu thiếu bằng chứng, tool tiếp tục chặn để tránh tạo ảnh trùng. Đồng bộ Flow Agent với extension `1.3.48`.
- Bản `2.4.168` tự sửa checkpoint Flow cũ khi phát hiện project ID đang thuộc một workspace đã hoàn tất và quét đầy đủ project không tìm thấy prompt lỗi; chỉ khi không có ảnh/checkpoint thành công trong workspace hiện tại mới chuyển sang project mới. Nếu thiếu bằng chứng đầy đủ, vẫn giữ chặn gửi lại để tránh tạo ảnh trùng.
- Bản `2.4.167` đồng bộ Flow Agent với extension `1.3.47` đi kèm; sửa lỗi chặn khởi động Flow do tool vẫn yêu cầu `1.3.46`. Launcher portable tiếp tục tự mở Flow như cấu hình hiện tại.
- Bản `2.4.166` tự khôi phục ca Flow đã bị chặn khi mở lại tool: chỉ gỡ chặn tạm thời cho ca hẹn giờ đang lỗi để dò project bằng prompt khớp chính xác; nếu không xác định duy nhất project, checkpoint lại dừng an toàn. Không tự gửi lại prompt đang không rõ trạng thái.
- Bản `2.4.165` tự lưu mã project Flow vào checkpoint ngay khi giao diện Flow xác nhận project, kể cả khi generation bị timeout; khi workflow được tiếp tục, tool tự mở đúng project và đối soát prompt để tránh tạo trùng. Không đổi profile, cookie hoặc dữ liệu workspace.
- Bản `2.4.164` hoàn tất vá selector High sau khi thử trực tiếp bằng bridge: giữ lại kiểm tra DOM tùy chọn chỉ đọc, bỏ các thao tác menu chỉ phục vụ chẩn đoán. Xác minh High rồi hoàn tác về Medium mà không gửi prompt, không đổi hội thoại và không tạo checkpoint mới.
- Bản `2.4.163` sửa chọn mức High theo DOM ChatGPT hiện tại: điều khiển `Power` có `role=menuitem`, nhãn `Power` và `tabindex`; chỉ cho phép focus phần tử này khi menu xác nhận có thang Power, rồi đọc lại giá trị sau từng phím. Không gửi lại prompt.
- Bản `2.4.161` sửa lỗi chẩn đoán menu High phát sinh ở bản trung gian `2.4.160`: biến điều khiển tùy chọn được nội suy đúng trước khi chạy trong trang ChatGPT, khôi phục kiểm tra phiên và DOM; không gửi prompt hoặc thay đổi dữ liệu workspace.
- Bản `2.4.160` thêm chẩn đoán DOM chỉ đọc, giới hạn trong menu chọn mức suy luận ChatGPT và chỉ bật khi yêu cầu chẩn đoán nội bộ chỉ định rõ; dùng để xác định chính xác điều khiển Power sau thay đổi giao diện.
- Bản `2.4.158` nâng Chat Bridge lên `1.0.84`: phát hiện trường hợp cửa sổ Electron còn báo hiển thị nhưng renderer ChatGPT đã chuyển sang `visibility=hidden`; khôi phục trạng thái renderer trước khi tiếp tục đọc đúng lượt trả lời, giữ checkpoint và không gửi lại prompt.
- Bản `2.4.157` khôi phục workspace từ managed home cũ `Documents\\DocuRandom` khi app hiện tại dùng `Documents\\DocuRandom Studio`: gộp mục còn thiếu vào registry hiện hành, giữ nguyên các workspace và workspace đang mở gần nhất, sao lưu registry trước khi nhập, không di chuyển/xóa dữ liệu dự án và chỉ chạy nhập một lần.
- Bản `2.4.154` nâng Chat Bridge lên `1.0.83`: khi probe DOM của câu trả lời treo, tool phục hồi bằng cách tải lại đúng cuộc hội thoại `/c/...` qua kênh điều hướng của Electron; không probe chồng lệnh, không tải lại trang Home và không gửi lại prompt. Nếu chưa có URL hội thoại chính xác, vẫn dừng an toàn và giữ checkpoint.
- Bản `2.4.153` sửa lỗi cắt khoảng trắng và xuống dòng ở đầu/cuối phản hồi ChatGPT khi ghi vào callback, checkpoint và transcript; chuỗi gốc từ DOM được giữ nguyên, còn gói nội dung vẫn được phân tích theo marker. Giữ nguyên checkpoint và dữ liệu workspace hiện có.
- Bản `2.4.152` nâng Chat Bridge lên `1.0.82`: giữ nguyên chuỗi Markdown ChatGPT đã render từ DOM qua bridge đến ứng dụng, gồm khoảng trắng và xuống dòng đầu/cuối; chỉ loại nhãn speaker ở fallback khung hội thoại và vẫn dừng nếu không có câu trả lời. Không gửi lại prompt khi DOM thiếu, giữ nguyên checkpoint.
- Bản `2.4.150` nâng Chat Bridge lên `1.0.80`: selector ChatGPT ưu tiên textbox có nhãn `Ask ChatGPT`/`Hỏi ChatGPT` khi trang còn giữ editor cũ `Start writing`; chỉ dùng fallback nếu đó là vùng nhập duy nhất. Nếu vẫn có nhiều ô nhập hợp lệ, tool dừng an toàn thay vì nhập nhầm nội dung cuộc trò chuyện. Không đổi checkpoint, cookie, fingerprint hay profile Donut.
- Bản `2.4.149` nâng Chat Bridge lên `1.0.79`: khi ô soạn thảo ChatGPT remount ngay sau lúc kiểm tra sẵn sàng, tool chờ lại trong đúng cuộc trò chuyện tối đa 10 giây trước khi nhập; không lặp prompt hoặc lệnh gửi. Nếu composer vẫn mất, lỗi kèm chẩn đoán an toàn về selector/DOM (không ghi nội dung prompt) để phân biệt selector mới với trang chưa sẵn sàng. Giữ nguyên checkpoint, cookie, fingerprint và profile Donut.
- Bản `2.4.148` nâng Chat Bridge lên `1.0.78`: ưu tiên selector ô soạn thảo ChatGPT ổn định, chờ ô soạn thảo remount và xác minh bản nháp trước khi tiếp tục để tránh chèn lặp hoặc chèn thiếu; bộ chọn mức suy luận được tìm theo điều khiển ngữ nghĩa và xác minh đúng thang bốn mức. Khi không có phản hồi, bridge ghi thêm chẩn đoán DOM nhưng vẫn giữ nguyên quy tắc không gửi lại prompt đã được nhận. Giữ nguyên checkpoint, cookie, fingerprint và profile Donut.
- Bản `2.4.147` nâng Chat Bridge lên `1.0.77`: khi checkpoint xác nhận đã chọn tệp nhưng thẻ tệp biến mất, chỉ tải lại đúng hội thoại và thử nạp lại một lần sau hai lần xác minh composer trống; lượt đã gửi vẫn không được gửi lại. Phần tổng hợp cuối yêu cầu ChatGPT trả nội dung trực tiếp dạng văn bản thuần, không tạo document đính kèm khiến tool chỉ nhận tên tệp và không đọc được dữ liệu. Giữ nguyên checkpoint, cookie, fingerprint và profile Donut.
- Bản `2.4.146` nâng Chat Bridge lên `1.0.76`: nhả lượt thao tác chung ngay sau khi nhấp Gửi thành công, trong khi vẫn xác minh riêng từng hội thoại trước khi đánh dấu đã nhận. Điều này cho phép các workspace khác tiếp tục gửi trong lúc ChatGPT xác nhận chậm; không gửi lại prompt chưa chắc chắn và không đổi checkpoint, cookie, fingerprint hay profile Donut.
- Bản `2.4.145` nâng Chat Bridge lên `1.0.75`: báo tiến độ khi chờ lượt thao tác giữa các cửa sổ ChatGPT, dùng nhấp chuột tin cậy để mở bộ chọn High và xác nhận nhãn High hiện tại. Lượt đã chọn tệp nhưng chưa gửi được giải phóng sớm nếu kênh trả kết quả ngắt. Tệp đính kèm cũ khác yêu cầu hiện tại chỉ được bỏ liên kết sau khi tải lại đúng hội thoại và hai lần xác minh ô soạn thảo trống; giữ nguyên tệp đã lưu để kiểm tra. Không đổi checkpoint, cookie hay profile Donut.
- Bản `2.4.144` nâng Chat Bridge lên `1.0.74`: giới hạn thời gian mở cuộc hội thoại ChatGPT trước khi gửi prompt và giải phóng cửa sổ kẹt; tiếp tục báo trạng thái khi trang vẫn phản hồi nhưng bộ chọn model xuất hiện chậm. Tìm bộ chọn ở cả phần đầu hội thoại, vẫn đọc lại chính xác mức High trước khi gửi. Giữ nguyên checkpoint và dữ liệu profile.
- Bản `2.4.143` nâng Chat Bridge lên `1.0.73`: nhận diện một ô soạn thảo khi ChatGPT hiển thị cả phần bao và ô nhập con cùng lúc. Chỉ dùng ô nhập thật nếu xác định duy nhất; các ô độc lập vẫn dừng an toàn trước khi gửi. Bổ sung chẩn đoán cấu trúc ô nhập không chứa nội dung prompt để kiểm tra trực tiếp các trang đang lỗi. Checkpoint và profile giữ nguyên.
- Bản `2.4.142` nâng Chat Bridge lên `1.0.72`: sửa lỗi biến chưa khai báo khi probe xác nhận gửi gặp gián đoạn tạm thời; Bridge chờ và đọc lại đúng hội thoại mà không gửi lại prompt. Khi ô soạn thảo chưa hiện nhưng DOM vẫn phản hồi, Bridge ghi heartbeat từ probe thật để không khóa nhầm lượt đang phục hồi sau 120 giây. Checkpoint và dữ liệu profile được giữ nguyên.
- Bản `2.4.141` sửa Checkview khi YouTube Studio trả trang Analytics lỗi nhưng không hiện nút thử lại: tool tự tải lại đúng trang video tối đa hai lần, thử lại video lỗi một lần ở cấp quét, và không lặp lại toàn bộ profile như lỗi kết nối. Lượt quét vẫn dừng an toàn nếu chưa đọc đủ video; không dùng dữ liệu dở dang để đặt Like. Chat Bridge `1.0.71` nhận diện cấu trúc tin nhắn ChatGPT hiện hành để đọc câu trả lời đã có trong đúng cuộc hội thoại và khôi phục checkpoint mà không gửi lại prompt; chỉ đánh dấu lượt gửi đã được nhận khi thấy tin nhắn mới, không dựa vào riêng nút Stop.
- Bản `2.4.140` chuyển tín hiệu hủy Checkview tới worker ngay khi đóng toàn bộ workspace, để lượt đọc đang mở profile kịp đóng profile của chính nó trước khi ứng dụng thoát. Không đổi fingerprint, cookie hoặc dữ liệu profile.
- Bản `2.4.139` đóng profile do chính nhánh khôi phục upload mở sau khi đã xác minh video cũ hoàn tất hoặc đã hẹn Public. Giải phóng suất Wayfern cho Checkview và workspace tiếp theo, nhưng giữ profile mở khi kết quả upload còn chưa rõ hoặc profile đã mở sẵn trước đó. Không đóng lại ID tab từ phiên trình duyệt cũ; chỉ cảnh báo dọn tab khi cả Bridge lẫn CDP đều không xác nhận đã đóng. Không gắn lại tệp, không đổi fingerprint hay dữ liệu profile.
- Bản `2.4.138` xác nhận profile đã mở bằng CDP trước khi bỏ dấu chặn lệnh mở và xóa dấu này sau khi chính tool đóng profile thành công. Sửa trường hợp upload xong thì Checkview hoặc khôi phục bản nháp bị chặn bởi trạng thái `accepted` cũ; vẫn giữ checkpoint và khóa chống upload trùng khi YouTube chưa xác nhận hoàn tất.
- Bản `2.4.137` giải phóng profile do Checkview, upload hoặc chuyển Public tự mở trong cả trường hợp lỗi/hủy/timeout, để lỗi ở một workspace không làm đầy dần slot Wayfern. Checkview tiếp tục xếp lượt, không gửi lặp lệnh mở sau khi Wayfern báo đủ phiên; upload vẫn giữ checkpoint và không gắn lại tệp khi trạng thái chưa rõ. Profile đã mở sẵn được giữ nguyên; không đổi fingerprint, cookie, proxy hay dữ liệu workspace. Giới hạn đồng thời do Wayfern áp dụng vẫn được tôn trọng.
- Bản `2.4.136` nhận diện Wayfern từ chối mở do đã dùng hết số phiên của gói là lỗi hết chỗ tạm thời, không còn khóa nhầm profile như một lệnh mở chưa rõ kết quả; sau khi có slot, người dùng có thể tiếp tục mà không cần restart Donut. Checkview đóng profile do chính lượt quét mở khi lượt đó kết thúc; upload/Public chỉ đóng profile do workflow mở sau khi thao tác thành công, giúp các workspace được xử lý lần lượt mà không chiếm dần cả bốn slot. Profile đã mở sẵn được giữ nguyên; không thay fingerprint, checkpoint hay dữ liệu profile. Giới hạn Wayfern vẫn do gói hiện tại áp dụng.
- Bản `2.4.135` sửa Checkview khi Donut đã khởi động lại nhưng nhật ký mở profile cũ vẫn chặn các workspace: chỉ gỡ trạng thái cũ khi xác minh được tiến trình Donut mới và không còn trình duyệt của profile đó. Lệnh mở mới vẫn đi qua Donut và được giới hạn một lần; lỗi quyền fingerprint vẫn dừng tự động, không sửa dữ liệu profile. Nút Upload/Tiếp tục do người dùng bấm có thể yêu cầu Donut thử lại đúng một lần sau khi quyền hợp lệ. Checkview không giữ lượt quét thêm hai phút cho một lệnh cũ chắc chắn chưa được xác nhận; khi YouTube Studio báo lỗi Analytics, tool thử nút tải lại một lần rồi ghi đúng nguyên nhân nếu trang vẫn lỗi.
- Bản `2.4.134` hủy timer phục hồi YouTube đã xếp trước đó khi Donut trả lỗi quyền fingerprint không thể tự phục hồi; callback cũ cũng dừng, tránh thử mở profile lặp lại. Checkpoint được giữ và người dùng vẫn có thể chủ động thử lại sau khi xử lý quyền. Không thay đổi fingerprint, profile, lịch Public hoặc dữ liệu workspace.
- Bản `2.4.133` sửa checkview phân trang YouTube Studio: Donut Bridge `1.1.39` xác nhận cùng một trang mới ổn định hai lần sau khi bấm Next; không bấm Next lại nếu Wayfern trả phản hồi rỗng. Tool chỉ thử lại thao tác đọc snapshot, đối chiếu trang hiện tại trước khi tiếp tục và dừng an toàn nếu chuyển trang chưa xác nhận. Lỗi Wayfern từ chối fingerprint đã được DocuRandom rút gọn nay được nhận diện là lỗi dừng, lưu cờ chặn để không lặp cảnh báo/upload tự động. Không thay đổi fingerprint, profile, checkpoint, Flow hay lịch Public.
- Bản `2.4.132` khóa lệnh mở Donut xuyên tiến trình và chỉ công nhận profile khi PID còn sống cùng trang debug phản hồi. Chat Bridge `1.0.70` giữ tệp tạm/checkpoint theo đúng hội thoại khi lần chọn tệp chưa rõ kết quả, xác minh file card trước khi gửi, không tự tạo chat mới hoặc gửi lại tệp mơ hồ. Bản cài mới cần nghiệm thu trên chat chẩn đoán và profile thử trước khi mở lịch sản xuất.
- Bản `2.4.131` chống lặp lệnh mở profile Donut sau timeout bằng sổ trạng thái bền vững theo profile: khi kết quả chưa rõ, mọi lần Continue chỉ thăm dò trạng thái/debug endpoint; không gửi lệnh mở mới nếu chưa xác nhận. Lỗi quyền/gói được phân loại terminal. Chat Bridge `1.0.69` giới hạn một DOM probe chưa kết thúc trên mỗi renderer, gỡ chặn khi renderer điều hướng sang tài liệu mới, tuần tự hóa import tệp và dừng khi lần chọn file đã phát đi nhưng chưa xác nhận card; tuyệt đối không reload/retry tệp trong trạng thái đó. Inline fallback chỉ dùng khi biết file chưa được chọn hoặc ChatGPT xác nhận từ chối file trùng, trong đúng hội thoại và trước khi gửi prompt.

- Bản `2.4.130` sửa đường nhập audit lớn vào ChatGPT Web: nhập ưu tiên qua đúng `WebContents` của conversation thay vì phụ thuộc focus toàn cửa sổ; chỉ chuyển sang CDP sau khi đọc xác nhận composer chưa đổi. Xác minh từng đoạn và từ chối Send khi composer biến mất/thay đổi; lỗi ghi rõ URL/trạng thái DOM, checkpoint và lượt ChatGPT không bị gửi lại. Chat Bridge `1.0.68`; không sửa profile, cookie hay dữ liệu workspace.

- Bản `2.4.129` tuần tự hóa riêng thao tác focus/nhập/gửi của các cửa sổ ChatGPT Bridge chạy nền để tránh nhiều workspace giành focus cùng lúc khiến composer không nhận ký tự (`thực tế 0`). Khóa được nhả ngay khi xác nhận lượt đã gửi; phần ChatGPT suy nghĩ/trả lời vẫn chạy song song theo cấu hình. Chat Bridge `1.0.67`; checkpoint VOICEVOX lỗi trước đó tiếp tục được giữ và không tự gửi lại prompt.
- Bản `2.4.128` sửa luồng audit VOICEVOX trên ChatGPT Web: manifest gửi audit bỏ metadata engine/timing âm vị bị lặp nhưng giữ nguyên nội dung, kana, thứ tự mora/cao độ, accent, pause, speaker/style, các tham số audit được phép và digest đối chiếu. Nếu ChatGPT không nhận file sau phục hồi, Bridge `1.0.66` chỉ với stage audit VOICEVOX sẽ nạp JSON đã xác thực vào đúng conversation trước khi gửi một lượt; không tạo chat mới, không gửi trùng và không đụng vào `.vvproj` gốc. Kiểm thử giới hạn schema, dung lượng, conversation và thời điểm trước khi gửi.

- Bản `2.4.127` sửa xác minh file ChatGPT: nếu không có đúng một composer đang hiển thị, bridge không còn suy luận `ready` từ nút gửi ở nơi khác trên trang; trạng thái chọn file trong input không bị coi là file đã gắn. Mọi bằng chứng đính kèm/nút gửi được giới hạn trong composer hiện hành. Thêm kiểm thử hồi quy cho lỗi `selectedFiles` có tên nhưng `visibleComposers=0`; Chat Bridge `1.0.65`. Checkpoint không bị xóa và không tự gửi lại prompt.

- Bản `2.4.126` làm rõ chẩn đoán Flow: `flow_tabs` là tổng trên các client/profile đang nối bridge, kèm số client thực sự có tab Flow; không còn khiến mỗi workspace trông như sở hữu toàn bộ tab của mọi profile. Gói cài im lặng không tự mở DocuRandom sau khi thay runtime, để không tự kích hoạt checkpoint cần người dùng xác minh. Gói cài kèm Flow Agent `1.3.45` và Donut Bridge `1.1.38`; checkpoint upload `uncertain` vẫn được giữ nguyên, không upload lại trong lúc cài.

- Bản `2.4.125` sửa an toàn upload YouTube: mọi video render có checkpoint `complete` phải chờ đủ 5 phút từ mốc hoàn tất/file được ghi gần nhất, rồi xác nhận file cuối không rỗng và ổn định trước khi băm hoặc gửi. Cấm mọi biến thể tên `*.part.mp4`; nếu checkpoint upload cũ còn `uncertain` trên file `.part` cùng video, khóa lượt upload mới để không đăng trùng. Nếu thời gian chờ băng qua mốc 15:00, upload tự chuyển sang Unlisted như quy tắc lịch hiện hành. Chờ có thể hủy và sẽ tính tiếp từ checkpoint sau khi mở lại.

- Bản `2.4.121` sửa phục hồi Flow khi kênh phản hồi MV3 đóng giữa chừng: extension hỏi xác nhận hủy driver trên đúng tab trước khi cho phép retry; chỉ retry khi driver xác nhận prompt chưa từng được chạm tới. Checkpoint cũ tự khôi phục `flow_project_id`/client ID nếu cùng một identity đã được ghi trong lỗi durable; project legacy vẫn chỉ tự ghép khi thấy đúng prompt trong lịch sử Flow.
- Bản `2.4.120` sửa quy tắc chốt lịch Public: **15:00 là giờ dừng chuyển Công khai tự động**. Nếu scheduler thức dậy trễ sau mốc 13:30, nó không còn catch-up và bật Public ngay; video đã upload Unlisted vẫn giữ nguyên hàng đợi FIFO để xử lý ở ca kế tiếp. Sau 15:00, queue Public không chặn lane mới: video render xong tiếp tục được upload **Không công khai**, lưu checkpoint/archive và render video tiếp theo. Thêm guard ở scheduler, staging và worker promotion cùng test hồi quy cho tình huống 15:48.

- Bản `2.4.119` hoàn thiện sổ lịch sử chống trùng YouTube. Sau upload đã xác nhận, tiêu đề được ghi bằng đường dẫn package đã đóng gói thay vì đường dẫn `Output` cũ đã bị chuyển đi; registry upload cũng giữ bản sao tiêu đề nhỏ, độc lập với archive video. Khi khởi động, tool đối soát các phiên `complete`/`pending_public` đã có URL trong `uploaded_sessions.json`, đọc package/checkpoint/queue theo thứ tự an toàn và bổ sung idempotent vào `content_history.json`/`CHECKLIST_NOI_DUNG_DA_LAM.txt`. Xóa archive để giải phóng dung lượng không làm mất lịch sử; phiên chưa xác nhận vẫn không được ghi.

- Bản `2.4.118` sửa khôi phục ChatGPT sau khi bridge khởi động lại làm conversation cũ không còn trong bộ nhớ cục bộ. Nếu checkpoint chứng minh chính xác lượt Master Prompt sửa tệp đã được lưu trước khi gửi và chưa có phản hồi, tool tạo conversation sạch và gửi lại đúng một lần; không gọi lại conversation cũ, không gửi lại lượt không chắc chắn và không thay đổi dữ liệu/profile workspace. Nếu xảy ra sự cố giữa lúc chuyển conversation, checkpoint có cờ an toàn và dừng fail-closed để tránh tạo trùng.

- Bản `2.4.97` chuyển lịch YouTube sang hàng đợi FIFO theo ca Public: 04:30/08:30/13:30 mỗi ca chỉ chuyển đúng một video sang Công khai; video kế tiếp chỉ upload Không công khai sau khi ca trước đã xác nhận Public và upload xong mới bắt đầu render tiếp. Queue được promotion ở cả ba mốc, không còn chỉ xử lý buổi sáng. Thêm vùng an toàn 45 phút trước mốc Public kế tiếp; video chưa kịp hoàn tất giữ nguyên checkpoint, không upload dồn hoặc tự tạo video kế tiếp. Trạng thái liên tục được lưu qua restart, không thay đổi cơ chế chống đăng trùng/profile.

- Bản `2.4.96` khôi phục nguyên tắc **mỗi ca YouTube chỉ một video**. Video render xong trước 15:00 chỉ được ghi `ready` và chờ đúng mốc upload kế tiếp; không còn upload ngay lúc render xong nên không xảy ra hai video dồn trong một ca. Nếu render hoàn tất sau khi mốc đã trôi qua, checkpoint được chuyển sang ca kế tiếp. Ngoại lệ ca tối sau 15:00 vẫn cho phép upload ngay ở chế độ **Không công khai** để ca sáng chuyển đúng video sang **Công khai**. Không thay đổi chống đăng trùng, checkpoint hoặc dữ liệu profile.

- Bản `2.4.95` khôi phục upload YouTube theo sự kiện: video vừa render xong và đủ gói dữ liệu sẽ được upload ngay, không chờ mốc upload kế tiếp. Trước 15:00 giữ quyền riêng tư đã chọn; từ 15:00 trở đi tool upload **Không công khai**, giữ đúng video trong hàng đợi và ca sáng chỉ chuyển video đó sang **Công khai**. Upload đang chạy không bị cắt, không gắn file lần hai; checkpoint, hàng đợi và chống đăng trùng vẫn được giữ qua restart.

- Bản `2.4.93` gộp Flow, API 1MM và kho dữ liệu vào một thẻ hạ tầng dùng chung compact. Bố cục responsive mới giảm chiều cao phần đầu, giữ bộ chọn tài nguyên và bảng hàng chờ điều phối hiển thị trong cùng cửa sổ; không thay đổi dữ liệu workspace hoặc logic chạy.

- Bản `2.4.90` thêm cấu hình **Kho workspace dùng chung** ở khu vực hệ thống, tách khỏi phần chọn workspace. Có thể chọn một thư mục mới để chuyển toàn bộ Media, Output, Prompts, VOICEVOX và checkpoint; tool sao chép/kiểm tra trước, cập nhật các đường dẫn state rồi mới đăng ký kho mới, đưa dữ liệu cũ vào Thùng rác Windows sau khi thành công. Workspace mới luôn dùng kho đã chọn và cấu hình được giữ qua restart.
- Bản `2.4.89` gom các lượt ChatGPT của **một video** vào đúng một conversation: Master Prompt tạo thread mới, thumbnail và audit VOICEVOX nối lại bằng cùng `thread_id`; video kế tiếp mới tạo conversation mới. Thumbnail và audit được xếp tuần tự để không gửi hai lượt đồng thời trên cùng thread. Checkpoint lưu cả thread/response identity, retry/restart không mở chat mới và không gửi trùng. Prompt của từng stage có hợp đồng output riêng (ảnh, xác nhận chính xác hoặc JSON audit), phần giải thích ngoài artifact không được commit vào workflow.

- Bản `2.4.87` sửa mất kết nối tạm thời với Flow Agent: lỗi 503 trước khi prompt được gửi không còn bị giới hạn ở ba lần thử ngắn rồi dừng batch. Tool giữ nguyên đúng prompt hiện tại, chờ extension/Edge kết nối lại tối đa 180 giây và chỉ chuyển về checkpoint khi hết thời gian; không mở tab/project mới, không gửi lại các ảnh đã hoàn tất. Thêm test hồi quy cho reconnect chậm và timeout an toàn.

- Bản `2.4.78` sửa lỗi sau thao tác Gửi ChatGPT: nếu trang còn ở shell `chatgpt.com/` thì bridge không được reload shell hoặc tạo cuộc hội thoại mới; chỉ phục hồi khi đã có URL `/c/...` chính xác. Bridge `1.0.54` bổ sung test hồi quy cho trạng thái gửi chưa xác nhận và giữ checkpoint chống gửi trùng. Không thay đổi profile, cookie, proxy hoặc dữ liệu workspace.
- Bản `2.4.77` sửa lỗi phục hồi Master Prompt: khi đính kèm tệp thất bại, bridge không còn điều hướng về trang ChatGPT mới (`chatgpt.com/`) làm mất cuộc hội thoại của workspace. Tool tải lại đúng URL `/c/...` hiện tại, giữ nguyên `thread_id` và thêm test chống hồi quy. Bridge `1.0.53` cũng giữ renderer conversation ở trạng thái page-visible nhưng trong suốt/click-through, để ChatGPT tạo DOM phản hồi mà không bật cửa sổ lên màn hình. Không thay đổi profile, cookie, proxy hoặc dữ liệu workspace.

- Bản `2.4.76` sửa watchdog khởi động ChatGPT Web: probe bridge không còn chặn quá lâu trên cổng loopback bị lọc, nên workspace nhận diện được bridge/CLI thực sự bị treo thay vì bị giữ vô hạn. Không đặt giới hạn thời gian lên lúc ChatGPT đang suy nghĩ.

- Bản `2.4.74` sửa lỗi bridge giữ lượt ChatGPT vô hạn khi giao diện đã hoàn tất nhưng kênh đọc output không còn phát dữ liệu. Watchdog chỉ coi timestamp/callback mới là heartbeat; trạng thái `active_turns` đứng yên không còn được gia hạn giả. Bridge tự nhả lượt nếu renderer/bridge mất heartbeat thực sự, còn câu trả lời chậm nhưng vẫn phát trạng thái sẽ tiếp tục được chờ, không gửi lại prompt. Chat Bridge `1.0.51`.

- Bản `2.4.74` phục hồi luồng đọc ChatGPT của bản cũ: checkpoint được cập nhật lại sau khi lượt mới đã được xác nhận để giữ đúng URL `/c/...`, renderer ẩn dùng fallback DOM theo assistant-role của bản cũ, và checkpoint cũ thiếu URL vẫn được đọc lại mà không gửi lại prompt.

- Bản `2.4.58` sửa deadlock làm DocuRandom chuyển sang `Not responding` khi Donut mở nhiều profile: hàng đợi ChatGPT không còn gọi callback khi đang giữ khóa dùng chung; log từ worker được đưa về Tk qua event queue. Không đổi profile, proxy, cookie hay dữ liệu workspace.
- Bản `2.4.57` sửa treo giao diện khi nhiều workspace chạy đồng thời: dùng một bộ xử lý sự kiện Tk dùng chung, chia lượt công bằng và gộp các cập nhật tiến độ trung gian. Sự kiện hoàn tất/lỗi/checkpoint vẫn giữ FIFO; không thay đổi profile, proxy, cookie hay dữ liệu workspace.
- Bản `2.4.56` sửa nhánh tự thu hồi phản hồi ChatGPT Web khi socket stream bị ngắt: khởi tạo đúng cổng Bridge loopback để không rơi vào `AttributeError` và giữ nguyên checkpoint. Giữ nguyên watchdog heartbeat và hàng đợi của bản `2.4.55`; Bridge `1.0.39`.
- Bản `2.4.55` sửa workflow ChatGPT bị giữ slot vô hạn: Bridge phát heartbeat liveness, hủy đúng `thread_id/turn_id` khi heartbeat mất, giữ checkpoint và không gửi lại prompt. Workspace đang chờ được ghi rõ là `queued`; phản hồi ChatGPT chậm nhưng Bridge còn heartbeat vẫn được chờ nguyên vẹn. Bridge `1.0.39`.
- Bản `2.4.53` hoàn tất luồng chờ output ChatGPT theo bằng chứng thực: sau Send, checkpoint được lưu trước thao tác trusted input; nếu DOM vẫn trả lời thì chờ vô hạn, không dùng mốc 20 giây để reload. Nhãn `Thinking/Đang suy nghĩ` và speaker label không còn bị đọc nhầm là output; chỉ phục hồi khi probe renderer lỗi liên tiếp hoặc ChatGPT báo lỗi mạng. Bridge `1.0.38`, app `2.4.53`.
- Bản `2.4.52` sửa cơ chế đọc output ChatGPT Web theo trạng thái thực: không dùng thời gian cố định để kết luận lượt trả lời bị mất, không tự reload khi ChatGPT chỉ đang suy nghĩ lâu, và không gửi lại prompt. Tool chờ assistant/completion evidence trong DOM; chỉ phục hồi đúng cuộc hội thoại khi renderer/probe hoặc lỗi mạng có bằng chứng hỏng. Cửa sổ ChatGPT vẫn renderer-visible nhưng trong suốt, không chiếm màn hình.
- Bản `2.4.51` sửa lỗi ChatGPT Web phản hồi chậm sau khi đã nhận prompt: không còn kết luận thiếu DOM ở mốc 60 giây trong khi output vẫn đang được ChatGPT hoàn tất. Bộ đọc chờ tối đa 180 giây cho assistant DOM xuất hiện, sau đó chỉ phục hồi đúng cuộc hội thoại một lần và không gửi lại prompt; thêm test xác nhận output xuất hiện trễ vẫn được đọc đủ.
- Bản `2.4.50` sửa lỗi ChatGPT Web bị giữ ở `DOM complete/hidden`: các cửa sổ hội thoại được giữ renderer-visible nhưng trong suốt, bỏ taskbar và click-through; không di chuyển cửa sổ ra tọa độ âm, không chiếm màn hình người dùng, và có smoke test Electron xác nhận `document.visibilityState=visible` trước khi đọc/nhập/gửi.
- Bản `2.4.49` sửa lỗi ChatGPT Web bị báo hết phiên hoặc không đọc được giao diện giả khi nhiều workspace chạy cùng lúc: Bridge giữ trạng thái đăng nhập đã xác nhận qua các lần hydrate/redirect/probe timeout tạm thời, chỉ hạ trạng thái khi DOM có dấu hiệu đăng xuất rõ ràng, và chờ renderer sẵn sàng tối đa 30 giây trước khi trả 401. Không reload định kỳ và không gửi lại prompt.
- Bản `2.4.48` sửa lỗi nhận diện sai conversation mới của ChatGPT: URL home tạm thời `https://chatgpt.com/` không còn bị coi là một conversation khác trước khi SPA commit `/c/<id>`. Bridge giữ nguyên URL `/c/<id>` đã biết, ghi `thread_id` vào checkpoint ngay cả khi lượt đọc DOM thất bại, và coi lỗi lệch conversation là lỗi dừng để không tự tạo thêm lượt mới. Giữ nguyên watchdog `2.4.47`: không reload định kỳ sau 10 phút chỉ vì câu trả lời chậm hoặc đứng chữ; chỉ phục hồi theo tín hiệu renderer/DOM/mạng và không gửi lại prompt đã có khả năng được nhận.
- Bản `2.4.47` sửa watchdog ChatGPT Web theo tín hiệu thực tế: **không còn reload định kỳ sau 10 phút chỉ vì câu trả lời chậm hoặc đứng chữ**. Khi renderer vẫn trả probe DOM, đúng conversation URL, còn cờ đang sinh/đang chờ hoặc không có lỗi mạng, tool tiếp tục chờ; mốc 60 phút chỉ là trần an toàn để giữ checkpoint, không tự reload hay gửi lại. Nếu probe DOM thất bại liên tiếp, DOM trả lỗi, rời sai conversation hoặc báo lỗi mạng, tool reload đúng URL tối đa một lần rồi đọc lại. Nếu lỗi xảy ra quanh nút Send, tool chỉ reload và xác minh lượt đã nhận, không bấm/gửi lại prompt đã có khả năng đi tới ChatGPT. Trạng thái log hiển thị rõ `accepted-waiting`, `generating`, `awaiting-completion-signal`, `completion-candidate`, `error` và trạng thái DOM/visibility.
- Bản `2.4.46` sửa lượt ChatGPT Web bị treo vô hạn: Bridge có watchdog bất biến 45 phút, cảnh báo không tiến triển sau 10 phút và chỉ reload đúng cuộc hội thoại một lần, không gửi lại prompt. Thay đổi HTML phụ như hydration không còn được tính là tiến triển giả. Scheduler không tự lặp cùng checkpoint mỗi 15 phút sau khi quá hạn; checkpoint vẫn giữ nguyên để tiếp tục có kiểm soát.
- Bản `2.4.45` sửa nhận diện hoàn tất phản hồi ChatGPT: không còn phụ thuộc duy nhất vào nút Copy, bổ sung kiểm tra trạng thái dừng/streaming/lỗi và đọc lại renderer ở cửa sổ off-screen không focus. Khi cần phục hồi, tool đối chiếu đúng assistant turn và nội dung sau một lần reload mà không gửi lại prompt.
- Bản `2.4.44` sửa false-disconnect của Flow khi nhiều workspace cùng kiểm tra backend: probe cửa sổ Windows chạy nền có cache, không khóa `/health`; health timeout trên socket đang lắng nghe được giữ là backend còn sống để không spawn bản trùng. Luồng mở Flow dùng một hàng đợi dùng chung, chờ extension reconnect rồi tái sử dụng tab hiện tại; không mở thêm cửa sổ, không tạo project hoặc gửi prompt trùng. Checkpoint render/upload vẫn được giữ nguyên qua restart.
- Bản `2.4.43` gom trạng thái/khởi động Flow về launcher dùng chung, sửa false-timeout khi backend đã sống nhưng `/health` phản hồi chậm, và đưa API key 1MM về một ô cấu hình toàn tool; target, gói, số lượng và mã đơn vẫn giữ riêng từng workspace.
- Bản `2.4.41` cập nhật đường đọc checkview YouTube qua extension `1.1.33`: đọc toàn bộ trang bằng DOM ổn định, tự thử lại lần đọc rỗng sau điều hướng, giữ tab để tái sử dụng và chỉ báo cáo video khi đã xác minh đủ dữ liệu; Flow Agent đi kèm là `1.3.39` và không tự mở thêm cửa sổ trong luồng thường.
- Bản `2.4.40` sửa hàng đợi checkview YouTube: nhiều workspace thức cùng lúc nay vào FIFO công bằng với tối đa 2 lượt đọc nền, không còn để một profile vừa xong giành lại khóa và làm DaiTa3 hoặc profile khác bị đói lượt. Vị trí chờ, hủy và giải phóng lane được xử lý an toàn; tab checkview vẫn được giữ lại để tái sử dụng. Hoàn tất scan được xác nhận bằng toàn bộ danh sách video + kết quả 60 phút, không dựa vào việc tab đóng.
- Bản `2.4.39` sửa timeout khôi phục project Flow: handler không còn cắt ở 60 giây trong khi extension có thể phải thử nhiều route của cùng project. Thêm deadline động đủ cho toàn bộ route, timeout riêng cho probe tab và phân loại đúng lỗi `workspace-flow-tab`; prompt vẫn giữ trong checkpoint, không tạo project mới hoặc gửi trùng.
- Bản `2.4.32` sửa quy tắc ca YouTube: mỗi profile chỉ nhận **một video ở mỗi mốc upload**. Nếu render chưa xong khi mốc đến, tool giữ nguyên render/checkpoint và chuyển video sang đúng mốc kế tiếp; khi render xong sớm, video cũng chỉ chờ đến ca đã định, không upload chen ngay trong ca hiện tại. Chỉ sau khi lượt upload của ca được YouTube xác nhận đầy đủ metadata thì workspace mới mở ca sản xuất tiếp theo.
- Bản `2.4.31` tăng tốc hàng YouTube an toàn: tối đa **3 lượt truyền tệp đồng thời** khi mỗi lượt dùng một profile Donut khác nhau, khởi động lệch nhau 5 giây. Cùng profile vẫn giữ FIFO, check-view tự nhường chỗ và pha nhập/xác minh metadata vẫn khóa DOM riêng để không tranh điều khiển. Vì vậy 11 video không còn bị dàn thành một hàng duy nhất; giới hạn 3 vẫn bảo vệ proxy, RAM và các tác vụ Minecraft khỏi việc mở ồ ạt 20 wizard.
- Bản `2.4.30` tách rõ phần **sửa metadata** khỏi phần **phát hành công khai** của lịch YouTube. Nếu lượt upload/khôi phục đã qua ca chiều cuối ngày (14:45/15:00 hoặc mốc chiều cuối do người dùng cấu hình), tool vẫn nối đúng video và điền/xác minh đủ title, mô tả, SEO, thumbnail, đối tượng và AI nhưng ghi trạng thái `deferred_public`, đặt video **Không công khai** và giữ nguyên checkpoint thay vì lặp lỗi bộ chọn visibility. Ca sáng kế tiếp sẽ nhận đúng checkpoint đó, không gắn file lần hai, chuyển sang **Công khai**, rồi mới lưu kho và cho workflow/render tiếp theo chạy. Trạng thái này được giữ qua đóng/mở tool và hiển thị rõ trên lịch/Telegram.
- Bản `2.4.29` căn lại trần chờ truyền tệp YouTube theo lịch ca cách nhau 5 giờ: trần cứng là 4 giờ 30 phút, chừa 30 phút để lưu checkpoint và phục hồi trước ca kế tiếp. Đây không phải thời gian chờ cố định; tệp hoàn tất sẽ đi tiếp ngay, còn trạng thái không tiến triển vẫn cảnh báo sau 10 phút và tự nhả lượt sau 30 phút.
- Bản `2.4.28` bổ sung watchdog cho upload YouTube: nếu phần trăm truyền tệp đứng yên 10 phút, tool báo rõ trạng thái cho người dùng/Telegram; nếu không có tiến triển trong 30 phút, tool giữ nguyên checkpoint và tự thử lại đúng tab, không chiếm làn upload vô thời hạn. Upload còn có tiến triển vẫn được phép hoàn tất bình thường, không bị cắt sớm chỉ vì mốc ca kế tiếp.
- Nếu đóng tool trong lúc upload đang ở `upload`/`youtube-retry`, lần mở sau không chuyển phiên sang `Skipped` chỉ vì đã qua giờ. Tool đưa đồng hồ ca sang lần kế tiếp, giữ đúng video/profile/attempt và tự nối lại; nếu đã lỡ mốc, user nhận thông báo riêng. Không upload lại file và không bỏ qua ca cuối ngày âm thầm.

- Bản `2.4.27` sửa lỗi hàng YouTube bị lan trạng thái **đang tự khôi phục đúng bản nháp** giữa nhiều workspace. Khi checkpoint đã có video ID nhưng chưa xác nhận `transfer_complete`, tool không còn đọc đúng tab cũ đúng một lần rồi nhả làn upload; nó giữ nguyên làn/profile và chờ cùng mốc truyền 100% ổn định như lượt upload mới. Bridge-only vẫn theo dõi được phần trăm mà không cần raw CDP, có thể hủy an toàn, và checkpoint ghi rõ `transfer_complete=false`. Sau khi tệp hoàn tất, tool mới sửa/xác minh metadata và cho workspace kế tiếp chạy; tuyệt đối không gắn file lần hai.

- Bản `2.4.26` ngăn cùng một workspace khởi động hoặc tiếp tục nhiều lượt render đồng thời. Checkpoint render được khóa theo workspace, còn các workspace khác vẫn chạy song song; lỗi clip hỏng, sai số frame và ghi đè checkpoint do render trùng được loại bỏ.
- Bản `2.4.25` đổi lịch đăng mặc định thành `04:30`, `08:30` và `13:30` (giờ Việt Nam). Mỗi workspace cũ sẽ được di trú một lần sang đúng ba mốc này; ID ca, trạng thái bật/tắt và checkpoint đang chạy được giữ lại để không mất tiến độ.
- Bản `2.4.24` sửa bộ hẹn giờ khi mốc upload đến trong lúc video vẫn đang render: các ca thường không bị hủy hoặc chuyển vào `Skipped`; checkpoint được giữ và upload ngay sau khi render hoàn tất. Riêng mốc upload cuối ngày, video chưa xong sẽ được giữ nguyên để upload ở ca sáng hôm sau, kể cả khi tool được khởi động lại.
- Scheduler không còn dừng tự phục hồi trước giờ upload 15 phút. Timer YouTube cũ bị hủy/đối chiếu theo identity checkpoint nên không thể gọi nhầm phiên trước sau khi workspace đã bắt đầu video mới. Hàng ChatGPT vẫn mặc định xếp từng workspace; giới hạn render và số phiên ChatGPT là hai hàng độc lập.

- Bản `2.4.23` sửa gói Donut Local Developer: khi có quyền automation hợp lệ, Wayfern nhận token qua cả tham số khởi chạy và biến môi trường, tránh mất quyền điều khiển CDP sau khi cập nhật browser. Không thay đổi dữ liệu profile hay chèn token giả.

- Bản `2.4.22` giới hạn toàn tool tối đa **2 upload YouTube** và **2 lượt kiểm tra view**; workspace còn lại tự hoãn nên 20 profile không mở dồn 20–40 cửa sổ Studio, giảm nghẽn proxy/mạng và RAM. Upload vẫn ưu tiên, làm theo FIFO, lệch thời điểm bắt đầu 30 giây và giữ khóa riêng từng profile.
- Nếu YouTube/Donut/proxy lỗi trước khi video được gắn (`ERR_PROXY_CONNECTION_FAILED`, `ERR_TUNNEL_CONNECTION_FAILED`, timeout điều hướng hoặc mất MCP/CDP), tool xác minh chưa có mã lượt, đóng target lỗi rồi tự mở target mới và thử lại có giới hạn; nếu proxy vẫn hỏng, checkpoint tự lên lịch nối lại 2–15 phút thay vì dừng hẳn. Sau khi file đã gắn, cơ chế chống trùng vẫn tuyệt đối: chỉ nối lại đúng tab/video/bản nháp, không bao giờ gắn file lần hai. Một cảnh báo phục hồi giống nhau chỉ gửi Telegram tối đa một lần mỗi 30 phút thay vì lặp ở mọi nhịp.
- Checkpoint kiểu DaiTa16 đã gắn file nhưng mất cả target nay được dò lại trong trang Nội dung của đúng profile. Tool chỉ xét 10 video mới nhất trong cửa sổ 72 giờ, bắt buộc đúng một ứng viên khớp tên file/tiêu đề và thời lượng video do FFprobe xác minh, đồng thời loại trạng thái đang tải hoặc upload thất bại. Khi đủ bằng chứng, video ID được ghi bền vững vào checkpoint rồi tool mở đúng trang Edit để điền/xác minh metadata; hai ứng viên giống nhau hoặc thiếu thời lượng đều bị từ chối, không chọn bừa video gần nhất.
- ChatGPT Web Bridge `1.0.29` sửa trường hợp ô Lexical tự nối thêm vài ký tự của bản nháp cũ sau prompt (ví dụ cần 79 nhưng thực tế 92 và 79 ký tự đầu đúng). Trước khi gửi, bridge phát hiện đúng mẫu này, xóa sạch rồi nạp lại đúng một lần và vẫn bắt buộc đối chiếu tuyệt đối toàn bộ prompt; nội dung thiếu hoặc bị đổi vẫn dừng an toàn.

- Bản `2.4.21` tối ưu cho 20 workspace chạy cùng máy với Minecraft modded 16 GB: số workspace chỉ làm tăng hàng đợi, không nhân số tiến trình nặng. Toàn tool giữ tối đa 2 render, tối đa 6 luồng FFmpeg mỗi render, tối đa 2 yêu cầu VOICEVOX và 4 luồng engine; dành ít nhất một phần ba RAM cùng một phần ba CPU logic cho Minecraft/Windows. FFmpeg và VOICEVOX chạy ở mức ưu tiên Windows thấp hơn bình thường nên server/game/UI được cấp CPU trước. Độ phân giải, FPS, CRF/CQ, preset encoder, nội dung và chất lượng giọng không bị hạ.
- Hàng YouTube cho phép tối đa 2 upload thực sự đồng thời khi thuộc hai profile Donut khác nhau, nhưng thời điểm mở wizard lệch nhau 30 giây. Mỗi profile có khóa riêng nên upload/check-view không đè nhau; video cùng profile vẫn tuần tự. Với 20 tác vụ, hai vị trí upload luân phiên nhận tác vụ kế tiếp; một lượt đang phục hồi giữ đúng vị trí/profile của nó nhưng không chặn vị trí còn lại.

- Bản `2.4.20` giữ toàn bộ upload YouTube của mọi workspace/profile trong một hàng FIFO duy nhất và thêm 30 giây nghỉ ổn định sau khi mỗi lượt nhả khóa. Video kế tiếp chỉ bắt đầu sau khi lượt trước đã tải xong, lưu/xác minh đủ metadata, đóng target và hết khoảng nghỉ; vì vậy nhiều workspace vẫn chạy liên tục theo thứ tự nhưng không tạo hai phiên truyền tệp hoặc hai wizard YouTube chồng nhau. Trong lúc tự nối lại Donut/CDP hoặc phục hồi đúng bản nháp, lượt hiện tại vẫn giữ làn upload nên workspace sau không thể chen vào.

- Bản `2.4.19` biến lỗi giữa bước upload/điền metadata YouTube thành trạng thái khôi phục tự động. Ngay trước khi gắn file, tool lưu một mã lượt duy nhất cùng target/tab, title, mô tả, SEO tags, thumbnail, đối tượng, khai báo AI và quyền riêng tư. Nếu YouTube/Donut ngắt ở bất kỳ bước nào, tool nối lại đúng tab hoặc đúng video ID, không gắn lại file; khi đã có bản nháp, tool mở `/video/<id>/edit`, điền mọi trường còn thiếu, Save rồi tải lại để đối chiếu. Có link video, đóng `Saving` hoặc nhìn thấy video trong Content không còn đủ để báo thành công; phải xác nhận lại title, mô tả, SEO, thumbnail, Không dành cho trẻ em, nội dung AI và đúng Public/Unlisted/Private.
- Bộ điều khiển upload không còn giả định luôn phải bấm Next đúng ba lần. Nó nhận diện bước YouTube hiện đang mở, chờ tối đa năm phút cho phần đối tượng/quyền riêng tư trên proxy chậm và giới hạn số lệnh để không bấm dồn. Mất Donut MCP/Bridge tạm thời được thử nối lại ngay; nếu vẫn chưa sẵn sàng, workspace tự lên lịch phục hồi checkpoint sau 2–15 phút, kể cả sau khi mở lại tool. Ca/video kế tiếp bị khóa cho tới khi ca trước thực sự đạt kiểm tra đầy đủ.
- Chuỗi lịch mặc định chuyển thành **làm 04:30 → đăng 10:00; làm 10:00 → đăng 13:00; làm 13:00 → đăng 04:30 hôm sau**. Tại 04:30, 10:00 và 13:00, upload ca trước luôn được chạy trước; workflow kế tiếp chỉ bắt đầu ngay sau khi upload đó báo thành công, không bắt đầu khi video mới chỉ được tải lên nhưng metadata còn thiếu. Lịch mặc định v1/v2 được nâng tại chỗ và giữ nguyên trạng thái/claim đang chạy; lịch người dùng tự chỉnh không bị ghi đè.

- Bản `2.4.18` chuyển toàn bộ upload YouTube sang một hàng đợi FIFO dùng chung cho mọi workspace và mọi profile. Mỗi lúc chỉ một video được phép thực hiện trọn quy trình truyền tệp, chờ 100%, ghi/xác minh title, mô tả, SEO tags, thumbnail, audience, nội dung AI, quyền riêng tư và đóng tab; lượt kế tiếp chỉ bắt đầu sau khi lượt trước đã giải phóng hoàn toàn. Trạng thái workspace đang chờ hiển thị đúng vị trí trong hàng, hủy khi chờ không làm ảnh hưởng lượt đang chạy. Check view trên mọi profile tự hoãn trong lúc có upload đang chạy hoặc đang xếp hàng; upload mới sẽ yêu cầu các lượt check view đang hoạt động dừng tại checkpoint an toàn trước khi chiếm trình duyệt. Cơ chế chống đăng trùng và checkpoint cũ được giữ nguyên.

- Bản `2.4.17` sửa dứt điểm lỗi VOICEVOX `ACCENT_NOTFOUND` do ChatGPT đề xuất `correctedKana` không đúng cú pháp AquesTalk. Tool nhận diện riêng toàn bộ nhóm lỗi kana HTTP 400 của VOICEVOX; chỉ pronunciation patch lỗi của đúng box bị loại, còn nhân vật, style, preset, override và nội dung vẫn được giữ rồi render tiếp bằng cách đọc gốc an toàn. Prompt audit nay buộc sửa tối thiểu từ `currentQuery.kana` và giải thích rõ dấu accent. Bản này đồng thời thêm hậu kiểm tiếng Nhật có chọn lọc bằng Faster Whisper `small`: ưu tiên box từng sửa phát âm và câu có tên/số/thuật ngữ, tối đa 12 câu, chạy local bằng CPU INT8, một worker và tối đa 6 luồng để không tranh GPU render/Minecraft. Model được tải một lần vào Data; lỗi tải/ASR chỉ tạo báo cáo cảnh báo, tuyệt đối không làm hỏng WAV hay tự đổi lời.

- Bản `2.4.16` sửa vòng lặp **FLOW_UI_SUBMISSION_UNCERTAIN: phase=no-progress** khi Edge/Flow chạy nền hoặc bị thu nhỏ. Nếu driver chưa phát ra bất kỳ giai đoạn nào trước khi hết thời gian, tool xác định chắc chắn prompt chưa được chạm tới, phục hồi ngay trên đúng tab hiện tại rồi tự gửi lại đúng prompt đang dở; ảnh đã hoàn tất trong checkpoint không bị tạo lại.
- Checkpoint cũ do Flow Agent `1.3.23` khóa nhầm ở đúng trạng thái `phase=no-progress` được tự mở khi bấm **Tiếp tục**. Các trường hợp đã bắt đầu nhập/gửi vẫn tiếp tục bị khóa an toàn để không tạo ảnh trùng. Flow Agent nâng lên `1.3.24`.
- Bộ theo dõi trạng thái Flow tạm nghỉ trong lúc một lượt tạo ảnh đang chiếm backend, tránh các lần kiểm tra hai giây cạnh tranh với tác vụ nền và làm trạng thái Flow nhấp nháy mất kết nối. Hàng đợi chung vẫn bảo đảm mỗi lúc chỉ một workspace và một prompt thao tác với Flow.

- Bản `2.4.15` sửa dứt điểm hộp thoại **Phản hồi cập nhật lớn bất thường**. Updater không còn tải một lượt tới 100 Release (JSON hiện đã vượt 2 MB); thay vào đó đọc danh sách tag gọn, chọn phiên bản DocuRandom cao nhất rồi chỉ tải đúng metadata của Release đó. Kênh vẫn công khai, không yêu cầu đăng nhập GitHub và chữ ký Ed25519/SHA-256 của gói cập nhật vẫn được xác minh như cũ.
- Telegram Bot nay tự di chuyển cấu hình DPAPI còn nằm trong dữ liệu cũ sang thư mục `Data` mà không ghi đè cấu hình mới, kể cả máy đã có marker migration từ bản trước. Nếu cùng bot đang được một DocuRandom khác nhận lệnh, máy này chuyển sang chế độ **chỉ gửi thông báo** thay vì tê liệt hoàn toàn; nút Telegram hiển thị trạng thái riêng, log ứng dụng ghi rõ lỗi/trạng thái và bot gửi một thông báo khi kết nối thành công.
- Quy trình phát hành từ bản này chỉ ghi ghi chú của phiên bản hiện tại vào GitHub Release, không lặp toàn bộ changelog khiến API tiếp tục phình. Đã xác minh trực tiếp kênh tag public và toàn bộ 663 kiểm thử + 24 subtest.

- Bản `2.4.14` sửa phần chẩn đoán giới hạn ChatGPT Web không còn thu cả JavaScript ẩn của trang vào thông báo lỗi. Bridge `1.0.28` chỉ đọc chữ đang hiển thị trong popup/phần giao diện ngoài hội thoại, loại bỏ nguy cơ nhận nhầm script ẩn và chỉ ghi thông báo giới hạn ngắn gọn vào log.
- Lỗi dừng tại **Chờ ô soạn thảo** được xác định là xảy ra trước khi gửi prompt: thumbnail giữ `submitted=false`, tự nằm lại trong hàng chờ và sẽ được tạo sau nhịp nghỉ nếu chưa có ảnh. Cơ chế nghỉ chung 5 phút, chạy lại tuần tự và không đổi tài khoản của `2.4.13` được giữ nguyên.
- Màn hình chọn workspace có thêm **Hàng ChatGPT toàn tool**. Tùy chọn **Xếp hàng từng workspace** mặc định bật và được lưu ngoài workspace: mỗi lần chỉ một lượt ChatGPT chạy, các lượt còn lại giữ checkpoint theo FIFO để giảm nguy cơ `Too many requests`; render, Flow và VOICEVOX vẫn song song. Thay đổi áp dụng ngay cho lượt chưa bắt đầu và không hủy lượt đang chạy.

- Bản `2.4.13` nhận diện trực tiếp popup **Too many requests** của ChatGPT Web thay vì để popup che giao diện rồi báo nhầm mất đăng nhập, thiếu model High hoặc mất ô soạn thảo. Bridge chỉ quét thông báo nổi/phần giao diện ngoài hội thoại nên nội dung bình thường có nhắc tới cụm từ này không bị nhận nhầm.
- Khi tài khoản bị giới hạn tạm thời, tool giữ nguyên checkpoint, dừng toàn bộ lượt ChatGPT mới trong 5 phút rồi tự chạy lại tuần tự; thumbnail chưa gửi và audit VOICEVOX vẫn nằm trong hàng chờ. Số lượt ChatGPT hoạt động đồng thời được giới hạn tối đa 3 để tránh chính cấu hình CPU/RAM mạnh tạo một đợt yêu cầu quá lớn. Render, Flow và các phần độc lập vẫn tiếp tục khi đủ dữ liệu.
- ChatGPT Web Bridge nâng lên `1.0.27`. Nếu người dùng đổi tài khoản trong lúc workflow đang giữ hội thoại cũ, tool vẫn dừng an toàn vì tài khoản mới không thể truy cập cuộc hội thoại của tài khoản trước. Đã xác minh 57 kiểm thử Bridge, 171 kiểm thử trọng tâm và toàn bộ 658 kiểm thử + 24 subtest của tool đều đạt.

- Bản `2.4.12` sửa lỗi checkpoint ChatGPT đã có phản hồi nhưng giao diện tài khoản không hiện thanh hành động/nút Copy, khiến Bridge báo **ChatGPT đã ngừng sinh nhưng không có dấu hiệu kết thúc lượt**. Khi phục hồi một tin đã gửi, Bridge tải lại đúng URL hội thoại một lần và chỉ nhận phản hồi nếu mã lượt, toàn bộ text và HTML giữ nguyên tuyệt đối; không gửi lại prompt và không dùng văn bản đứng yên đơn thuần làm bằng chứng.
- ChatGPT Web Bridge nâng lên `1.0.26`. Cơ chế mới chỉ áp dụng cho lượt phục hồi từ checkpoint; lượt tạo mới vẫn bắt buộc có tín hiệu hoàn tất bình thường, nên không thể âm thầm nhận câu trả lời đang viết dở. Đã bổ sung kiểm thử cả trường hợp DOM giữ nguyên được nhận và DOM thay đổi sau reload bị từ chối.
- Đã xác minh 55 kiểm thử Bridge, 59 kiểm thử tích hợp ChatGPT Web và toàn bộ 654 kiểm thử + 24 subtest của tool đều đạt.

- Bản `2.4.11` sửa lỗi YouTube có thể báo metadata hoàn tất dù thumbnail tùy chỉnh chưa được lưu. Khi workspace có chọn thumbnail, tool giờ bắt buộc xác nhận preview xuất hiện sau khi gắn file và vẫn còn sau Save + tải lại đúng video; thiếu ảnh sẽ giữ checkpoint, thử sửa đúng một lần rồi báo rõ trường `thumbnail`, không còn âm thầm bỏ qua.
- Tool luôn gửi nguyên file ảnh người dùng đã chọn trước. Chỉ khi YouTube không xác nhận ảnh đó, tool mới tạo một bản JPEG tương thích trong dữ liệu nội bộ mà không sửa ảnh gốc. Đã thử thật bằng code trên profile **Test** với `5.png`: YouTube nhận nguyên PNG, lưu thành công trên đúng video đã có, giữ nguyên title, mô tả, SEO tags, khai báo AI, Không dành cho trẻ em và trạng thái Công khai; không upload lại video.
- Toàn bộ bộ kiểm thử đạt 654 bài và 24 subtest.

- Bản `2.4.10` sửa dứt điểm việc YouTube bị công nhận hoàn tất khi tệp mới truyền 47–56%. Tool luôn ưu tiên phần trăm của hộp upload đang sống, bắt buộc chờ đủ 100% và không còn nhầm thông báo **Checks complete** hoặc hộp hoàn tất cũ là bằng chứng truyền tệp xong.
- Luồng sửa đúng video sau upload tương thích giao diện YouTube Studio mới: lựa chọn `PUBLIC/UNLISTED/PRIVATE` theo thuộc tính trạng thái ổn định thay vì ID đã bị YouTube bỏ, đồng thời xác nhận nút **Done** qua Browser Bridge khi cửa sổ automation chạy ngoài màn hình và accessibility không nhìn thấy nút. Sau đó tool vẫn bấm Save đúng một lần, tải lại trang Edit và chỉ hoàn tất khi title, mô tả, SEO tags, nội dung AI, đối tượng trẻ em và quyền riêng tư đều khớp.
- Đã kiểm thử thực tế bằng code trên profile **Test** với video khoảng 2 GB: tiến trình được theo dõi đến 100%, bản nháp cũ được nối lại mà không upload trùng, metadata được lưu và video được đặt Công khai thành công. Toàn bộ bộ kiểm thử đạt 651 bài và 24 subtest.

- Bản `2.4.9` thêm nút **Sửa ChatGPT Web** ngay cạnh phần kiểm tra kết nối. Nút này dựng lại renderer/tab bị treo, dọn riêng cache HTTP/code/GPU tạm và tự thay Bridge cũ khi cần; cookie đăng nhập, Local Storage, lịch sử hội thoại, liên kết phiên cùng checkpoint workflow đều được giữ nguyên. Nếu ChatGPT đang xử lý bất kỳ lượt nào, thao tác sửa bị chặn để không cắt ngang hoặc gửi trùng.
- ChatGPT Web Bridge `1.0.25` nhận cả bố cục bộ chọn model mới không còn `aria-haspopup`, và nếu trang chỉ tải được ô nhập nhưng thiếu bộ chọn GPT-5.6 Sol/High thì tự tải lại đúng hội thoại một lần **trước khi** đính kèm hoặc gửi prompt. Tool vẫn bắt buộc đọc lại mức High thật; không hạ model âm thầm và không coi precheck xanh là bằng chứng gửi thành công.
- Đã kiểm tra thật bằng code trên phiên ChatGPT của máy: chạy sửa cache/renderer vẫn giữ trạng thái đăng nhập, sau đó chọn model High, nhập, gửi đúng một tin và nhận phản hồi `OK`. Bridge có 53 kiểm thử JavaScript; toàn bộ bộ kiểm thử Python đạt 642 bài, 24 subtest và bỏ qua 5 bài phụ thuộc môi trường máy thật.

- Bản `2.4.8` sửa hai lỗi còn lại từ log máy khách: mốc upload `100%` nay được giữ như bằng chứng truyền tệp hoàn tất dù YouTube lập tức ẩn/thay hộp tiến trình; hộp **Leave site** phát sinh ngoài lệnh điều hướng được hủy tại chỗ nhưng không còn làm hỏng một lệnh đọc CDP đang chạy.
- Luồng upload không còn cố ghi metadata vào textbox Polymer tạm thời ngay sau khi gắn file. Tool tạo đúng một draft `PRIVATE`, chờ đủ byte, lấy đúng video ID, rồi tuần tự ghi title, mô tả, SEO tags, thumbnail, khai báo AI, Không dành cho trẻ em và quyền riêng tư đã chọn trên trang Edit của chính video đó; cuối cùng tải lại để xác minh dữ liệu đã lưu thật. Nhiều profile vẫn truyền file song song, riêng pha sửa metadata được điều phối để các cửa sổ Donut nền không tranh input.
- Donut Bridge `1.1.14` ưu tiên đúng hộp/nút YouTube đang hiển thị thay vì phần tử mẫu ẩn trùng ID và nhận riêng hộp **Video upload complete** phủ trên wizard. Sau mốc này tool không còn phụ thuộc nút Close thiếu ổn định của Polymer: nó giữ đúng video ID, mở target Edit riêng, lưu và tải lại xác minh rồi mới dọn target upload. Trước mỗi nút Next/Save/radio và trường nhập, tool tái kích hoạt riêng target ở cửa sổ ngoài màn hình; radio và SEO chip có đường commit ngữ nghĩa riêng, nên thao tác vẫn được YouTube nhận khi người dùng thu nhỏ profile hoặc làm việc ở tab khác.

- Bản `2.4.7` sửa lỗi upload YouTube qua Donut CDP bị timeout ở `Page.navigate` khi Studio còn giữ hộp **Leave site**: tool dọn hộp thoại trên đúng target automation, chờ phản hồi điều hướng trong giới hạn dài hơn và kiểm tra lại frame đang sống thay vì gửi lệnh điều hướng lần hai.
- Cửa sổ upload riêng chạy nền ngoài vùng màn hình nhưng vẫn giữ layout đầy đủ của YouTube Studio; không phủ/giành tab của người dùng. Tool đọc đúng phần trăm truyền tệp, không rời trang khi còn dưới 100%, chỉ đóng target sau khi metadata và trạng thái lưu đã được tải lại xác minh. Nếu phiên chưa chắc chắn, target vẫn được giữ nguyên để Tiếp tục mà không upload lại.
- Sửa nút **Đã upload YouTube thủ công** và bộ hẹn giờ bị lỗi `inspect_workspace_pipeline is not defined`; checklist nay xác nhận/lưu marker đúng cả khi workspace chưa mở. Đã test thật bằng code trên profile Test: phục hồi đúng lượt upload cũ, không đăng trùng, rồi xác minh lại title, mô tả, SEO tags, thumbnail, khai báo AI, Không dành cho trẻ em và `PRIVATE` trên chính video `jAOqEs3c5A8`.

- Bản `2.4.6` sửa vòng đời checklist: tiêu đề chỉ được ghi sau khi YouTube xác nhận upload hoặc người dùng đánh dấu **Đã upload YouTube thủ công** ở thẻ workspace. Video mới render nhưng chưa đăng không còn làm bẩn danh sách chống trùng; khi bắt đầu phiên mới, tool cảnh báo và đóng gói phiên cũ vào `Skipped` trước khi tiếp tục, không mất dữ liệu.
- Thẻ workspace có checkbox xác nhận upload thủ công, lưu marker bền vững và hiển thị rõ trạng thái sau khi mở lại tool. Thumbnail AI được gắn theo đúng file Tổng hợp; phiên mới không dùng lại `current.png` của video cũ, còn cùng checkpoint thì không tạo ảnh trùng.
- Flow Agent `1.3.23` tăng thời gian chờ lệnh UI cho tab nền/thu nhỏ, đánh thức renderer bằng CDP và thử lại hữu hạn ngay trên tab hiện tại. Không reload trang, không mở thêm cửa sổ và không gửi lại prompt đã xác nhận; `FLOW_UI_COMMAND_TIMEOUT:browser-command` thoáng qua được phục hồi an toàn.
- Đã bổ sung kiểm thử cho việc loại title chưa upload và xác nhận thủ công; dữ liệu workspace/checkpoint hiện tại vẫn được giữ nguyên khi cập nhật.

- Bản `2.4.5` sửa upload YouTube chuyển trang quá sớm khi tệp vẫn đang truyền. Link video được YouTube cấp trước chỉ được lưu làm mã chống trùng; tool tiếp tục giữ nguyên tab, đọc và hiển thị đúng phần trăm/thời gian còn lại từ hộp upload, chỉ công nhận hoàn tất khi nút **Close** đã khả dụng ổn định.
- Sau khi truyền tệp xong, tool bấm Close và bắt buộc xác nhận hộp upload biến mất rồi mới mở đúng video để lưu lại title, mô tả, SEO tags, thumbnail, khai báo AI, Không dành cho trẻ em và quyền hiển thị cuối. Hộp **Leave site** cũ được hủy an toàn; lỗi/mất kết nối giữ tab cùng checkpoint và tuyệt đối không gắn tệp lần hai.
- Sửa trường hợp người dùng chọn **Công khai** nhưng video còn **Riêng tư** do bước xác minh bị chặn giữa chừng. Đã test thật bằng code trên profile Test: video `DP9k_k6OIEY` được phục hồi đúng lượt, metadata khớp sau tải lại và máy chủ YouTube xác nhận `PUBLIC`.
- Sửa race khi hủy ChatGPT Web trong bước audit VOICEVOX khiến luồng đọc HTTP đôi lúc lỗi `'NoneType' object has no attribute 'peek'`. Chỉ luồng sở hữu response được đóng socket; hủy được ánh xạ đúng sang trạng thái dừng và giải phóng hàng đợi mà không tạo lượt audit kế tiếp ngoài ý muốn.
- Đã qua 635 kiểm thử Python và 24 subtest; dữ liệu workspace, checkpoint và cấu hình hiện tại được giữ nguyên khi cập nhật.

- Bản `2.4.4` thêm **Hàng render toàn tool** ngay ngoài màn hình chọn workspace: chọn tối đa 1–4 video render đồng thời và xem số video đang render/đang chờ. Giới hạn này tính chung tất cả workspace, không phải mỗi workspace được chạy thêm 1–4 video. Có thể xếp hàng hơn 4 workspace; ví dụ 10 video với mức 2 thì 2 video render, 8 video chờ.
- Mức đã chọn được lưu riêng trong Data, giữ lại khi tắt/mở và cập nhật tool. Mở workspace cũ hoặc khôi phục mặc định của riêng workspace không ghi đè mức chung; các ô chỉnh trong workspace được đồng bộ. Đổi mức áp dụng cho hàng đợi hiện tại: giảm mức không hủy video đang render, các lượt chờ dùng cấu hình mới khi bắt đầu.
- Đây là giới hạn cho công đoạn render, không phải giới hạn toàn bộ ChatGPT/Flow/VOICEVOX và không phải cơ chế bảo đảm TPS cho Minecraft. Khi chạy chung máy với server, có thể chủ động hạ mức render để giảm cạnh tranh tài nguyên.

- Bản `2.4.3` sửa dứt điểm trường hợp YouTube chỉ nhận tệp video nhưng bỏ trống tiêu đề, mô tả, SEO tags và các lựa chọn dù giao diện từng báo đã điền. Toàn bộ thao tác nhập, đổi lựa chọn và bấm nút nay dùng sự kiện bàn phím CDP tin cậy; mỗi trường được blur để YouTube/Polymer chốt dữ liệu thay vì chỉ thay đổi DOM hiển thị.
- Lượt upload mới luôn giữ Private ở bước wizard đầu tiên. Sau khi YouTube cấp đúng video ID, tool mở trang chỉnh sửa của chính video đó, điền/khôi phục metadata, thumbnail, Không dành cho trẻ em, khai báo nội dung AI, SEO tags và quyền riêng tư cuối; sau đó tải lại trang từ máy chủ và bắt buộc đối chiếu toàn bộ dữ liệu trước khi ghi checkpoint hoàn tất. Trạng thái `Saving` hoặc hộp thoại `Video processing` không còn bị hiểu nhầm là đã lưu metadata.
- Checkpoint `uncertain` cũ đã có URL được sửa trực tiếp trên đúng video hiện hữu khi bấm Tiếp tục, không upload tệp lần hai. Đã test thật bằng code qua Donut Local trên profile Test: video Không công khai giữ đúng title, mô tả, 5 SEO tags, audience và AI sau một lần mở lại độc lập; chạy lặp trả đúng URL cũ và không tạo video trùng. Bộ kiểm thử: 614 test Python và 14 subtest.

- Bản `2.4.2` cập nhật Flow Agent `1.3.22` cho giao diện Flow mới: nhận đúng nút tạo dự án, chờ ô nhập xuất hiện, chọn Nano Banana 2 Lite/1 ảnh và thu ảnh theo media ID. Đã test thật bằng Microsoft Edge: một prompt, một ảnh 1376×768, hoàn tất trong khoảng 19 giây, checkpoint hoàn tất và không gửi lại.
- Các lệnh trình duyệt và vòng phục hồi UI có giới hạn; phục hồi trên đúng tab của tác vụ, không tự reload dự án hoặc mở nhiều cửa sổ. Khi chưa chắc prompt đã gửi hay chưa, giữ checkpoint và yêu cầu xác minh thay vì gửi trùng. Trang đăng nhập Google được báo đúng trạng thái, không yêu cầu cấp thêm quyền vào tài khoản Google.
- ChatGPT Bridge `1.0.24` đọc lại đúng hội thoại đã lưu một lần khi giao diện thiếu dấu hiệu kết thúc, không gửi lại prompt và không chấp nhận câu trả lời dở. Bộ kiểm thử: 612 test Python, 57 test JavaScript; kiểm thử đóng gói bridge đạt. Lỗi thiếu dấu hiệu kết thúc được kiểm tra mô phỏng, chưa tái hiện ở phiên ChatGPT thật.

- Hotfix `2.4.1` sửa kiểm tra kết nối Flow: phiên tạo ảnh qua giao diện có tab Flow và heartbeat hợp lệ không còn bị backend từ chối chỉ vì thiếu token API cũ. Desktop và backend dùng cùng điều kiện sẵn sàng; token đã lưu không thay thế được một tab Flow thực sự trên đường UI.
- Khi extension mất kết nối trước lúc gửi, tool chỉ thử lại có giới hạn ở prompt hiện tại rồi dừng an toàn, không báo lỗi lần lượt toàn bộ 20 prompt, không tự mở thêm tab hoặc refresh trang. Ảnh đã hoàn tất và checkpoint được giữ; Tiếp tục chỉ gửi phần chưa hoàn tất. Phiên UI đang dùng được không còn bị yêu cầu làm mới token khi reconnect.
- Chặn cờ render cũ khiến VOICEVOX hoàn tất sớm kích hoạt dựng bằng Media của lượt trước khi Flow vẫn đang chạy. Log nay ghi thay đổi kết nối, số tab Flow, phiên bản extension và trạng thái có/không token, không ghi giá trị token. Đã qua 605 kiểm thử tự động; chưa kiểm chứng trực tiếp bản vá trên máy khách. Giữ Flow Agent 1.3.19, dữ liệu workspace và cấu hình hiện tại.

- Bản `2.4.0` sửa dứt điểm upload YouTube chỉ gắn video nhưng không giữ tiêu đề, mô tả hoặc SEO tags khi chạy qua Donut Local. Tool nay khôi phục đúng cửa sổ automation riêng trước khi nhập vì YouTube Studio loại bỏ layout của trình chỉnh sửa khi cả cửa sổ bị thu nhỏ; tab người dùng đang làm việc không bị thay thế hoặc kích hoạt nhầm.
- Tiêu đề và mô tả được nhập bằng sự kiện CDP tin cậy vào đúng editor đang hiển thị, sau đó được đọc lại và bắt buộc ổn định đủ lâu để phát hiện trường hợp Polymer tự trả về tên file. Tool thử lại có giới hạn và vẫn dừng trước nút đăng nếu metadata không khớp; thumbnail, SEO tags, lựa chọn không dành cho trẻ em, nội dung AI và quyền riêng tư tiếp tục dùng đúng checkpoint cũ.
- Phiên upload dở được nối lại trên chính target đã gắn file, tuyệt đối không đính kèm hoặc upload video lần hai. Sau khi máy chủ xác nhận đúng metadata và URL, tab automation được đóng chính xác bằng Browser Bridge với MCP làm đường dự phòng, tránh tích tụ cửa sổ/tab sau nhiều workspace. Bản vá đã được demo thật thành công trên profile Test ở chế độ Không công khai và qua toàn bộ 588 bài kiểm thử.

- Hotfix `2.3.99` sửa kiểm tra Flow thực tế sau khi Google chuyển sang `flow.google.com`. Endpoint health của backend nay chuyển đầy đủ trạng thái tab và capability `ui_text_to_image` từ tiện ích lên desktop. Tài khoản có API credit bằng 0 nhưng giao diện Flow/Nano Banana vẫn còn khả năng tạo ảnh sẽ tiếp tục qua đường UI thay vì bị chặn nhầm trước khi gửi prompt. Flow Agent 1.3.19 thêm đường dự phòng qua `chrome.tabs` cho các bản Chromium không cung cấp đầy đủ `chrome.windows.getAll`; đồng thời nhận biết trang `labs.google/fx/tools/flow` hiện chỉ là trang giới thiệu và chuyển chính tab trống đó sang ứng dụng `flow.google.com`, không mở thêm Edge và không đụng tới project đang tạo ảnh. Chỉ khi cả credit lẫn UI đều không dùng được tool mới báo hết lượt; checkpoint và quy tắc không gửi trùng vẫn giữ nguyên.

- Hotfix `2.3.98` tương thích địa chỉ Google Flow mới `flow.google.com` đồng thời giữ hỗ trợ địa chỉ `labs.google` cũ. Tiện ích Flow 1.3.17 được cấp đúng quyền host, tự tìm lại cùng một tab Flow sau chuyển hướng và không còn lỗi `Extension manifest must request permission`. Nếu quyền tiện ích chưa cập nhật, hàng đợi dừng ngay prompt đầu tiên chưa gửi, không đánh dấu bỏ qua và không thử lan sang các prompt sau; bấm Tiếp tục sau khi tiện ích nạp lại sẽ nối đúng checkpoint. Donut chưa mở lúc khởi động nay là trạng thái chờ bình thường, không gửi bốn cảnh báo Telegram trùng cho bốn workspace. Timeout VOICEVOX thoáng qua vẫn được ghi đầy đủ vào log và tự nối lại nhưng không còn tạo thêm cảnh báo lỗi Telegram trước khi engine kịp sẵn sàng.

- Hotfix `2.3.97` sửa hai lỗi VOICEVOX khi nhiều workspace vừa mở lại. Đường dẫn WAV đã được checkpoint nhưng VOICEVOX chưa tạo xong nay được nhận là trạng thái đang chờ, không còn ghi `audio-probe-worker VALUEERROR` hoặc gửi cảnh báo Telegram sai cho từng workspace. Bộ đọc audit chấp nhận phản hồi JSON hoàn chỉnh có ký tự xuống dòng/tab chưa escape của ChatGPT nhưng vẫn kiểm tra nghiêm schema, project digest, speaker/style, mapping từng box và phạm vi thông số. Phản hồi thật sự sai được tự gửi lại trong tối đa ba lượt ChatGPT mới; nếu cả ba lượt vẫn không hợp lệ, workflow tiếp tục bằng project gốc an toàn, giữ nguyên giọng, style, pronunciation và bảy thông số thay vì làm VOICEVOX dừng. Bản dự phòng không được dùng làm checkpoint audit lâu dài nên lần workflow sau vẫn có thể audit lại bình thường.

- Bản `2.3.96` xử lý triệt để Flow khi Edge bị thu nhỏ hoặc người dùng chuyển sang tab khác: toàn bộ nhịp chờ DOM được chuyển sang service worker, renderer Flow được giữ ở lifecycle hoạt động bằng wake pulse nhưng không đưa tab/cửa sổ lên trước. Lỗi `FLOW_UI_TIMEOUT_UNSENT` và timeout trước khi gửi giữ nguyên lượt FIFO của workspace hiện tại, tự phục hồi rồi gửi lại đúng prompt đó; workspace sau không còn chen lượt. Trang Flow không bị reload và prompt đã gửi vẫn tuyệt đối không gửi trùng. Đồng thời sửa VOICEVOX chấp nhận `consonant: null`/`consonantLength: null` hợp lệ ở mora chỉ có nguyên âm, chặn lỗi `Box ..., mora ....consonant không hợp lệ` trước bước AI audit/render.

- Bản `2.3.95` tích hợp AI audit VOICEVOX vào đúng workflow của tool. Nhân vật được khóa theo lựa chọn hiện tại trong DocuRandom; ChatGPT nhận UUID nhân vật, style đang chọn và chỉ danh sách style của chính nhân vật đó, nên có thể đổi sắc thái theo từng đoạn nhưng không thể đổi sang giọng khác. Trước khi render, tool tạo `.vvproj`, thiết kế preset theo nội dung, map toàn bộ box, áp dụng override vi mô và audit cách đọc tiếng Nhật; file FINAL được kiểm tra lại rồi chính query FINAL mới được đưa vào VOICEVOX để xuất WAV + SRT. Có thêm nút audit một `.vvproj` sẵn có và xuất đồng thời `[Tên]_VOICEVOX_FINAL_audited.vvproj` + `[Tên]_content_FINAL.txt`. Checkpoint gắn digest project/query để không gọi AI hoặc tạo voice lại oan, đồng thời từ chối style ngoài nhân vật, JSON sai, thông số ngoài phạm vi hay không theo bước `0.01`. Project VOICEVOX cũ thiếu `pauseLengthScale` được nâng cấp an toàn. Chế độ này bật mặc định và có thể tắt để quay lại một style/thông số chung như trước.
- Hotfix `2.3.94` sửa triệt để vòng reload sau `FLOW_UI_TIMEOUT_UNSENT`: Flow Agent `1.3.15` không còn reload tài liệu Flow, xóa local storage/cookie hay tự gửi lại prompt trong cùng request khi watchdog UI hết hạn. Driver được hủy/phục hồi và tiêm lại ngay trên đúng tab hiện tại; project, ảnh đang tạo và checkpoint workspace được giữ nguyên. Prompt chưa gửi chỉ được thử lại từ checkpoint, prompt đã xác nhận gửi tuyệt đối không gửi trùng, còn ảnh hợp lệ đã lưu không bị tạo lại. `/flow_restart` nay chỉ reload service worker và phục hồi driver tại chỗ; hard reload trang được tách riêng cho lỗi token API 401, không tham gia luồng tạo ảnh qua giao diện.
- Hotfix `2.3.93` sửa Flow khi Edge bị thu nhỏ/tab chạy nền: Flow Agent `1.3.14` ưu tiên mở và chọn đúng **Nano Banana 2 Lite ×1** bằng phần tử DOM sống, chỉ dùng phím tin cậy làm dự phòng và luôn đọc lại trạng thái model trước khi gửi prompt. Retry nay ghi nhớ đúng tab/cửa sổ Flow qua vòng đời service worker, ưu tiên refresh tab đó; chỉ mở một cửa sổ Edge khi tab thật sự không còn nên không sinh nhiều Edge sau mỗi lần lỗi. Telegram có thêm lệnh toàn cục `/flow_restart`: reload riêng Flow Agent, refresh đúng một tab Flow, chống bấm lặp và báo kết quả về bot; lệnh không cắt ngang prompt đang gửi để bảo toàn checkpoint/chống tạo trùng.
- Hotfix `2.3.92` hoàn thiện thao tác chọn realtime YouTube sau khi kiểm tra trực tiếp hai trạng thái menu đóng/mở trên profile Test. Google Studio dùng role Accessibility `option` cho hai mục **Last 48 hours/Last 60 minutes**, trong khi 2.3.91 chưa nhận role này và phím Enter không kích hoạt ổn định. Tool nay bấm đúng phần tử bằng khung DOM sống (không tọa độ cố định, không điều khiển chuột Windows), nhận đúng mục `option` 60 phút và chỉ đọc view sau khi menu đã đóng, nút hiện tại lẫn báo cáo đều xác nhận 60 phút. Đã thử thực tế bằng cách cố tình chuyển về 48 giờ rồi để nguyên hàm production tự đưa lại 60 phút; kết quả menu đóng và báo cáo được xác minh chính xác.
- Hotfix `2.3.91` sửa bộ kiểm tra realtime YouTube khi Studio đã ghi nhớ sẵn khung **60 phút**: tool không còn nhấn lại nút đang chọn khiến menu thời gian mở ra nhưng không đóng. Tool chỉ mở menu khi trạng thái hiện tại là **48 giờ**, sau đó mới chọn **60 phút**; nếu đã là 60 phút thì chờ báo cáo tải xong và chỉ chấp nhận số view khi cả nút khung thời gian lẫn báo cáo đều xác nhận 60 phút. Đã kiểm tra trực tiếp trên profile Test với bốn video công khai và bổ sung kiểm thử chống tái phát; chế độ kiểm tra chỉ đọc không gửi đơn 1MM.
- Hotfix `2.3.90` bổ sung nút **Mở GitHub** ngay cạnh ô đăng nhập Donut Local. Nút mở trực tiếp biểu mẫu fine-grained token chính thức đã điền sẵn chủ sở hữu `JustHari01` và quyền chỉ đọc; người dùng chỉ cần chọn kho `donutbrowser-local`, tạo mã, dán lại rồi bấm **Lưu & kiểm tra**. Nếu bấm **Cài đặt Donut Local** khi chưa đăng nhập, tool cũng tự mở đúng trang thay vì chỉ hiện cảnh báo không có đường dẫn; nếu Windows không mở được trình duyệt, link được sao chép vào clipboard.
- Hotfix `2.3.89` nối đúng nút **Cài đặt/Cập nhật Donut Local** với kênh Developer private đã ký `JustHari01/donutbrowser-local`. Sau khi lưu đăng nhập GitHub một lần, DocuRandom tự kiểm tra lúc khởi động, báo khi có bản mới, tải đúng gói portable, xác minh chữ ký Ed25519 + SHA-256, cài nguyên tử vào `Components/DonutLocal`, đóng bản cũ, mở bản mới và tự nối MCP; người dùng không phải vào GitHub tải ZIP hay tự chạy installer. Luồng cũ gọi nhầm installer public `DocuRandom-DN` đã bị loại khỏi nút giao diện. Bản mới vẫn dùng nguyên dữ liệu profile/cookie/proxy/fingerprint của DonutBrowser và tự phục hồi bản thành phần trước nếu bản cập nhật không mở được MCP/Developer Tools.
- Hotfix `2.3.87` tách hoàn toàn upload YouTube, kiểm tra realtime và Google Flow khỏi tab người dùng. Mỗi tác vụ mở cửa sổ automation riêng đã thu nhỏ; upload dùng CDP chỉ để gắn đúng file, còn DocuRandom Donut Bridge 1.1.5 điền/đọc lại metadata và click theo DOM ổn định ngay cả khi tab ẩn, có keepalive để worker không rơi kết nối khi YouTube xử lý lâu. Upload luôn ưu tiên theo từng profile: lượt check view định kỳ đang chờ sẽ được hoãn 2 phút, lượt đang đọc sẽ dừng ở checkpoint an toàn, còn yêu cầu 1MM đã gửi thì chỉ chờ kết quả chứ không gửi lại. Checkpoint upload lưu chính xác target do tool sở hữu nên nút Tiếp tục nối lại đúng phiên, không gắn video lần hai. Thêm nút **Cài/Cập nhật Bridge cho tất cả profile**: tool đăng ký một gói qua MCP Donut Local, thêm vào các nhóm tiện ích sẵn có mà không thay thế thành viên, và gán nhóm DocuRandom cho profile Wayfern chưa có nhóm; chạy lại không tạo tiện ích/nhóm trùng. Profile không cần mở sẵn: lúc upload, tool tự mở profile, kích hoạt và ghép Bridge qua phiên xác thực ngắn hạn trong đúng profile, không cần bấm biểu tượng extension; profile đã mở trước lúc cập nhật chỉ cần đóng/mở lại một lần. Đã kiểm tra thật 11/11 profile trên máy này, kể cả lần chạy lặp idempotent. Flow Agent 1.3.13 cũng dùng cửa sổ riêng thu nhỏ, không còn fallback kích hoạt tab hiện tại.
- Hotfix `2.3.86` sửa tự động upload YouTube và theo dõi realtime trên Donut Local: tool chấp nhận đúng loader mới khi YouTube Studio redirect/bootstrap, không còn mở rồi điều hướng cùng một tab hai lần; nút upload được nhận diện bằng `BUTTON` thật nằm trong `#upload-button` thay vì lớp bọc Web Component không nhận Enter. Tab tự động hóa riêng được kích hoạt bằng CDP trước khi gửi phím, hoàn toàn không dùng tọa độ hay di chuyển chuột; các bước chọn video, điền tiêu đề/mô tả/SEO tags, Không dành cho trẻ em, khai báo AI, chọn quyền riêng tư và Lưu vẫn bám theo DOM/Accessibility. Phần xem realtime cũng dùng cùng cơ chế điều hướng mới, không gọi miền Runtime bị chặn và đọc qua toàn bộ số trang/video cho tới khi YouTube hết nút Trang tiếp theo; không còn giới hạn 100 video/20 trang, đồng thời dùng dấu vân tay ID mỗi trang để chặn vòng lặp. Đã kiểm thử upload thực tế thành công ở chế độ Không công khai và đọc realtime thành công trên profile Test.
- Hotfix `2.3.85` sửa trạng thái Donut Local xanh giả: trước khi đọc profile, theo dõi realtime hoặc upload YouTube, tool xác minh đúng runtime `donut-browser-local` cùng đủ bộ công cụ automation cần thiết. Nếu cổng MCP đang thuộc Donut chính thức, tool dừng sớm trước khi mở tab/chọn file, hướng dẫn đóng đúng ứng dụng và giữ nguyên checkpoint để bấm **Tiếp tục**; không còn đến giữa lượt mới báo thiếu quyền browser automation. Không thay đổi profile, cookie, proxy hay dữ liệu Donut.
- Hotfix `2.3.84` sửa lỗi nghiêm trọng khi file Tổng hợp có `CHECKLIST NỘI DUNG ĐÃ LÀM (CHỐNG TRÙNG)` ngay sau mục `TIÊU ĐỀ`: checklist không còn bị cộng nhầm vào tiêu đề YouTube (trường hợp 48 ký tự bị báo sai thành 261 ký tự). Nhờ đó scheduler không còn chuyển video đã render hoàn chỉnh vào `Skipped` chỉ vì lỗi đọc metadata. Lượt upload cũng có thể khôi phục đúng một video khi người dùng copy lại dữ liệu từ `Skipped` mà không còn checkpoint gốc; nếu có nhiều video, tool vẫn dừng để tránh chọn nhầm. Preview Telegram tự bỏ qua thumbnail đã bị xóa/di chuyển thay vì ghi exception. Không thay đổi Flow, VOICEVOX, CTA/logo, lịch hoặc cơ chế chống upload trùng.
- Hotfix `2.3.83` sửa lịch upload dùng lại video đã render thủ công: khi ca workflow đến giờ, video hoàn chỉnh được giữ nguyên để ca YouTube upload sử dụng, không bị chuyển vào `Skipped` trước khi upload. Bộ ghi tệp cấu hình ChatGPT dùng chung được khóa theo đường dẫn và tự thử lại khi Windows tạm trả `WinError 5`; đường dẫn file Tổng hợp cũ trong bộ nhớ cũng không còn che khuất checkpoint hợp lệ. Flow cũng không còn giữ khóa `FLOW_UI_BUSY` vô hạn: một tác vụ giao diện không có tiến triển sẽ được hủy/làm mới tab sau thời gian an toàn, còn workspace chỉ giữ prompt trong checkpoint để thử lại, không lặp im lặng hàng giờ. Không thay đổi VOICEVOX, CTA/logo hay theo dõi realtime YouTube.
- Hotfix `2.3.82` tăng thời gian kiểm tra cấu hình Flow thật trên tab Google Flow, để bước xác nhận **Nano Banana 2 Lite × 1 ảnh** không tự cắt ngang khi trang vừa reload, đang mở project hoặc UI phản hồi chậm. Không thay đổi workflow/lịch/thumbnail; chỉ nạp lại Flow Agent lên `1.3.12` và giữ nguyên yêu cầu: nếu chưa xác nhận đúng Lite ×1 thì không gửi prompt.
- Hotfix `2.3.81` sửa `FLOW_UI_IMAGE_COUNT_NOT_CONFIRMED`: Flow Agent xác nhận lựa chọn **1 ảnh** qua trạng thái DOM thật của button/radio (native checked, ARIA, data-state, biểu tượng chọn và roving tab), giữ đúng lớp cấu hình đang mở và tự làm mới riêng tab Flow rồi thử lại một lần nếu giao diện vừa đổi. Dropdown model phân biệt chặt ba lựa chọn hiện tại: **Nano Banana Pro**, **Nano Banana 2** và **Nano Banana 2 Lite**; chỉ đúng chữ Lite mới được gửi prompt. Trước khi phát hành đã có đường kiểm tra sống chỉ đọc/cài Lite ×1 mà không chạm prompt; tiện ích cũng tự nạp lại worker mới sau cập nhật mà không cần đóng toàn bộ Edge.
- Hotfix `2.3.80` khóa toàn bộ luồng tạo ảnh Google Flow ở **Nano Banana 2 Lite** và đúng **1 ảnh cho mỗi prompt**. Cấu hình cũ, checkpoint cũ, popup extension, API/MCP và tham số gọi từ workspace đều bị chuẩn hóa về Lite ×1. Trước từng prompt — kể cả sau reload — extension phải mở cài đặt và xác nhận lại model cùng số lượng; nếu không tìm thấy hoặc không xác nhận được trạng thái đã chọn thì dừng trước nút gửi. Đã bỏ hoàn toàn fallback sang Nano Banana Pro để không chọn nhầm model hoặc tạo thừa ảnh.
- `2.3.80` sửa hàng đợi thumbnail nhiều workspace: mỗi file Tổng hợp sở hữu một cuộc trò chuyện và một ảnh riêng; lỗi trước khi gửi prompt tự thử lại, kể cả sau khi mở tool, còn lượt đã xác nhận gửi vẫn khóa chống trùng. Thumbnail cùng trạng thái của video cũ được lưu vào Uploaded/Skipped rồi dọn khỏi phiên mới. Telegram bỏ nhu cầu `/select`, mọi lệnh đọc/vận hành nhận trực tiếp nhiều số/tên workspace; `/thumbnail_prompt` chỉ trả đúng **PROMPT THUMBNAIL HOÀN CHỈNH**.
- Lịch đang ON và `/checkview on` được khôi phục sau mở lại/cập nhật, trong khi workflow chạy tay vẫn đứng yên. Mốc đã lỡ không chạy bù; ca lỗi thử lại từ checkpoint mỗi 15 phút, nhưng nếu video chưa sẵn sàng trước giờ đăng 15 phút thì tách phiên lỗi vào `Skipped`, dọn workspace và chuyển sang ca sau. ChatGPT Web tự tính số phiên từ CPU/RAM, tối đa 20 trang hoạt động; máy 64 GB có thể chạy 20 workspace, còn render/VOICEVOX/Flow vẫn được điều phối theo tài nguyên để tránh treo máy.
- Bản `2.3.79` sửa lỗi ứng dụng tự nối workflow khi mở lại hoặc sau cập nhật. Mỗi lần khởi động, mọi workspace luôn đứng yên: lịch hẹn và YouTube Like bị khóa, checkpoint cùng cấu hình ba ca vẫn giữ nguyên, và tool không còn khởi tạo ngầm toàn bộ workspace gây nặng máy. Chỉ lệnh **Chạy/Tiếp tục**, **Bắt đầu hẹn giờ** hoặc xác nhận bật YouTube Like của người dùng mới mở đúng workspace/chức năng tương ứng. Khi cập nhật, một lần bấm **Khởi động lại** sẽ tự dừng đồng loạt các tác vụ, chờ ghi checkpoint rồi cài bản mới; không cần dừng từng workspace. Với ca lặp hằng ngày, giao diện và bộ chọn chỉ hiện giờ; chỉ lịch một lần mới hiện ngày. Nút **Bắt đầu hẹn giờ** tự ghép đúng ngày cho ca qua đêm `15:30 → 05:00 hôm sau`, kể cả khi bấm sau 15:30 hoặc dữ liệu cũ chỉ lưu hai giờ trên cùng ngày.
- Telegram hỗ trợ nhóm workspace theo số: `/select 1 2 3`, sau đó các lệnh vận hành/trạng thái áp dụng đồng loạt; cũng có thể ghi `1 2 3` trực tiếp sau lệnh. Lệnh `/checkview on 1 2 3` bật bộ theo dõi realtime/Auto Like cho từng kênh đủ điều kiện, còn `/checkview off 1 2 3` dừng ngay và không gửi đơn mới; kết quả từng workspace được trả riêng.
- Flow backend và VOICEVOX Engine đã có sẵn được khởi động nền ngay khi mở tool nhưng không tự chạy workflow, tạo ảnh/voice, upload hay đặt Like. Mỗi thẻ workspace có nút **Xóa checkpoint** kèm xác nhận để bỏ hẳn phiên video đang dở mà không xóa Media/Output/VOICEVOX, lịch, kênh/profile, cấu hình hoặc checklist chống trùng. Nếu người dùng xóa thủ công toàn bộ dữ liệu phiên cũ, launcher tự nhận checkpoint mồ côi và cập nhật workspace về trạng thái sẵn sàng.
- Bản `2.3.78` cài sẵn chuỗi ba ca hằng ngày cho mọi workspace: đăng YouTube lúc `05:00`, `10:00`, `15:00`; sau mỗi lượt đăng 30 phút tự chạy workflow mới lúc `05:30`, `10:30`, `15:30` để chuẩn bị video cho mốc kế tiếp. Workspace dùng chuỗi mặc định 2.3.77 được nâng lịch tại chỗ, giữ nguyên ca đang chạy và tự lưu cấu hình ngay khi ứng dụng mở. Telegram không còn gửi từng prompt ảnh; cảnh báo vận hành, upload và YouTube Like được phân loại bằng HTML dễ đọc, còn link/kết quả có khối **Copy**.
- Bộ theo dõi YouTube Like duyệt bằng phần tử giao diện qua các trang video tiếp theo và các vùng cuộn/lazy-load của tab Analytics nền, tối đa 100 video ứng viên mỗi lượt. Không dùng tọa độ, không chiếm tab upload của người dùng và chế độ kiểm tra chỉ đọc vẫn tuyệt đối không gửi đơn 1MM.
- Bản `2.3.77` bổ sung theo dõi realtime YouTube Like qua 1MM cho từng workspace/profile Donut. Tool dùng tab Analytics nền riêng, tự lấy link video và view 60 phút, quét lại mỗi 2 phút, tính ổn định 5–10%, chỉ đặt từ 10 Like, tải catalog mới trước mỗi quyết định để chọn gói hợp lệ rẻ nhất và khóa 30 phút/chống đơn trùng.
- Telegram nay gửi thẻ kết quả dễ đọc sau khi phát hiện video đủ tiêu chí và đặt đơn thành công: có tên workspace, tiêu đề cùng link video, view 60 phút, tỷ lệ, số Like, gói rẻ nhất và mã đơn. Trạng thái đơn chưa chắc chắn cũng có thẻ cảnh báo riêng và tự dừng để bảo vệ số dư.
- VOICEVOX nay phục hồi lời thoại trực tiếp từ đúng file Tổng hợp của workspace khi ô kịch bản hoặc checkpoint giao diện bị trống. File thực tế có tiêu đề `THOẠI VOICEVOX`, tiền tố `A:` và hàng trăm dòng vẫn được đọc trọn vẹn; tool không quét hoặc chọn nhầm file mới nhất của workspace khác.
- ChatGPT Web Bridge 1.0.23 cho đủ 20 phiên workspace/thumbnail chạy đồng thời trên máy 64 GB RAM và không còn hạ toàn bộ hàng đợi về một phiên khi một câu trả lời chậm. Phản hồi dài được chờ không giới hạn; khi DOM ngừng tiến triển, bridge tải lại đúng cuộc hội thoại đã lưu rồi tiếp tục đọc, tuyệt đối không gửi prompt lần hai.
- Flow gặp `FLOW_UI_BUSY` sẽ chờ tác vụ giao diện trước giải phóng rồi tự kiểm tra lại cho tới khi chạy được hoặc người dùng hủy. Prompt hiện tại vẫn nằm trong checkpoint, không bị đánh dấu bỏ qua và không phát lại khi trạng thái gửi chưa chắc chắn.
- Mọi workspace được khởi tạo nền ngay khi mở tool và mặc định có chuỗi ba ca hằng ngày: **làm 05:00 → đăng 10:00; làm 10:00 → đăng 15:00; làm 15:00 → đăng 05:00 hôm sau**. Tại các mốc 05:00/10:00/15:00, lượt đăng của ca trước luôn được xử lý trước khi workflow kế tiếp bắt đầu; lịch đã tùy chỉnh từ trước vẫn được giữ.
- Nếu đến giờ đăng mà ca trước đang lỗi hoặc không có video hoàn chỉnh, scheduler đánh dấu bỏ qua mốc đó, chuyển toàn bộ dữ liệu dở sang `Skipped` của chính workspace, dọn sạch `Output`/`Media`/`VOICEVOX` cùng checkpoint dùng một lần rồi chạy ca kế tiếp. Master Prompt, cấu hình, profile/kênh, lịch và checklist chống trùng vẫn được giữ.

- Bản `2.3.76` nâng bộ hẹn giờ thành danh sách tối đa 12 ca riêng cho từng workspace. Mỗi ca ghép đúng giờ **sản xuất + render** với giờ **upload YouTube**, hỗ trợ một lần hoặc hằng ngày; các ca chồng lấn bị từ chối vì một workspace chỉ được giữ một phiên video chưa đăng.
- Workflow hẹn giờ nay phải hoàn tất ChatGPT → Flow/media → VOICEVOX → MP4 rồi mới chuyển sang trạng thái **video sẵn sàng, chờ đăng**. Mốc đang bận, thiếu media, lỗi quét hoặc render chưa nhận tác vụ không còn bị đánh dấu đã chạy oan; yêu cầu render được lưu bền vững và tự nối lại sau lỗi hoặc khi mở lại tool.
- Mỗi ca chỉ upload đúng video của chính ca đó. Ca mới chờ phiên trước upload và lưu kho toàn bộ `Output`, `Media`, VOICEVOX cùng dữ liệu liên quan; không ghi đè video cũ và không tạo lượt đăng thứ hai khi trạng thái upload chưa chắc chắn.
- ChatGPT Web được giới hạn tối đa 2 phiên hoạt động đồng thời dù có 10 workspace; các workspace còn lại xếp hàng công bằng. Khi một phản hồi đứng lâu, hàng đợi tự hạ xuống 1 phiên trong 15 phút, thu hồi đúng DOM/checkpoint và tự nối lại sau 15–120 giây mà không gửi lại Master Prompt đã gửi. Lỗi đăng nhập, CAPTCHA, policy hoặc hết lượt vẫn dừng và báo rõ để người dùng xử lý.
- Precheck VOICEVOX của nhiều workspace nay dùng một hàng đợi chung: workspace đang chờ lượt khởi động/cài engine không còn bị hiểu nhầm là treo sau 120 giây. Lỗi kết nối tạm thời và lỗi `ChatGPT response stopped making progress` đều tự recheck từ checkpoint tối đa 12 lượt; Flow đang chạy được phép hoàn tất trước, prompt đã gửi không bị gửi lại, và Telegram chỉ báo dừng thật sự khi đã hết khả năng phục hồi.
- Flow backend nhận biết nhiều browser profile có extension. Nếu cả hai model ảnh trên một tài khoản đều hết lượt, tool đánh dấu riêng profile đó và tự chuyển đúng prompt sang profile còn lượt; timeout hoặc trạng thái không chắc chắn tuyệt đối không bị chuyển để tránh gửi trùng. Nhiều tab cùng một tài khoản không được coi là tăng hạn mức.
- Telegram có thêm nút/lệnh `/hide_menu` để đóng bàn phím lệnh nhanh. Bàn phím không còn bị đánh dấu luôn thường trực và chỉ hiện lại khi người dùng gọi `/menu`, `/help` hoặc `/start`.
- Bản `2.3.75` tự lấy **PROMPT THUMBNAIL HOÀN CHỈNH** từ đúng file Tổng hợp, gửi đúng một lượt tạo ảnh qua ChatGPT Web, chờ trạng thái ảnh thật trong DOM rồi tải dữ liệu gốc về ô Thumbnail YouTube. Cùng một file/prompt có khóa SHA-256 bền vững: lượt đã gửi, lỗi chưa chắc chắn hoặc đã hoàn tất tuyệt đối không tự tạo lại.
- ChatGPT được hỏi xác nhận trong cùng cuộc trò chuyện và tool chỉ chốt khi nhận đúng **ĐÃ TẠO**. Bridge 1.0.21 sửa cờ Stop toàn trang có thể mắc sau khi ảnh xong: xác nhận chính xác đứng yên 2 giây được chấp nhận mà không gửi lại; lý do từ chối/lỗi thật được đưa nguyên văn về UI và Telegram.
- Thumbnail chọn tay, ảnh ChatGPT và ảnh Telegram dùng chung một ô. Nguồn mới thay nguồn cũ; ảnh do tool quản lý được chuyển vào lịch sử, ảnh gốc bên ngoài không bị xóa. Telegram tự gửi preview ảnh thật sau khi thay và `/thumbnail_current` trả ảnh xem trước thay vì chỉ gửi đường dẫn trên máy.
- Upload tự động chờ thumbnail AI đang chạy hoặc thumbnail bắt buộc còn thiếu; khi ảnh hợp lệ được gắn, hàng đợi mới tiếp tục. Render/video vẫn được giữ nếu tạo thumbnail lỗi, và người dùng có thể thay ngay bằng ảnh thủ công hoặc Telegram mà không tạo thêm lượt ChatGPT.
- Bản `2.3.74` biến Telegram Bot thành bảng điều khiển từ xa dùng chung cho toàn bộ DocuRandom. Bot tự đăng ký Menu lệnh và bàn phím nút nhanh tiếng Việt; người dùng không phải nhớ cú pháp để xem/chọn workspace, chạy/tiếp tục/tạm dừng/hủy hoặc kiểm tra log và lỗi.
- Telegram nhận ảnh JPG/PNG/WEBP tối đa 20 MB làm thumbnail cho workspace đang chọn hoặc workspace ghi trong caption. Ảnh được kiểm tra thật bằng Pillow, chỉ thay sau mã xác nhận dùng một lần trong 2 phút; ảnh cũ do bot quản lý được chuyển vào lịch sử và file gốc ngoài workspace không bao giờ bị xóa.
- Thêm lệnh lấy Prompt Thumbnail trực tiếp từ đúng file Tổng hợp, xem/bỏ thumbnail, khởi động và kiểm tra Flow/VOICEVOX sau khi tool hoặc máy khởi động lại. Lệnh từ xa không mở hộp thoại lỗi chặn màn hình; kết quả thành công hoặc lỗi được gửi lại bot.
- Thêm điều khiển lịch theo giờ Việt Nam qua Telegram: đặt workflow/upload bằng giờ ngắn như `08:30 daily` hoặc ngày giờ đầy đủ, xem lịch, bắt đầu và hủy canh lịch. Bộ khóa hiện có tiếp tục ngăn một workspace tạo tác vụ hoặc lượt upload trùng.
- Thông báo lỗi Telegram nay ghi rõ workspace, công đoạn, mã/chi tiết và hướng phục hồi. Các lỗi YouTube `uncertain`, lưu kho sau upload, Flow, VOICEVOX, scheduler, render và callback giao diện đều được báo; lỗi Telegram vẫn không làm dừng tác vụ trên máy.
- Bản `2.3.73` thêm thời gian xuất hiện độc lập cho CTA và logo: toàn bộ video, đoạn đầu, đoạn cuối hoặc khoảng tùy chọn; CTA vẫn có chế độ tương thích **Phát một lần**. Render tay, workflow và lịch tự động đều dùng đúng cấu hình đã lưu.
- Preview CTA video giờ phát từ đầu đến hết toàn bộ tệp rồi tự lặp, có nút phát/tạm dừng, thanh tiến độ và đồng hồ. Luồng xem trước dùng FFmpeg độ phân giải thấp ở nền để giao diện vẫn phản hồi; xóa nền xanh/đen/trong suốt dùng cùng thông số với video xuất cuối.
- Thêm Telegram Bot toàn cục: token mới được xác minh qua HTTPS và mã hóa bằng Windows DPAPI, chỉ một private chat ghép cặp bằng mã dùng một lần mới được phép nhận trạng thái/prompt ảnh/lỗi và gửi lệnh điều khiển workflow.
- Telegram bỏ toàn bộ lệnh cũ trước mỗi phiên tool, tăng `update_id` sau từng update và không tự thử lại lệnh hết hạn. Upload YouTube qua bot mặc định cần mã xác nhận thứ hai chỉ dùng một lần trong 2 phút, sau đó vẫn phải qua checkpoint và khóa SHA-256 chống đăng trùng hiện có.
- Các lệnh bot gồm `/status`, `/workspaces`, `/select`, `/run`, `/continue`, `/pause`, `/cancel`, `/logs`, `/upload` và `/confirm_upload`. Lỗi Telegram chỉ đổi trạng thái kết nối, không làm hỏng hoặc dừng workflow trên máy.
- Bản `2.3.72` thêm checklist chống trùng nội dung riêng cho từng workspace và mặc định bật. Mỗi video render thành công được đánh dấu `[x]` trong `CHECKLIST_NOI_DUNG_DA_LAM.txt`; bản JSON bền vững nằm trong `.docurandom`, không bị xóa khi upload xong, lưu kho phiên cũ hoặc làm sạch `Media`/`Output`.
- Sửa lịch hẹn Flow làm một workspace treo 6 phút rồi khóa toàn bộ workspace sau bằng `FLOW_UI_BUSY`. Flow Agent 1.3.9 thu hồi đúng tác vụ treo, chờ kết quả hủy để biết prompt đã gửi hay chưa, chỉ thử lại prompt chưa gửi và giải phóng phiên trước khi chuyển workspace; checkpoint không bỏ qua hàng loạt prompt.
- Sửa upload YouTube khi profile Donut đã mở sẵn: tool đọc toàn bộ tab của đúng profile, ưu tiên và kiểm tra từng tab Studio, hoặc tự mở một tab Studio riêng nếu cần. Mỗi tab phải hoàn tất đúng lần điều hướng mới rồi tool mới dò và kích hoạt ô video bằng DOM/CDP, nên tab không active/cửa sổ thu nhỏ/proxy tải chậm không còn làm bấm nhầm DOM cũ.
- Trước lệnh **BẮT ĐẦU**, tool gửi checklist của đúng workspace vào ChatGPT, hỏi xác nhận và chỉ tiếp tục khi nhận đúng **ĐÃ XONG**. Lịch sử đầy đủ vẫn nằm trên máy; phần gửi vào ChatGPT tự giới hạn 300 mục/24.000 ký tự để không làm tái phát lỗi tin nhắn quá lớn.
- Nếu ChatGPT vẫn chọn tiêu đề giống hoặc gần như giống nội dung cũ, tool yêu cầu đổi sang chủ đề khác ngay trong cùng phiên. Sau giới hạn sửa an toàn, tool dừng trước Flow/VOICEVOX/render thay vì âm thầm tạo video trùng; file Tổng hợp cũng chứa checklist để người dùng kiểm tra.
- Bảng điều khiển nay khóa/xám dần đúng nhóm thông số đã được từng công đoạn chốt. Các mục chưa được công đoạn sử dụng vẫn chỉnh được; thẻ trạng thái cho biết rõ ChatGPT, Flow, VOICEVOX, render hay YouTube đang giữ nhóm nào.
- Bản `2.3.71` sửa engine tạo ảnh Flow không còn để một lỗi tạo ảnh làm đứng toàn bộ hàng đợi. Mỗi prompt phải được xác nhận đã gửi; nếu Flow từ chối hoặc không trả ảnh sau khi đã nhận prompt, tool ghi nhận riêng, làm mới giao diện bị kẹt và tiếp tục prompt kế tiếp thay vì bỏ qua toàn bộ phần còn lại.
- Checkpoint Flow v2 lưu nguyên văn toàn bộ prompt, các ảnh đã xong và danh sách prompt chưa gửi. Bấm **Tiếp tục** chỉ gửi phần chưa được xác nhận, không tạo lại ảnh hoặc gửi lại prompt đã hoàn thành. Flow Agent 1.3.8 đồng thời sửa xác minh prompt nhiều dòng, chỉ công nhận nút gửi thành công khi giao diện thật sự bắt đầu tạo và tự làm mới trạng thái Flow nếu prompt đã gửi nhưng không trả ảnh.
- Sửa hồi quy xuất hiện sau ảnh đầu tiên: bộ dò cấu hình cũ quét icon `crop/tune` trên toàn trang nên có thể nhầm nút crop của thumbnail đã tạo với nút cấu hình cạnh composer, mở lớp xem ảnh và che ô prompt. Bộ dò mới chỉ chọn phần tử editable và nút điều khiển trong cùng vùng composer, ghi nhớ cấu hình theo project, không dùng tọa độ và không quét nút trên các tile đầu ra.
- Bản `2.3.70` đồng bộ cửa sổ xem trước CTA/logo với giao diện Donut Mono của DocuRandom: cùng nền tối, card, tab, ô nhập, nút chính/phụ, trạng thái focus và nhịp khoảng cách. Dialog tự chọn kích thước phù hợp màn hình, hỗ trợ `Esc` để hủy và `Ctrl+Enter` để áp dụng.
- Canvas preview và bảng điều khiển nay co giãn theo tỷ lệ cửa sổ. Video luôn giữ đúng tỷ lệ ngang/dọc/vuông; người dùng vẫn kéo được vạch chia và tỷ lệ đã chọn được duy trì khi đổi kích thước, không còn cột điều khiển cố định 360 px hoặc preview cố định 760×520.
- Hai bảng chính của màn hình workspace cũng giữ tỷ lệ khi cửa sổ phóng to/thu nhỏ, đồng thời vẫn tôn trọng kích thước tối thiểu của từng bên. Hai nút hẹn giờ được phân cấp rõ: **Bắt đầu** màu thành công, **Hủy** dạng danger phụ; khi lịch hoạt động, nút bắt đầu đổi thành **Đang canh lịch** và bị khóa.
- Bản phát hành tiếp tục dùng giao diện native đã kiểm thử của DocuRandom. Phần thử nghiệm shadcn/WebView không được đóng gói, không thêm runtime trình duyệt và không thay đổi luồng workflow, render, hẹn giờ hoặc khóa chống đăng trùng YouTube.
- Bản `2.3.69` thêm trình xem trước đúng khung video thành phẩm cho CTA và logo: mô phỏng nền mờ + media chính hoặc chế độ cắt đầy khung, cho phép kéo trực tiếp để đổi vị trí, chỉnh kích thước và chỉnh độ rõ logo từ 0–100%.
- CTA nhận video/ảnh do người dùng cung cấp và tự nhận diện nền xanh, nền đen hoặc nền trong suốt. Preview và pipeline FFmpeg dùng cùng lựa chọn; chroma/black key có mép mềm và khử tràn màu trước khi đóng trực tiếp vào MP4 cuối.
- Tệp CTA/logo được sao chép vào vùng tài sản bền vững riêng của workspace. Render tay, workflow lặp và workflow hẹn giờ đều chụp cùng cấu hình; lưu kho/dọn `Media` sau upload không làm mất tài sản.
- Bộ hẹn giờ có hai hành động rõ ràng **Bắt đầu hẹn giờ** và **Hủy hẹn giờ**. Tick chỉ chọn lịch; tool chỉ kích hoạt sau khi bấm Bắt đầu. Khi hủy, ngày giờ và lựa chọn lặp vẫn được giữ để dùng lại, đồng thời trạng thái hoạt động luôn hiển thị trên giao diện.
- Bản `2.3.68` sửa lỗi precheck ChatGPT Web bị mắc kẹt vì bộ cài 2.3.67 ghi phiên bản bridge 1.0.20 nhưng vô tình mang `app.asar` 1.0.18. Gói mới chứa đúng ChatGPT Web Bridge 1.0.20, tự đóng bridge cũ khi rảnh, mở lại bridge mới và giữ nguyên profile/phiên đăng nhập.
- Quy trình phát hành nay đọc trực tiếp cả `package.json` và phiên bản runtime bên trong `app.asar`, đối chiếu với source rồi mới khóa SHA-256. Build sẽ dừng ngay nếu bridge đóng gói cũ hoặc hai phiên bản bên trong không khớp, nên lỗi gắn nhãn mới lên payload cũ không thể lọt qua lần nữa.
- Bản `2.3.67` thay hoàn toàn hai ô gõ ngày giờ bằng Date & Time Picker trực quan theo giao diện tối của DocuRandom. Người dùng bấm lịch để chọn ngày, chuyển tháng, chọn giờ/phút hoặc dùng Hôm nay/Ngày mai/Bây giờ; giá trị được chuẩn hóa theo giờ Việt Nam nên không còn lỗi sai định dạng.
- Workflow và YouTube có nút **Chọn ngày & giờ** riêng, vẫn giữ công tắc bật/tắt và **Lặp hằng ngày** độc lập. Các ô kết quả là chỉ đọc; bấm trực tiếp vào ô cũng mở picker.
- Bản `2.3.66` thêm hai lịch độc lập cho từng workspace theo giờ Việt Nam (GMT+7): một lịch khởi động toàn bộ workflow + render, một lịch upload YouTube. Mỗi lịch có thể tắt, chạy một lần hoặc lặp hằng ngày; nếu giờ upload đến trước khi video xong, tool chỉ chờ đầu ra hoàn chỉnh rồi mới nhận đúng một lượt upload.
- Thêm ô chọn thumbnail trực tiếp trong phần YouTube. Ảnh PNG/JPG/JPEG/WEBP hợp lệ được gắn cùng video; ảnh bị xóa hoặc không chọn sẽ được bỏ qua để video vẫn upload bình thường. Cơ chế tự tìm thumbnail cũ trong `Output`/`Media` vẫn được giữ.
- Sau khi YouTube trả URL và checkpoint đã xác nhận `complete`, tool tự lưu toàn bộ `Output`, `Media`, `VOICEVOX`, bản sao `Prompts`, checkpoint và log vào `Uploaded\\<ngày-giờ-tạo-video>_...`. Video và gói nội dung trong kho được đổi tên theo giờ tạo ở Việt Nam, có manifest kèm URL/kênh/profile để tra cứu.
- Chỉ sau khi lưu kho hoàn tất, workspace mới được làm sạch để phiên kế tiếp dùng lại cùng tên output mà không ghi đè dữ liệu cũ. Cấu hình, Master Prompt, lịch, profile YouTube, lịch sử render và sổ chống đăng trùng được giữ lại. Trạng thái `uncertain` hoặc chưa có URL tuyệt đối không di chuyển/xóa dữ liệu.
- Sửa lỗi YouTube Studio trả DOM lớn hơn 1 MiB làm WebSocket CDP đóng mã `1009`. Kết nối CDP chỉ chấp nhận loopback và không còn đặt giới hạn kích thước thông điệp, nên trang Studio nặng không làm ngắt phiên.
- Bản `2.3.65` tự nhận checkpoint lỗi sớm ở 20–25%, dò tất cả tab Studio trong đúng profile và chỉ tiếp tục khi file đang gắn khớp chính xác cả tên lẫn số byte. Nhánh khôi phục không gọi lệnh gắn video; nếu tab cũ không còn, tool dừng và giữ khóa thay vì tạo lượt thứ hai.
- Nút YouTube đổi thành **TIẾP TỤC PHIÊN ĐÃ GẮN** khi đủ điều kiện khôi phục, sau đó điền metadata, chờ upload, bấm Lưu đúng một lần và ghi URL xác nhận như luồng bình thường.
- DocuRandom Studio nay tự cập nhật ẩn danh từ Release public `JustHari01/DocuRandom-DN`: không có màn hình đăng nhập GitHub, không nhận/lưu token. Kênh dùng asset riêng và vẫn bắt buộc chữ ký Ed25519, SHA-256, kích thước và kiểm tra cấu trúc trước khi thay thế.
- Donut Local đã mở Developer Tools/MCP chuyển sang kênh installer public `JustHari01/DocuRandom-DN`. EXE DocuRandom không còn nhúng source/toolchain hay installer Donut, giúp nhẹ hơn; máy build phát hành một asset duy nhất theo mẫu `DocuRandom-DN_X.Y.Z_x64-setup.exe`.
- Máy chưa có `DocuRandom-DN` chỉ hiện **CÀI ĐẶT DONUT LOCAL**; máy đã cài chỉ hiện **CẬP NHẬT DONUT LOCAL**. Tool tự đọc Release, so phiên bản với Registry và hỏi người dùng khi có bản mới.
- Sau xác nhận, tool tải installer đúng một lần, đối chiếu kích thước và digest SHA-256 do GitHub công bố, yêu cầu Donut đóng toàn bộ profile rồi kết thúc cây tiến trình còn sót, cài silent, đọc lại Registry và mở lại MCP. Mọi lỗi dừng ngay; profile/cookie/proxy tại `%LOCALAPPDATA%\DonutBrowser` không bị xóa.

- Upload YouTube nay dùng trực tiếp Donut MCP/CDP local theo DOM/Accessibility, không dùng tọa độ chuột, không phụ thuộc kích thước cửa sổ/ngôn ngữ và không cần extension. Tool luôn đưa tab chính về YouTube Studio, chỉ nhận đúng ô video `Filedata` và từ chối nhầm ô thumbnail.
- Thêm khóa chống đăng trùng toàn cục bằng SHA-256 nội dung video + profile. Khóa được ghi bền vững trước đúng một lần gắn file; đổi tên/chuyển workspace hoặc bấm Upload lại không thể tạo lượt thứ hai.
- Sau khi file đã được gắn, mọi lỗi proxy, logout, chuyển trang, cảnh báo YouTube, metadata không khớp hoặc mất kết nối đều chuyển sang **Cần xác minh** và khóa thử lại tự động. Tool chỉ ghi hoàn tất khi lấy được đường dẫn video xác nhận từ chính hộp upload.
- Lỗi mạng Chromium/`ERR_PROXY_*`, trang Google đăng nhập và thông báo `alert` ở bất kỳ ngôn ngữ nào được đưa nguyên nhân về giao diện/checkpoint, thay vì treo chờ hoặc tiếp tục thao tác mù.

- Kênh cập nhật private trước đây đã được thay hoàn toàn bằng Release public chỉ đọc. Máy khách không cần tài khoản, token hay thao tác đăng nhập GitHub.
- Updater DocuRandom tải asset công khai qua GitHub API và vẫn xác minh Ed25519, SHA-256, kích thước cùng cấu trúc gói trước khi cài đè.
- Nút cập nhật kiểm tra trực tiếp trong nền; lỗi mạng chỉ được báo về tool và không làm thay đổi bản đang dùng.
- Donut Local vẫn cài tách biệt, không ghi đè chương trình Donut chính thức; tool chỉ chấp nhận đúng danh tính `donut-browser-local`, đủ MCP/CDP bắt buộc và chữ ký hợp lệ.

- Thay thành phần Donut đóng kèm bằng **Donut Local**: GitHub kiểm tra tag stable chính thức hằng ngày, áp dụng bản vá tích hợp có phạm vi cố định, chạy test, build Windows và phát hành gói có chữ ký Ed25519 cùng mã nguồn AGPL tương ứng.
- Máy khách chỉ đọc catalog Donut Local, xác minh chữ ký, kích thước và SHA-256 trước khi cài nguyên tử vào `Components\DonutLocal`; tuyệt đối không tải nhầm gói Donut chính thức để thay cho Local.
- Khi Donut Local có bản mới, tool tự báo phiên bản và changelog. Người dùng xác nhận một lần; tool tải/cài, chỉ đóng đúng `DonutLocal.exe` nằm trong thư mục do tool quản lý, tự mở lại và xác nhận MCP có tên `donut-browser-local`.
- Release component Donut Local dùng kênh riêng, không được phép chiếm mục `Latest` của DocuRandom Studio; vì vậy updater của các bản tool cũ không đọc nhầm component thành bản cập nhật ứng dụng.
- Donut Local tiếp tục dùng chung `%LOCALAPPDATA%\DonutBrowser`, nên profile, cookie, proxy, fingerprint, browser/GeoIP và đăng nhập được giữ nguyên. Quyền MCP/automation vẫn theo tài khoản Donut thật, không giả mạo hay bỏ qua entitlement.

- Mở lại Donut MCP và giao diện upload YouTube trong màn hình workspace/bảng dựng video. Công cụ đọc đúng profile đã chọn, giữ checkpoint riêng cho từng workspace và điền metadata vào đúng video.
- Bổ sung tiêu đề phân đoạn `SEO tags` ngay sau `Mô tả` trong yêu cầu xuất file Tổng hợp. Tool tách riêng nội dung của mục này để điền đúng ô Tags của YouTube Studio, không trộn vào mô tả, prompt thumbnail hoặc prompt hình ảnh.
- File Tổng hợp được chuẩn hóa với thứ tự: Tiêu đề → Mô tả → SEO tags → Prompt Thumbnail → Thoại VOICEVOX → Prompt hình ảnh. Bộ đọc vẫn tương thích với cách ghi `SEO TAG`, `SEO TAGS` và checkpoint cũ.

- Bổ sung nguồn FFmpeg dự phòng độc lập để bản portable vẫn đóng gói và máy mới vẫn tự chuẩn bị bộ render khi máy chủ tải chính tạm thời lỗi hoặc trả HTTP 503.

- Tạm ẩn toàn bộ Donut MCP và auto đăng YouTube khỏi màn hình workspace lẫn bảng điều khiển dựng video. Tool không kiểm tra MCP, không đọc profile Donut và không tự upload sau render trong bản này.
- Cờ auto-upload cũ được ép tắt an toàn; dữ liệu kết nối, lựa chọn profile và checkpoint YouTube đã có vẫn được giữ cục bộ để có thể phục hồi nếu tính năng được thiết kế lại và bật lại sau.
- Gói phát hành không kèm Browser Bridge thử nghiệm. Các workflow ChatGPT, Flow, VOICEVOX, watermark và render video không bị ảnh hưởng.

- Sửa lỗi Flow thỉnh thoảng giật giao diện làm điểm bấm ô prompt lệch sang thumbnail rồi mở màn hình xem ảnh. Flow Agent 1.3.6 nay focus trực tiếp đúng phần tử ô nhập, xác minh focus trước khi chèn prompt, tự nhấn Escape để thoát màn hình xem ảnh và chỉ bấm các nút khác sau khi vị trí đã đứng yên.
- Bổ sung kiến trúc giống Flow Automation: catalog selector có phiên bản nằm local, đầy đủ quyền `debugger`, `cookies`, `tabs`, `downloads`; nếu UI hỏng trước khi gửi prompt, extension giữ nguyên cookie đăng nhập, làm mới local state/cache của Flow và thử lại đúng một lần.
- Cơ chế mới được đối chiếu với Flow Automation 3.3.0: dùng selector `div[role="textbox"]`, tự ổn định Flow ở bố cục lưới/tile cố định, bật hiển thị chữ và xóa prompt cũ, chờ ngẫu nhiên trước/sau khi chèn chữ bằng sự kiện bàn phím của trình duyệt; phần triển khai trong DocuRandom là mã độc lập và bổ sung kiểm tra chống bấm nhầm chặt hơn.

- Khi gặp lỗi trên, prompt ảnh chưa gửi vẫn được giữ nguyên trong checkpoint. Sau khi cập nhật/nạp lại Flow Agent, bấm **Tiếp tục** sẽ đi thẳng từ prompt đang dở; không chạy lại ChatGPT, không xuất lại file Tổng hợp và không làm mất các ảnh đã hoàn thành.

- Sửa trường hợp Flow backend, Edge, extension và phiên đăng nhập đều đang hoạt động nhưng service worker cũ trả `extension_version` rỗng, khiến giao diện báo sai **Đang chờ phiên Google Flow**. Phiên legacy có Flow key hợp lệ giờ được dùng ngay; chỉ phiên báo rõ một phiên bản khác mới bị yêu cầu nạp lại extension.

- Trạng thái không tương thích extension giờ hiển thị đúng **Flow Agent cần nạp lại** cùng nguyên nhân thật, thay vì rơi vào thông báo chung yêu cầu mở/refresh Flow.

- Sửa lỗi precheck Flow có thể lặp vô hạn và liên tục mở thêm tab Edge/Google Flow khi token API cũ chưa lấy được. Flow Agent 1.3.3 nhận đúng khả năng tạo ảnh qua giao diện Flow đang mở, nên phiên UI hợp lệ không còn bị báo chưa sẵn sàng chỉ vì thiếu token API không cần thiết.

- Mọi workspace trong cùng tool dùng chung khóa mở Edge: chạy nhiều workspace đồng thời chỉ được mở tối đa một tab Flow trong 90 giây. Các lần poll, watchdog và reconnect của extension hoàn toàn thụ động, không được tự sinh tab mới.

- Extension giữ nguyên tab Google Sign-in nếu Flow đang chuyển hướng để đăng nhập; precheck không còn ghi đè trang đăng nhập, refresh vòng lặp hoặc mở cửa sổ mới. Reconnect chỉ refresh khi trình duyệt báo chắc chắn đã có tab Flow.

- Tích hợp đăng YouTube qua **Donut Browser MCP** ngay trong DocuRandom. Kết nối MCP là cài đặt toàn cục ở màn hình workspace; người dùng tự dán URL local rồi bấm **Test kết nối**. Tool chỉ báo thành công sau khi khởi tạo MCP thật và đọc được group/profile; nếu Donut chưa cài/chưa mở/chưa bật MCP, giao diện báo rõ và có nút tải Donut Browser.

- URL/token MCP không được lưu trong workspace hoặc log. Tool chỉ chấp nhận địa chỉ loopback trên chính máy, mã hóa khóa bằng Windows DPAPI theo tài khoản đang dùng và chỉ hiện phần đuôi đã che. Mỗi workspace chỉ lưu ID group/profile, quyền riêng tư và lựa chọn khai báo AI.

- Mỗi workspace chọn chính xác **group → profile/kênh Donut**. Tool chỉ đưa profile Wayfern/Camoufox có thể điều khiển vào danh sách, hiển thị trạng thái proxy mà không lộ thông tin đăng nhập proxy, và tự mở profile đã chọn tới YouTube Studio khi cần.

- Upload luôn khóa đúng video MP4 và file TXT Tổng hợp thuộc thư mục Output/checkpoint của chính workspace; từ chối đường dẫn ngoài workspace và không tìm theo “file mới nhất” trên máy. Tiêu đề, mô tả, hashtag SEO, từ khóa SEO và thumbnail được đọc từ gói Tổng hợp; lựa chọn khai báo nội dung chỉnh sửa/tổng hợp bằng AI được áp dụng trước khi lưu/xuất bản.

- Mặc định YouTube là **Riêng tư**. Có thể upload thủ công hoặc tự upload sau render, theo dõi phần trăm thời gian thực và hủy riêng. Các workspace dùng cùng một profile Donut được xếp hàng FIFO; profile khác có thể chạy song song, nên tab/profile/video không bị điều khiển chéo.

- Thêm checkpoint riêng `youtube_upload.json`: màn hình workspace tiếp tục hiển thị upload đã xong, đang dở hay có lỗi sau khi tắt/mở tool; bấm Upload/Tiếp tục dùng lại đúng workspace. Toàn bộ bước MCP, chọn nguồn, profile, CDP, metadata và lỗi YouTube được ghi vào log có file/dòng/hàm nhưng không ghi khóa MCP/cookie.

- Nâng log chẩn đoán lên dạng có cấu trúc: ghi thời gian tới mili-giây, tiến trình, luồng chạy, workspace, job, file/dòng code, tên hàm và toàn bộ stack trace khi lỗi. Các nhánh ChatGPT, Flow, VOICEVOX, quét media, audio, watermark, FFmpeg/render, checkpoint, cập nhật và quản lý workspace đều ghi nguyên nhân thật trước khi trả thông báo ngắn lên giao diện.

- Bổ sung log cấp ứng dụng và Flow backend tại thư mục `Data/Logs`, mỗi kênh chỉ giữ file hiện tại cùng một bản xoay vòng tối đa 5 MB. Lỗi ngoài callback, lỗi thread nền, lỗi Tk và lỗi Python không thể ném ra ngoài đều được thu nhận thay vì biến mất không dấu vết.

- Log tự che URL token MCP, Bearer/OAuth token, API key, mật khẩu, secret và cookie. Nội dung Master Prompt, cookie đăng nhập và dữ liệu tài khoản không được ghi chỉ để phục vụ chẩn đoán.

- Sửa lỗi sau cập nhật khiến tool nhận đủ prompt ảnh nhưng tiện ích không nhập/gửi được vào Google Flow rồi báo sai **bỏ qua 30/30**. Flow Agent 1.3.2 nhận diện ô soạn thảo và nút gửi mới, dùng phím Enter thật kèm nhấp dự phòng, đồng thời chỉ xác nhận đã gửi khi giao diện thực sự bắt đầu tạo.

- Nếu giao diện Flow thay đổi hoặc tiện ích không gửi được prompt, workflow dừng ngay tại prompt hiện tại và giữ nguyên toàn bộ checkpoint; không còn đi giả qua 30 prompt. Nút **Tiếp tục** dùng mã lượt mới để thử thật, không phát lại kết quả lỗi cũ từ bộ nhớ idempotency.

- Tool kiểm tra phiên bản tiện ích đang thực sự chạy trong Edge. Nếu Edge còn giữ script của bản cũ sau khi tool cập nhật, precheck yêu cầu đóng/mở Edge đúng một lần thay vì báo xanh giả rồi làm mất lượt prompt.

- Bổ sung log tiến trình Flow theo từng bước và nguyên nhân thật. Đã kiểm chứng trực tiếp bằng Flow trên máy thật: prompt được nhập, gửi, tạo ảnh 16:9 và tải thành công về đúng thư mục workspace.

- Thêm **thư mục chủ đề** ngay tại màn hình workspace: có thể tạo, đổi tên và xóa thư mục; workspace được nhóm rõ theo từng chủ đề và có khu vực **Chưa phân loại**.

- Mỗi workspace có ô tích chọn và tay nắm kéo `⠿`. Người dùng có thể chọn nhiều workspace rồi kéo một lần vào thanh tên thư mục; thứ tự thao tác không làm đổi đường dẫn thật, media, video, VOICEVOX hay checkpoint.

- Xóa thư mục chủ đề chỉ đưa các workspace bên trong về **Chưa phân loại**, không xóa hoặc di chuyển bất kỳ dữ liệu dự án nào trên ổ đĩa. Phân loại được lưu bền vững và tự phục hồi từ bản backup registry.

- Thêm nút **Mở file Tổng hợp** trên từng thẻ workspace. Tool mở đúng file TXT đã ghi trong checkpoint; workspace cũ chưa có đường dẫn checkpoint vẫn tự tìm gói tổng hợp mới nhất và bỏ qua file chat phụ.

- Nút **Tiếp tục** của ChatGPT giờ kiểm tra đúng phản hồi đã gửi nhưng chưa kịp ghi checkpoint: Bridge mở lại URL chat riêng của workspace, đọc tiếp DOM nếu ChatGPT còn viết, hoặc thu hồi nguyên phản hồi nếu ChatGPT đã xong. Chỉ sau khi lưu câu trả lời vào checkpoint tool mới được gửi câu tiếp theo; nếu Bridge chứng minh tin trước chưa từng được submit thì mới gửi lại chính tin đó đúng một lần. Không gửi lại Master Prompt/**TIẾP** đã được ChatGPT nhận và không đổi sang phiên khác.

- Bridge 1.0.20 lưu cục bộ URL chat cùng bằng chứng lượt đang dở theo `thread_id`, nên có thể phục hồi đúng DOM sau khi Bridge mở lại. Precheck chỉ kiểm tra phiên đăng nhập/dịch vụ; workspace khác đang chạy không còn làm nút **Tiếp tục** mắc ở **ChatGPT Web đang kiểm tra**.

- Sửa lỗi máy khách `ChatGPT did not finish the response before the timeout`: phản hồi Thinking/Extended còn tăng nội dung sẽ tiếp tục được theo dõi, chỉ dừng khi thật sự không tiến triển; vẫn có giới hạn an toàn tuyệt đối để không treo vô hạn.

- Cho 10+ workspace giữ các phiên ChatGPT Web, URL chat và checkpoint riêng; số lượt sinh thật sự được chạy chồng theo cấu hình máy (1–10 phiên theo CPU/RAM). Lượt vượt sức máy vẫn ở trạng thái **Đang làm · chờ lượt**, có hiển thị số phiên đang chạy và vị trí chờ; không ép toàn bộ tool về một phiên tuần tự.

- Bridge 1.0.19 tách rõ **đang có phiên chạy** với **đã đầy công suất**, nên phiên thứ hai không còn bị chặn chỉ vì phiên thứ nhất đang viết. Bridge cũng không tính các chat cũ đang rảnh là “hết chỗ”: cửa sổ rảnh được thu hồi theo LRU nhưng URL cuộc chat vẫn được giữ; khi workspace tiếp tục, tool mở lại đúng chat đó thay vì báo nhầm **Cần thiết lập/đăng nhập ChatGPT Web**.

- Hàng đợi Flow vẫn khóa theo nguyên batch workspace. Prompt, `media_id`, checkpoint và thư mục `Media` được giữ riêng; ảnh hoàn thành nhanh/chậm hoặc lỗi ngay trước lượt workspace kế tiếp không thể bị lưu chéo. Có kiểm thử hồi quy riêng cho hai workspace liên tiếp.

- Sửa vòng lặp sau câu kiểm tra prompt ảnh: ngay khi ChatGPT trả **ĐÃ XONG** (không phân biệt hoa/thường hoặc dấu câu), tool dừng gửi **TIẾP** và chuyển thẳng sang yêu cầu xuất file tổng hợp. Checkpoint cũ đã dừng đúng tại **ĐÃ XONG** cũng được phục hồi vào bước xuất cuối, không lặp lại.

- Việc kiểm tra đủ tiêu đề, mô tả, thumbnail, thoại và prompt ảnh được chuyển về sau phản hồi xuất tổng hợp cuối. Nếu file cuối thật sự thiếu, tool giữ checkpoint và báo phần thiếu; tuyệt đối không quay ngược vào vòng **TIẾP**.

- Hiển thị trạng thái bền vững ngay trên từng thẻ workspace: **Đã render xong**, **Đang làm**, **Chưa hoàn tất**, **Có lỗi** hoặc **Chưa bắt đầu**; màu trạng thái, phần trăm và tên video vẫn đúng sau khi tắt/mở tool.

- Danh sách workspace giờ đọc đồng thời checkpoint, lịch sử render và file video thật trong thư mục Output. Chỉ gắn nhãn hoàn tất khi tìm thấy video hợp lệ; nếu AI/VOICEVOX mới xong nhưng chưa render thì hiển thị rõ là chưa hoàn tất.

- Thêm tìm kiếm theo tên workspace/tên video, bộ lọc trạng thái và sắp xếp theo việc cần xử lý, thời gian cập nhật, tên hoặc video mới nhất; mỗi thẻ có nút **Xem video** riêng.

- Dùng chung một bộ điều phối precheck ChatGPT cho mọi workspace: các workspace không còn đồng thời kiểm tra/refresh cùng một Bridge, kết quả kiểm tra gần nhất được chia sẻ an toàn và các lượt chờ được gom để tránh gây lag phiên.

- Khi ChatGPT đang phục vụ workspace khác, trạng thái vẫn giữ màu xanh **Phiên vẫn tốt** và workspace mới tự xếp hàng. Một lần kiểm tra DOM/runtime chập chờn được giữ trạng thái đã xác minh trong thời gian ngắn nhưng không thể kéo dài trạng thái đăng nhập cũ vô hạn.

- Sửa lỗi ngẫu nhiên workflow đứng im sau khi bấm chạy: precheck giờ có bộ điều phối riêng, nhận cả kết quả kiểm tra nền đã chạy trước đó và luôn đánh thức bước tiếp theo khi ChatGPT/Codex, VOICEVOX và Flow đã sẵn sàng.

- Thêm watchdog cho từng nhánh precheck: nếu callback hoặc tiến trình kiểm tra bị kẹt, tool tự nối lại một lần; nếu vẫn không phản hồi thì dừng có kiểm soát, giữ checkpoint và chỉ rõ chính xác thành phần lỗi thay vì treo vô hạn.

- Log giờ ghi toàn bộ vòng đời workflow/precheck cùng trạng thái chờ theo thời gian thực. Mỗi workspace chỉ giữ `diagnostic.log` của phiên đang mở và một `latest-YYYYMMDD-HHMMSS.log` của phiên gần nhất; đóng tool sẽ tự chốt log, mở lại bắt đầu file mới và không tạo rác.

- Khóa tuyệt đối vị trí và kích thước khung media chính: ảnh tĩnh được zoom/pan nhẹ **bên trong** khung; video chỉ phát nội dung tự nhiên bên trong khung. Chỉ lớp background phóng lớn/làm mờ được pan và dao động độc lập.

- Thêm preset mặc định **Mượt đa dạng · 42 mẫu**, áp dụng cho mọi cặp ảnh/video; loại các chuyển cảnh pixel/squeeze/wind/slice gây mỏi mắt khỏi mặc định nhưng vẫn giữ chúng trong preset Năng động/Toàn bộ khi người dùng chủ động chọn.

- Random bảng màu và effect riêng theo từng cảnh, không còn phủ một tông màu cho toàn bộ video; tránh lặp bảng màu ở hai cảnh liền nhau. Màu/effect chỉ áp vào media trong khung, không nhuộm lớp nền mờ hoặc subtitle.

- Bổ sung sáu lớp màu tùy chọn (archival warmth, silver retention, soft newsprint, neutral film, bleach bypass và broadcast documentary); preset Dày không còn tự chèn VHS, motion trail hoặc pixel pulse.

- Sau khi dùng EXE cài/cập nhật thủ công, launcher tải về tự xóa an toàn khi ứng dụng mới đã được đặt vào thư mục quản lý; Desktop chỉ còn một lối tắt **DocuRandom Studio**, không nhân thêm hai biểu tượng giống nhau.

- Sửa lỗi đóng gói khiến tool yêu cầu ChatGPT Bridge 1.0.18 nhưng EXE thực tế vẫn mang 1.0.16; bản phát hành giờ chứa đúng Bridge 1.0.18 và khóa SHA-256 mới.

- Đồng bộ vòng đời Bridge với tool: lúc khởi động tự đóng các Bridge DocuRandom cũ còn treo rồi chỉ mở một phiên đúng profile; lúc đóng hẳn tool sẽ đóng cả Bridge hiện tại và Bridge legacy nhưng giữ nguyên profile/cookie đăng nhập trên ổ đĩa.

- Việc chuyển qua lại giữa các workspace chỉ ẩn/hiện giao diện, không chạy shutdown Bridge và không khởi tạo lại tác vụ; workspace đang làm tiếp tục chạy nền, giữ phần trăm cùng checkpoint riêng.

- Sửa trường hợp máy khách đã đăng nhập ChatGPT nhưng kiểm tra giao diện nghiêm ngặt báo âm giả: tool đối chiếu thêm trạng thái sống từ Bridge có token cục bộ, chỉ chấp nhận khi profile xác nhận chat thường có lưu và vẫn từ chối Temporary Chat.

- Ghi trạng thái kiểm tra ChatGPT (`running/authenticated/runtime/busy/bridge`) vào log chẩn đoán mà không ghi access token, cookie hay nội dung tài khoản, giúp xác định lỗi trên máy khách từ xa.

- Tối ưu thao tác kéo co giãn bảng điều khiển: thanh chia ngang/dọc dùng chế độ kéo nhẹ, chỉ bố trí lại toàn bộ nội dung một lần khi thả chuột nên không còn giật theo từng pixel.

- Gom các đợt cập nhật kích thước và vùng cuộn của phần cài đặt, danh sách tác vụ và workspace theo nhịp; cuộn chuột ổn định hơn, không làm giao diện tính toán lặp dồn dập.

- Giao diện tự scale theo độ phân giải màn hình trong giới hạn an toàn; kích thước cửa sổ, chữ và giới hạn các bảng giữ tỷ lệ hợp lý trên màn hình nhỏ lẫn 2K/4K.

- Thêm preset VOICEVOX **Custom**: tự chuyển sang Custom khi người dùng sửa bất kỳ thông số nào; chọn preset chỉ xem/gợi ý, chỉ thay thông số sau khi bấm **Áp dụng**.

- Nút kiểm tra ChatGPT giờ hiện trạng thái đang kiểm tra và hộp xác nhận rõ phiên đăng nhập, Bridge, Codex CLI cùng model khi mọi thành phần đã sẵn sàng.

- Gỡ phần **Mục tiêu/góc nhìn biên tập** và ô xác nhận quyền khỏi giao diện/quy trình kiểm tra theo yêu cầu.

- Tăng bảo vệ dữ liệu qua cập nhật: App vẫn tách riêng khỏi Data/Workspaces, tự nhập dữ liệu cũ còn thiếu, giữ bản backup registry và tự phục hồi workspace từ metadata nằm ngay trong từng thư mục dự án.

- Flow Agent 1.3.1 tạo ảnh trực tiếp bằng giao diện Google Flow, tự tạo dự án, nhập prompt, chờ kết quả và tải ảnh về đúng workspace.

- Tự chuyển một lần giữa **Nano Banana Pro** và **Nano Banana 2 Lite** khi model đang dùng chạm giới hạn tạo ảnh hằng ngày; prompt vừa lỗi được thử lại nguyên vẹn trên model còn lại.

- Phân biệt giới hạn model với CAPTCHA, giới hạn tốc độ, lỗi chính sách và hết credit toàn tài khoản; nếu cả hai model đều hết lượt, tác vụ dừng rõ ràng và giữ checkpoint, không lặp vô hạn.

- Danh sách prompt `Image 1.`, `Image 2.`… tiếp tục được xử lý tuần tự theo hàng chờ FIFO toàn cục, cách nhau ngẫu nhiên 3–5 giây và không trộn ảnh giữa các workspace.

- Làm nền mờ chuyển động rõ theo cả trái/phải và lên/xuống, kết hợp pan ngẫu nhiên và dao động riêng cho từng cảnh; áp dụng đồng nhất cho cả ảnh lẫn video.

- Căn giữa phụ đề một dòng và hai dòng theo cùng tâm của dải nền; dải nền khít đúng chiều rộng khung media 90%, chữ không tràn khỏi khung.

- Đổi subtitle tiếng Nhật sang Yu Gothic Bold, giữ nguyên cỡ chữ, viền mờ, nền tối và vùng an toàn đã duyệt.

- Sửa triệt để subtitle bị phóng đại: chuyển SRT sang ASS với PlayRes đúng bằng video đầu ra, giữ chữ trắng viền đen mờ, nền tối, tối đa hai dòng và vị trí theo mẫu chuẩn.

- Cho phép VOICEVOX chạy đồng thời giữa nhiều workspace (tự chọn 1–3 luồng theo CPU/RAM); Flow vẫn giữ hàng đợi FIFO riêng. Mỗi workspace chỉ tạo một narration tại một thời điểm, WAV/SRT/checkpoint không bị dùng lẫn.

- Bổ sung lớp kiểm tra cửa sổ Edge trực tiếp từ Windows: cả extension cũ còn treo nền sau cập nhật cũng không thể làm trạng thái Flow xanh giả.

- Sửa trạng thái Flow xanh giả khi đã đóng Edge: extension tự ngắt phiên khi không còn cửa sổ Edge, backend loại kết nối nền/stale và tự nối lại khi mở Edge mà không cần khởi động lại tool.

- Sửa nút **Tiếp tục** sau CAPTCHA/khởi động lại: xác minh checkpoint nội dung rồi bỏ qua hoàn toàn ChatGPT/Codex, Flow phục hồi ảnh đã có và chạy tiếp đúng prompt còn dở.

- Tăng nhẹ độ dày viền đen của chữ phụ đề để chữ Nhật nổi rõ hơn trên nền nhiều chi tiết.

- Hạ dải phụ đề sát mép dưới khung ảnh chính hơn, giữ nguyên chữ trắng viền đen và nền mờ một lớp.

- Sửa subtitle: dùng panel đen mờ ngang ở phần dưới như mẫu tham chiếu, chữ trắng viền đen, căn giữa và không còn các ô đen riêng từng dòng.

- Nền của media video giờ phóng lớn 18%, làm mờ và pan độc lập theo chuyển động từng cảnh; nền không còn đứng yên phía sau lớp media chính.

- Media chính trong nền mờ giờ lấp đầy khung 90% (viền khoảng 10%, crop rất nhẹ theo tỷ lệ) thay vì co theo chiều hẹp, để lớp nền chuyển động phía sau nhìn rõ hơn; nền sau vẫn phóng lớn và mờ.

- Chế độ **Giữ trọn media + nền mờ** đặt media chính ở 90% canvas (viền khoảng 10%) và dùng cùng media phóng lớn/làm mờ ở phía sau, tạo bố cục “khung tranh chuyển động” cho cả ảnh lẫn video.

- Sửa đường dẫn cache template trên Windows khi thư mục tạm dùng tên 8.3; bản sạch luôn được ghi đúng thư mục workspace đã chọn.

- Mặc định bật crop template Flow ở mép phải (`120/1376`), không cần cài engine AI; bản sạch được lưu trong `Media\\_watermark_clean` và được dùng trực tiếp khi render.
- Mặc định giữ trọn ảnh/video bằng nền mờ trên canvas YouTube 16:9 (1920×1080), tránh mất nội dung sau crop; có thể đổi sang **Cắt đầy khung** nếu muốn.
- Gỡ watermark Gemini vẫn là tùy chọn riêng cho watermark không nằm ở mép phải; ảnh gốc không bị ghi đè.

- Ảnh Flow sau khi gỡ watermark được lưu rõ ràng trong `Media\\_watermark_clean` và render dùng chính bản sạch đó; ảnh gốc không bị ghi đè.
- Thêm template crop chung theo ảnh mẫu: cắt 120/1376 phần bên phải, giữ nguyên chiều cao (`1376×768` thành `1256×768`) để loại watermark góc phải dưới còn sót.
- Cache sạch được bỏ qua khi quét media đệ quy để không tạo cảnh trùng.

- Bổ sung log chẩn đoán có timestamp cho từng tác vụ; lỗi render hiển thị mã lỗi và nút **Mở log chẩn đoán**.
- Log được lưu riêng trong `.docurandom/diagnostic.log` của workspace (hoặc thư mục dữ liệu ứng dụng), tự xoay khi vượt 5 MB để không làm đầy ổ đĩa.

- Sửa lỗi máy khách dừng ở bước `Xóa watermark` với thông báo `WindowsPath is not JSON serializable`.
- Checkpoint render và manifest giờ tự chuyển mọi đường dẫn Windows sang chuỗi; có thể bấm **Tiếp tục** và khôi phục cả phụ đề SRT mà không phải làm lại các bước đã hoàn tất.

- Sửa triệt để trường hợp tải cập nhật xong nhưng khởi động lại vẫn ở bản cũ do Windows khóa thư mục `App`.
- Updater chờ tool đóng bình thường; nếu tiến trình bị treo, nó chỉ buộc đóng đúng tiến trình nằm trong thư mục ứng dụng đã xác minh.
- Nếu Windows không cho đổi tên cả thư mục, updater tự chuyển sang cài đè từng file trong `App`, có bản sao rollback an toàn.
- Bản mới phải tự khởi động và xác nhận đúng số phiên bản thì cập nhật mới được coi là thành công; nếu không, tool tự phục hồi bản cũ.
- Gói đã tải và staging được giữ sau lỗi để lần thử tiếp theo không phải tải lại từ đầu.
- Ghi biên nhận kết quả cập nhật và hiển thị rõ **Cập nhật thành công** hoặc nguyên nhân chưa hoàn tất ở lần mở kế tiếp; không còn hiện lỗi kỹ thuật trần như `locked`.
- Giữ toàn bộ sửa lỗi FFmpeg 9 và checkpoint/nút Tiếp tục tác vụ từ bản 2.3.15.
- Biến Donut thành thành phần all-in-one (thiết kế cũ ở bản 2.3.59): EXE từng đóng kèm gói portable chính thức và đặt vào `Components\Donut`. Thiết kế này được thay bằng Donut Local có chữ ký ở bản 2.3.60.
- Nút Donut toàn cục từng kiểm tra gói stable chính thức. Cơ chế hiện hành chỉ nhận catalog Donut Local đã build/test/ký trên GitHub.
- Donut do tool quản lý dùng chung `%LOCALAPPDATA%\DonutBrowser` với Donut chính thức, nên giữ nguyên profile, cookie, proxy, fingerprint, browser/GeoIP và trạng thái đăng nhập. Tool không tạo kho profile thứ hai và không giả mạo/mở khóa quyền gói trả phí.
- Tool tự bật MCP trong cài đặt Donut, tự đọc đúng token/cổng hiện hành, lưu lại bằng Windows DPAPI và kiểm tra thật bằng danh sách group/profile. Không còn bắt người dùng sao chép/dán URL MCP; ô URL chỉ còn là phương án dự phòng.
- Đã kiểm chứng thiết kế dùng chung dữ liệu trên máy với Donut 0.29.6: MCP 51080 đọc đúng 2 group / 11 profile hiện có. Bản Donut Local mới còn xác minh thêm danh tính MCP riêng và chữ ký phát hành trước khi được cài.
- Hotfix `2.3.88` sửa dứt điểm lượt upload YouTube đã lưu trên máy chủ nhưng cửa sổ vẫn quay `Saving...`: DocuRandom đối chiếu trực tiếp đúng video ID, tiêu đề, mô tả, toàn bộ SEO tags và quyền riêng tư trước khi đánh dấu hoàn tất, giữ link/checkpoint nếu giao diện phản hồi chậm, rồi đóng đúng cửa sổ automation do tool mở. Bridge `1.1.10` đọc chính xác tags đã được YouTube biến thành chip, không nhầm ô nhập trống; bộ cài Bridge cho tất cả profile được cập nhật cùng bản tool. Theo dõi realtime bắt buộc chuyển và xác minh khung **60 phút**, đọc hết mọi trang video, đồng thời chỉ video **Công khai** mới được lấy view và gửi đúng link sang 1MM; Không công khai/Private/không xác định đều dừng an toàn. Kênh Donut Local được chuyển khỏi asset public `0.29.6 r1` build sai sang release private `JustHari01/donutbrowser-local`; tool chỉ nhận gói `0.30.0` có `developer-tools`, MCP/REST/CDP và chữ ký hợp lệ, nên bản cũ không còn được đề xuất.
- Bản `2.4.75` sửa hồi quy reader ChatGPT theo hành vi bản cũ: cập nhật checkpoint lần hai sau khi lượt đã được xác nhận để lưu đúng URL `/c/...`, đọc fallback assistant-role trong renderer ẩn, và cho phép thu hồi checkpoint cũ thiếu URL mà không gửi lại prompt. Chat Bridge `1.0.52`.
- Bản `2.4.88` sửa pipeline VOICEVOX AI audit: thời gian xếp hàng ChatGPT không còn làm hết watchdog 15 phút; audit tự động không còn fallback im lặng sang project gốc. Nếu chưa nhận được phản hồi audit, workflow dừng ở VoiceVox và giữ checkpoint, không render giọng chưa được chỉnh. Thumbnail vẫn được xếp ngay sau khi lưu file Tổng hợp và chạy song song với VOICEVOX/Flow.
- Bản `2.4.91` đưa số phiên ChatGPT và số workspace render thành cấu hình dùng chung có lưu bền vững. ChatGPT không còn chỉ có công tắc tuần tự: chọn `N` sẽ cấp đúng tối đa `N` lượt cho các workspace khác nhau, còn giới hạn chống rate-limit vẫn tự giữ hàng. Hàng render toàn tool mở rộng từ 2 lên 12 lane có kiểm soát. Launcher gom Flow, 1MM và kho dữ liệu vào các thẻ hạ tầng co giãn 3/2/1 cột, giữ nguyên mọi nút; danh sách workspace và tiến trình nhận phần không gian còn lại.
- Bản `2.4.92` sửa bố cục launcher: bộ chọn số workspace render và số phiên ChatGPT được ghim lên đầu vùng tiến trình, không còn bị thẻ danh sách workspace đẩy ra ngoài màn hình ở cửa sổ thấp.
- Bản `2.4.94` sửa khóa recovery ChatGPT/thumbnail: Bridge lưu trạng thái đã gửi rõ ràng, đọc đúng cuộc hội thoại trước khi retry, tự giải phóng khóa pre-submit đã chứng minh chưa gửi và chỉ gửi lại đúng một lần; recovery thumbnail giữ chế độ ảnh để lấy dữ liệu ảnh về. Bridge tích hợp nâng lên `1.0.57`.
- Bản `2.4.98` chuyển scheduler YouTube thành một **làn tuần hoàn theo từng workspace**: video A đã render xong được upload **Không công khai**, chỉ sau khi upload xác nhận thành công mới bắt đầu render B. Mỗi mốc 04:30/08:30/13:30 chỉ chuyển đúng video cũ nhất trong hàng đợi sang **Công khai**; sau khi xác nhận Public, nếu video kế tiếp đã sẵn sàng thì upload Unlisted rồi mới render video tiếp theo. Video trễ ca được giữ nguyên và chuyển sang ca sau, không upload dồn, không đăng trùng. Các bản ghi chờ Public cũ hơn lần bật lịch hiện tại được lưu vào `deferred_public_stale.json` thay vì chiếm lượt của video hiện tại; không xóa video hay dữ liệu YouTube.
- Bản `2.4.112` sửa ChatGPT Web dùng chung: trước khi gửi, nếu conversation đã tải xong nhưng SPA chưa gắn composer, Bridge tự reload đúng URL `/c/...` một lần rồi tiếp tục chờ cùng workspace; không mở chat mới và không gửi lại prompt. Upload Master Prompt chỉ được xác nhận sau khi có dấu vết tệp trong UI, nút gửi đã sẵn sàng và trạng thái upload ổn định; lỗi được giữ nguyên checkpoint và log nêu chẩn đoán renderer giới hạn. Bridge `1.0.61`. Không thay đổi Donut/Wayfern, profile, cookie, proxy hoặc dữ liệu workspace.
- Bản `2.4.113` bổ sung tự phục hồi checkpoint cũ bị ChatGPT báo thiếu tệp Master Prompt: tool đối chiếu đúng conversation, không gửi lại câu xác nhận chưa được gửi, gửi lại Master Prompt tối đa một lần với xác minh attachment mới, rồi dừng fail-closed nếu vẫn thiếu. Không thay đổi dữ liệu workspace/profile và không lặp prompt vô hạn.
- Bản `2.4.114` mở rộng chẩn đoán thiếu tệp Master Prompt cho toàn bộ các dạng phản hồi đã gặp: tệp không khả dụng, danh sách tệp trống, tìm kiếm trả về 0 tệp và lượt kết thúc ngay sau probe file. Cơ chế tự sửa vẫn chỉ chạy tối đa một lần cho đúng conversation, sau đó dừng fail-closed nếu attachment chưa được ChatGPT xác nhận; thêm hồi quy cho cả 12 checkpoint hiện có. Không gửi prompt trong kiểm tra chẩn đoán và không thay đổi Donut/Wayfern, profile hoặc dữ liệu workspace.
- Bản `2.4.124` sửa Flow dùng lại tab nền mà extension đang quản lý khi chuyển workspace, thay vì tạo thêm tab chỉ vì tab hiện tại đang mở project khác. Project khớp checkpoint vẫn được ưu tiên; tab Flow đang hiển thị/được người dùng chọn không bị chiếm, và tool không đóng tab chưa xác minh quyền sở hữu. Extension tăng lên `1.3.45`.
# 2.4.219 — tự phục hồi ChatGPT và thumbnail có giới hạn

- Lỗi ChatGPT thiếu DOM phản hồi có tối đa ba lượt tự dò đúng checkpoint, với thời gian chờ và bộ đếm lưu qua restart. Chế độ này không gửi lại prompt đã nhận và không tạo hội thoại mới khi mất checkpoint.
- Thumbnail bị lỗi tạm thời “công cụ không khởi tạo được ảnh” được thử lại một lần trong hội thoại riêng sau 15 phút, khi hai lần chẩn đoán xác nhận phiên cũ rảnh và không có ảnh/pending. Giữ khóa với lỗi chính sách, hạn mức hoặc kết quả chưa rõ.
- Trước khi cấp lượt thumbnail, kiểm tra lại hash package/prompt và bộ đếm; lịch Public và khóa chống upload trùng giữ nguyên.
- Tương thích chẩn đoán thực tế của Bridge 1.0.94: các trường đếm ảnh/lỗi tùy chọn không khóa lượt đã xác nhận không tạo ảnh; so sánh thêm nội dung và kích thước DOM giữa hai lần dò để phát hiện kết quả thay đổi.

# 2.4.220 — giữ điều kiện thumbnail qua khởi động lại

- Khi biến file Tổng hợp chưa được nạp lúc startup, kiểm tra thumbnail dò lại đúng package trên đĩa trước khi cho phép upload tự động. Checkpoint queued được giữ nếu ảnh còn thiếu, không mở profile hoặc chọn tệp.
- Với video đã upload Unlisted trước đây nhưng kho State xác nhận lượt tạo thumbnail kết thúc không có ảnh và queue không có thumbnail, giữ Unlisted thay vì tự chuyển Public. Không tạo upload thứ hai hoặc thay ảnh của video khác.
- Bản `2.4.224` phục hồi import Master Prompt bằng nguyên văn UTF-8 đã kiểm tra SHA-256 khi Bridge xác minh hai lần composer trống sau khi tải lại đúng chat trước lúc gửi. Không upload lại file, không gửi lại lượt đã có checkpoint submission; lỗi quota/đăng nhập và composer còn file vẫn dừng an toàn. Bridge `1.0.98` nhận thêm lỗi khởi tạo ảnh mới của ChatGPT để thử lại đúng một lượt sau khi phản hồi đã kết thúc.
