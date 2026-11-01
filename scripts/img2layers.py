"""把一張平面圖拆成可編輯圖層，輸出 PPTX。

analyze：OCR 找文字 → 從原圖擦掉文字 → rembg 去背找主體 → 補背景 → 寫 layers.json + 各層 PNG
build：讀 layers.json（可先人工/Claude 修正）→ 產生 PPTX（每層一個物件，文字是可編輯文字框）
"""
import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image


def imread(path):
    # cv2.imread 讀不了中文路徑，改用 imdecode
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    if img is None:
        sys.exit(f"讀不到圖片：{path}")
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    elif img.shape[2] == 4:
        # 透明底先鋪白
        a = img[:, :, 3:4].astype(np.float32) / 255
        img = (img[:, :, :3] * a + 255 * (1 - a)).astype(np.uint8)
    return img


def save_png(path, bgr_or_bgra):
    mode = cv2.COLOR_BGRA2RGBA if bgr_or_bgra.shape[2] == 4 else cv2.COLOR_BGR2RGB
    Image.fromarray(cv2.cvtColor(bgr_or_bgra, mode)).save(path)


# ---------- 文字 ----------

def ocr_text(img, min_score):
    from rapidocr import RapidOCR
    res = RapidOCR()(img)
    if res.boxes is None:
        return []
    out = []
    for box, txt, score in zip(res.boxes, res.txts, res.scores):
        if score < min_score or not txt.strip():
            continue
        pts = np.array(box, dtype=np.float32)
        x, y, w, h = cv2.boundingRect(pts.astype(np.int32))
        out.append({"text": txt, "score": round(float(score), 3), "bbox": [x, y, w, h], "quad": pts.tolist()})
    return out


def text_pixels(img, bbox):
    """回傳 (文字像素 mask, 文字顏色 hex)。背景色取框邊緣中位數，跟背景差很多的就是字。"""
    x, y, w, h = bbox
    crop = img[y:y + h, x:x + w].astype(np.int16)
    ring = np.concatenate([crop[0], crop[-1], crop[:, 0], crop[:, -1]])
    bg = np.median(ring, axis=0)
    dist = np.abs(crop - bg).sum(axis=2)
    core = dist > max(60, 0.45 * dist.max())
    if core.sum() < 4:
        mask = np.ones(dist.shape, np.uint8)
        color = 255 - bg
    else:
        color = np.median(crop[core], axis=0)
        # 文字像素（含反鋸齒邊緣）的顏色落在「背景色→文字色」這條線上；
        # 框裡其他顏色的線條、圖示離這條線很遠，不擦
        v = color - bg
        diff = crop - bg
        t = (diff @ v) / max(1.0, float(v @ v))
        resid = np.linalg.norm(diff - t[..., None] * v, axis=2)
        mask = ((t > 0.12) & (resid < 40)).astype(np.uint8)
    b, g, r = [int(np.clip(c, 0, 255)) for c in color]
    return mask, f"#{r:02X}{g:02X}{b:02X}"


def erase_text(img, texts):
    full = np.zeros(img.shape[:2], np.uint8)
    for t in texts:
        x, y, w, h = t["bbox"]
        mask, color = text_pixels(img, t["bbox"])
        t["color"] = color
        full[y:y + h, x:x + w] |= mask
    if not full.any():
        return img.copy()
    full = cv2.dilate(full * 255, np.ones((3, 3), np.uint8), iterations=3)
    return cv2.inpaint(img, full, 5, cv2.INPAINT_TELEA)


def fill_hole(img, hole):
    """大洞用 TELEA 會拉出放射狀條紋，也會把物件下方的陰影往上拖。
    改成逐列取洞左右兩側顏色做線性內插（海報、桌面這類上下漸層的背景最自然），再柔化接縫。"""
    H, W = hole.shape
    src = img.astype(np.float32)
    out = src.copy()
    m = hole > 0
    for y in np.nonzero(m.any(axis=1))[0]:
        row = m[y]
        # 找出這一列每段連續的洞 [a, b)
        edges = np.diff(np.concatenate([[0], row.astype(np.int8), [0]]))
        for a, b in zip(np.nonzero(edges == 1)[0], np.nonzero(edges == -1)[0]):
            left = np.median(src[y, max(0, a - 6):a], axis=0) if a > 0 else None
            right = np.median(src[y, b:min(W, b + 6)], axis=0) if b < W else None
            if left is None and right is None:
                continue
            left = right if left is None else left
            right = left if right is None else right
            t = np.linspace(0, 1, b - a + 2)[1:-1, None]
            out[y, a:b] = left * (1 - t) + right * t
    # 逐列內插會有橫紋，只在洞裡做垂直方向模糊
    smooth = cv2.GaussianBlur(out, (1, 0), sigmaX=0.1, sigmaY=6)
    a = cv2.GaussianBlur(m.astype(np.float32), (0, 0), 2)[:, :, None]
    return (smooth * a + src * (1 - a)).astype(np.uint8)


def classify_text(t):
    x, y, w, h = t["bbox"]
    vertical = h > 1.5 * w and len(t["text"]) >= 2
    t["vertical"] = vertical
    # 字高 ≈ 框的短邊；PPT 字級要比字形高一點
    t["font_px"] = round((w if vertical else h) * 0.82, 1)


# ---------- 物件 ----------

def fill_interior(alpha):
    """插畫主體顏色跟背景很像時（米白兔子配米色底），rembg 只抓到描邊，身體是空的。
    先閉運算把描邊接起來，再把外輪廓內部全部填滿。"""
    binary = (alpha > 127).astype(np.uint8)
    k = max(5, (max(alpha.shape) // 100) | 1)
    closed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    filled = np.zeros_like(binary)
    cv2.drawContours(filled, contours, -1, 1, thickness=cv2.FILLED)
    # 填滿的內部設不透明，外緣保留 rembg 原本的柔邊
    inner = cv2.erode(filled, np.ones((5, 5), np.uint8))
    return np.where(inner > 0, 255, alpha).astype(np.uint8)


def subject_masks(img, model, min_area_ratio, keep_holes=False):
    from rembg import new_session, remove
    rgb = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    alpha = np.array(remove(rgb, session=new_session(model), only_mask=True))
    if not keep_holes:
        alpha = fill_interior(alpha)
    binary = (alpha > 127).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(binary)
    min_area = img.shape[0] * img.shape[1] * min_area_ratio
    out = []
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] < min_area:
            continue
        comp = (labels == i)
        # 保留 rembg 的柔邊 alpha，只限在這個連通區塊附近
        near = cv2.dilate(comp.astype(np.uint8), np.ones((7, 7), np.uint8)) > 0
        out.append(np.where(near, alpha, 0).astype(np.uint8))
    return out


def grabcut_mask(img, rect):
    x, y, w, h = rect
    mask = np.zeros(img.shape[:2], np.uint8)
    bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    cv2.grabCut(img, mask, (x, y, w, h), bgd, fgd, 5, cv2.GC_INIT_WITH_RECT)
    fg = np.isin(mask, (cv2.GC_FGD, cv2.GC_PR_FGD)).astype(np.uint8) * 255
    return cv2.GaussianBlur(fg, (3, 3), 0)


def cut_layer(img, alpha):
    ys, xs = np.nonzero(alpha > 8)
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    rgba = np.dstack([img[y0:y1, x0:x1], alpha[y0:y1, x0:x1]])
    return rgba, [int(x0), int(y0), int(x1 - x0), int(y1 - y0)]


# ---------- analyze ----------

def analyze(args):
    src = Path(args.input)
    out = Path(args.outdir)
    (out / "layers").mkdir(parents=True, exist_ok=True)
    img = imread(src)
    H, W = img.shape[:2]

    texts = [] if args.no_text else ocr_text(img, args.min_score)
    for t in texts:
        classify_text(t)
    clean = erase_text(img, texts)

    alphas = [] if args.no_objects else subject_masks(clean, args.model, args.min_area, args.keep_holes)
    for g in args.grab or []:
        alphas.append(grabcut_mask(clean, [int(v) for v in g.split(",")]))

    objects, union = [], np.zeros((H, W), np.uint8)
    for i, a in enumerate(alphas, 1):
        if not (a > 8).any():
            continue
        rgba, bbox = cut_layer(clean, a)
        name = f"layers/object_{i:02d}.png"
        save_png(out / name, rgba)
        objects.append({"name": f"物件{i}", "file": name, "bbox": bbox})
        union |= (a > 8).astype(np.uint8)

    bg = clean
    if union.any() and not args.keep_bg:
        hole = cv2.dilate(union * 255, np.ones((9, 9), np.uint8), iterations=2)
        bg = fill_hole(clean, hole)
    save_png(out / "layers/background.png", bg)
    save_png(out / "original.png", img)

    # 預覽：紅框文字、綠框物件，標編號，給人/Claude 核對
    pv = img.copy()
    for i, t in enumerate(texts):
        x, y, w, h = t["bbox"]
        cv2.rectangle(pv, (x, y), (x + w, y + h), (0, 0, 255), 2)
        cv2.putText(pv, f"T{i}", (x, max(12, y - 3)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
    for i, o in enumerate(objects, 1):
        x, y, w, h = o["bbox"]
        cv2.rectangle(pv, (x, y), (x + w, y + h), (0, 200, 0), 2)
        cv2.putText(pv, f"O{i}", (x + 3, y + 16), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 200, 0), 2)
    save_png(out / "preview.png", pv)

    for i, t in enumerate(texts):
        t["id"] = i
    layers = {
        "source": str(src.resolve()),
        "size": [W, H],
        "font": args.font,
        "background": "layers/background.png",
        # 背景上擦過字的位置；texts 裡刪掉的 id，build/render 會把原圖貼回去
        "erased": [{"id": t["id"], "bbox": t["bbox"]} for t in texts],
        "objects": objects,
        "texts": [{k: t[k] for k in ("id", "text", "bbox", "color", "font_px", "vertical", "score")} for t in texts],
    }
    (out / "layers.json").write_text(json.dumps(layers, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"文字 {len(texts)} 段、物件 {len(objects)} 個 → {out / 'layers.json'}")
    print(f"預覽：{out / 'preview.png'}  背景：{out / 'layers/background.png'}")


def final_background(d, L):
    """texts 裡被刪掉的字（誤判、不想拆）要留在背景上：把原圖那一塊貼回去。"""
    kept = {t.get("id") for t in L["texts"]}
    for t in L["texts"]:
        kept.update(t.get("merged", []))  # 併進別段的字，背景照樣保持擦掉
    restore = [e["bbox"] for e in L.get("erased", []) if e["id"] not in kept]
    path = d / L["background"]
    if not restore:
        return path
    bg = imread(path)
    orig = imread(d / "original.png")
    H, W = bg.shape[:2]
    for x, y, w, h in restore:
        x0, y0, x1, y1 = max(0, x - 6), max(0, y - 6), min(W, x + w + 6), min(H, y + h + 6)
        bg[y0:y1, x0:x1] = orig[y0:y1, x0:x1]
    out = d / "layers/background_final.png"
    save_png(out, bg)
    return out


def text_image(d, t, others=()):
    """as_image 的字（書法標題、印章這類特殊字型）：從原圖把字摳成去背 PNG，保留原本字形。
    others：其他文字框，框重疊時別把鄰居的字一起摳進來。"""
    orig = imread(d / "original.png")
    H, W = orig.shape[:2]
    x, y, w, h = t["bbox"]
    x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
    crop = orig[y0:y1, x0:x1].astype(np.float32)
    ring = np.concatenate([crop[0], crop[-1], crop[:, 0], crop[:, -1]])
    bg = np.median(ring, axis=0)
    c = np.array([int(t["color"][i:i + 2], 16) for i in (5, 3, 1)], np.float32)  # #RRGGBB → BGR
    v = c - bg
    diff = crop - bg
    k = (diff @ v) / max(1.0, float(v @ v))
    resid = np.linalg.norm(diff - k[..., None] * v, axis=2)
    alpha = (np.clip(k, 0, 1) * 255 * (resid < 60)).astype(np.uint8)
    for o in others:
        if o is t:
            continue
        ox, oy, ow, oh = o["bbox"]
        alpha[max(0, oy - y0):max(0, oy + oh - y0), max(0, ox - x0):max(0, ox + ow - x0)] = 0
    # 半透明的反鋸齒邊緣，顏色要還原成純文字色，不然疊到新背景上會帶舊底色
    rgba = np.dstack([np.broadcast_to(c, crop.shape).astype(np.uint8), alpha])
    path = d / f"layers/text_{t.get('id', 'x')}.png"
    save_png(path, rgba)
    return path, [x0, y0, x1 - x0, y1 - y0]


FONT_FILES = {
    "Microsoft JhengHei": "msjh.ttc", "微軟正黑體": "msjh.ttc",
    "PMingLiU": "mingliu.ttc", "新細明體": "mingliu.ttc",
    "DFKai-SB": "kaiu.ttf", "標楷體": "kaiu.ttf",
    "Times New Roman": "times.ttf", "Georgia": "georgia.ttf",
}
BOLD_FILES = {"msjh.ttc": "msjhbd.ttc", "times.ttf": "timesbd.ttf", "georgia.ttf": "georgiab.ttf"}


def font_file(t, default):
    name = t.get("font") or default or "Microsoft JhengHei"
    f = FONT_FILES.get(name, "msjh.ttc")
    if t.get("bold"):
        f = BOLD_FILES.get(f, f)
    return f"C:/Windows/Fonts/{f}"


def fit_px(t, default_font):
    """字高估出來的字級對英數大寫會偏小（大寫只佔字身約 7 成）。
    橫書時改用實際字型量字寬，讓文字寬度貼合原框；差太多（OCR 字數不對）就退回字高估計。"""
    from PIL import ImageFont

    px = t["font_px"]
    if t["vertical"] or t.get("fit") is False:
        return px
    lines = t["text"].split("\n")
    longest = max(lines, key=len)
    if not longest.strip():
        return px
    try:
        width100 = ImageFont.truetype(font_file(t, default_font), 100).getlength(longest)
    except OSError:
        return px
    fitted = t["bbox"][2] / width100 * 100
    return fitted if 0.7 * px <= fitted <= 1.5 * px else px


# ---------- build ----------

def build(args):
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE
    from pptx.oxml.ns import qn
    from pptx.util import Emu, Pt

    d = Path(args.dir)
    L = json.loads((d / "layers.json").read_text(encoding="utf-8"))
    W, H = L["size"]
    # 長邊縮到 13.333 吋（16:9 簡報寬），保持原圖比例
    emu_per_px = int(13.333 * 914400 / max(W, H))
    px = lambda v: Emu(int(v * emu_per_px))
    pt_per_px = emu_per_px / 12700

    prs = Presentation()
    prs.slide_width, prs.slide_height = px(W), px(H)
    blank = prs.slide_layouts[6]

    s = prs.slides.add_slide(blank)
    s.shapes.add_picture(str(final_background(d, L)), 0, 0, px(W), px(H)).name = "背景"
    for o in L["objects"]:
        x, y, w, h = o["bbox"]
        s.shapes.add_picture(str(d / o["file"]), px(x), px(y), px(w), px(h)).name = o["name"]

    font = args.font or L.get("font") or "Microsoft JhengHei"
    for t in L["texts"]:
        if t.get("as_image"):
            path, (x, y, w, h) = text_image(d, t, L["texts"])
            s.shapes.add_picture(str(path), px(x), px(y), px(w), px(h)).name = "文字圖：" + t["text"][:12]
            continue
        x, y, w, h = t["bbox"]
        size = fit_px(t, font)
        pad = 0.15 * (w if t["vertical"] else h)
        box = s.shapes.add_textbox(px(x - pad), px(y - pad), px(w + 2 * pad), px(h + 2 * pad))
        box.name = "文字：" + t["text"][:12]
        tf = box.text_frame
        tf.word_wrap = False
        tf.auto_size = MSO_AUTO_SIZE.NONE
        tf.vertical_anchor = MSO_ANCHOR.MIDDLE
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        if t["vertical"]:
            tf._txBody.find(qn("a:bodyPr")).set("vert", "eaVert")
        fname = t.get("font") or font
        # text 裡的 \n 拆成多個段落（多行字合併成一個框）
        for n, line in enumerate(t["text"].split("\n")):
            p = tf.paragraphs[0] if n == 0 else tf.add_paragraph()
            p.alignment = {"left": 1, "center": 2, "right": 3}[t.get("align", "left")]
            run = p.add_run()
            run.text = line
            f = run.font
            f.size = Pt(max(4, size * pt_per_px))
            f.bold = t.get("bold", False)
            f.underline = t.get("underline", False)
            f.color.rgb = RGBColor.from_string(t["color"].lstrip("#"))
            f.name = fname
            # 中文字要設 East Asian 字型才會生效
            rPr = run._r.get_or_add_rPr()
            ea = rPr.find(qn("a:ea"))
            if ea is None:
                ea = rPr.makeelement(qn("a:ea"), {})
                rPr.append(ea)
            ea.set("typeface", fname)

    if not args.no_original:
        s2 = prs.slides.add_slide(blank)
        s2.shapes.add_picture(str(d / "original.png"), 0, 0, px(W), px(H)).name = "原圖（對照用）"

    out = Path(args.output)
    prs.save(out)
    print(f"已輸出 {out}（背景 1 + 物件 {len(L['objects'])} + 文字 {len(L['texts'])}）")


# ---------- render ----------


def render(args):
    """沒有 PowerPoint 時，用 layers.json 疊出近似成品的 PNG，跟原圖並排對照。"""
    from PIL import ImageDraw, ImageFont

    d = Path(args.dir)
    L = json.loads((d / "layers.json").read_text(encoding="utf-8"))
    canvas = Image.open(final_background(d, L)).convert("RGBA")
    for o in L["objects"]:
        canvas.alpha_composite(Image.open(d / o["file"]).convert("RGBA"), tuple(o["bbox"][:2]))
    draw = ImageDraw.Draw(canvas)
    for t in L["texts"]:
        if t.get("as_image"):
            path, (x, y, w, h) = text_image(d, t, L["texts"])
            canvas.alpha_composite(Image.open(path).convert("RGBA"), (x, y))
            continue
        font = ImageFont.truetype(font_file(t, L.get("font")), max(4, int(fit_px(t, L.get("font")))))
        x, y, w, h = t["bbox"]
        lines = t["text"].split("\n")
        align = t.get("align", "left")
        if t["vertical"]:
            # 直書：每行是一欄，由右往左
            col_w = w / len(lines)
            for c, line in enumerate(lines):
                step = h / max(1, len(line))
                cx = x + w - col_w * (c + 0.5)
                for i, ch in enumerate(line):
                    draw.text((cx, y + step * (i + 0.5)), ch, font=font, fill=t["color"], anchor="mm")
        else:
            row_h = h / len(lines)
            ax, anchor = {"left": (x, "lm"), "center": (x + w / 2, "mm"), "right": (x + w, "rm")}[align]
            for r, line in enumerate(lines):
                cy = y + row_h * (r + 0.5)
                draw.text((ax, cy), line, font=font, fill=t["color"], anchor=anchor)
                if t.get("underline"):
                    x0, _, x1, y1 = draw.textbbox((ax, cy), line, font=font, anchor=anchor)
                    draw.line([(x0, y1 + 2), (x1, y1 + 2)], fill=t["color"], width=max(1, font.size // 16))
    orig = Image.open(d / "original.png").convert("RGBA")
    W, H = orig.size
    sheet = Image.new("RGB", (W * 2 + 20, H), "white")
    sheet.paste(orig, (0, 0))
    sheet.paste(canvas, (W + 20, 0))
    sheet.save(args.output)
    print(f"左原圖、右重組：{args.output}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="拆圖層，寫 layers.json")
    a.add_argument("-i", "--input", required=True)
    a.add_argument("-o", "--outdir", required=True)
    a.add_argument("--model", default="isnet-general-use", help="rembg 模型（u2net / isnet-general-use / birefnet-general）")
    a.add_argument("--min-area", type=float, default=0.004, help="物件最小面積（佔全圖比例）")
    a.add_argument("--min-score", type=float, default=0.5, help="OCR 信心下限")
    a.add_argument("--grab", action="append", help="額外框選物件 x,y,w,h（原圖像素），可重複")
    a.add_argument("--font", default="Microsoft JhengHei")
    a.add_argument("--no-text", action="store_true", help="不拆文字")
    a.add_argument("--no-objects", action="store_true", help="不拆物件")
    a.add_argument("--keep-bg", action="store_true", help="背景不補洞（物件下面保留原圖）")
    a.add_argument("--keep-holes", action="store_true", help="物件內部的洞不填滿（甜甜圈、杯把這類真的鏤空的物件）")
    a.set_defaults(func=analyze)

    b = sub.add_parser("build", help="依 layers.json 產生 PPTX")
    b.add_argument("-d", "--dir", required=True)
    b.add_argument("-o", "--output", required=True)
    b.add_argument("--font", help="覆蓋全部文字字型")
    b.add_argument("--no-original", action="store_true", help="不加原圖對照頁")
    b.set_defaults(func=build)

    r = sub.add_parser("render", help="用 layers.json 模擬成品，輸出原圖/重組對照 PNG")
    r.add_argument("-d", "--dir", required=True)
    r.add_argument("-o", "--output", required=True)
    r.set_defaults(func=render)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
