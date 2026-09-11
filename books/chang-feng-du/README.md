# Trường Phong Độ — 长风渡 / 嫁纨绔

Tác giả: 墨书白 (Mặc Thư Bạch). Bản web có 176 mục: lời mở đầu, 171 chương đánh tên, mục kết thúc và 3 ngoại truyện. ID database là thứ tự mục; ID 1 là lời mở đầu, ID 2 tương ứng “第一章” ở Tấn Giang. Không suy ra ID từ số trong tiêu đề.

## Nguồn đã kiểm tra ngày 11/09/2026

- Mục lục đối chiếu: https://www.jjwxc.net/onebook.php?novelid=3415053
- Nguồn đọc: https://www.kanunu8.com/book5/changfengdu/
- Nguồn bù: 19 chương miễn phí Tấn Giang, đối chiếu bằng tiêu đề gốc trong manifest và thứ tự các tiêu đề lân cận; không đi qua liên kết mua/VIP. Mục số 7 của Kanunu bị chặn do thiếu khoảng 300 chữ so với bản tham chiếu.
- `reference-manifest.json` chốt 176 URL/tiêu đề nguồn, tiêu đề và số chữ công bố ở Tấn Giang. Mục lục thay đổi sẽ dừng để kiểm tra. Crawl từng batch 5 mục; không nạp cả truyện vào một prompt.
- Chương mở đầu trên Kanunu trùng bản Tấn Giang theo toàn bộ nội dung chuẩn hóa (2.074 chữ, 39 đoạn). Các mẫu chương gộp 20, hai chương 23/24, kết thúc 173 và ngoại truyện cuối đã được kiểm tra. Đây là kiểm tra mẫu, không phải chứng nhận mọi chương đã tải.
- Nguồn 95590 bị loại do làm mất chữ trong tên 顾九思; 51shucheng trả HTTP 403. Không tự dùng nguồn lỗi để bù chương.
- Mỗi chương phải qua đối chiếu tiêu đề, độ dài 95–110% số chữ tham chiếu, chữ Trung đọc được và phát hiện nội dung trùng. Lời tác giả sau dòng riêng `作者有话要说：` được tách khỏi chính văn. Khác biệt ấn bản cần kiểm tra thủ công, không tự nới ngưỡng.

## Chạy

Tại thư mục dự án `D:\Project\NovelCrawler`, dùng `start.cmd` để chạy và cùng lệnh đó để tiếp tục. `start.cmd status` xem trạng thái. `start.cmd once` chạy một batch mỗi công đoạn. Không bật launcher `background` cũ vì nó dùng scheduler cũ, khác pipeline hai worker hiện tại.

Local config dùng ChatGPT Terra medium qua CLI đã đăng nhập, mỗi batch dịch 1 và biên tập 1 mục. Giữ quota 48 lượt/ngày UTC, 4.000 lượt tổng, 2 triệu token dự phòng/ngày và 180 triệu tổng, cách lượt tối thiểu 30 giây, tối đa 2 lần thử mỗi chương/công đoạn. Glossary review cũng tính quota. Không tự tăng hạn mức hoặc trích xuất API key.

Đồng bộ bản code mới: prompt tối đa 200.000 byte, timeout 600 giây, output tối đa 200.000 byte; vẫn giữ quota ngày/tổng. Các giới hạn này vẫn áp dụng: những chương gộp rất dài có thể cần chia nhỏ trước khi dịch. Nếu vượt cap, pipeline phải dừng công đoạn đó, giữ tiến độ; không cắt bớt nội dung hoặc tự nới quota.

`allow_missing_chapters = false`: chỉ xuất EPUB cuối và tạo outbox Kindle khi đủ 176 mục đã biên tập. Bản xem trước chỉ lưu local. Gmail connector gửi file cuối từ `trquoctoann@gmail.com` đến `trquoctoann_DSsROK@kindle.com`; outbox `ready` chưa có nghĩa email đã gửi hoặc Kindle đã nhận.

Truyện cũ được tạm dừng trong config, giữ nguyên database và lịch sử chi phí. Dữ liệu khảo sát 渣夫重生了 chỉ nằm trong `data/research-zha-fu`; chưa tạo job hay gọi agent cho truyện đó.
