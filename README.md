# Novel Crawler

Crawl tiểu thuyết Trung Quốc từ nhiều nguồn → dịch tiếng Việt → biên tập → một EPUB → Send to Kindle. Python 3.11+, SQLite, lưu hoàn toàn local.

## Chạy bằng một lần mở

**Nhấp đúp `start.cmd`**, hoặc trong PowerShell:

```powershell
.\start.cmd
```

Lần đầu script tự tạo `.venv`, bổ sung thư viện nếu thiếu và tạo `config.toml`. Sau đó tự khởi tạo/mở DB và chạy cả chuỗi theo lịch **ở nền**, không cần giữ cửa sổ terminal. Cấu hình hiện có không bị ghi đè. `start.cmd stop` yêu cầu dừng an toàn sau batch hiện tại; mở lại tiếp tục tiến độ cũ. Chạy `start.cmd run` nếu muốn theo dõi foreground (Ctrl+C để dừng).

Theo dõi tại **`data/status.txt`**; lỗi chi tiết ở `data/pipeline.log`, có xoay log. Mỗi ngày tạo một backup SQLite trong `data/backups/`. Không có cloud storage. Máy tắt/ngủ thì job không chạy; khi mở lại không chạy bù hàng loạt. Chưa tự đăng ký Windows startup task.

Các lệnh tùy chọn, không cần dùng trong vận hành thường:

```powershell
.\start.cmd once     # Một chu kỳ các job đến hạn rồi thoát
.\start.cmd status   # Xem tiến độ, quota và lỗi
.\start.cmd test     # Toàn bộ kiểm thử offline, không gọi agent/email
.\start.cmd demo     # Tạo EPUB mẫu bằng văn bản tự viết
.\start.cmd sources  # Xem/đối chiếu các mục lục, không gọi agent
```

`python -m novel_crawler` cũng mặc định chạy toàn bộ scheduler nếu đã có dependencies. `start.cmd` chuẩn bị chúng tự động.

## Business đã chốt

| Hạng mục | Quy tắc |
|---|---|
| Mục lục chuẩn | Mỗi truyện có danh mục cố định 1..N. ID chương thuộc truyện, không thuộc website |
| Nguồn nội dung | Thử theo thứ tự ưu tiên; tự dùng nguồn bù khi nguồn trước lỗi, thiếu hoặc trả nội dung không hợp lệ |
| Đồng nhất chương | Tiêu đề chuẩn hóa + thứ tự chương lân cận + tiêu đề trang thực; không ghép chỉ vì cùng số chương |
| Chống trùng | Hash văn bản chuẩn hóa; so sánh độ trùng các cụm ký tự với toàn bộ chương đã lưu, phát hiện cả một chương bị ghép vào chương khác |
| Chương chưa rõ | Ghi danh sách cần kiểm tra; tiếp tục crawl các chương khác. Dịch/biên tập dừng tại khoảng trống để giữ ngữ cảnh |
| Tiến độ | Không lấy lại/gọi agent lại cho chương hoàn thành; mỗi batch lưu ngay vào SQLite |
| Văn phong | Việt ngữ, cổ trang, Hán–Việt, giữ giọng hiện đại của nhân vật xuyên không khi đúng nguyên tác |
| Biên tập | Bản dịch có cảnh báo được đưa sang biên tập đối chiếu; chỉ bản biên tập không còn vấn đề chưa giải quyết mới vào EPUB |
| Kindle | Một file duy nhất; gửi một lần sau khi toàn bộ truyện đã biên tập. Không gửi các bản tăng dần gây trùng sách |

Đọc [quy tắc ghép nguồn chi tiết](docs/ARCHITECTURE.md) và [kết quả khảo sát nguồn](docs/SOURCES.md).

## Mặc định vận hành

- Crawl tối đa **5 chương/lượt**, dịch **2**, biên tập **2**; mỗi job chạy theo lịch 30 phút. Export/gửi kiểm tra mỗi giờ. Job chỉ làm phần đã đủ điều kiện.
- **48 lượt gọi CLI/ngày UTC**, **4.000 lượt tổng**, cách nhau ít nhất 30 giây, timeout 180 giây, tối đa 2 lần thử/chương/bước. Tổng này đủ khoảng 1.597 × 2 bước và một phần dự phòng; hệ thống không tự nâng trần.
- Reservation tối đa 2 triệu token dự phòng/ngày, 180 triệu tổng; prompt tối đa 50 KB, output allowance 12.000 token. Khi đạt bất kỳ trần nào thì dừng gọi. Những giá trị này chỉnh trong `config.toml`, được đọc lại khi scheduler lặp.
- Một worker/CLI tại một thời điểm bằng OS lock. Nhiều truyện có trạng thái riêng, chia sẻ cùng ngân sách trong một DB. Thứ tự truyện trong config hiện cũng là thứ tự ưu tiên.
- HTTP lỗi mạng tạm thời thử tối đa 3 lần/ứng viên, đợi tăng dần giữa các lần; thử nguồn bù ngay trong lượt hiện tại. Không tự retry trang sai nội dung, captcha, font chưa giải mã hoặc chương bị cắt.

Các trần quota đã tăng từ chế độ pilot 24 lượt tổng của bản kickoff để có thể chạy truyện dài. **Tăng trần tổng không có nghĩa chạy một lần toàn bộ truyện**: trần ngày và batch vẫn áp dụng. Mỗi chương thường cần 2 lượt, do đó 48 lượt/ngày tương đương tối đa khoảng 24 chương/ngày, trước lỗi/quota ngoài của tài khoản.

**CLI không cung cấp hard cap token/USD từ phía server.** Trần cứng của ứng dụng là số lần khởi chạy. Reservation tính bảo thủ bằng byte prompt + output allowance; usage thật được cập nhật nếu CLI trả về. Hệ thống/reasoning/retry nội bộ có thể dùng thêm token. Timeout dừng process tree, không bảo đảm hủy tính toán đã gửi lên server. Không tự đổi sang API trả phí hoặc tự đổi model.

## Agent

Không cần mua API để chạy chế độ CLI mặc định. Đăng nhập tài khoản ChatGPT trong Codex CLI; không trích API key/cookie từ phiên web. Nếu muốn chuyển sang API sau này, OpenAI API có [billing riêng với ChatGPT](https://help.openai.com/en/articles/9039756). Gemini API cấp key qua AI Studio, có [free tier cho một số model và paid tier riêng](https://ai.google.dev/gemini-api/docs/billing); việc có tài khoản Gemini web không tự bảo đảm model/quota API mong muốn.

Mặc định **`codex_cli`: `gpt-5.6-terra`, effort `medium`**. Tận dụng đăng nhập ChatGPT; đã kiểm thử một mẫu thật. Codex preflight từ chối tài khoản đăng nhập bằng API key trong adapter này. [Cơ chế đăng nhập chính thức](https://learn.chatgpt.com/docs/auth)

Adapter **`gemini_cli`: `gemini-3.8-flash`, thinking `HIGH`** vẫn có, nhưng thử trên máy ngày 2026-09-08 bị Google từ chối xác thực: `IneligibleTierError / UNSUPPORTED_CLIENT`, yêu cầu chuyển sang Antigravity. Vì vậy chưa xác nhận được suy luận bằng model này trên tài khoản hiện tại. Không tự nâng cấp CLI toàn máy, trích cookie, đổi nhà cung cấp hay bỏ qua bước xác thực.

Circuit breaker tách theo provider; quota tính chung. Một lỗi Gemini không chặn Terra. Lỗi/timeout của provider đang dùng sẽ tạm dừng provider đó, giữ lượt gọi đã dùng, tránh đốt quota bằng retry. Các phép kiểm thử live cũng ghi vào cùng sổ lượt gọi.

## Nội dung và chất lượng

Mỗi call nhận [skill dịch](novel_crawler/prompts/translation/SKILL.md), glossary, ghi chú liên tục và đoạn cuối chương trước; mỗi call là session mới. JSON phải đủ các ID đoạn, không trộn thứ tự, không sót chữ Hán/markup, không thay chữ số hay thuật ngữ đã khóa. Bước biên tập đọc nguồn + draft + các cảnh báo trước đó.

Schema Codex đặt số phần tử đầu ra đúng bằng số đoạn nguồn, kể cả chương hơn 100 đoạn; bộ kiểm tra vẫn xác nhận ID theo thứ tự. Ràng buộc `minItems`/`maxItems` dựa trên [tài liệu Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs). Đã phát hiện một lần biên tập chỉ trả 100/109 đoạn và chặn trước EPUB; lịch sử lần lỗi được giữ khi sửa và thử lại.

Các kiểm tra không chứng minh bản dịch đúng nghĩa 100%. Hệ thống giữ bản gốc và raw response để kiểm tra. Glossary được khóa hash để tránh đổi tên giữa truyện. Tên mới dựa vào continuity note; registry thực thể dài hạn và chia chương quá dài chưa triển khai. Khi cần xử lý một lỗi agent đã xác minh, có thể dùng lệnh nâng cao `python -m novel_crawler retry --book BOOK_ID --chapter N`; thao tác này không hoàn quota hay xóa giới hạn số lần thử.

Chương bù chưa xác định được không làm mất dữ liệu cũ. Bảng `source_provenance` lưu website, URL, số chương gốc, tiêu đề, bằng chứng ghép và fingerprint; `source_candidates` lưu từng lần thử; `source_reviews` giữ các khoảng trống. Thêm nguồn mới vào config sẽ tự đối chiếu lại và mở lại những chương từng bị chặn vì thiếu nguồn.

## Thêm nguồn hoặc truyện

Sao chép khối `[[books]]` cùng các `[[books.sources]]` trong config, đặt ID truyện riêng. Giữ `source_url` cấp book là mục lục chuẩn; mỗi source có `id`, URL mục lục, encoding, CSS selector và adapter. Được bổ sung nguồn dự phòng mà không tạo truyện mới; không đổi mục lục chuẩn/số chương của truyện đang chạy để tránh trộn ấn bản.

Các nguồn có thể thiếu chương hoặc lệch số. Tiêu đề trùng chỉ được ghép khi nằm trong một chuỗi tiêu đề khớp chính xác giữa hai mốc chắc chắn. Không tự ghép phần trên/dưới, chương bị tách nhiều trang hoặc chỉ có tiêu đề kiểu “Chương 100” không đủ bằng chứng. Nguồn truyền thống/giản thể có tên khác đáng kể cũng cần adapter/alias được xác minh; không dùng fuzzy match yếu để đoán.

## EPUB và email Kindle

**Cấu hình đang dùng:** `gmail_connector`, với tài khoản Gmail đã xác minh và địa chỉ Kindle lưu trong `config.toml` local. Không cần SMTP password cho chế độ này. Khi đủ sách, Python tạo bản chụp EPUB bất biến và outbox `ready`. Một Codex heartbeat kiểm tra mỗi ngày lúc 20:00 theo giờ máy, gửi qua Gmail đã kết nối, rồi ghi ID thư vào ledger. Codex/kết nối Gmail phải hoạt động khi heartbeat chạy. Heartbeat phát sinh tối đa một lượt kiểm tra theo lịch mỗi ngày, dùng quota tài khoản Codex riêng với số lần CLI trong DB. Không có vòng lặp gọi agent dịch ở heartbeat. Có thể chậm tới lần kiểm tra ngày tiếp theo sau khi EPUB hoàn thành.

Gmail bridge xác minh hash, địa chỉ và trạng thái trước gửi, claim một lần, giữ `sending`/`unknown` nếu kết quả không rõ để tránh gửi trùng. Chưa gửi email thật vì sách chưa hoàn thành. Địa chỉ Gmail gửi phải nằm trong danh sách email được phép của Amazon. Tạm gác chương 1.264 nghĩa vẫn giữ khoảng trống; không tự coi bản thiếu chương là bản hoàn chỉnh.

EPUB 3 reflowable, tiếng Việt NFC, khoảng 20 chương/XHTML, mục lục tới từng chương. Không ép font/kích thước: người đọc chọn font và cỡ chữ trên Paperwhite 5. Kiểm thử cả sách 1.597 chương với 81 file XHTML, kiểm tra ZIP/XML, manifest, spine và tất cả anchor. EPUB mẫu: `data/epub/demo-dem-o-lang.epub`. Chưa kiểm tra trên thiết bị Kindle vật lý.

Nếu dùng chế độ **SMTP thay cho Gmail connector**, cần cấu hình một lần: `kindle.transport="smtp"`, `smtp_host`, `sender`, `recipient` (`...@kindle.com`), biến môi trường `KINDLE_SMTP_USERNAME` / `KINDLE_SMTP_PASSWORD`, và `kindle.enabled=true`. Thêm sender vào danh sách email được phép trong tài khoản Amazon. Có thể dùng SMTP AWS SES hoặc SMTP đang có. Không ghi mật khẩu vào repo.

Khi thiếu cấu hình email, crawl/dịch/EPUB vẫn chạy; trạng thái báo chờ SMTP. Không thể tự suy ra địa chỉ Kindle và thông tin đăng nhập của bạn. Khi đủ toàn bộ chương, scheduler sẽ gửi. `submitted` nghĩa SMTP đã nhận, chưa phải xác nhận vào thiết bị. `sending`/`unknown` sau crash/lỗi đều chặn gửi lại tự động; kiểm tra thư viện Amazon trước khi xử lý thủ công.

Giữ ID sách/anchor ổn định không bảo đảm thay thế EPUB trên Amazon mà giữ vị trí đọc. Vì vậy mặc định chỉ gửi bản cuối; bản preview local tạo qua lệnh nâng cao `python -m novel_crawler preview --book BOOK_ID` và không được tự gửi.

## Kiểm thử và trạng thái

`start.cmd test` kiểm tra đổi số chương, thiếu chương, tiêu đề trùng, nội dung trùng/gộp, sai tác phẩm, HTML xen thẻ giữa câu, fallback, giới hạn retry, resume, quota, crash, khóa worker, backup và chu trình crawl → dịch → edit → EPUB → SMTP giả lập. Không có mạng hay lượt suy luận trong bộ test này.

Khi bật chạy nền ngày 2026-09-08: đã crawl 10 chương đầu, dịch và biên tập xong 4 chương bằng Terra medium. Bản xem thử local ở `data/epub/han-men-bai-jia-zi.preview.epub`; chưa gửi Kindle. Tiến độ mới nhất đọc ở `data/status.txt`. Chương 1.264 đã được thử nguồn bù và chuyển sang `source_review` do nội dung không hợp lệ. Gemini tạm gác theo yêu cầu.

Bản biên tập lại chương 4 đủ 109 đoạn nhưng báo hai loại lỗi trình bày. Đã kiểm tra trực tiếp cả nguồn và bản Việt: nối lại một câu đầy đủ bị ngắt giữa hai dòng, bỏ hai ký hiệu ASCII rác sau dấu chấm, lưu audit và kiểm tra bản cuối 108 đoạn. Không gọi agent lần thứ ba hoặc tăng giới hạn. Adapter cũng được sửa để newline trong HTML không trở thành đoạn truyện mới; quy tắc biên tập phân biệt lỗi định dạng có thể sửa với nội dung thực sự thiếu.

Tổng 9 lần khởi chạy CLI của đợt nghiệm thu đều được giữ trong DB, gồm lỗi xác thực Gemini, một lỗi khởi động Codex do sandbox kiểm thử chỉ cho đọc thư mục tài khoản, và lần biên tập lại sau sửa nguồn; không xóa để làm đẹp số liệu. Lỗi sandbox đã xác minh và acknowledge, giữ nguyên reservation; chạy launcher từ Windows dùng quyền tài khoản thông thường. Bản nguồn trước các lần sửa parser nằm trong các file audit ở `data/`. Lỗi ngắt đoạn do thẻ rác ở chương 1 đã đối chiếu Shuqi, sửa mà không thay fingerprint nội dung; bản dịch cũ được nối đúng ID đoạn rồi biên tập lại, không dịch lại toàn chương lần thứ ba. Cache chương 2–5 cũng đã rà soát cùng lỗi ngắt đoạn; hai cặp đoạn Việt của chương 2 được kiểm tra và nối lại mà không thay từ hoặc gọi agent.
