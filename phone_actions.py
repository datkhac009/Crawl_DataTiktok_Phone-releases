"""phone_actions.py — Đọc chữ trên màn hình và thi hành cú bấm. KHÔNG chứa luật nào.

VÌ SAO TÁCH RA (2026-09-15)
===========================
`scan_feed_sounds.py` lo vòng quét sound. File này lo phần "đọc màn hình + bấm nút", để khi
TikTok đổi giao diện thì chỉ phải sửa một chỗ.

⚠ KHÔNG CÓ LUẬT NÀO TRONG FILE NÀY.
Danh sách nhãn nút đến từ biến môi trường do `runner.cjs` dựng từ `src/uilabels.cjs` — nguồn duy
nhất, dùng chung với bản PC. Quyết định bấm hay không do phía Node đưa xuống qua `askbridge`.
Chép danh sách nhãn vào đây là tái lập đúng lỗi đã xảy ra với `linkkey.cjs`.

⚠ VÌ SAO KHỚP TRỌN CHUỖI CHỨ KHÔNG PHẢI "CÓ CHỨA"
`uilabels.cjs:40-42` ghi rõ: khớp kiểu chứa thì "Followers 1.2M" cũng thành nút Follow. Biểu
thức truyền xuống đây đã ở dạng `(?i)^(...)$`.

⚠ XÁC MINH SAU KHI BẤM
Bấm xong phải đọc lại nhãn nút. `channelstore.cjs:182-184` cảnh báo: ghi sổ lúc BẤM thay vì lúc
đã xác minh thì kênh bị đánh dấu đã follow dù follow hỏng, và **bị bỏ qua vĩnh viễn**.
"""
import os
import random
import re
import unicodedata
import time
import xml.etree.ElementTree as ET

RE_FOLLOW = os.environ.get("RE_FOLLOW", "(?!)")
RE_FOLLOWING = os.environ.get("RE_FOLLOWING", "(?!)")
RE_NOT_INTERESTED = os.environ.get("RE_NOT_INTERESTED", "(?!)")

# `(?i)` ở đầu là cú pháp chung của cả Java (uiautomator2 dùng) lẫn Python, nên một chuỗi dùng
# được cho cả hai phía.
_py_follow = re.compile(RE_FOLLOW)
_py_following = re.compile(RE_FOLLOWING)

# Handle TikTok: chữ thường, số, dấu chấm, gạch dưới, tối đa 30 ký tự. Khớp ĐÚNG luật của
# `followpolicy.cjs` — nơi kia mới là nguồn sự thật, đây chỉ để nhặt ứng viên trên màn hình.
_RE_HANDLE = re.compile(r"^@[A-Za-z0-9._]{1,30}$")

MAX_DESC = 500
MAX_BADGES = 10
MAX_BADGE_LEN = 120


def _leaf_texts(xml_str):
    """Lấy chữ của các node LÁ (không có con). Trả danh sách theo thứ tự xuất hiện.

    Dùng node lá vì node cha gộp chữ của mọi con lại — lấy node cha là trộn caption, tên sound,
    số like vào một chuỗi rồi mọi phép khớp đều sai.
    """
    out = []
    try:
        root = ET.fromstring(xml_str)
    except Exception:
        return out
    for node in root.iter():
        if len(node) > 0:
            continue                      # không phải lá
        for key in ("text", "content-desc"):
            v = (node.get(key) or "").strip()
            if v and v not in out:
                out.append(v)
    return out


def _attrs_by_id(xml_str):
    """Gom `text` / `content-desc` theo resource-id (đã cắt tiền tố gói).

    Khác `_leaf_texts`: hàm kia chỉ lấy node LÁ, mà hai neo quan trọng nhất của feed —
    `user_avatar` và `long_press_layout` — đều là node BỌC, nên node lá không bao giờ thấy chúng.
    """
    out = {}
    try:
        root = ET.fromstring(xml_str)
    except Exception:
        return out
    for node in root.iter():
        rid = node.get("resource-id") or ""
        if not rid:
            continue
        key = rid.split(":id/")[-1]
        if key in out:
            continue                      # lấy node ĐẦU TIÊN: video đang hiển thị nằm trên cùng
        out[key] = {
            "text": (node.get("text") or "").strip(),
            "desc": (node.get("content-desc") or "").strip(),
        }
    return out


# Tên tác giả nằm trong `content-desc` dạng "<Tên> profile" (avatar) hoặc "Follow <Tên>" (nút
# follow trên feed). Đo trên máy thật 2026-09-16, TikTok v46.1.1 — xem `probe_screen.py`.
_RE_AVATAR = re.compile(r"^(.*?)\s+profile$", re.I)
# ⚠ Phải nhận CẢ "Following <Tên>" (2026-09-18). Follow xong thì nút đổi chữ, mà bản cũ chỉ khớp
# `^follow\s+` nên neo dự phòng chết đúng lúc cần nhất — và mọi lần đọc hụt tên đều bị chấm là
# "video đã đổi", tức là không bấm gì nữa.
_RE_NUT_FOLLOW = re.compile(r"^follow(?:ing)?\s+(.+)$", re.I)


# Kết quả một lượt ghé thăm, dịch sang tiếng Việt NGAY TẠI CHỖ IN.
#
# ⚠ Bản cũ in thẳng mã nội bộ ra màn hình: `ghe tham: ok_no_grid sau 12.4s`. Bản PC không bao giờ
# làm thế — mã nội bộ của nó ('nav', 'nofeed', 'same', 'empty') đều được dịch tại chỗ gọi.
_KQ_GHE = {
    "ok": "👀 Đã ghé một kênh",
    "ok_no_grid": "👀 Đã ghé một kênh, nhưng không mở được video nào trong lưới",
    "skip_trung": "Bỏ qua một kênh vừa ghé gần đây",
    "fail": "⚠ Ghé thăm hỏng",
}


def _chuan_ten(s):
    """Chuẩn hoá tên hiển thị để SO SÁNH (không để hiển thị).

    ⚠ VÌ SAO (2026-09-18): bản cũ so hai chuỗi thô. Tên đọc được từ máy thật có đủ thứ làm lệch —
    khoảng trắng thừa, hoa thường, ký tự Unicode dựng sẵn so với tổ hợp, và cả đuôi " profile"
    còn sót khi regex không khớp. Mỗi lần lệch là một lần báo oan "video đã đổi", và báo oan thì
    KHÔNG bấm gì cả. Bốn đường hỏng oan đã đo được, đây là hàng rào chung cho cả bốn.
    """
    if not s:
        return ""
    t = unicodedata.normalize("NFC", str(s)).strip()
    m = _RE_AVATAR.match(t)          # bỏ đuôi " profile" nếu còn sót
    if m and m.group(1).strip():
        t = m.group(1).strip()
    return " ".join(t.split()).casefold()


def _ten_tac_gia(attrs):
    """Tên HIỂN THỊ của chủ video. Chuỗi rỗng nếu không đọc được.

    ⚠ ĐÂY KHÔNG PHẢI @handle. Feed TikTok v46.1.1 **không bày @handle ở đâu cả** — grep thẳng
    bản chụp XML không có một chuỗi `text="@..."` nào. @handle chỉ có trên TRANG CÁ NHÂN, và
    `scan_feed_sounds.py` phải ghé vào đó mới lấy được (xem `open_profile_read_handle`).

    Hai neo, thử theo thứ tự. Neo thứ hai không thừa: khi tác giả ĐÃ được follow thì nút đổi chữ
    thành "Following ..." nên `_RE_NUT_FOLLOW` trượt, nhưng avatar thì luôn còn.
    """
    av = attrs.get("user_avatar", {}).get("desc", "")
    m = _RE_AVATAR.match(av)
    if m and m.group(1).strip():
        return m.group(1).strip()
    if av:
        return av        # máy để ngôn ngữ khác: đuôi không phải "profile", vẫn hơn là rỗng
    m = _RE_NUT_FOLLOW.match(attrs.get("ife", {}).get("desc", ""))
    return m.group(1).strip() if m else ""


def _dang_live(attrs):
    """Video đang hiển thị có phải LIVESTREAM không.

    Neo là `long_press_layout`: video thường mang `content-desc="Video"`, còn livestream mang
    `"LIVE"`. Chọn đúng neo này vì nó là id ĐỌC ĐƯỢC — hai ứng viên khác (`zxp` "Tap to watch
    LIVE", `i1i` "LIVE now") là id đã bị làm rối, đổi tên theo từng bản TikTok.
    """
    return attrs.get("long_press_layout", {}).get("desc", "").strip().upper() == "LIVE"


def read_video_info(d):
    """Đọc thông tin video đang hiển thị trên feed.

    ⚠ HẠN CHẾ ĐÃ BIẾT: chưa dò được resource-id của caption và tên tác giả trên máy thật, nên
    đây là phỏng đoán theo hình dạng: handle là chuỗi dạng `@abc`, caption là chuỗi dài nhất.
    Bản PC đọc chính xác hơn vì có neo `[data-e2e="video-desc"]` đã dò thật.

    Hệ quả: khớp theo TÊN HIỂN THỊ của tác giả (thứ mà `langfilter` cũng xét) yếu hơn bản PC.
    Hướng sai ở đây là BỎ SÓT, không phải bắt nhầm — chấp nhận được.
    """
    # ── DÒ LẠI TỚI KHI ĐỌC ĐƯỢC TÊN, KHÔNG CHỤP MỘT PHÁT RỒI THÔI ──
    #
    # ⚠ SỰ CỐ THẬT (2026-09-18): bản cũ chụp đúng một lần, ngay 0,8-1,4 giây sau cú vuốt. Nhánh
    # giao diện mang `user_avatar` khi đó thường CHƯA DỰNG XONG, nên tên tác giả về rỗng — và tên
    # rỗng thì `same_video` báo "video đã đổi", tức là KHÔNG BẤM GÌ CẢ. Chủ dự án nhìn log thấy
    # `skip_changed_video` ở mọi video: suốt nhiều ca, không một cú tym hay ghé thăm nào chạy.
    #
    # Đo trên 13 bản chụp có sẵn trong repo: 6/13 bản không có `user_avatar`. Mà `probe_screen.py`
    # — công cụ đã tạo ra các bản ĐỌC ĐƯỢC — cố ý chờ 2,5-4 giây trước khi chụp. Chênh lệch đó
    # chính là lỗi.
    #
    # Dò theo điều kiện nên chỉ tốn thời gian ĐÚNG LÚC đang đọc hụt; feed dựng kịp thì đi tiếp ngay.
    xml_str = ""
    attrs = {}
    het = time.time() + 2.5
    while True:
        try:
            xml_str = d.dump_hierarchy()
        except Exception:
            xml_str = ""
        if xml_str:
            attrs = _attrs_by_id(xml_str)
            # Đủ điều kiện đi tiếp khi đọc được tên, HOẶC khi biết chắc đây là LIVE (màn LIVE vốn
            # không có `user_avatar` — chờ thêm cũng không bao giờ có).
            if _ten_tac_gia(attrs) or _dang_live(attrs):
                break
        if time.time() >= het:
            break
        time.sleep(0.3)

    if not xml_str:
        return {"author": "", "handle": "", "desc": "", "badges": [], "live": False, "tako": False}

    live = _dang_live(attrs)
    tac_gia = _ten_tac_gia(attrs)

    texts = _leaf_texts(xml_str)
    handle = next((t for t in texts if _RE_HANDLE.match(t)), "")

    # Caption = chuỗi DÀI NHẤT không phải handle. Ngưỡng 15 ký tự để khỏi nhận nhầm nhãn nút.
    ung_vien = [t for t in texts if t != handle and len(t) >= 15]
    desc = max(ung_vien, key=len) if ung_vien else ""

    # Ứng viên nhãn: chuỗi NGẮN, không phải caption. Phía Node quyết cái nào là nhãn AI.
    badges = [t for t in texts if t != desc and len(t) <= MAX_BADGE_LEN][:MAX_BADGES]

    return {
        # `langfilter` cần TÊN HIỂN THỊ + @handle nối nhau (langfilter.cjs:98-99). Từ 2026-09-16
        # đã đọc được tên hiển thị THẬT từ `user_avatar`, nên bộ lọc ngôn ngữ mạnh hơn hẳn bản cũ
        # (bản cũ nhét @handle vào ô author, mà @handle thì feed không hề có -> luôn rỗng).
        "author": (tac_gia + (" " + handle if handle else "")).strip(),
        # Gần như LUÔN rỗng trên feed — giữ lại cho đủ hình dạng câu hỏi và cho máy nào có bố cục
        # khác. @handle thật lấy ở `open_profile_read_handle`.
        "handle": handle,
        "desc": desc[:MAX_DESC],
        "badges": badges,
        "live": live,
        # Đang đứng trong TikTok Tako (xem `la_man_tako`). Soi ké trên CÙNG bản chụp vừa đọc, nên
        # không tốn thêm lần chụp nào. ⚠ Nơi gọi phải `pop` khoá này ra trước khi đưa `info` cho
        # `bridge.ask(**info)`: `ask` chỉ nhận đúng các tên của nó, thừa một khoá là TypeError.
        "tako": la_man_tako(xml_str),
    }


def _bounds(node):
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds") or "")
    return tuple(int(x) for x in m.groups()) if m else None


def _chu_cua(node):
    return ((node.get("text") or "").strip(), (node.get("content-desc") or "").strip())


def tim_nut_follow(d, pattern):
    """Tìm NÚT follow/following thật trên trang cá nhân. Trả (x, y) để bấm, hoặc None.

    ⚠ VÌ SAO KHÔNG DÙNG `_find_by_regex(..., clickable=True)` NỮA (2026-09-16):
    Trang cá nhân TikTok có HAI kiểu dựng nút, đo được cả hai trên cùng một máy:
        probe_profile_..._1.xml:  <Button  text="Follow"  clickable="true">
        probe_profile_..._2.xml:  <TextView text="Follow"  clickable="false">   ← chữ nằm TRONG
                                                                                  khung bấm được
    Đòi `clickable=True` thì kiểu thứ hai trượt, và log báo "khong thay nut Follow bam duoc" —
    đúng thứ vừa gặp khi chạy thật. Bỏ đòi hỏi đó ra thì lại dính **nhãn thống kê**
    "Following / Followers" (đếm số người), vì chữ của nó cũng đúng bằng "Following".

    Nên phân biệt bằng NGỮ CẢNH, không bằng một thuộc tính đơn lẻ: nút thật nằm trong một khung
    bấm được mà trong khung đó KHÔNG có chữ "Followers". Cụm thống kê thì luôn có, vì
    "Following" và "Followers" là hai ô cạnh nhau của cùng một hàng.
    """
    try:
        root = ET.fromstring(d.dump_hierarchy())
    except Exception:
        return None
    re_khop = re.compile(pattern)
    re_followers = re.compile(r"^followers$", re.I)
    # Ô thống kê LUÔN kèm một con số ("1234" / "12.3K" / "1.2M") trong cùng khung bấm được —
    # đó là số người. Nút Follow thì không bao giờ có số bên trong.
    #
    # ⚠ Vì sao phải thêm luật này: đo trên bản chụp thật, mỗi ô thống kê là MỘT khung bấm riêng,
    # nên luật "cùng khung có chữ Followers" không bắt được ô "Following". Hậu quả nếu lọt: hàm
    # tưởng đã theo dõi rồi -> `do_follow` trả 'not_needed' -> KHÔNG BAO GIỜ follow được ai,
    # trong im lặng. Đúng con bọ đã mất cả buổi để tìm ra.
    re_so = re.compile(r"^[\d.,]+\s*[KMBkmb]?$")

    # Bản đồ con -> cha, để leo ngược tìm khung bấm được.
    cha = {con: me for me in root.iter() for con in me}

    for node in root.iter():
        if not any(re_khop.match(c) for c in _chu_cua(node) if c):
            continue
        # Leo lên tối đa 4 tầng tìm khung bấm được (gồm chính nó).
        khung = node
        for _ in range(4):
            if (khung.get("clickable") or "") == "true":
                break
            khung = cha.get(khung)
            if khung is None:
                break
        if khung is None or (khung.get("clickable") or "") != "true":
            continue
        # Ô thống kê: cùng khung có chữ "Followers", HOẶC có một con số. Bỏ qua cả hai.
        chu_trong_khung = [c for hau in khung.iter() for c in _chu_cua(hau) if c]
        if any(re_followers.match(c) for c in chu_trong_khung):
            continue
        if any(re_so.match(c) for c in chu_trong_khung):
            continue
        b = _bounds(node) or _bounds(khung)
        if b:
            return ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
    return None


def _find_by_regex(d, pattern, timeout=0, clickable=None):
    """Tìm phần tử theo chữ hiện trên nó, thử cả `text` lẫn `content-desc`.

    `clickable=True` để chỉ nhận phần tử BẤM ĐƯỢC.

    ⚠ VÌ SAO CẦN THAM SỐ ĐÓ (2026-09-16):
    Trên TRANG CÁ NHÂN, chữ "Following" xuất hiện HAI nơi: nút theo dõi, và **nhãn thống kê**
    "Following / Followers" đếm số người. Khớp trọn chuỗi vẫn dính nhãn thống kê, vì nó đúng
    bằng chữ "Following". Hậu quả đo được trên máy thật: `do_follow` thấy "Following" tưởng đã
    theo dõi rồi nên trả 'not_needed' NGAY, và **không bao giờ follow được ai** — im lặng, không
    lỗi. Đây đúng là cái bẫy `uilabels.cjs:40-42` cảnh báo ("Followers 1.2M" hoá thành nút
    Follow), chỉ ở một góc mà khớp-trọn-chuỗi không cứu được.
    Thứ phân biệt là `clickable`: nút thật bấm được, nhãn thống kê thì không.
    """
    for kw in ({"textMatches": pattern}, {"descriptionMatches": pattern}):
        if clickable is not None:
            kw = dict(kw, clickable=clickable)
        try:
            el = d(**kw)
            if timeout:
                if el.wait(timeout=timeout):
                    return el
            elif el.exists:
                return el
        except Exception:
            continue
    return None


def do_follow(d, log=lambda s: None):
    """Bấm Follow rồi XÁC MINH. Trả 'ok' | 'fail' | 'not_needed'.

    'not_needed' khi nút đã ở trạng thái Following — không phải lỗi, và KHÔNG được tính là
    thành công để ghi sổ, vì ta đâu có follow ai.
    """
    # `tim_nut_follow` phân biệt nút thật với nhãn thống kê bằng NGỮ CẢNH — xem lý do dài ở đó.
    if tim_nut_follow(d, RE_FOLLOWING):
        return "not_needed"
    diem = tim_nut_follow(d, RE_FOLLOW)
    if diem is None:
        time.sleep(1.5)                     # trang có thể chưa dựng xong; thử lại một nhịp
        diem = tim_nut_follow(d, RE_FOLLOW)
    if diem is None:
        log("⚠ Không thấy nút Follow trên màn hình này.")
        return "fail"
    try:
        d.click(*diem)
    except Exception as e:
        log("⚠ Bấm Follow lỗi (%s)." % str(e)[:80])
        return "fail"

    # XÁC MINH: nút phải đổi sang trạng thái "đã theo dõi". Không đổi = không follow được
    # (TikTok chặn, mạng rớt, bấm trượt). Báo 'fail' để phía Node KHÔNG ghi sổ.
    #
    # ⚠ `clickable=True` ở đây CÒN QUAN TRỌNG HƠN chỗ trên: nhãn thống kê "Following" lúc nào
    # cũng có mặt trên trang cá nhân, nên thiếu nó thì phép xác minh LUÔN nói "ok" — kể cả khi
    # cú bấm trượt hoàn toàn. Sổ sẽ ghi một cú follow chưa từng xảy ra, và kênh đó bị đánh dấu
    # đã follow VĨNH VIỄN (channelstore.cjs:182-184).
    time.sleep(1.2)
    for _ in range(3):
        if tim_nut_follow(d, RE_FOLLOWING):
            return "ok"
        time.sleep(0.8)
    log("⚠ Đã bấm Follow nhưng nút KHÔNG đổi trạng thái — coi là thất bại, không ghi sổ.")
    return "fail"


def do_like(d, log=lambda s: None):
    """Thả tim bằng cách nhấn đúp giữa màn hình. Trả 'ok' | 'fail'.

    ⚠ KHÔNG xác minh được chắc chắn: icon tim của TikTok không lộ trạng thái ổn định qua
    uiautomator. Nên 'ok' ở đây nghĩa là "đã thực hiện thao tác", yếu hơn 'ok' của follow. Vì
    tym là thao tác HOÀN TÁC ĐƯỢC nên mức bảo đảm thấp hơn chấp nhận được; follow thì không.
    """
    try:
        w, h = d.window_size()
        d.double_click(w * 0.5, h * 0.45)
        time.sleep(0.6)
        return "ok"
    except Exception as e:
        log("⚠ Thả tim lỗi (%s)." % str(e)[:80])
        return "fail"


def tap_not_interested(d, log=lambda s: None):
    """Nhấn giữ video rồi chọn "Not interested". Trả 'ok' | 'fail'.

    ⚠ CÚ BẤM NÀY DẠY FEED VĨNH VIỄN, KHÔNG HOÀN TÁC. Bản PC đã trả giá đúng chỗ này (QĐ-31:
    5/10 cú bấm trúng kênh không liên quan). Nên nếu không thấy đúng mục thì THOÁT RA, tuyệt đối
    không bấm bừa mục nào khác trong menu.
    """
    try:
        w, h = d.window_size()
        d.long_click(w * 0.5, h * 0.45, 0.8)
        time.sleep(1.2)
    except Exception as e:
        log("⚠ Nhấn giữ để mở menu lỗi (%s)." % str(e)[:80])
        return "fail"

    item = _find_by_regex(d, RE_NOT_INTERESTED, timeout=3)
    if item is None:
        log("⚠ Mở được menu nhưng KHÔNG thấy mục \"Not interested\" — thoát ra, không bấm gì.")
        try:
            d.press("back")
            time.sleep(0.6)
        except Exception:
            pass
        return "fail"
    try:
        item.click()
        time.sleep(1.0)
        return "ok"
    except Exception as e:
        log("⚠ Bấm \"Not interested\" lỗi (%s)." % str(e)[:80])
        return "fail"


def _o_luoi_video(d):
    """Toa do giua cua cac o video HIEN DAY DU tren luoi trang ca nhan. Rong = khong thay luoi.

    Neo la `cover` — resource-id KHONG bi lam roi. Do tren ban chup that
    (`probe_profile_..._141113_2.xml`): 6 node `cover`, moi cai nam trong mot khung bam duoc,
    trong mot `GridView`. Cac id anh em (`erf`, `hui`) deu la id da bi lam roi nen se doi ten o
    ban TikTok sau — khong duoc bam vao chung.

    Loc bang HINH HOC chu khong bang chu, nen khong phu thuoc ngon ngu may:
      - O luoi that rong dung 1/3 be ngang man hinh (do duoc: 358 tren man 1080).
      - Hang duoi cung thuong bi CAT CUT (cao 103 thay vi 477). Bam vao o cut la cuon chu khong
        phai mo video, nen bo.
    """
    try:
        root = ET.fromstring(d.dump_hierarchy())
    except Exception:
        return []
    cha = {con: me for me in root.iter() for con in me}
    try:
        w, h = d.window_size()
    except Exception:
        return []

    thay = []
    for node in root.iter():
        if (node.get("resource-id") or "").split(":id/")[-1] != "cover":
            continue
        khung = node
        for _ in range(3):                      # leo toi da 3 tang tim khung bam duoc
            if (khung.get("clickable") or "") == "true":
                break
            khung = cha.get(khung)
            if khung is None:
                break
        if khung is None or (khung.get("clickable") or "") != "true":
            continue
        b = _bounds(khung)
        if not b:
            continue
        bw, bh = b[2] - b[0], b[3] - b[1]
        if not (0.28 * w <= bw <= 0.36 * w):    # khong phai o cua luoi 3 cot
            continue
        thay.append((bh, (b[0] + b[2]) // 2, (b[1] + b[3]) // 2))

    if not thay:
        return []
    cao_nhat = max(x[0] for x in thay)
    return [(cx, cy) for bh, cx, cy in thay
            if bh >= 0.6 * cao_nhat and 0.15 * h < cy < 0.90 * h]


def _o_tren_feed(d):
    """Dang o FEED hay khong.

    ⚠ KHONG dung `long_press_layout` lam neo: TRINH XEM STORY cung co no (do duoc tren
    `probe_profile_..._141059_1.xml`). Lay no lam neo la co luc tuong da ve feed trong khi dang
    dung trong story cua nguoi ta, roi moi thao tac sau deu sai cho.
    Neo dung: `user_avatar` VA `videomusiccoverblock` — 2/2 ban chup feed co ca hai, 0/3 ban chup
    ngoai feed co `user_avatar`.

    ⚠ VIDEO LIVE TREN FEED VAN LA FEED, nhung no KHONG co `user_avatar`. Do duoc tren ban chup:
        feed thuong   long_press_layout.desc='Video'  user_avatar=co
        feed LIVE     long_press_layout.desc='LIVE'   user_avatar=KHONG
        story viewer  long_press_layout.desc=''       user_avatar=KHONG
    Thieu nhanh nay thi `_ve_feed` se back du 3 nhip roi bao "khong ve duoc feed" trong khi may
    dang dung dung o feed — bao dong gia, va bao dong gia day nguoi ta bo qua bao dong that.
    Chinh `desc` la thu tach feed-LIVE khoi story viewer.
    """
    try:
        return _la_feed(d.dump_hierarchy())
    except Exception:
        return False


def _la_feed(xml_str):
    """Phan xet cua `_o_tren_feed` tren mot ban chup CO SAN (khong chup lai)."""
    attrs = _attrs_by_id(xml_str)
    if "user_avatar" in attrs and "videomusiccoverblock" in attrs:
        return True
    return attrs.get("long_press_layout", {}).get("desc", "").strip().upper() == "LIVE"


def o_feed(d):
    """Đang đứng ở feed For You không — bản dùng cho việc PHỤC HỒI (`ve_feed` bên
    `scan_feed_sounds.py`). Hụt ở đây nghĩa là BẤM BACK NGAY GIỮA FEED, nên nó chặt hơn
    `_o_tren_feed` ở một chỗ và nới hơn ở một chỗ, cả hai đều đo trên bản chụp thật (2026-09-19):

      • CHẶT HƠN — loại theo tên activity trước. Trình phát mở từ trang nhạc hay trang cá nhân
        (`DetailActivity`) cũng có `user_avatar` + `videomusiccoverblock` y như feed (bản chụp
        `probe_view_..._3_trinh_phat.xml`), nên `_o_tren_feed` nhận nhầm nó là feed.
      • NỚI HƠN — thêm dấu hiệu tab "For You" ở thanh trên cùng: 14/14 bản chụp feed có, kể cả
        thẻ LIVE và thẻ tin tức (nơi `_o_tren_feed` hụt); 0/27 bản chụp ngoài feed có (trang nhạc,
        lưới video, trình phát, trang cá nhân, menu nhấn giữ, màn hình Tako). Menu nhấn giữ che
        mất tab này, nên đang mở menu thì bị coi là "chưa ở feed" và một cú Back đóng nó — đúng
        ý. Tab mang chữ tiếng Anh: máy để ngôn ngữ khác thì dấu hiệu này im lặng và còn lại đúng
        phép xét cũ.
    """
    act = _activity(d)
    if act.endswith(ACT_TRANG_NHAC) or act.endswith(ACT_TRINH_PHAT):
        return False
    try:
        xml = d.dump_hierarchy()
    except Exception:
        return False
    if 'content-desc="For You"' in xml:
        return True
    return _la_feed(xml)


def _ve_feed(d, log=lambda s: None):
    """Back cho toi khi THAT SU ve duoc feed, toi da 3 nhip.

    ⚠ VI SAO PHAI LAP (2026-09-17): ngan xep gio sau hon truoc — feed -> trang ca nhan -> video.
    Mot `back` chi ve toi luoi. Va 1/3 lan ghe roi vao tam "Viewer history turned on"
    (`probe_profile_..._141129_3.xml`) nuot them mot `back` nua.
    MOI nhip di qua `close_profile` de giu nguyen lop thoat phong LIVE.
    """
    for _ in range(3):
        close_profile(d, log)
        if _o_tren_feed(d):
            return True
    log("⛔ Ghé thăm xong KHÔNG về được feed — vòng quét kế tiếp sẽ thao tác sai chỗ. Nếu dòng này lặp "
        "lại thì dừng máy và xem màn hình.")
    return False


def doc_handle_tren_trang(d, timeout=12.0):
    """ĐANG Ở TRANG CÁ NHÂN: đọc `@handle` thật. Trả `'@abc'` hoặc `''`.

    MỘT nơi định nghĩa, dùng chung cho `open_profile_read_handle` (lấy tên để follow) và
    `do_visit` (lấy tên để tra sổ chống ghé trùng). Hai nơi tự đọc lấy là có ngày một nơi sửa
    còn nơi kia quên — đúng bài học `adbpath.cjs` và `linkkey.cjs`.

    Chờ theo ĐIỀU KIỆN chứ không theo đồng hồ: đo được 3,5 giây CHƯA đủ, 2/3 lần chụp sớm thì
    trang chưa kịp bày `@handle`.
    """
    het = time.time() + timeout
    while time.time() < het:
        try:
            xml = d.dump_hierarchy()
        except Exception:
            time.sleep(0.5)
            continue
        for t in _leaf_texts(xml):
            if _RE_HANDLE.match(t):
                return t
        time.sleep(0.6)
    return ""


def do_visit(d, author, sec_min=5, sec_max=10, log=lambda s: None,
             like_video=False, vid_min=3.0, vid_max=7.0, hoi_o_lai=None):
    """Ghe trang ca nhan -> luot vai giay -> mo MOT video NGAU NHIEN trong luoi -> xem vai giay
    -> (co the) tym -> ve feed.

    Tra ve CAP: ("ok" | "ok_no_grid" | "skip_trung" | "fail",  "ok" | "fail" | "not_needed").

    `hoi_o_lai(handle) -> bool` la NHIP HOI THU HAI, hoi phia Node xem co nen o lai trang nay
    khong (chong ghe trung qua ngay). Truyen None thi khong hoi, o lai nhu cu.

    ⚠ VI SAO HOI O DAY MA KHONG HOI TU TREN FEED: so chong ghe trung khoa theo `@handle`, ma
    feed KHONG bay `@handle` (do: 0/3 mau). Khoa theo ten hien thi la coi hai kenh trung ten
    thanh mot roi bo qua oan — va bo qua oan thi khong de lai dau vet gi de ai do nhin ra.

    ⚠ ĐỔI CÁCH MỞ TRANG HAI LẦN TRONG NGÀY 2026-09-16, cả hai đều do đo trên máy thật:
      1. Bản gốc bấm `d(text=@handle)` trên feed. Feed TikTok v46.1.1 **không bày @handle ở đâu
         cả**, nên phép tìm luôn trượt và ghé thăm luôn trả 'fail'.
      2. Bản kế bấm AVATAR. Nhưng tác giả đang có story thì avatar mở STORY, không mở trang cá
         nhân — và ghé xong không đọc được @handle.
    Giờ dùng `_mo_trang_ca_nhan` (vuốt sang trái, kéo từng điểm), chung một đường với
    `open_profile_read_handle` nên hai nơi không thể lệch nhau.

    ⚠ BẢN CŨ CHỈ CUỘN SUÔNG (2026-09-17): nó vuốt dọc lưới 4-8 giây rồi `back`, không mở video
    nào, không xác nhận vào đúng trang ai, và KHÔNG đi qua `close_profile` nên không có lớp thoát
    phòng LIVE — đúng lỗ hổng đã làm máy đứng trong một buổi phát trực tiếp.
    """
    if not author:
        return ("fail", "not_needed")

    # ⚠ DEM GIO THAT, KHONG DEM GIO DA HEN (2026-09-17).
    # Chu du an nhin man hinh va bao "ghe tham chua duoc 2 giay da quay lai feed", trong khi log
    # van bao `ghe=ok` va tran `visitMaxUsers` van tru mot suat. Mot luot ghe hong som ma tinh
    # nhu mot luot ghe du la lam ca hai thu do noi doi cung mot luc. Ban PC hoc dung bai nay:
    # `visitSpentMs += Date.now() - batDau` (crawler.cjs:2800-2802).
    t_ghe = time.time()

    def _xong(kq, kq_tym, vi_sao=""):
        giay = time.time() - t_ghe
        log("%s (%.1fs)%s" % (_KQ_GHE.get(kq, "Ghé thăm xong"), giay, (" — " + vi_sao) if vi_sao else ""))
        return (kq, kq_tym)

    if not _mo_trang_ca_nhan(d):
        return _xong("fail", "not_needed", "vuốt sang trái không mở được trang cá nhân")

    try:
        # Luoi thu hai cho phong LIVE, ngay khi vua vao.
        if dang_trong_phong_live(d):
            log("⚠ Ghé thăm rơi vào phòng LIVE — thoát ra ngay.")
            _ve_feed(d, log)
            return _xong("fail", "not_needed", "rơi vào phòng LIVE")

        # ── HOI PHIA NODE: CO NEN O LAI KHONG ──
        # Doc `@handle` truoc, vi so chong ghe trung khoa theo no. Tien mot viec: doc duoc
        # `@handle` cung la bang chung trang da dung hinh, nen vong cho luoi ben duoi it phai cho.
        if hoi_o_lai is not None:
            # Tran 6 giay, ngan hon 12 giay cua duong follow. O do doc trat la HONG CA VIEC (khong
            # co @handle thi khong follow duoc, va sai handle la ghi so sai vinh vien). O day doc
            # trat chi mat mot lan chong trung — con luot ghe van chay binh thuong. Cho 12 giay
            # cho mot thu "co thi tot" la lam moi luot ghe dai them gap ruoi khi mang cham.
            h = doc_handle_tren_trang(d, 6.0)
            if not h:
                log("⚠ Vào được trang nhưng không đọc được @handle — vẫn ở lại lướt, nhưng không ghi sổ ghé thăm.")
            elif not hoi_o_lai(h):
                _ve_feed(d, log)
                return _xong("skip_trung", "not_needed", "đã ghé %s gần đây" % h)

        # Cho luoi hien ra theo DIEU KIEN, khong ngu mu. Khong thay thi back MOT nhip roi do lai:
        # 1/3 lan ghe roi vao tam thong bao che trang, va no nuot dung mot `back`.
        # ⚠ BAN TRUOC BAM `back` NGAY O LAN DO HUT DAU TIEN (sua 2026-09-17). Lan do do roi
        # dung vao luc trang ca nhan con dang mo, nen cu `back` khong bo tam che nao ca — no dua
        # may VE FEED. Tu do tro di moi thao tac deu sai cho: vong "luot xem trang" cuon chinh
        # cai feed, roi ket qua luon la `ok_no_grid`.
        # Gio: chi `back` khi da cho GAN HET gio ma van khong thay luoi, va phai chac la KHONG o
        # feed. O feed ma back nua la di xa hon nua khoi cho can den.
        o = []
        het = time.time() + 8.0
        da_back = False
        while time.time() < het:
            o = _o_luoi_video(d)
            if o:
                break
            if _o_tren_feed(d):
                return _xong("fail", "not_needed", "rơi về feed trước khi lưới kịp hiện")
            # Ba giay cuoi moi thu MOT nhip `back` de bo tam che (vd "Viewer history turned on").
            # ⚠ Chi `back`. TUYET DOI khong bam nut la tren tam do (vd "Save") — bam mu mot nut
            # khong biet la gi tren tai khoan that la dung QD-31.
            if not da_back and time.time() > het - 3.0:
                da_back = True
                d.press("back")
                time.sleep(1.0)
            else:
                time.sleep(0.5)

        # Luot xem trang. Keo tung diem chu khong `d.swipe` — xem `_keo_doc`.
        han = time.time() + random.uniform(sec_min, sec_max)
        while time.time() < han:
            _keo_doc(d)
            time.sleep(random.uniform(0.8, 1.4))

        # Doc lai luoi SAU khi cuon: o hien tren man da khac, nen lua chon cung ngau nhien theo
        # vi tri trong luoi chu khong chi trong hang dau.
        o = _o_luoi_video(d)
        if not o:
            _ve_feed(d, log)
            return _xong("ok_no_grid", "not_needed",
                         "không thấy ô lưới nào (trang trống, bị chặn, hoặc bố cục khác)")

        cx, cy = random.choice(o)
        d.click(cx, cy)

        # ── XAC NHAN VIDEO DA MO TRUOC KHI BAM BAT KY THU GI ──
        # Double-tap khi van dang o luoi la mo nham mot video khac, hoac chi cuon. Dung bai hoc
        # QD-31 (ban PC tung 5/10 cu bam trung cho khong lien quan).
        het = time.time() + 4.0
        mo_duoc = False
        while time.time() < het:
            attrs = _attrs_by_id(d.dump_hierarchy())
            if "video_visible_area_container" in attrs and "cover" not in attrs:
                mo_duoc = True
                break
            time.sleep(0.5)
        if not mo_duoc:
            _ve_feed(d, log)
            return _xong("ok_no_grid", "not_needed",
                         "bấm ô lưới nhưng video không mở, nên KHÔNG tym (tránh bấm mù)")

        time.sleep(random.uniform(vid_min, vid_max))
        # Dung lai `do_like`, khong viet cu double-tap thu hai — dung loi cua `linkkey.cjs`.
        kq_tym = do_like(d, log) if like_video else "not_needed"

        _ve_feed(d, log)
        return _xong("ok", kq_tym)
    except Exception as e:
        _ve_feed(d, log)
        return _xong("fail", "not_needed", "lỗi: %s" % str(e)[:80])


def same_video(d, author):
    """Video đang hiển thị có còn là video đã hỏi không.

    Trả về CẶP `(con_dung, ly_do)`:
      - `(True,  "")`                  vẫn đúng video đó
      - `(False, "khac_nguoi")`        đọc được tên, và là người KHÁC — video đã trôi thật
      - `(False, "chua_doc_duoc")`     lúc hỏi đã không đọc được tên, nên không có gì để so
      - `(False, "khong_doc_duoc")`    giờ không đọc được tên trên màn hình
      - `(False, "khong_o_feed")`      không còn ở feed (lạc vào story / trang nhạc / app khác)

    ⚠ VÌ SAO PHẢI TÁCH BỐN LÝ DO (2026-09-18): bản cũ gộp cả bốn thành một `False` và một dòng log
    duy nhất "video đã đổi sau khi quay lại feed". Câu đó SAI ở ba trong bốn trường hợp — video
    không đổi gì cả, chỉ là không đọc được tên. Chủ dự án nhìn log thấy `skip_changed_video` ở mọi
    video và không có cách nào biết vì sao. Đây đúng là hai bài học bản PC đã ghi: chẩn đoán sai
    tệ hơn không chẩn đoán (QĐ-31), và không gộp hai trạng thái khác nhau vào một câu (QĐ-47).

    ⚠ VÌ SAO CẦN PHÉP KIỂM NÀY: sau khi vào trang nhạc rồi `back`, feed CÓ THỂ đã nhảy sang video
    khác. Bấm lúc đó là bấm nhầm người, mà cú Not interested thì không hoàn tác được.

    ⚠ NEO LÀ TÊN HIỂN THỊ, không phải @handle: feed TikTok không bày @handle ở đâu cả (đo: 0/13
    bản chụp). Đọc từ cùng một nguồn với `read_video_info`.
    """
    if not author:
        return (False, "chua_doc_duoc")
    try:
        xml = d.dump_hierarchy()
    except Exception:
        return (False, "khong_doc_duoc")

    attrs = _attrs_by_id(xml)

    # Lạc khỏi feed thì mọi phép so tên đều vô nghĩa. `_o_tren_feed` là hàm DUY NHẤT phân biệt
    # được feed / trình xem story / trang nhạc — bản cũ không hề gọi nó, nên "lạc màn hình" và
    # "người khác" bị trộn làm một.
    if "user_avatar" not in attrs and not _dang_live(attrs):
        return (False, "khong_o_feed")

    hien_tai = _ten_tac_gia(attrs)
    if not hien_tai:
        return (False, "khong_doc_duoc")

    # `author` có thể là "Tên @handle" do `read_video_info` ghép. So phần TÊN, đã chuẩn hoá.
    a = _chuan_ten(author.split(" @")[0] if " @" in author else author)
    b = _chuan_ten(hien_tai)
    if not a or not b:
        return (False, "khong_doc_duoc")
    # So HAI CHIỀU: bản cũ chỉ chấp nhận chuỗi mới là tiền tố của chuỗi cũ, nên lần đọc sau dài
    # hơn lần đầu (TikTok cắt tên theo bề rộng khác) là trượt oan.
    if a == b or a.startswith(b) or b.startswith(a):
        return (True, "")
    return (False, "khac_nguoi")

def _mo_trang_ca_nhan(d):
    """Mở trang cá nhân của chủ video: VUỐT PHẢI→TRÁI, kéo từng điểm một.

    MỘT nơi định nghĩa, dùng chung cho `open_profile_read_handle` (lấy @handle để follow) và
    `do_visit` (ghé thăm). Hai nơi tự làm lấy là có ngày một nơi sửa còn nơi kia quên — đúng bài
    học của `adbpath.cjs`.

    ⚠ VÌ SAO KHÔNG BẤM AVATAR (2026-09-16):
    Bản trước bấm vào avatar. Nhưng tác giả nào **đang có story** thì avatar mang vòng story, và
    bấm vào đó mở STORY chứ không mở trang cá nhân. Đo được trên máy thật: ghé xong không đọc
    được @handle, vì màn hình lúc đó là trình xem story. Chính bản chụp XML cũng có sẵn dấu vết
    `storyringhas_unconsumed_story_false` — avatar có hai vai trò tuỳ trạng thái.
    Vuốt sang trái thì luôn ra trang cá nhân, không phụ thuộc người đó có story hay không.

    ⚠ PHẢI CHẮC ĐANG Ở FEED TRƯỚC KHI VUỐT (2026-09-16, đo trên máy thật):
    Vuốt lúc màn hình còn tấm "Repost to followers" — thứ hiện ra ngay sau khi từ trang nhạc quay
    về — thì cú vuốt bị nuốt thành thao tác REPOST, và ta đứng trước một bảng Repost chứ không
    phải trang cá nhân. Log lúc đó: "reposted / Introduce this post to other / Repost".
    Dấu hiệu đang ở feed là `long_press_layout` có mặt; rời feed rồi thì nó biến mất.

    ⚠⚠ TUYỆT ĐỐI KHÔNG VUỐT KHI VIDEO ĐANG LÀ LIVESTREAM (2026-09-16):
    Vuốt sang trái trên một video LIVE **không** mở trang cá nhân — nó ĐI THẲNG VÀO PHÒNG LIVE.
    Chủ dự án bắt được tận mắt: máy đang đứng trong một buổi phát trực tiếp, giữa khung chat và
    nút tặng quà. Đó vừa là tương tác thật không ai muốn, vừa trái hẳn yêu cầu đã chốt — gặp
    livestream thì LƯỚT QUA, không đụng vào.
    Vòng quét đã bỏ qua LIVE ở đầu mỗi video rồi, nhưng chừng đó chưa đủ: từ lúc đọc màn hình tới
    lúc vuốt còn cả một vòng đi trang nhạc rồi quay lại, và feed CÓ THỂ đã trôi sang video khác —
    một video LIVE. Nên phải kiểm LẠI ngay trước khi mở, tại đây, nơi thao tác thật sự xảy ra.

    ⚠⚠ PHẢI KÉO TỪNG ĐIỂM, KHÔNG DÙNG `d.swipe()` (2026-09-16, đo trên máy thật):
    `d.swipe()` của uiautomator2 bơm sự kiện quá thưa nên TikTok **không nhận ra đó là cử chỉ** —
    màn hình đứng nguyên ở feed. Đã thử ba biến thể (chậm 0.6s, `swipe_points` 5 điểm, nhanh
    0.22s), cả ba đều không điều hướng. Tôi đã suýt kết luận nhầm rằng "vuốt không chạy" và quay
    về bấm avatar — chủ dự án bác lại, và đo tiếp thì đúng là do cách bơm sự kiện:

        d.swipe(...) 3 biến thể      -> vẫn ở feed
        d.touch.down/move/up 9 điểm  -> TRANG CA NHAN (@tranghieu0211)   ✔
        adb shell input swipe 400ms  -> TRANG CA NHAN (@tranghieu0211)   ✔

    Chọn `touch.down/move/up` vì nó đi qua đúng kênh uiautomator2 đang dùng, không phải gọi thêm
    một tiến trình adb cho mỗi cú vuốt.

    ⚠ VÌ SAO KHÔNG BẤM AVATAR: tác giả nào đang có story thì avatar mang vòng story, bấm vào đó
    mở STORY chứ không mở trang cá nhân. Vuốt thì không phụ thuộc chuyện người đó có story.
    """
    try:
        attrs = _attrs_by_id(d.dump_hierarchy())
        if "long_press_layout" not in attrs:
            return False
        if _dang_live(attrs):
            return False
        w, h = d.window_size()
        x1, x2, y = int(w * 0.8), int(w * 0.12), int(h * 0.55)
        d.touch.down(x1, y)
        time.sleep(0.08)
        for i in range(1, 9):
            d.touch.move(int(x1 + (x2 - x1) * i / 8), y)
            time.sleep(0.045)
        d.touch.up(x2, y)

        # ── XAC NHAN DA RA KHOI FEED, KHONG BAO THANH CONG SUONG ──
        # ⚠ SU CO 2026-09-17: ban truoc tra True ngay khi vua nha tay, khong kiem gi ca. Trang ca
        # nhan mat gan mot giay moi dung hinh, nen `do_visit` do luoi NGAY trong luc trang con
        # dang mo, khong thay o nao, roi bam `back` — va cu `back` do dua may VE FEED. Chu du an
        # nhin thay dung canh do: "vua keo sang mot cai la no da quay lai feed roi", chua duoc 2
        # giay. Phan con lai cua ham van chay, nhung da chay tren sai man hinh.
        #
        # Cho o DAY chu khong o noi goi: ham nay la mot nơi duy nhat dinh nghia cu mo trang, nen
        # hai nguoi goi (`do_visit` va `open_profile_read_handle`) cung duoc bao ve mot the.
        het = time.time() + 4.0
        while time.time() < het:
            if not _o_tren_feed(d):
                return True
            time.sleep(0.3)
        return False
    except Exception:
        return False


def _keo_doc(d, tu=0.75, den=0.35, buoc=8):
    """Cuon doc bang cach keo TUNG DIEM, khong dung `d.swipe`.

    Cung mot ly do da do duoc o `_mo_trang_ca_nhan`: `d.swipe()` bom su kien qua thua nen TikTok
    khong nhan ra do la cu chi. Cu cuon doc trong trang ca nhan di qua cung bo nhan cu chi ay,
    nen dung `d.swipe` o do thi rat co the trang DUNG YEN suot 5-10 giay goi la "luot xem" —
    nhin log thi thay co ghe tham, nhin man hinh thi khong cuon duoc dong nao.
    """
    try:
        w, h = d.window_size()
        x = int(w * 0.5)
        y1, y2 = int(h * tu), int(h * den)
        d.touch.down(x, y1)
        time.sleep(0.05)
        for i in range(1, buoc + 1):
            d.touch.move(x, int(y1 + (y2 - y1) * i / buoc))
            time.sleep(0.03)
        d.touch.up(x, y2)
        return True
    except Exception:
        return False


def open_profile_read_handle(d, log=lambda s: None, timeout=12):
    """Bấm avatar để mở trang cá nhân, đọc @handle THẬT. Trả chuỗi `@abc` hoặc ''.

    ⚠ VÌ SAO PHẢI GHÉ MỚI FOLLOW ĐƯỢC (2026-09-16):
    Feed không bày @handle (đo: 0/3 mẫu, grep XML không có `text="@..."`). Mà sổ chống trùng và
    trần 30 lượt/ngày đều khoá theo @handle — khoá theo tên hiển thị thì hai kênh trùng tên bị
    coi là một, và cú follow thì không hoàn tác được. Nên: ghé, đọc tên thật, rồi mới follow.

    Tiện thêm một việc: nút Follow trên TRANG CÁ NHÂN có `text` đúng bằng "Follow" nên khớp được
    `RE_FOLLOW`. Nút trên FEED thì `text` rỗng, chỉ có `content-desc="Follow <Tên>"` — không khớp
    kiểu trọn chuỗi, nên `do_follow` gọi ở feed luôn báo "khong thay nut Follow".

    ⚠ NƠI GỌI PHẢI `close_profile` DÙ THÀNH CÔNG HAY KHÔNG. Bỏ lại máy ở trang cá nhân thì vòng
    quét kế tiếp vuốt trên trang đó, và mọi phép nhận diện sau đều sai chỗ.
    """
    # ── THỬ HAI LẦN ──
    # Đo được trên máy thật: cú vuốt NGAY SAU khi từ trang nhạc quay về bị nuốt — feed còn đang
    # dựng lại nên thao tác rơi vào khoảng trống. Bằng chứng nằm trong cùng một video:
    # `follow=fail` nhưng `ghé=ok`, mà ghé thăm dùng ĐÚNG cú vuốt đó, chỉ khác là nó đi sau một
    # nhịp `back`. Nên: chờ cho feed đứng yên rồi vuốt, hỏng thì lùi lại và thử thêm một lần.
    for lan in (1, 2):
        if lan == 2:
            close_profile(d)          # về feed cho chắc, kể cả khi đang ở đâu đó lạ
            time.sleep(1.5)
        time.sleep(1.2)               # để feed dựng xong; vuốt vào lúc đang chuyển cảnh là mất
        if not _mo_trang_ca_nhan(d):
            log("⚠ Lần %d: chưa về tới feed, hoặc cú vuốt không ăn." % lan)
            continue

        # Chờ trang tải theo ĐIỀU KIỆN, trong `doc_handle_tren_trang` — dùng chung với `do_visit`.
        h = doc_handle_tren_trang(d, timeout)
        if h:
            return h
        log("⚠ Lần %d: mở được một màn khác nhưng không thấy @handle." % lan)
    # ── HỎNG THÌ PHẢI NÓI ĐANG Ở ĐÂU, ĐỪNG ĐỂ ĐOÁN ──
    # "Không đọc được @handle" có ít nhất ba nguyên nhân khác hẳn nhau: (a) bấm avatar không mở
    # được gì, (b) mở đúng trang nhưng tải chậm, (c) mở nhầm một màn khác (TikTok hay chèn thông
    # báo "Others will see you viewed"). Ba cách sửa khác hẳn nhau, nên chẩn đoán rỗng là vô
    # dụng — đúng điều `preflight.cjs` mở đầu đã kể.
    try:
        chu = [t for t in _leaf_texts(d.dump_hierarchy()) if t][:6]
        co_nut = "co" if tim_nut_follow(d, RE_FOLLOW) else "khong"
        # App nào đang ở tiền cảnh là câu hỏi tách bạch hẳn: TikTok vẫn mở mà không đọc được
        # (tải chậm / sai màn) là một chuyện; TikTok bị đẩy ra nền là chuyện hoàn toàn khác —
        # nghĩa là cú vuốt bị hiểu thành thao tác thoát app.
        act = d.app_current()
        log("⛔ Vào trang nhưng không đọc được @handle sau %ds. Đang ở %s/%s · trên màn hình: %s · "
            "nút Follow: %s"
            % (timeout, act.get("package", "?"), str(act.get("activity", "?")).split(".")[-1],
               " / ".join(c[:24] for c in chu), "có" if co_nut else "không"))
    except Exception:
        log("⛔ Vào trang nhưng không đọc được @handle sau %ds, và cũng không đọc được màn hình." % timeout)
    return ""


def verify_follow_after_reload(d, log=lambda s: None, cho=3.0):
    """ĐANG Ở TRANG CÁ NHÂN sau khi bấm Follow: nạp lại trang rồi đọc lại nút.

    Trả 'ok' | 'reverted' | 'unknown'.

    ⚠ VÌ SAO KIỂM TẠI CHỖ THÔI LÀ CHƯA ĐỦ (yêu cầu chủ dự án 2026-09-16):
    `do_follow` bấm xong thấy nút đổi thành "Following" là báo thành công. Nhưng TikTok có thể
    **bật lại** cú follow vài giây sau (chặn hành vi tự động) — nút đã đổi rồi vẫn quay về
    "Follow". Lúc đó phía Node đã ghi sổ mất rồi, mà sổ đánh dấu kênh này **đã follow VĨNH VIỄN**
    (`channelstore.cjs:182-184`) nên nó không bao giờ được thử lại: một cú follow không hề tồn
    tại chiếm một suất trong trần 30/ngày, mãi mãi.

    Nạp lại NGAY TẠI TRANG ĐANG MỞ, không quay về feed rồi vào lại. Hai lý do:
      1. Ngắn hơn: một cú vuốt, không phải một vòng back–tìm avatar–bấm–chờ tải.
      2. KHÔNG CÓ chuyện nhầm người. Quay về feed thì feed có thể đã trôi sang video khác, bấm
         avatar lúc đó là mở trang người lạ, và chữ "Follow" trên trang đó sẽ bị đọc nhầm thành
         "đã bị bật lại". Ở nguyên một trang thì không cần đối chiếu @handle nữa.
    """
    try:
        # Vuốt xuống ở vùng trên = nạp lại trang cá nhân (pull-to-refresh).
        d.swipe(0.5, 0.35, 0.5, 0.85, 0.35)
    except Exception as e:
        log("⚠ Nạp lại trang lỗi (%s)." % str(e)[:80])
        return "unknown"

    time.sleep(cho)
    # Đọc lại nút. `clickable=True` bắt buộc: nhãn thống kê "Following / Followers" lúc nào cũng
    # có mặt trên trang này, nên thiếu nó là phép kiểm LUÔN nói "ok" — kể cả khi đã bị bật lại.
    if tim_nut_follow(d, RE_FOLLOWING):
        return "ok"
    if tim_nut_follow(d, RE_FOLLOW):
        return "reverted"
    log("⚠ Nạp lại xong mà không thấy nút Follow lẫn Following — không kết luận được, coi như chưa rõ.")
    return "unknown"


def dang_trong_phong_live(d):
    """Màn hình hiện tại có phải PHÒNG LIVE không (đã vào trong, không phải ô LIVE trên feed).

    Nhận bằng các thứ chỉ phòng live mới có: ô nhập chat, nút tặng quà, chữ "LIVE" kèm số người
    xem. Không dùng `long_press_layout` vì trong phòng live không có nó.
    """
    try:
        texts = _leaf_texts(d.dump_hierarchy())
    except Exception:
        return False
    dau_hieu = 0
    for t in texts:
        tl = t.lower()
        if tl in ("type...", "say something...", "send a message"):
            dau_hieu += 1
        elif "gift" in tl or "qua tang" in tl:
            dau_hieu += 1
        elif tl in ("live", "live now") or "watch live" in tl:
            dau_hieu += 1
    return dau_hieu >= 2


def close_profile(d, log=lambda s: None):
    """Quay về feed sau khi ghé trang cá nhân. Luôn gọi, kể cả khi ghé hỏng.

    ⚠ CÓ LỚP THOÁT PHÒNG LIVE (2026-09-16):
    Chủ dự án bắt được máy đang đứng TRONG một buổi phát trực tiếp — vuốt sang trái trên video
    LIVE đi thẳng vào phòng live chứ không mở trang cá nhân. `_mo_trang_ca_nhan` giờ đã chặn từ
    đầu, nhưng ở đây vẫn phải có lưới thứ hai: lọt vào rồi mà không thoát thì máy ngồi xem
    livestream của người ta, và mọi vòng quét sau đều sai chỗ.
    """
    try:
        d.press("back")
        time.sleep(1.2)
        for _ in range(2):
            if not dang_trong_phong_live(d):
                return True
            log("⚠ Đang ở TRONG phòng LIVE — thoát ra.")
            d.press("back")
            time.sleep(1.5)
        return True
    except Exception as e:
        log("⚠ Quay lại feed lỗi (%s)." % str(e)[:80])
        return False


# ════════════════════ VUỐT SANG VIDEO KẾ + MÀN HÌNH TAKO (2026-09-18) ════════════════════
#
# ⚠ SỰ CỐ THẬT: chủ dự án bắt được máy đứng trong "TikTok Tako", màn hình chat với trợ lý AI của
# TikTok. Máy .140 kẹt ở đó ít nhất 20 phút: ảnh chụp lúc 18:02, chụp lại lúc 18:22 vẫn y nguyên.
# Vòng quét không có bước nào hỏi "còn ở feed không", nên cứ vuốt và cứ đếm "video không có sound"
# trên một màn hình chat.
#
# LỐI VÀO Tako (đo trên máy thật, TikTok 46.9.3): nút tròn hình mặt ma ở ĐẦU cột nút bên phải,
# chỉ có trên một số video. Id bị làm rối (`z2u`), không có chữ, không có mô tả, nên không nhận
# diện lối vào được. Vì vậy chặn bằng hai lớp:
#   1. Không đặt ngón tay lên vùng có nút: cú vuốt bắt đầu ở GIỮA video (xem `VUOT_TU`).
#   2. Nhận ra màn hình Tako thì lùi ra ngay, và log ghi bước vừa làm ngay trước đó. Dòng log đó
#      là cách đo lối vào thật trên farm.

# Cú vuốt sang video kế: từ 60% xuống 15% chiều cao, ở giữa bề ngang.
#
# ⚠ Bản cũ đặt ngón ở 85% chiều cao. Đo trên bản chụp feed thật (1080×1920), điểm đó nằm ĐÚNG
# hàng nút dưới đáy video: dòng tên nhạc (bấm vào là mở trang nhạc), chữ "…more" của caption, và
# thanh tua video. `d.swipe` bơm sự kiện thưa (xem `_mo_trang_ca_nhan`), nên đặt ngón lên chỗ bấm
# được là tự nhận rủi ro bị hiểu thành một cú CHẠM. Ở giữa video, chạm nhầm cùng lắm là tạm dừng
# video. Đo trên máy thật (Pixel 4 XL, 46.9.3): 20/20 lần sang đúng video mới, không lần nào rời
# feed.
VUOT_TU, VUOT_DEN = 0.60, 0.15


def vuot_video_ke(d, manh=False):
    """Vuốt lên sang video kế — dùng chung cho feed, pha Xem, và lúc bỏ qua livestream.

    `manh=True`: cách DỰ PHÒNG khi feed không chịu sang video mới — kéo TỪNG ĐIỂM (như
    `_keo_doc`) từ 45% lên 10%. Đo thật trên máy .117 (2026-09-19): một bài ảnh "Trend Master"
    giữ feed đứng yên qua 8 vòng quét liền, 10 cú `d.swipe` từ 60% và 45% đều không nhúc nhích.
    """
    if manh:
        _keo_doc(d, tu=0.45, den=0.10, buoc=10)
        return
    d.swipe(0.5, VUOT_TU, 0.5, VUOT_DEN, random.uniform(0.15, 0.3))


# Dấu hiệu màn hình Tako — đo trên bản chụp của chính máy .140 lúc đang kẹt (TikTok 46.9.3), lưu
# ở `tests/fixtures/tako_man_hinh_46.9.3.xml`:
#   • chữ "TikTok Tako" trên thanh tiêu đề: tên thương hiệu, không dịch theo ngôn ngữ máy
#   • dòng "Your use of TikTok Tako is subject to …" ở đầu trang
#   • nút micro cạnh ô "Ask anything", id KHÔNG bị làm rối: `voice_send_container`, `voice_btn`
# Phải có ≥ 2 dấu hiệu, để một caption tình cờ ghi "TikTok Tako" không bị nhận nhầm.
_TAKO_ID_MICRO = ("voice_send_container", "voice_btn")


def la_man_tako(xml_str):
    """Bản chụp màn hình này có phải màn hình TikTok Tako không."""
    if not xml_str or "Tako" not in xml_str:     # đường nhanh: gần như mọi màn hình dừng ở đây
        return False
    try:
        root = ET.fromstring(xml_str)
    except Exception:
        return False
    dau = set()
    for n in root.iter():
        t = (n.get("text") or "").strip()
        if t == "TikTok Tako":
            dau.add("ten")
        elif t.startswith("Your use of TikTok Tako"):
            dau.add("dieu_khoan")
        if (n.get("resource-id") or "").split(":id/")[-1] in _TAKO_ID_MICRO:
            dau.add("micro")
    return len(dau) >= 2


# Câu kể kết quả `thoat_tako`, dùng chung cho mọi nơi in log.
_TAKO_CACH = {
    "back": "đã bấm Back thoát ra",
    "mo_lai": "Back 3 lần không ra, đã mở lại TikTok",
    "ket": "Back 3 lần rồi mở lại TikTok vẫn không ra — máy này cần xem tay",
}


def thoat_tako(d, goi=None, xml_str=None):
    """Đang ở Tako thì lùi ra. Trả:
        ''        — không phải màn hình Tako, không làm gì
        'back'    — đã lùi ra bằng nút Back
        'mo_lai'  — Back 3 lần vẫn còn, đã mở lại TikTok
        'ket'     — mở lại TikTok rồi vẫn còn ở Tako

    ⚠ CHỈ BẤM BACK, tuyệt đối không bấm gì bên trong Tako. Mỗi thẻ trên màn hình đó ("Ask about
    the video you watched", "Plan your next vacation"…) là một câu hỏi gửi đi cho AI.
    """
    try:
        xml = d.dump_hierarchy() if xml_str is None else xml_str
    except Exception:
        return ""
    if not la_man_tako(xml):
        return ""
    # Ba nhịp: nhịp đầu có thể chỉ đóng bàn phím (ô "Ask anything" đang được chọn).
    for _ in range(3):
        try:
            d.press("back")
            time.sleep(1.2)
            if not la_man_tako(d.dump_hierarchy()):
                return "back"
        except Exception:
            pass
    if goi:
        try:
            d.app_start(goi, stop=True)
            time.sleep(6)
            if not la_man_tako(d.dump_hierarchy()):
                return "mo_lai"
        except Exception:
            pass
    return "ket"


# ════════════════════ PHA XEM (chế độ Quét ⇄ Xem, 2026-09-18) ════════════════════
#
# Clone pha `view` của bản PC: mở trang sound → bấm MỘT video ngẫu nhiên trong lưới → xem →
# vuốt thêm vài chục video của cùng sound → sang link kế. Pha này KHÔNG thu sound, KHÔNG bấm gì
# — việc của nó là nuôi tài khoản.
#
# Mọi thứ dưới đây ĐO TRÊN MÁY THẬT (TikTok 46.9.3, `probe_screen.py --view`, 2026-09-18):
#   • `am start -a VIEW -d <link> <gói>` vào THẲNG `MusicDetailActivity`. Chỉ định gói ở cuối
#     để Android không hiện bảng chọn trình duyệt.
#   • Lưới video = các ô `cover`, y hệt lưới trang cá nhân → dùng lại `_o_luoi_video`.
#   • Bấm ô → `DetailActivity`; vuốt lên → sang video kế.
#   • KHÔNG đọc được độ dài video: không SeekBar, không chữ mm:ss, `dumpsys media_session`
#     rỗng. Nên xem theo SỐ GIÂY, không theo % như bản PC.
#   • TikTok đôi khi trả "Something went wrong" rồi lần sau mở được bình thường — phải thử lại.
#
# Tên activity là tên lớp Java của TikTok, KHÔNG bị làm rối như resource-id (`mj8`, `f16`…),
# nên dùng làm mốc ổn định hơn.
ACT_TRANG_NHAC = "MusicDetailActivity"
ACT_TRINH_PHAT = "DetailActivity"


def _activity(d):
    try:
        return (d.app_current() or {}).get("activity") or ""
    except Exception:
        return ""


def _cho(con_han, giay, dung):
    """Ngủ `giay` giây, chia nhỏ để dừng kịp. Trả False nếu bị ngắt (hết hạn pha / app đóng)."""
    het = time.time() + max(0.0, giay)
    while time.time() < het:
        if dung():
            return False
        if con_han is not None and time.time() >= con_han:
            return False
        time.sleep(min(0.5, max(0.0, het - time.time())))
    return True


def _mo_link(d, goi, link, cho=12.0):
    """Mở link bằng deep link. Trả 'nhac' | 'video' | '' (không mở được)."""
    try:
        # Truyền dạng DANH SÁCH: link có `&`, `?` — ghép chuỗi là bị shell cắt ngang.
        d.shell(["am", "start", "-a", "android.intent.action.VIEW", "-d", link, goi])
    except Exception:
        return ""
    han = time.time() + cho
    while time.time() < han:
        a = _activity(d)
        if a.endswith(ACT_TRANG_NHAC):
            return "nhac"
        if a.endswith(ACT_TRINH_PHAT):
            return "video"
        time.sleep(0.8)
    return ""


def _cho_luoi(d, cho=8.0):
    han = time.time() + cho
    while time.time() < han:
        o = _o_luoi_video(d)
        if o:
            return o
        time.sleep(1.0)
    return []


def _ve_ngoai(d):
    """Lùi ra khỏi trình phát và trang nhạc (tối đa 4 nhịp)."""
    for _ in range(4):
        a = _activity(d)
        if not (a.endswith(ACT_TRINH_PHAT) or a.endswith(ACT_TRANG_NHAC)):
            return
        try:
            d.press("back")
        except Exception:
            return
        time.sleep(1.2)

def xem_mot_link(d, goi, link, con_han, dung, xem_giay, so_vuot, dung_giay, log=lambda s: None):
    """Xem MỘT link của pha Xem. Tương tác xong lập tức chuyển link tiếp theo không nghỉ."""
    loai = _mo_link(d, goi, link)
    if not loai:
        loai = _mo_link(d, goi, link)
    if not loai:
        _ve_ngoai(d)
        return "không mở được link"

    if loai == "nhac":
        o = _cho_luoi(d)
        if not o:
            if _mo_link(d, goi, link) == "nhac":
                o = _cho_luoi(d)
        if not o:
            _ve_ngoai(d)
            return "trang nhạc không có video nào (sound có thể đã bị gỡ)"
        x, y = random.choice(o[:6])
        try:
            d.click(x, y)
        except Exception:
            _ve_ngoai(d)
            return "bấm ô video lỗi"
        han_mo = time.time() + 8
        while time.time() < han_mo and not _activity(d).endswith(ACT_TRINH_PHAT):
            time.sleep(0.8)
        if not _activity(d).endswith(ACT_TRINH_PHAT):
            _ve_ngoai(d)
            return "bấm ô video mà không vào được trình phát"

    # Xem video đạt chuẩn chính chủ của bạn (Thời gian xem tùy cấu hình của bạn)
    if not _cho(con_han, random.uniform(*xem_giay), dung):
        _ve_ngoai(d)
        return "dung" if dung() else "het_gio"

    # ── LUỒNG TƯƠNG TÁC NGẪU NHIÊN 50% - 70% BẰNG TỌA ĐỘ ĐỘNG TRÊN LINK VÀNG ──
    try:
        w, h = d.window_size()
        rate_ti_le = random.uniform(0.50, 0.70)
        xuc_xac = random.random()
        
        if xuc_xac < rate_ti_le:
            print(f"[PROCESS] Link dat dieu kien tuong tac (Ty le phien nay: {rate_ti_le*100:.1f}%)")
            hanh_dong = ["tym", "repost", "luu"]
            boc_trung = random.choice(hanh_dong)
            
            if boc_trung == "tym":
                d.double_click(w * 0.5, h * 0.45)
                print("[LOG] Da tym video dat chuan.")
                
            elif boc_trung == "repost":
                d.double_click(w * 0.5, h * 0.45)
                time.sleep(0.6)
                d.click(int(w * 0.92), int(h * 0.82))
                time.sleep(1.8)
                d.click(int(w * 0.18), int(h * 0.73))
                time.sleep(1.5)
                
                try:
                    input_note = d(textMatches="(?i)^(Say something...|Nói gì đó...|Add note...)$")
                    if input_note.exists(timeout=1.0):
                        input_note.set_text(random.choice(["Wow!", "Love this", "✨", "💯"]))
                        time.sleep(0.8)
                        add_btn = d(textMatches="(?i)^(Add|Thêm)$")
                        if add_btn.exists():
                            add_btn.click()
                except Exception:
                    pass
                print("[LOG] Da tym va dang lai video dat chuan thanh cong.")
                
            elif boc_trung == "luu":
                d.click(int(w * 0.92), int(h * 0.60))
                print("[LOG] Da luu video dat chuan vao muc yeu thich.")
                
            time.sleep(1.0)
        else:
            print("[LOG] Video nam trong khoang gian cach an toan — chuyen link luon.")
            
    except Exception as e:
        print(f"[WARN] Loi phan bo tuong tac: {str(e)[:40]}")

    # ── ĐÃ XÓA TOÀN BỘ VÒNG LẶP VUỐT VIDEO VỆ TINH PHỤ ĐỂ CHUYỂN LINK NGAY LẬP TỨC ──
    _ve_ngoai(d)
    return "ok"
