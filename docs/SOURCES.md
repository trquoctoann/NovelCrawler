# Khảo sát nguồn — 2026-09-08

Tác phẩm đúng: **寒门败家子**, tác giả **寻北仪**, nhân vật 王渊 / 李诗涵; trên Fanqie còn ghi tên khác **回到古代搞工厂**. Cả Fanqie và Shuqi ghi đã hoàn thành, 1.597 chương; chương cuối 天下一统（全文完）. [Fanqie](https://fanqienovel.com/page/7143311019090119711), [Shuqi](https://t.shuqi.com/book/8724153.html)

| Nguồn đã xem | Bằng chứng | Kết luận cho dự án |
|---|---|---|
| Fanqie | HTTP 200, danh mục 1.597 chương riêng biệt; HTML chương 1 có 987 ký tự private-use; chương 1.597 chỉ có trích đoạn, thông báo đọc miễn phí trong app / SVIP trên web | Ưu tiên để xác minh tác phẩm và danh mục; **không đáp ứng free + full text + dễ crawl bằng HTTP hiện tại** |
| Shuqi | Đúng tác giả, 1.597 chương, có thông tin cấp quyền số từ 中企瑞铭; link tới app để đọc trọn bộ | Nguồn đối chiếu metadata; chưa chứng minh lấy miễn phí toàn văn qua web |
| Piaotian | HTTP mục lục hiện có 1.596 chương; thiếu **1264 淳于安求援**, dù liên kết cuối là 1597. Trang tự ghi nội dung do thành viên đăng lại | Chọn làm nguồn nội dung ưu tiên, đối chiếu theo danh mục Fanqie và bù khoảng trống từ nguồn khác |
| 8xiaoshuo | Request HTTP tới mục lục trả 403 / trang kiểm tra trình duyệt | Không phù hợp crawler HTTP đơn giản |
| Dingdian | Request tới mục lục trả 200 nhưng body rỗng trong lần thử | Không đủ dữ kiện để chọn |
| ixdzs8 / 516500 | Có hồ sơ cùng nội dung, danh mục tới 1.553; chương thử trả trang xác minh trình duyệt | Chưa dùng làm nguồn HTTP tự động |
| 8book / 297534 | Danh mục ghi đủ 1.597, có 1.264; robots trả 520, link chương dẫn sang tên miền khác với nội dung blog thuế không liên quan | Loại khỏi cấu hình; kết quả tìm kiếm/danh mục không đủ chứng minh trang chương còn đúng |

Chương cuối Fanqie nêu 2.078 chữ nhưng web không đăng nhập chỉ trả trích đoạn; ký tự bị thay thế bằng glyph riêng. Đây là hai vấn đề khác nhau: giải mã font cũng không cung cấp phần văn bản bị giới hạn. [Trang chương cuối](https://fanqienovel.com/reader/7353283521487571992)

Piaotian được kiểm tra bằng HTTP trực tiếp và đọc số chương từ tiêu đề, không suy ra từ vị trí liên kết. [Mục lục được kiểm tra](https://www.piaotia.com/html/15/15289/)

**Theo yêu cầu mới, cho phép bù giữa nhiều nguồn nhưng giữ một mục lục chuẩn.** Danh mục Fanqie có 1.597 ID chuẩn; 1.596 tiêu đề Piaotian khớp duy nhất và đúng thứ tự lân cận. Adapter Piaotian đã thử chương 1, 1.263 và 1.597. Chương cuối trích được khoảng 2.070 ký tự, đối chiếu metadata Fanqie 2.078 chữ; sai khác đếm ký tự không tự chứng minh hai bản hoàn toàn giống nhau.

Piaotian dùng HTML cũ với nhiều đoạn không nằm trong content div. Parser gom văn bản bên trong thẻ inline, loại quảng cáo theo ranh giới xác minh được. Kiểm thử live phát hiện **nguồn tự ngắt địa danh 成州 bởi thẻ rác `</di>` và hai cụm xuống dòng**. Đã đối chiếu câu liền mạch trên [Shuqi chương đầu](https://t.shuqi.com/v2/query/8724153/830312.html), rồi sửa adapter chỉ nối đúng mẫu rác này, giữ nguyên toàn bộ ký tự và các đoạn bình thường. HTTPX dùng user-agent thư viện mặc định, không giả trình duyệt; vẫn kiểm tra robots.txt, không bỏ qua 403 hoặc captcha.

**Chương 1.264 chưa có nguồn toàn văn khả dụng được xác minh.** Liên kết “chương tiếp” từ 1.263 trên Piaotian nhảy thẳng tới trang có tiêu đề 1.265; Dingdian trả body rỗng; Fanqie là ứng viên dự phòng nhưng font bị mã hóa/giới hạn toàn văn. Hệ thống ưu tiên kiểm tra các khoảng trống mục lục sớm, thử nguồn bù, sau đó ghi `source_review` nếu chưa đạt điều kiện. Nó vẫn crawl các chương khác và chỉ dịch liên tục tới trước khoảng trống.

Nguồn ưu tiên và agent mặc định đã bật trong cấu hình mới. Điều này cho phép bắt đầu từng batch, **không phải xác nhận đã có đủ nội dung toàn bộ truyện**. Chỉ khi có đủ tất cả bản biên tập mới xuất/gửi bản cuối. Không ghép theo số chương trần, không tự đổi ấn bản hoặc đoán nội dung thiếu. Sản phẩm dành cho đọc cá nhân, chưa có luồng xuất bản công khai.
