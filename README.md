# 圖片拆圖層 Image Layers → PPTX

[Claude Code](https://claude.com/claude-code) skill：把一張「黏在一起」的平面圖（AI 生成的海報、廣告圖、社群圖卡）拆成可以分開編輯的圖層，輸出 PowerPoint 檔。

- **文字** → 可以直接改字的文字框（字型、顏色、大小、直書都盡量照原圖）
- **主體物件** → 去背成獨立圖片，可以拖拉、縮放
- **背景** → 擦掉文字、挖掉物件後自動補洞

用 ChatGPT 生圖時，常常整張已經 90 分，只差一個字或一個物件位置不對，叫它重改反而越改越糟。拆成圖層之後，改字、移東西就跟編輯簡報一樣，不必整張重新生成。效果類似 Canva 的 Magic Layers，但全部在自己電腦上跑，不用登入任何網站。

---

## 快速開始

在 Claude Code 輸入：

```
/image-layers-pptx 把 ~/Downloads/海報.png 拆成圖層
```

也可以直接講：

- 「這張 AI 生的圖幫我拆圖層」
- 「圖片轉成可以編輯的 PPT」
- 「把海報拆成文字跟物件，我只想改一個字」

Claude 會在圖片旁邊產生 `海報_layers.pptx`：第 1 頁是拆好的圖層，第 2 頁是原圖對照。

---

## 流程

1. **拆**：OCR 找出文字 → 從原圖擦掉文字 → 去背找出主體 → 補好背景
2. **核對**：Claude 看標了框的預覽圖，修正 OCR 認錯的字（簡體、漏空白、形近字）、合併被拆成兩框的多行字、刪掉誤判的裝飾，挑接近的字型
3. **對照**：疊出重組結果跟原圖並排，位置、大小、顏色跑掉就再調
4. **輸出**：產生 PPTX

書法標題、印章這類特殊字型，用一般字型重打會走樣，可以改成從原圖摳出來的去背圖片，保留原本字形（能移動縮放，不能改字）。

---

## 直接用腳本

skill 背後是 `scripts/img2layers.py`，可以單獨使用。

```bash
# 1. 拆圖層：產生 layers.json、preview.png、layers/*.png
python img2layers.py analyze -i 海報.png -o 海報_layers

# 2.（可選）手動修 海報_layers/layers.json 後，疊出原圖/重組對照圖
python img2layers.py render -d 海報_layers -o 對照.png

# 3. 輸出 PPTX
python img2layers.py build -d 海報_layers -o 海報_layers.pptx
```

`analyze` 常用選項：

| 選項 | 用途 |
|------|------|
| `--grab x,y,w,h` | 自動去背沒抓到的物件，用框選補拆（可重複） |
| `--model` | 去背模型：`isnet-general-use`（預設）、`u2net`、`birefnet-general` |
| `--keep-holes` | 物件本身有鏤空（甜甜圈、杯把）時加，不要把內部填滿 |
| `--keep-bg` | 背景不補洞，物件不打算移動時最乾淨 |
| `--no-text` / `--no-objects` | 只拆物件 / 只拆文字 |

`layers.json` 裡每段文字可以加的欄位：`text`、`font`、`bold`、`underline`、`align`、`as_image`、`merged`、`fit`，說明見 [SKILL.md](SKILL.md)。

---

## 需求

- Windows（字型從 `C:\Windows\Fonts` 讀；PPTX 本身在哪裡開都可以）
- Python 3.10+
- 套件：

```bash
python -m pip install rembg python-pptx rapidocr onnxruntime opencv-python-headless pillow
```

第一次執行會下載去背模型（約 180 MB，存在 `~/.rembg`）和 OCR 模型。

## 限制

- 自動去背只抓得到最明顯的主體，小圖示、按鈕底框通常留在背景，要用 `--grab` 補拆
- 物件的陰影、倒影留在背景上，物件移太遠會看到原本的陰影
- 背景補洞是左右內插，照片類背景補的地方會糊，物件移開才看得到
- 字壓在線條或圖案上時，擦字的地方會留一點殘色
- 字型只是近似，原圖若是特殊設計字，要自己換字型
- 拆得沒有 Canva Magic Layers 那麼細，但免費、離線、不用帳號

---

## 安裝

```bash
git clone https://github.com/lng255639-cpu/image-layers-pptx.git ~/.claude/skills/image-layers-pptx
```

裝好後開新對話。之後要更新：`git -C ~/.claude/skills/image-layers-pptx pull`。

## 移除

```bash
rm -rf ~/.claude/skills/image-layers-pptx
```

## 授權

[MIT](LICENSE)
