# Thư viện Master sản xuất 11.1.0-r1

Hai mươi file TXT trong thư viện là Master hoàn chỉnh, độc lập. Mỗi file chứa cả giao thức
chung và module chuyên ngành; không cần gửi file `source/protocol.txt` hay nạp
thêm một Master gốc. Bản G-13 11.0.0-r1 vẫn giữ nguyên để nối các lượt cũ.

| Mã | Nội dung | File dùng trong tool |
|---|---|---|
| G-13 | Máy công cụ và đo lường | `master-G13-11.1.0-r1.txt` |
| G-10 | Đường sắt và hạ tầng | `master-G10-11.1.0-r1.txt` |
| G-09 | Ô tô, hybrid và pin | `master-G09-11.1.0-r1.txt` |
| G-12 | Vật liệu và thiết bị bán dẫn | `master-G12-11.1.0-r1.txt` |
| G-16 | Linh kiện và chuỗi cung ứng | `master-G16-11.1.0-r1.txt` |
| G-11 | Cảng, sân bay và logistics | `master-G11-11.1.0-r1.txt` |
| R-A6 | Tiêu chuẩn và chứng nhận | `master-RA6-11.1.0-r1.txt` |
| R-B2 | Thương hiệu, bảo hành và hậu mãi | `master-RB2-11.1.0-r1.txt` |
| R-B3 | Bằng sáng chế và nguồn gốc công nghệ | `master-RB3-11.1.0-r1.txt` |
| R-B4 | Tri thức ngầm và monozukuri | `master-RB4-11.1.0-r1.txt` |
| R-B5 | Hợp đồng, đấu thầu và hợp tác quốc tế | `master-RB5-11.1.0-r1.txt` |
| R-B6 | Khủng hoảng và khả năng phục hồi hệ thống | `master-RB6-11.1.0-r1.txt` |
| R-C1 | Kỹ sư, chuyển giao và hệ sinh thái | `master-RC1-11.1.0-r1.txt` |
| R-C2 | Tài chính, ngoại hối và thanh khoản | `master-RC2-11.1.0-r1.txt` |
| R-C3 | Ngân hàng, tín dụng và xếp hạng | `master-RC3-11.1.0-r1.txt` |
| R-H3-01 | Năng lượng mới và tài nguyên biển sâu | `master-RH301-11.1.0-r1.txt` |
| R-H3-02 | Hàng không vũ trụ và thăm dò không gian | `master-RH302-11.1.0-r1.txt` |
| R-H3-03 | HVAC, bơm nhiệt và thích ứng khí hậu | `master-RH303-11.1.0-r1.txt` |
| R-H3-04 | Kỹ thuật hậu cần quốc phòng và cứu hộ | `master-RH304-11.1.0-r1.txt` |
| R-H3-05 | Nghệ thuật biểu diễn và công nghệ giải trí | `master-RH305-11.1.0-r1.txt` |

## Cách chọn

Trong workspace, ở ô Master Prompt chọn **Chọn Master theo ngách…**, chọn chủ đề
và **Áp dụng cho lượt mới**. Tool lưu lựa chọn riêng tại
`.docurandom/production-master.json`. Một lần **Tiếp tục** vẫn dùng Master của
video đang dở; file draft, biên nhận, lịch và queue không bị đổi bởi nút chọn.

Có thể chọn **Dùng Master tự dán / nạp TXT**, hoặc nạp một TXT đã tự chỉnh. Việc
nạp TXT/dán/sửa trực tiếp chuyển về chế độ thủ công để thư viện không ghi đè.
Không sửa trực tiếp file trong thư viện đã đóng gói: hash sẽ không còn khớp.
Hãy lưu bản chỉnh riêng rồi nạp TXT nếu muốn tự sửa.

Chỉ DaiTa14 được đặt dùng G-13 mới cho lượt mới trong đợt thử hiện hành. Các
workspace khác giữ lựa chọn cũ; hai mươi ngách là khả năng lựa chọn, không phải lệnh
chuyển các kênh hoặc tự bật lại lịch đang tắt.

## Master đóng vai trò gì trong end-to-end?

1. Tool lấy đúng Master/version/domain/hash, lịch sử nội dung và bộ nhớ học của
   đúng kênh/ngách. ChatGPT nhận Master, xác nhận ĐÃ NẠP; nhận BẮT ĐẦU và số phút.
2. ChatGPT phân tích brief/bằng chứng, đề xuất 10 đề tài, tự chọn một, tạo 5 title,
   tự chọn một, xuất story-context rồi viết thoại tiếng Nhật theo PART/TIẾP.
3. ChatGPT rà lại lời hứa/kịch bản/nguồn và xuất mô tả, SEO, prompt thumbnail,
   prompt ảnh. Tool đọc marker, kiểm đủ trường và lưu file tổng hợp.
4. Code mở chat thumbnail riêng để tạo ảnh thật, lưu URL/biên nhận/hash và kiểm
   OCR + ảnh độc lập. New Master giữ đúng ba dòng màu và hai micro-label phù hợp
   bộ kiểm. Chat viết kịch bản không được trả một câu ĐÃ TẠO thay cho ảnh.
5. Code dùng thoại tạo/audit VOICEVOX và WAV/SRT, đối soát chỗ ASR nghi ngờ. Nhánh
   Flow dùng bộ prompt ảnh; các nhánh có thể song song và dùng lại cache hợp lệ.
   Nhịp ảnh vẫn một ảnh/30 giây, ví dụ 10 phút có 20 prompt.
6. Code xác định chapter theo cue/audio thật, lập timeline, phụ đề, chuyển động,
   nhạc/CTA theo cấu hình, dựng FFmpeg và giữ checkpoint. Master không tự render.
7. Code kiểm đủ video, metadata và thumbnail trước upload, xác minh trạng thái
   truyền/xử lý và dữ liệu server trước Public. Timeout hoặc chữ ĐÃ XONG của AI
   không thể thay cổng kiểm này.
8. Theo cấu hình đã chốt: đến ca A được xác minh Public → B đủ nguyên liệu được
   upload Unlisted → bắt đầu C. Nếu B chưa đủ/không kịp cửa sổ ca, giữ checkpoint;
   Master không tự bỏ ca, đổi lịch hoặc Public thay người điều phối.
9. Bộ học lưu số liệu và phân tích định kỳ vào lịch sử kênh. Lượt mới nhận bài
   học phù hợp khi domain khớp; lượt đang dở không bị chèn một brief mới vào chat.

## Quy trình nào mới, phần nào giữ nguyên?

| Phần | Quy trình trong thư viện mới |
|---|---|
| Giao thức tool | Giữ ĐÃ NẠP, BẮT ĐẦU, số phút, TIẾP, marker và định dạng tổng hợp |
| Chọn nội dung | Phân tích bối cảnh/lịch sử trước 10 đề tài và 5 title; tự chọn như cũ |
| Cách kể | Điều tra, phục hồi, đánh đổi, vòng đời hoặc kết quả hỗn hợp; không ép phản diện/Nhật thắng |
| Phản biện | Đối chiếu brief/nguồn, sửa chọn lọc trước khi xuất; không tạo thêm các cuộc chat đánh giá bắt buộc |
| Chuyên ngành | Sáu bộ family, thuật ngữ, vật chứng, giới hạn đối chứng và hình ảnh khác nhau |
| Voice/sub | Chỉ dẫn cue tối đa 40 ký tự; trần TTS legacy là hàng rào riêng; thời lượng thật do WAV đo |
| Thumbnail | Tạo riêng sau file tổng hợp, giữ URL; hai nhãn phụ tương thích bộ kiểm, không bắt cùng vật thể/bố cục |
| Lịch và vận hành | Giữ trong code; Master không thay lịch, model, profile, cơ chế chống upload/Public trùng |

## Code kiểm soát output như thế nào?

Đường Bridge này dùng kiểm tra tại tool, không dựa vào lời hứa xuất JSON của AI.
Tool cấp `request_id` bền qua restart cùng domain, phiên bản/hash Master và số
ảnh. ChatGPT trả lại JSON `production-contract`; sai ID, sai kiểu dữ liệu hay
thiếu trường đều bị từ chối. Metadata này không được đưa vào thoại.

Code kiểm title có khớp winner đã chọn, JSON bối cảnh có đúng domain, nhãn
verified có khai nguồn, thoại Nhật/cue tối đa 40 ký tự, đúng số prompt ảnh,
ba chuỗi chữ và màu thumbnail khớp hai phần prompt, hai micro-label, SEO và
chapter bắt đầu 00:00 theo thứ tự. Một tín hiệu ĐÃ XONG không thay được các kiểm này.
Nhãn verified có reference vẫn chỉ là lời báo của mô hình; code chưa tự chứng
minh nguồn đó đúng hoặc đủ để kết luận nguyên lý/sự kiện là thật.

Nếu sai, chỉ các trường lỗi được cho phép thay; phần hợp lệ giữ lại từ dữ liệu
đã lưu. Khi sửa cue dài chỉ cho đổi xuống dòng, không đổi chữ. Tool có tối đa
ba lượt sửa nội dung chung, dự trữ trước khi gửi và lưu số lần vào checkpoint.
Lượt đã gửi nhưng chưa rõ kết quả chỉ được thu hồi theo đúng biên nhận, không
tự phát một prompt mới. Mở lại không cấp lại ngân sách.

Gói được chấp nhận có hash và mã lượt trong checkpoint. Khôi phục kiểm lại gói
và nhận biết phần bộ nhớ kênh đã ghép vào Master; không coi dữ liệu học đó là
Master khác để nạp lại. Các Master cũ vẫn dùng giao thức cũ.

Các kiểm trên bảo đảm cấu trúc và quan hệ dữ liệu mà code có thể xác định.
Chúng không bảo đảm câu chuyện hay, nguồn chính xác, ảnh đạt chất lượng hoặc
video tăng view. Kiểm ảnh, audit giọng, đọc trạng thái YouTube và thử thực tế
vẫn là các bước riêng của dây chuyền.

## Dựa trên dữ liệu nào?

`evidence.json` ghi tên và hash ba DOCX cùng Master V10.46.34 đã dùng. Các báo
cáo cung cấp định hướng ngách, ví dụ định dạng, keyword, loại vật chứng và các
ước lượng cơ hội. Số view của video mẫu không chứng minh sự kiện kỹ thuật được
kể là thật. CPM/search volume/điểm cơ hội là ước lượng để đặt giả thuyết; không
được dùng như doanh thu hoặc lượt xem kỳ vọng đã xác nhận cho kênh này.

Bộ học kênh thật hiện chỉ thử ở DaiTa14/G-13. Nội dung của bộ nhớ được ghép khi
bắt đầu lượt mới, có lịch sử và giới hạn bằng chứng; không đóng đinh các số đó
vào cả hai mươi file. Năm ngách mở rộng chưa có thử nghiệm hiệu quả riêng. Thư viện
đã được kiểm tương thích kỹ thuật không đồng nghĩa đã chứng minh tăng view.
Không tự bật A/B trên video Public và không đổi ngách/lịch vì một outlier.

Nguồn cho từng câu kể không bắt buộc. Khi dùng số, quote hay sự kiện có tên tổ
chức cụ thể như bằng chứng thật, phải có căn cứ. Có thể tái dựng tình huống
ẩn danh dựa trên nguyên lý, ghi rõ phần tái dựng; không giả URL/quote/log thật.
Nhãn verified do AI tự ghi là lời báo của mô hình, không phải xác nhận độc lập.

## Duy trì file

`tools/build_research_masters.py` ghép `source/protocol.txt` với hai mươi module ngách
để sinh lại sáu TXT và manifest có hash. Chỉ nguồn này được sửa khi phát hành
thư viện; người dùng cuối vẫn dùng từng TXT đầy đủ. Các bản cũ được giữ riêng
theo phiên bản, không tự thay Master trong checkpoint đang chạy.

## Mã nghiên cứu và phạm vi kiểm chứng

Sáu mã G đã có giữ nguyên cả bytes và hash. Mười bốn mã R mới ánh xạ theo
A6, B2–B6, C1–C3 và H3-01–H3-05 của báo cáo; không tự gán lại G-01…G-18.
`domains.json` là danh mục dùng chung cho bộ sinh và UI. Mỗi ngách có charter
riêng với 10 family, glossary, nguồn/vật chứng, thumbnail/shot và giới hạn.

Người dùng tự nạp Master cho từng workspace; thư viện không tự thay lựa chọn.
Số liệu lịch sử chỉ đưa vào đúng kênh/ngách khi có; việc thêm chủ đề không tự
bật analytics hoặc dùng lại bài học G-13 cho kênh tài chính/văn hóa.

G-13 đã có thử Bridge thật trước đó. Kiểm thử runner/parser bằng phản hồi có
kiểm soát không thay bằng chứng đã chạy ChatGPT thật, tạo ảnh, render và đăng
video cho cả 20 ngách. Từng báo cáo kiểm thử ghi rõ phần thực tế đã chạy.
